"""
insights_engine.py
===================

Powers the "Insights" tab: a daily-refreshed intelligence brief that reads
back through each monitored municipality's recent meeting history (beyond
the normal 2-month calendar window), pulls out recurring governance topics
(zoning, CRA, DDA/DAC, data centers, etc. - the same subject areas the
project already cares about), checks whether those topics show up in
independent web coverage, ranks the results, and keeps a rolling 7-day
history so past runs stay visible.

STATUS AS OF THIS SESSION - READ BEFORE TRUSTING OUTPUT
---------------------------------------------------------
Following this project's own established discipline (see handoff.md's "Key
Methodological Lessons"): this session's sandbox has no outbound network
access, so NONE of the following have been executed against the real
internet this session:
  - The historical re-scrape loop (reuses scraper.py's own functions, which
    are each at whatever confirmed/unconfirmed status handoff.md says they
    are - this module doesn't change that status, it just calls them again
    with a different date window).
  - The Hugging Face Inference call (huggingface_hub.InferenceClient). The
    model name below is a reasonable, configurable default - NOT a verified
    working model against your HF_TOKEN. The very first real run's log
    output will show plainly whether it worked or fell back to the
    heuristic path (search for "[Insights LLM]" in the log).
  - The Serper.dev web cross-reference call. Same story - configurable,
    untested, logs plainly whether it worked (search for "[Insights
    Search]").
Everything is written to fail SOFT: if the LLM call fails, topics are
extracted via keyword-matching instead (tagged "method": "heuristic" in the
output so you can tell which insights are AI-synthesized vs. rule-based).
If the search call fails or no SERPER_API_KEY is set, insights are still
produced, just without a notability badge. Nothing here should crash the
GitHub Actions job even if both external services are completely
unreachable - ask for the "[Insights ...]"-prefixed log lines on the first
real run the same way you would for a new scraper.

WHAT THIS DOES NOT DO
----------------------
It does not re-run the *forward-looking* 2-month scrape that scraper.py
already does - that's untouched, data.json is unaffected by this module.
This module is purely additive: it writes insights.json and
insights_history.json.
"""

import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

import scraper as sc

try:
    import pdfplumber
    HAVE_PDFPLUMBER = True
except ImportError:
    HAVE_PDFPLUMBER = False

try:
    from PyPDF2 import PdfReader
    HAVE_PYPDF2 = True
except ImportError:
    HAVE_PYPDF2 = False

try:
    from huggingface_hub import InferenceClient
    HAVE_HF = True
except ImportError:
    HAVE_HF = False


# --- CONFIG -----------------------------------------------------------------

HISTORICAL_MONTHS_BACK = 6  # per user decision, 2026-09-05

INSIGHTS_OUTPUT_PATH = "insights.json"
INSIGHTS_HISTORY_PATH = "insights_history.json"
TOPIC_CACHE_PATH = "topic_cache.json"
HISTORY_RETENTION_RUNS = 7  # rolling 7-day window, per user request

MAX_DOC_CHARS = 8000        # cap for heuristic path (cheap, no cost concern)
MAX_LLM_DOC_CHARS = 3000    # smaller cap specifically for what's sent to the
                            # LLM - every character is billed input tokens,
                            # and free-tier HF credit is extremely small
                            # ($0.10/month) - see cost note below.
MAX_LLM_RESPONSE_TOKENS = 400  # trimmed from 800 - 6 short topics don't need more
MAX_TOPICS_PER_MEETING = 6

# --- COST NOTE (added after the first real run exhausted free HF credits) ---
# Hugging Face's free-tier accounts get a flat $0.10/month in Inference
# Provider credits, hard stop, no rollover. A 70B-class model burns through
# that in well under 100 calls, which is exactly what happened on the first
# real run (243 calls, all failed with 402 Payment Required once the $0.10
# was gone). Two things below exist specifically to make LLM-based analysis
# actually sustainable on that tiny budget:
#   1. A persistent topic cache (see TOPIC_CACHE_PATH below) so a given
#      historical meeting is only ever sent to the LLM ONCE, successfully -
#      not re-analyzed from scratch every single day.
#   2. A much smaller default model than the original 70B choice. Smaller
#      instruct models are typically several times cheaper per call. This
#      default is a reasonable guess, NOT a confirmed-cheapest option for
#      your account's current provider routing - check the next real run's
#      "[Insights LLM]" log lines (and your HF billing dashboard) to see how
#      many calls it got through before (if ever) hitting 402 again, and swap
#      via the INSIGHTS_LLM_MODEL env var if a different model fits better.

