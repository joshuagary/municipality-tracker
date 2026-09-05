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
HISTORY_RETENTION_RUNS = 7  # rolling 7-day window, per user request

MAX_DOC_CHARS = 8000       # cap extracted document text fed to the LLM
MAX_TOPICS_PER_MEETING = 6

# Configurable so a model swap never requires touching code - see the
# "STATUS" note above. Override via repo/workflow env var if this model
# isn't available on your HF_TOKEN's Inference Providers routing.
HF_MODEL = os.environ.get("INSIGHTS_LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct")

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

    print(f"[Insights History] Gathered {len(filtered)} unique qualifying "
          f"historical events across {months_back} months "
          f"(from {len(all_events)} raw rows before dedup).")
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
    categories = ", ".join(TOPIC_TAXONOMY.keys())
    doc_excerpt = text_blob[:MAX_DOC_CHARS] if text_blob else "(No document text available - base this only on the meeting title.)"
    prompt = f"""You are analyzing a public government meeting record for topic extraction.

Municipality: {muni_full}
Meeting title: {title}
Date: {date}
Document excerpt:
\"\"\"{doc_excerpt}\"\"\"

Identify up to {MAX_TOPICS_PER_MEETING} SPECIFIC, CONCRETE topics or agenda items actually discussed (not generic restatements of the meeting title). For each, assign the closest category from this list if it fits: {categories}. If none fit, use "{FALLBACK_CATEGORY}".

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"topic_title": "short specific topic", "category": "one of the categories above", "description": "1-2 sentence factual description grounded in the excerpt"}}]

If the excerpt gives no real substance beyond the title, return a single item summarizing the meeting title itself."""

    try:
        response = client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
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
        return topics or None
    except Exception as e:
        print(f"[Insights LLM] Extraction failed for {muni_full} "
              f"'{title}' ({date}): {e} - falling back to heuristic extraction.")
        return None


def extract_meeting_topics(event, text_blob):
    client = get_hf_client()
    title = event.get("title", "")
    muni_full = event.get("muni_full", "")
    date = event.get("date", "")

    topics = None
    if client is not None:
        topics = _llm_topics(client, muni_full, title, date, text_blob)
    if topics is None:
        topics = _heuristic_topics(muni_full, title, text_blob)

    for t in topics:
        t["muni_short"] = event.get("muni_short")
        t["muni_full"] = muni_full
        t["event_title"] = title
        t["date"] = date
        t["link"] = event.get("link")
    return topics


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


def cluster_recurring_themes(topic_entries, similarity_threshold=0.35):
    """Groups topic entries into themes: first by category, then by word-
    overlap similarity of topic_title within that category. Simple and
    dependency-free (no embeddings needed) - good enough to surface real
    recurring subjects without over-engineering the clustering step."""
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
            themes.append({
                "theme_title": representative["topic_title"],
                "category": category,
                "municipalities": municipalities,
                "cross_municipality": len(municipalities) > 1,
                "occurrence_count": len(entries_in_cluster),
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
                    for e in sorted(entries_in_cluster, key=lambda e: e.get("date") or "", reverse=True)
                ],
            })
    return themes


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

    topic_entries = []
    for event in historical_events:
        doc_text = fetch_document_text(event)
        topics = extract_meeting_topics(event, doc_text)
        topic_entries.extend(topics)
        time.sleep(0.1)  # gentle pacing against source sites / the LLM API

    print(f"[Insights] Extracted {len(topic_entries)} raw topic entries "
          f"from {len(historical_events)} historical meetings.")

    themes = cluster_recurring_themes(topic_entries)
    print(f"[Insights] Clustered into {len(themes)} candidate theme(s).")

    for theme in themes:
        web_result = web_cross_reference(theme)
        theme["web_notability"] = web_result
        theme["confidence"] = compute_confidence(theme, web_result)
        time.sleep(0.2)  # gentle pacing against the search API

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