# Configurable so a model swap never requires touching code - see the
# "STATUS" note above. The first real run exhausted the free $0.10/month HF
# credit using a 70B model after only ~100 calls. This default switches to a
# much smaller, typically far cheaper instruct model to stretch that budget
# further - UNCONFIRMED against real current provider pricing (couldn't be
# tested live this session). Override via repo/workflow env var if this
# model isn't routable, or if you find a cheaper one on your account.
HF_MODEL = os.environ.get("INSIGHTS_LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
HF_TOKEN = os.environ.get("HF_TOKEN")

# Topic taxonomy pulled directly from README.md's notes on what this project
# cares about (zoning/plans/plats, CRA, DDA/DAC, data centers, etc.). Used
# both to steer the LLM prompt and as the fallback heuristic keyword set
# when the LLM is unavailable.
TOPIC_TAXONOMY = {
    "Zoning & Land Use": [
        r"\bzoning\b", r"\bvariance\b", r"\brezon\w*\b", r"\bland use\b",
        r"\bplat\b", r"\bplans? and plats\b", r"\bPPRC\b", r"\bsite plan\b",
        r"\bspecial exception\b", r"\bcomprehensive plan\b",
    ],
    "Community Redevelopment (CRA)": [
        r"\bcommunity redevelopment\b", r"\bCRA\b", r"\bredevelopment agency\b",
        r"\bredevelopment group\b",
    ],
    "Downtown Development / DDA / DAC": [
        r"\bdowntown development\b", r"\bDDA\b", r"\bdowntown action committee\b",
        r"\bDAC\b",
    ],
    "Data Centers": [
        r"\bdata center\b", r"\bdata centre\b", r"\bhyperscale\b", r"\bserver farm\b",
    ],
    "Budget & Finance": [
        r"\bbudget\b", r"\bmillage\b", r"\bfiscal year\b", r"\btax rate\b",
    ],
    "Housing & Development": [
        r"\baffordable housing\b", r"\bworkforce housing\b", r"\bmixed.use\b",
        r"\bapartment\b", r"\bresidential development\b",
    ],
}
FALLBACK_CATEGORY = "Other Governance Matters"


# --- STEP 1: HISTORICAL EVENT GATHERING -------------------------------------
# Reuses every existing scrape_*() function unchanged, just with a widened
# date window (see set_date_window_override in scraper.py). No per-site
# parsing logic is duplicated here.

# Municipalities whose scraper fetches a NEW url per calendar month (so a
# single override call only ever nets 2 consecutive months) - these need the
# override looped across each historical month individually to get full
# 6-month coverage.
MULTI_MONTH_LOOP_SCRAPERS = [
    sc.scrape_boca_raton,
    sc.scrape_boynton_beach,
    sc.scrape_palm_beach_gardens,
    sc.scrape_wellington,
    sc.scrape_jupiter,
]

# Municipalities whose scraper fetches one page/API call and then filters
# by date in memory - a single widened override call captures everything
# the source already returns for the whole window in one shot.
SINGLE_WINDOW_SCRAPERS = [
    sc.scrape_west_palm_beach,
    sc.scrape_palm_beach_county,
    sc.scrape_delray_beach,
    sc.scrape_westlake,
    sc.scrape_downtown_wpb_dda,
    sc.scrape_palm_beach,
    sc.scrape_riviera_beach,
    sc.scrape_juno_beach,
    sc.scrape_jupiter_inlet_colony,
    sc.scrape_manalapan,
    sc.scrape_gulf_stream,
]


def _month_start(dt):
    return datetime(dt.year, dt.month, 1)


def _add_months(dt, n):
    month = dt.month - 1 + n
    year = dt.year + month // 12
    month = month % 12 + 1
    return datetime(year, month, 1)


# --- NOISE FILTERS (added per user request: cancelled meetings and mere
# schedule-day deviations are not insights, and are excluded before any
# analysis happens - not just hidden at display time, so they never cost an
# LLM call either) ---

CANCELLATION_PATTERN = re.compile(r'\bCANCEL(?:L)?ED\b', re.I)

# Matches parenthetical asides that only exist to note a scheduling quirk,
# e.g. "(March meeting is on a Monday, not the usual Tuesday)" or
# "(Will Follow the Joint Meeting)" - real examples seen in the first real
# run's log. These describe WHEN a meeting happens, not WHAT it's about, so
# they're stripped before topic extraction rather than mined as if they were
# substantive content.
SCHEDULE_NOTE_PATTERN = re.compile(
    r'\(\s*[^()]*\b(?:usual|instead of|rather than|moved from|different day|'
    r'not the usual|will follow|is on a)\b[^()]*\)',
    re.I,
)


def is_cancelled_event(title):
    return bool(CANCELLATION_PATTERN.search(title or ""))


def strip_schedule_notes(title):
    cleaned = SCHEDULE_NOTE_PATTERN.sub("", title or "")
    return re.sub(r'\s+', ' ', cleaned).strip()


def gather_historical_events(months_back=HISTORICAL_MONTHS_BACK):
    """Returns a deduplicated list of qualifying events from the last
    `months_back` months, reusing scraper.py's own scrape functions and
    whitelist filter (is_qualifying_event) - no new filtering logic here."""
    all_events = []
    now = datetime.now()
    this_month_start = _month_start(now)

    # --- multi-month-loop family ---
    for fn in MULTI_MONTH_LOOP_SCRAPERS:
        for i in range(1, months_back + 1):
            target_month = _add_months(this_month_start, -i)
            next_month = _add_months(target_month, 1)
            try:
                sc.set_date_window_override(target_month, next_month)
                events = fn()
                all_events.extend(events)
            except Exception as e:
                print(f"[Insights History] {fn.__name__} failed for "
                      f"{target_month.strftime('%Y-%m')}: {e}")
            finally:
                sc.clear_date_window_override()

    # --- single-window family ---
    window_start = _add_months(this_month_start, -months_back)
    window_end = now + timedelta(days=1)
    for fn in SINGLE_WINDOW_SCRAPERS:
        try:
            sc.set_date_window_override(window_start, window_end)
            events = fn()
            all_events.extend(events)
        except Exception as e:
            print(f"[Insights History] {fn.__name__} failed: {e}")
        finally:
            sc.clear_date_window_override()

    # Dedup using the project's existing stable key (muni + date + normalized
    # title) - the same key already used for the new-records diff feature.
    seen = set()
    deduped = []
    for ev in all_events:
        key = sc.compute_change_key(ev)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    # Keep only events strictly within the historical window (belt-and-
    # suspenders - the per-scraper filters should already guarantee this).
    cutoff = _add_months(this_month_start, -months_back)
    filtered = []
    for ev in deduped:
        try:
            ev_dt = datetime.strptime(ev["date"], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        if cutoff <= ev_dt <= now:
            filtered.append(ev)

    # Drop cancelled meetings entirely - per explicit user decision, a
    # cancellation is not unique or insightful and shouldn't consume an LLM
    # call or appear in any theme's citation list.
    cancelled_count = sum(1 for ev in filtered if is_cancelled_event(ev.get("title")))
    filtered = [ev for ev in filtered if not is_cancelled_event(ev.get("title"))]

    # Strip pure schedule-deviation asides from titles (in place) so neither
    # the heuristic nor the LLM path can mistake "meets on an unusual day
    # this month" for a real topic.
    for ev in filtered:
        ev["title"] = strip_schedule_notes(ev.get("title", ""))

    print(f"[Insights History] Gathered {len(filtered)} unique qualifying "
          f"historical events across {months_back} months "
          f"(from {len(all_events)} raw rows before dedup, "
          f"{cancelled_count} cancelled meeting(s) excluded).")
    return filtered


# --- STEP 2: DOCUMENT TEXT EXTRACTION ("reading the meeting minutes") ------

def fetch_document_text(event, timeout=20):
    """Best-effort extraction of the agenda/minutes document text behind an
    event's link. Returns None (not a crash) on any failure so the pipeline
    degrades gracefully to title/summary-only analysis for that meeting."""
    if event.get("has_agenda") is False:
        return None
    link = event.get("link")
    if not link or not link.startswith("http"):
        return None

    try:
        looks_like_pdf = link.lower().split("?")[0].endswith(".pdf")
        res = sc.fetch_hardened(link, timeout=timeout) or requests.get(
            link, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
        )
        if res is None or res.status_code != 200:
            return None

        if looks_like_pdf or res.headers.get("Content-Type", "").lower().startswith("application/pdf"):
            return _extract_pdf_text(res.content)
        else:
            soup = BeautifulSoup(res.text, "html.parser")
            text = soup.get_text(" ", strip=True)
            return text[:MAX_DOC_CHARS] if text else None
    except Exception as e:
        print(f"[Insights Docs] Failed to read document for "
              f"{event.get('muni_short')} {event.get('date')} "
              f"'{event.get('title')}': {e}")
        return None


def _extract_pdf_text(pdf_bytes):
    import io
    text = ""
    if HAVE_PDFPLUMBER:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages[:15]:  # cap pages read for speed
                    page_text = page.extract_text() or ""
                    text += page_text + "\n"
                    if len(text) >= MAX_DOC_CHARS:
                        break
        except Exception as e:
            print(f"[Insights Docs] pdfplumber failed: {e}")
    if not text and HAVE_PYPDF2:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page in reader.pages[:15]:
                text += (page.extract_text() or "") + "\n"
                if len(text) >= MAX_DOC_CHARS:
                    break
        except Exception as e:
            print(f"[Insights Docs] PyPDF2 fallback failed: {e}")
    text = text.strip()
    return text[:MAX_DOC_CHARS] if text else None


# --- STEP 3: TOPIC EXTRACTION (LLM, with heuristic fallback) ---------------

_hf_client = None
_hf_client_attempted = False
_hf_credits_exhausted = False  # set True the first time HF returns 402 -
# once true, every subsequent meeting in this run skips straight to
# heuristic extraction instead of making (and waiting on) a doomed call.
# Resets naturally on the next process run (i.e. the next day).


def get_hf_client():
    global _hf_client, _hf_client_attempted
    if _hf_client_attempted:
        return _hf_client
    _hf_client_attempted = True
    if not HAVE_HF:
        print("[Insights LLM] huggingface_hub not installed - using heuristic extraction only.")
        return None
    if not HF_TOKEN:
        print("[Insights LLM] No HF_TOKEN set - using heuristic extraction only.")
        return None
    try:
        _hf_client = InferenceClient(model=HF_MODEL, token=HF_TOKEN)
        print(f"[Insights LLM] Initialized Hugging Face client for model '{HF_MODEL}'.")
    except Exception as e:
        print(f"[Insights LLM] Could not initialize HF client: {e} - using heuristic extraction only.")
        _hf_client = None
    return _hf_client


def _heuristic_topics(muni_full, title, text_blob):
    """Rule-based fallback: keyword-match the title + any extracted document
    text against TOPIC_TAXONOMY. Coarser than the LLM path but never fails
    and never needs network access."""
    combined = f"{title} {text_blob or ''}"
    hits = []
    for category, patterns in TOPIC_TAXONOMY.items():
        for pat in patterns:
            m = re.search(pat, combined, re.I)
            if m:
                hits.append({
                    "topic_title": title if title else m.group(0).title(),
                    "category": category,
                    "description": f"{title} touched on {category.lower()}.",
                    "method": "heuristic",
                })
                break  # one hit per category is enough for the fallback path
    if not hits:
        hits.append({
            "topic_title": title,
            "category": FALLBACK_CATEGORY,
            "description": f"{title} (no specific tracked topic keyword matched).",
            "method": "heuristic",
        })
    return hits[:MAX_TOPICS_PER_MEETING]


def _llm_topics(client, muni_full, title, date, text_blob):
    global _hf_credits_exhausted
    if _hf_credits_exhausted:
        return None  # already know this run's credits are gone - don't waste a call finding out again

    categories = ", ".join(TOPIC_TAXONOMY.keys())

    if not text_blob:
        # No real document text to ground a specific finding in - per user
        # decision, we should NOT manufacture a generic "topic" out of just
        # the meeting title (that's exactly the noise this rewrite is meant
        # to eliminate). Returning an empty list is a legitimate, successful
        # LLM outcome (not a failure) - it means "nothing specific to report
        # for this meeting," and the event correctly contributes zero
        # candidate topics rather than a fake one.
        return []

    doc_excerpt = text_blob[:MAX_LLM_DOC_CHARS]
    prompt = f"""You are extracting SPECIFIC, NAMEABLE agenda items from a public government meeting document, for the purpose of detecting when the SAME real-world matter comes up again in a later meeting (same city or a different one).

Municipality: {muni_full}
Meeting title: {title}
Date: {date}
Document excerpt:
\"\"\"{doc_excerpt}\"\"\"

Extract up to {MAX_TOPICS_PER_MEETING} items that meet ALL of these rules:
- Each item must be a SPECIFIC, real thing being decided or discussed: an ordinance or resolution number, a named development/project, a specific applicant or company, a street address, a dollar figure, a case/permit number, or similar concrete detail actually present in the excerpt.
- If the same matter has an identifying number or name (e.g. "Ordinance No. 4316", "Case No. 2026-014", "Riverwalk Plaza project"), use that EXACT identifier verbatim as the topic_title every time it appears, so the same matter can be recognized as recurring later.
- Assign the closest category from this list if it fits: {categories}. If none fit, use "{FALLBACK_CATEGORY}".
- description must state the concrete action or substance (what is being proposed/approved/discussed), grounded only in the excerpt - do not speculate beyond it.

DO NOT include:
- Generic restatements of the meeting type/title (e.g. "City Council Meeting", "Board discussed zoning matters")
- Routine procedural items: call to order, roll call, pledge of allegiance, approval of minutes, adjournment, general public comment period
- Anything about WHEN the meeting happens (rescheduling, unusual day/time, "will follow the joint meeting", etc.) - scheduling logistics are never a topic
- Vague catch-alls with no identifiable specific subject

If nothing in the excerpt meets these rules, return an empty JSON array: []

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"topic_title": "specific identifier or subject", "category": "one of the categories above", "description": "1-2 sentence factual description grounded in the excerpt"}}]"""

    try:
        response = client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=MAX_LLM_RESPONSE_TOKENS,
            temperature=0.2,
        )
        content = response.choices[0].message.content.strip()
        content = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            raise ValueError("LLM did not return a JSON array")
        topics = []
        for item in parsed[:MAX_TOPICS_PER_MEETING]:
            if not isinstance(item, dict) or "topic_title" not in item:
                continue
            topics.append({
                "topic_title": item.get("topic_title", title)[:200],
                "category": item.get("category", FALLBACK_CATEGORY),
                "description": item.get("description", "")[:500],
                "method": "llm",
            })
        return topics  # may legitimately be [] - "successfully analyzed, nothing specific found" is NOT a failure
    except Exception as e:
        err_text = str(e)
        if "402" in err_text or "Payment Required" in err_text or "depleted" in err_text.lower():
            if not _hf_credits_exhausted:
                print(f"[Insights LLM] HF credits appear exhausted ({err_text[:200]}) - "
                      f"skipping all further LLM calls this run and falling back to heuristic "
                      f"extraction for every remaining meeting.")
            _hf_credits_exhausted = True
        else:
            print(f"[Insights LLM] Extraction failed for {muni_full} "
                  f"'{title}' ({date}): {e} - falling back to heuristic extraction.")
        return None


def extract_meeting_topics(event, text_blob):
    client = get_hf_client()
    title = event.get("title", "")
    muni_full = event.get("muni_full", "")
    date = event.get("date", "")

    llm_result = None
    if client is not None:
        llm_result = _llm_topics(client, muni_full, title, date, text_blob)

    if llm_result is not None:
        topics = llm_result  # may be [] - a legitimate "nothing specific found" LLM outcome
        used_llm = True
    else:
        topics = _heuristic_topics(muni_full, title, text_blob)
        used_llm = False

    for t in topics:
        t["muni_short"] = event.get("muni_short")
        t["muni_full"] = muni_full
        t["event_title"] = title
        t["date"] = date
        t["link"] = event.get("link")
    return topics, used_llm


# --- TOPIC CACHE (makes LLM analysis affordable on a $0.10/month budget) --
# Past meeting minutes never change once posted, so there's no reason to
# re-spend credits re-analyzing the same historical meeting every single
# day. Only LLM-derived results are cached as "done" - results that fell
# back to heuristic extraction are deliberately left OUT of the cache, so
# they stay eligible to be retried (and hopefully upgraded to real LLM
# analysis) on a future run once credits refill or a cheaper model is
# configured. This means the LLM-covered share of your 6-month history
# should gradually grow over time rather than being stuck at whatever the
# first run's $0.10 happened to cover.

def load_topic_cache(path=TOPIC_CACHE_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_topic_cache(cache, path=TOPIC_CACHE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _cache_key(event):
    return "|".join(sc.compute_change_key(event))


def prune_topic_cache(cache, valid_keys):
    """Drops cache entries for meetings that have aged out of the 6-month
    historical window, so the file doesn't grow forever."""
    return {k: v for k, v in cache.items() if k in valid_keys}


# --- STEP 4: CLUSTER RECURRING THEMES (within and across municipalities) ---

def _normalize_topic_key(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]", "", text)
    words = [w for w in text.split() if len(w) > 2]
    return set(words)


def _similarity(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def cluster_recurring_themes(topic_entries, similarity_threshold=0.5):
    """Groups topic entries into themes: first by category, then by word-
    overlap similarity of topic_title within that category. Simple and
    dependency-free (no embeddings needed) - good enough to surface real
    recurring subjects without over-engineering the clustering step.

    Threshold raised from 0.35 to 0.5 now that the LLM prompt asks for
    specific, verbatim identifiers (ordinance numbers, project names) rather
    than generic category restatements - tighter matching avoids merging
    genuinely unrelated items that happen to share common words."""
    by_category = defaultdict(list)
    for entry in topic_entries:
        by_category[entry["category"]].append(entry)

    themes = []
    for category, entries in by_category.items():
        clusters = []  # list of {"key_words": set, "entries": [...]}
        for entry in entries:
            words = _normalize_topic_key(entry["topic_title"])
            placed = False
            for cluster in clusters:
                if _similarity(words, cluster["key_words"]) >= similarity_threshold:
                    cluster["entries"].append(entry)
                    cluster["key_words"] |= words
                    placed = True
                    break
            if not placed:
                clusters.append({"key_words": words, "entries": [entry]})

        for cluster in clusters:
            entries_in_cluster = cluster["entries"]
            municipalities = sorted(set(e["muni_short"] for e in entries_in_cluster if e.get("muni_short")))
            # Use the longest topic_title as the representative label - tends
            # to be the most descriptive of the cluster.
            representative = max(entries_in_cluster, key=lambda e: len(e["topic_title"]))
            sorted_entries = sorted(entries_in_cluster, key=lambda e: e.get("date") or "", reverse=True)

            themes.append({
                "theme_title": representative["topic_title"],
                "category": category,
                "municipalities": municipalities,
                "cross_municipality": len(municipalities) > 1,
                "occurrence_count": len(entries_in_cluster),
                "citation_summary": _build_citation_summary(sorted_entries),
                "meetings": [
                    {
                        "muni_short": e.get("muni_short"),
                        "muni_full": e.get("muni_full"),
                        "title": e.get("event_title"),
                        "date": e.get("date"),
                        "link": e.get("link"),
                        "description": e.get("description"),
                        "method": e.get("method"),
                    }
                    for e in sorted_entries
                ],
            })
    return themes


def _build_citation_summary(sorted_entries):
    """Builds a plain-language 'why this is flagged' citation trail, e.g.
    'Previously discussed: City of Boca Raton (2026-07-13), City of Boca
    Raton (2026-06-09)' - used so a recurring theme states its evidence
    rather than just asserting recurrence."""
    if len(sorted_entries) < 2:
        return None
    prior = sorted_entries[1:]  # entries[0] is the most recent occurrence
    parts = [f"{e.get('muni_full', e.get('muni_short', '?'))} ({e.get('date', '?')})" for e in prior]
    return "Previously discussed: " + ", ".join(parts)


# --- STEP 5: WEB CROSS-REFERENCE (notability check) -------------------------

def web_cross_reference(theme, num_results=5):
    if not SERPER_API_KEY:
        return {"checked": False, "corroborated": False, "sources": []}

    muni_hint = theme["municipalities"][0] if theme["municipalities"] else ""
    query = f"{theme['theme_title']} {muni_hint} Florida"
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[Insights Search] Serper returned HTTP {resp.status_code} for query '{query}'.")
            return {"checked": True, "corroborated": False, "sources": [], "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        organic = data.get("organic", [])[:num_results]
        sources = [
            {"title": r.get("title"), "link": r.get("link"), "snippet": r.get("snippet")}
            for r in organic
        ]
        return {"checked": True, "corroborated": len(sources) > 0, "sources": sources}
    except Exception as e:
        print(f"[Insights Search] Serper request failed for query '{query}': {e}")
        return {"checked": True, "corroborated": False, "sources": [], "error": str(e)}


# --- STEP 6: CONFIDENCE SCORING + RANKING + STARRING ------------------------

def compute_confidence(theme, web_result):
    score = min(theme["occurrence_count"] * 15, 60)
    if theme["cross_municipality"]:
        score += 25
    if web_result.get("corroborated"):
        score += 15
    try:
        most_recent = max(m["date"] for m in theme["meetings"] if m.get("date"))
        recent_dt = datetime.strptime(most_recent, "%Y-%m-%d")
        if (datetime.now() - recent_dt).days <= 30:
            score += 5
    except (ValueError, TypeError):
        pass
    return min(score, 100)


# --- STEP 6b: INSIGHT-WORTHINESS FILTER ------------------------------------
# Per explicit user decision: a single, un-corroborated mention of something
# is "a meeting happened," not an insight. A theme only earns a place in
# insights.json if EITHER:
#   (a) it's genuinely recurring - the same specific matter came up in 2+
#       meetings, whether repeat sessions in one city (e.g. an ordinance's
#       first reading, hearing, and adoption) or the same subject surfacing
#       in multiple municipalities, OR
#   (b) it's independently corroborated by web search, even as a single
#       occurrence - e.g. a specific named ordinance real enough to show up
#       in independent coverage. This is deliberately modeled on the
#       "Ordinance Number 4316" example the user flagged as the right shape
#       of insight (which in practice satisfied both (a) and (b) at once -
#       it recurred across 3 meetings AND was web-corroborated).
# Everything else - a single, un-corroborated mention - is dropped rather
# than shown as a low-value "a meeting happened" card.

def is_insight_worthy(theme):
    return theme["occurrence_count"] >= 2 or theme["web_notability"].get("corroborated", False)


def assign_reason(theme):
    reasons = []
    if theme["occurrence_count"] >= 2:
        if theme["cross_municipality"]:
            reasons.append("recurring_across_municipalities")
        else:
            reasons.append("recurring_over_time")
    if theme["web_notability"].get("corroborated"):
        reasons.append("corroborated_by_web_search")
    theme["reasons"] = reasons


def load_insight_history(path=INSIGHTS_HISTORY_PATH):
    if not os.path.exists(path):
        return {"runs": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "runs" not in data:
                return {"runs": []}
            return data
    except (json.JSONDecodeError, OSError):
        return {"runs": []}


def theme_recurrence_key(theme):
    return (theme["category"], "".join(sorted(_normalize_topic_key(theme["theme_title"]))))


def apply_star_flags(themes, history):
    """Stars a theme if its recurrence key shows up in >=2 of the last
    HISTORY_RETENTION_RUNS daily runs (including today's, once saved)."""
    past_keys_by_run = []
    for run in history["runs"][-HISTORY_RETENTION_RUNS:]:
        keys = set(tuple(k) for k in run.get("theme_keys", []))
        past_keys_by_run.append(keys)

    for theme in themes:
        key = theme_recurrence_key(theme)
        appearances = sum(1 for keys in past_keys_by_run if key in keys)
        # +1 to count today's own occurrence, so a topic that showed up in
        # today's run plus at least 1 prior day gets starred.
        theme["starred"] = (appearances + 1) >= 2


def save_insight_history(themes, path=INSIGHTS_HISTORY_PATH):
    history = load_insight_history(path)
    today = datetime.now().strftime("%Y-%m-%d")

    run_record = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "theme_keys": [list(theme_recurrence_key(t)) for t in themes],
        "insights": [
            {
                "rank": i + 1,
                "theme_title": t["theme_title"],
                "category": t["category"],
                "confidence": t["confidence"],
                "starred": t.get("starred", False),
                "municipalities": t["municipalities"],
                "occurrence_count": t["occurrence_count"],
            }
            for i, t in enumerate(themes)
        ],
    }

    # Replace today's entry if this is a second run on the same date,
    # otherwise append.
    history["runs"] = [r for r in history["runs"] if r.get("date") != today]
    history["runs"].append(run_record)
    history["runs"] = history["runs"][-HISTORY_RETENTION_RUNS:]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"[Insights History] Saved run for {today}; "
          f"{len(history['runs'])} day(s) retained (max {HISTORY_RETENTION_RUNS}).")


# --- ORCHESTRATION -----------------------------------------------------------

def generate_insights_output():
    print("Starting Insights Engine...")
    print(f"[Insights] LLM available: {HAVE_HF and bool(HF_TOKEN)} "
          f"(model={HF_MODEL}) | Search available: {bool(SERPER_API_KEY)}")

    historical_events = gather_historical_events()

    cache = load_topic_cache()
    valid_keys = {_cache_key(ev) for ev in historical_events}
    cache = prune_topic_cache(cache, valid_keys)

    topic_entries = []
    cache_hits = 0
    llm_hits = 0
    heuristic_hits = 0
    docs_fetched = 0
    docs_unavailable = 0

    for event in historical_events:
        key = _cache_key(event)
        cached = cache.get(key)

        if cached is not None:
            # Already analyzed by the LLM in a prior run - past meeting
            # minutes don't change, so there's no reason to re-spend
            # credits on this one. Heuristic-only results are deliberately
            # never cached (see prune/cache-write logic below), so if we're
            # here, `cached` is guaranteed to be LLM-derived.
            topic_entries.extend(cached)
            cache_hits += 1
            continue

        doc_text = fetch_document_text(event)
        if doc_text:
            docs_fetched += 1
        else:
            docs_unavailable += 1
        topics, used_llm = extract_meeting_topics(event, doc_text)
        topic_entries.extend(topics)

        if used_llm:
            cache[key] = topics  # cache even if [] - avoids re-spending a
            llm_hits += 1        # credit re-confirming "nothing here" every day
        else:
            heuristic_hits += 1

        time.sleep(0.1)  # gentle pacing against source sites / the LLM API

    save_topic_cache(cache)
    print(f"[Insights Docs] Read real document text for {docs_fetched} "
          f"meeting(s); {docs_unavailable} had no agenda/minutes available "
          f"or the fetch failed (analyzed from title only).")
    print(f"[Insights] Topic extraction: {cache_hits} from cache (already "
          f"LLM-analyzed in a prior run), {llm_hits} newly LLM-analyzed this "
          f"run, {heuristic_hits} fell back to heuristic this run.")
    print(f"[Insights] Extracted {len(topic_entries)} raw topic entries "
          f"from {len(historical_events)} historical meetings.")

    themes = cluster_recurring_themes(topic_entries)
    print(f"[Insights] Clustered into {len(themes)} candidate theme(s).")

    for theme in themes:
        web_result = web_cross_reference(theme)
        theme["web_notability"] = web_result
        theme["confidence"] = compute_confidence(theme, web_result)
        time.sleep(0.2)  # gentle pacing against the search API

    search_checked = sum(1 for t in themes if t["web_notability"].get("checked"))
    search_corroborated = sum(1 for t in themes if t["web_notability"].get("corroborated"))
    search_errors = sum(1 for t in themes if t["web_notability"].get("error"))
    print(f"[Insights Search] {search_checked}/{len(themes)} theme(s) queried, "
          f"{search_corroborated} corroborated by independent web results, "
          f"{search_errors} request(s) failed.")

    # Per explicit user decision: drop anything that's neither recurring
    # (2+ occurrences) nor independently corroborated - a single,
    # un-corroborated mention is "a meeting happened," not an insight.
    pre_filter_count = len(themes)
    themes = [t for t in themes if is_insight_worthy(t)]
    for t in themes:
        assign_reason(t)
    print(f"[Insights] Insight-worthiness filter: kept {len(themes)} of "
          f"{pre_filter_count} candidate theme(s) (dropped "
          f"{pre_filter_count - len(themes)} single-occurrence, "
          f"non-corroborated mention(s)).")

    history = load_insight_history()
    apply_star_flags(themes, history)

    themes.sort(key=lambda t: t["confidence"], reverse=True)
    for i, t in enumerate(themes):
        t["rank"] = i + 1

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_months": HISTORICAL_MONTHS_BACK,
        "historical_meetings_scanned": len(historical_events),
        "insights": themes,
    }

    with open(INSIGHTS_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"[Insights] Wrote {len(themes)} ranked insight(s) to {INSIGHTS_OUTPUT_PATH}.")

    save_insight_history(themes)
    print("Insights Engine complete.")


if __name__ == "__main__":
    generate_insights_output()
