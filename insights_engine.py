"""
insights_engine.py
===================

Powers the "Leads" tab (formerly "Insights"): a daily-refreshed list of
real-estate / land-use story leads for a local journalist, built from the
agendas of every monitored municipality.

What changed from the old "recurring topics" design, and why
------------------------------------------------------------
The old pipeline read the first 3,000 characters of each agenda, asked an LLM
for loose "topics", and clustered topics by word overlap. On the project's
own data that produced:
  * false links (two towns' unrelated "Ordinance 2026-03" matched on number),
  * bare identifiers as card titles, with no plain-English meaning,
  * only 108 of 331 meetings yielding anything, almost all from 5 towns,
  * nothing about what is coming up.

This version instead:
  1. Reads whole agendas (up to 60 PDF pages), and for calendar pages that
     only LINK to an agenda, follows the agenda link (see _find_agenda_links).
  2. Asks the LLM for structured LINE ITEMS (kind, address, case number,
     named private parties, dollar amount, action, plain-English summary and
     a verbatim evidence quote), not "topics".
  3. Checks every extracted fact against the document text. Quotes,
     addresses, case numbers, party names and amounts that cannot be found in
     the document are dropped, so nothing in a lead is a model invention.
  4. Groups items into LEADS keyed by municipality + property (or case
     number), never by a bare ordinance number, so unrelated matters in
     different towns are never merged.
  5. Attaches deterministic signals (repeat review, amount changed, tabled,
     adjacent parcels, cross-town party, upcoming vote) and a research kit.
  6. Also reads UPCOMING agendas from data.json, so a lead can say
     "on the Dec 8 agenda".
  7. Writes a per-municipality coverage report into leads.json, so the UI can
     say plainly where the system cannot see agenda text.

Fail-soft design is unchanged: no network, no key, or exhausted quota never
crashes the workflow. Meetings that could not be analysed are NOT cached and
are retried on the next run.

STATUS: written without outbound network access. The document-link following
and the Gemini structured-output call have not been exercised against the
live internet. The first real run's "[Insights Docs]", "[Insights LLM]" and
"[Insights Coverage]" log lines (and coverage_by_municipality in leads.json)
show exactly what worked.
"""

import os
import re
import io
import json
import time
import hashlib
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urljoin, urlparse

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


# --- CONFIG -----------------------------------------------------------------

HISTORICAL_MONTHS_BACK = 6

LEADS_OUTPUT_PATH = "leads.json"
ITEMS_CACHE_PATH = "agenda_items_cache.json"
CACHE_SCHEMA = 2

MAX_DOC_CHARS = 60000          # stored/extracted per document
MAX_PDF_PAGES = 60
MAX_LLM_DOC_CHARS = 45000      # sent to the LLM (one request per meeting)
MAX_LLM_RESPONSE_TOKENS = 8192
MAX_ITEMS_PER_MEETING = 25
NODOC_RETRY_DAYS = 7           # re-try fetching a past meeting with no readable document this often
CACHE_SAVE_EVERY = 25          # persist progress so a killed job does not lose LLM work
MAX_LEADS = 60
WEB_CHECK_TOP_N = 10

GEMINI_MODEL = os.environ.get("INSIGHTS_LLM_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def _gemini_endpoint():
    # A function, since GEMINI_MODEL can be replaced mid-run by the
    # self-healing retry in _llm_items().
    return f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


# Free-tier Flash-Lite is documented around 15 requests/minute.
GEMINI_MIN_SECONDS_BETWEEN_CALLS = 4.5

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")


# --- ITEM VOCABULARY --------------------------------------------------------

ITEM_KINDS = [
    "variance", "rezoning", "site_plan", "development_agreement",
    "land_transaction", "easement", "plat", "comp_plan_amendment",
    "annexation", "demolition", "historic", "incentive_abatement",
    "density_height", "cra_property", "design_review", "contract_vendor",
    "other",
]

KIND_LABELS = {
    "variance": "Variance", "rezoning": "Rezoning", "site_plan": "Site plan",
    "development_agreement": "Development agreement", "land_transaction": "Land deal",
    "easement": "Easement", "plat": "Plat", "comp_plan_amendment": "Comp plan change",
    "annexation": "Annexation", "demolition": "Demolition", "historic": "Historic",
    "incentive_abatement": "Incentive or abatement", "density_height": "Density or height",
    "cra_property": "CRA property", "design_review": "Design review",
    "contract_vendor": "Vendor contract", "other": "Property item",
}

KIND_WEIGHT = {
    "rezoning": 30, "development_agreement": 30, "comp_plan_amendment": 28,
    "annexation": 28, "land_transaction": 28, "incentive_abatement": 28,
    "density_height": 26, "cra_property": 24, "variance": 18, "site_plan": 18,
    "demolition": 16, "historic": 14, "plat": 12, "easement": 10,
    "design_review": 10, "contract_vendor": 8, "other": 4,
}

# A single appearance of one of these is still worth a reporter's attention.
STRONG_KINDS = {
    "rezoning", "development_agreement", "comp_plan_amendment", "annexation",
    "land_transaction", "incentive_abatement", "density_height",
}

# Kinds where a change in the agenda wording between hearings can mean the
# request itself changed.
CHANGE_KINDS = {
    "variance", "site_plan", "rezoning", "development_agreement",
    "land_transaction", "density_height", "comp_plan_amendment",
}

KIND_GROUP = {
    "variance": "zoning", "rezoning": "zoning", "comp_plan_amendment": "zoning",
    "annexation": "zoning", "density_height": "zoning",
    "site_plan": "development", "development_agreement": "development",
    "design_review": "development", "demolition": "development",
    "historic": "development", "plat": "development", "other": "development",
    "land_transaction": "land", "easement": "land", "cra_property": "land",
    "incentive_abatement": "land", "contract_vendor": "development",
}

ENTITY_ROLES = {"developer", "applicant", "owner", "vendor", "engineer",
                "architect", "attorney", "contractor"}

QUESTIONS_BY_KIND = {
    "variance": [
        "What exactly is being waived, and what did staff recommend?",
        "Who is the applicant and what do they plan to build or change?",
        "Have there been code or permit issues at this address?",
    ],
    "rezoning": [
        "What can be built under the new zoning that cannot be built now?",
        "Who owns the land and who is the applicant?",
        "What did staff and neighbors say?",
    ],
    "site_plan": [
        "Who is the developer, and what is the unit count or square footage?",
        "What conditions were attached to the approval?",
        "What do the traffic and drainage reviews say?",
    ],
    "development_agreement": [
        "What does the municipality give up and get in the agreement?",
        "Who signs for the developer, and who owns the land?",
        "What are the deadlines and penalties?",
    ],
    "land_transaction": [
        "Who is buying or leasing, and at what price?",
        "Was the property appraised or competitively offered?",
        "Who owned it before and what was it last sold for?",
    ],
    "easement": [
        "Which project requires this easement?",
        "Who owns the property and who benefits?",
        "Is this a condition of a development approval?",
    ],
    "plat": [
        "How many lots or units does the plat create?",
        "Who owns the land and who is the applicant?",
    ],
    "comp_plan_amendment": [
        "Which parcels does this affect and who owns them?",
        "What density or use change does it allow?",
        "Is a specific developer behind the request?",
    ],
    "annexation": [
        "Who owns the land being annexed and what do they plan?",
        "What services and taxes change for the property?",
    ],
    "demolition": [
        "What is being demolished and what replaces it?",
        "Is the building historic or protected?",
    ],
    "historic": [
        "Who owns the property and what do they want to do with it?",
        "What would designation restrict?",
    ],
    "incentive_abatement": [
        "What is the dollar value of the incentive, and over how many years?",
        "What does the recipient have to deliver, and who checks?",
    ],
    "density_height": [
        "How much more density or height is being allowed, and for whom?",
        "Which parcels and owners benefit?",
    ],
    "cra_property": [
        "Who is getting CRA property or money, and on what terms?",
        "Was it competitively bid or offered?",
    ],
    "design_review": [
        "Who is the owner or applicant?",
        "Is an existing structure being replaced?",
        "Why has this needed more than one review?",
    ],
    "contract_vendor": [
        "What is the contract amount and term?",
        "Was it competitively procured?",
    ],
    "other": [
        "Who owns the property and who is the applicant?",
        "What is the next decision and when?",
    ],
}

ENTITY_QUESTIONS = [
    "What are the contract or project amounts in the agenda backup?",
    "Does this party also appear on private development applications?",
    "Which other municipalities have they worked with?",
]

# Names that are governments / agencies, not private parties a reporter would
# chase as developers or vendors.
_GOV_ENTITY_RE = re.compile(
    r"\b(city|town|village|county|state|commission|commissioners|board|authority|"
    r"district|department|dept|council|school|agency|cra|dda|committee|government|"
    r"municipal|municipality|clerk|sheriff|police|fdot|united states|federal)\b",
    re.I,
)

_ENTITY_RE_KEYWORDS = re.compile(
    r"engineer|survey|architect|planning|design|construction|develop|land|real estate|"
    r"property|permit|zoning|site|plat|building|inspection|landscap|contractor",
    re.I,
)


# --- STEP 0: DATES -----------------------------------------------------------

def _today_et():
    """Today's date in Eastern time (the workflow runs at 00:00 UTC, which is
    still the previous evening in Florida)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def _fmt_date(d):
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}"
    except (TypeError, ValueError):
        return d or "?"


def _month_start(dt):
    return datetime(dt.year, dt.month, 1)


def _add_months(dt, n):
    month = dt.month - 1 + n
    year = dt.year + month // 12
    month = month % 12 + 1
    return datetime(year, month, 1)


# --- STEP 1: HISTORICAL EVENT GATHERING -------------------------------------
# Reuses every existing scrape_*() function unchanged, just with a widened
# date window (see set_date_window_override in scraper.py).

MULTI_MONTH_LOOP_SCRAPERS = [
    sc.scrape_boca_raton,
    sc.scrape_boynton_beach,
    sc.scrape_palm_beach_gardens,
    sc.scrape_wellington,
    sc.scrape_jupiter,
]

SINGLE_WINDOW_SCRAPERS = [
    sc.scrape_west_palm_beach,
    sc.scrape_west_palm_beach_planning,
    sc.scrape_west_palm_beach_plans_plats,
    sc.scrape_palm_beach_county,
    sc.scrape_pbc_zoning_calendar,
    sc.scrape_delray_beach,
    sc.scrape_westlake,
    sc.scrape_downtown_wpb_dda,
    sc.scrape_palm_beach,
    sc.scrape_riviera_beach,
    sc.scrape_juno_beach,
    sc.scrape_jupiter_inlet_colony,
    sc.scrape_manalapan,
    sc.scrape_gulf_stream,
    sc.scrape_north_palm_beach,
]

CANCELLATION_PATTERN = re.compile(r'\bCANCEL(?:L)?ED\b', re.I)

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
    """Deduplicated qualifying events from the last `months_back` months,
    reusing scraper.py's own scrape functions and whitelist filter."""
    all_events = []
    now = datetime.now()
    this_month_start = _month_start(now)

    for fn in MULTI_MONTH_LOOP_SCRAPERS:
        for i in range(1, months_back + 1):
            target_month = _add_months(this_month_start, -i)
            next_month = _add_months(target_month, 1)
            try:
                sc.set_date_window_override(target_month, next_month)
                all_events.extend(fn())
            except Exception as e:
                print(f"[Insights History] {fn.__name__} failed for "
                      f"{target_month.strftime('%Y-%m')}: {e}")
            finally:
                sc.clear_date_window_override()

    window_start = _add_months(this_month_start, -months_back)
    window_end = now + timedelta(days=1)
    for fn in SINGLE_WINDOW_SCRAPERS:
        try:
            sc.set_date_window_override(window_start, window_end)
            all_events.extend(fn())
        except Exception as e:
            print(f"[Insights History] {fn.__name__} failed: {e}")
        finally:
            sc.clear_date_window_override()

    seen = set()
    deduped = []
    for ev in all_events:
        key = sc.compute_change_key(ev)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    cutoff = _add_months(this_month_start, -months_back)
    filtered = []
    for ev in deduped:
        try:
            ev_dt = datetime.strptime(ev["date"], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        if cutoff <= ev_dt <= now:
            filtered.append(ev)

    cancelled_count = sum(1 for ev in filtered if is_cancelled_event(ev.get("title")))
    filtered = [ev for ev in filtered if not is_cancelled_event(ev.get("title"))]
    for ev in filtered:
        ev["title"] = strip_schedule_notes(ev.get("title", ""))

    print(f"[Insights History] Gathered {len(filtered)} unique qualifying "
          f"historical events across {months_back} months "
          f"(from {len(all_events)} raw rows before dedup, "
          f"{cancelled_count} cancelled meeting(s) excluded).")
    return filtered


def load_upcoming_events(today_str, path="data.json"):
    """Upcoming meetings already scraped into data.json by scraper.py. These
    let a lead say 'on the Dec 8 agenda' and let a reporter see items before
    the vote. Cancelled meetings are skipped."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    events, seen = [], set()
    for ev in data if isinstance(data, list) else []:
        d = ev.get("date") or ""
        if d < today_str or is_cancelled_event(ev.get("title")):
            continue
        key = sc.compute_change_key(ev)
        if key in seen:
            continue
        seen.add(key)
        events.append(dict(ev))
    return events


# --- STEP 2: DOCUMENT TEXT ("reading the agenda") ---------------------------

PAGE_MARK_RE = re.compile(r"\n?\[\[PAGE (\d+)\]\]\n")


def _extract_pdf_text(pdf_bytes):
    """Text of a PDF with [[PAGE n]] markers so evidence can cite a page."""
    pages = []
    total = 0
    if HAVE_PDFPLUMBER:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for i, page in enumerate(pdf.pages[:MAX_PDF_PAGES], 1):
                    t = page.extract_text() or ""
                    pages.append((i, t))
                    total += len(t)
                    if total >= MAX_DOC_CHARS:
                        break
        except Exception as e:
            print(f"[Insights Docs] pdfplumber failed: {e}")
            pages = []
    if not any(t.strip() for _, t in pages) and HAVE_PYPDF2:
        pages, total = [], 0
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
                t = page.extract_text() or ""
                pages.append((i, t))
                total += len(t)
                if total >= MAX_DOC_CHARS:
                    break
        except Exception as e:
            print(f"[Insights Docs] PyPDF2 fallback failed: {e}")
            pages = []
    text = "".join(f"\n[[PAGE {n}]]\n{t}" for n, t in pages if t.strip()).strip()
    return text[:MAX_DOC_CHARS] if text else None


def _html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


_AGENDA_WORD = re.compile(r"\bagenda\b", re.I)
_MINUTES_WORD = re.compile(r"\bminutes\b", re.I)
_AGENDACENTER_ROOT = re.compile(r"/agendacenter/?(\?.*)?$", re.I)


_DOC_URL_HINTS = ("viewfile", "documentcenter/view", "agendaviewer", "/files/agenda")


def _looks_like_document_url(lower_url):
    return lower_url.split("?")[0].endswith(".pdf") or any(h in lower_url for h in _DOC_URL_HINTS)


def _find_agenda_links(html, base_url, limit=2):
    """Calendar/event pages (CivicPlus Calendar.aspx, Granicus, etc.) usually
    only LINK to the agenda. Returns up to `limit` absolute URLs most likely
    to be the agenda DOCUMENT, best first.

    First real run (2026-11-21) showed the earlier text-only rule following the
    site-wide "Agendas & Minutes" navigation page (.../129/Agendas-Minutes) on
    nearly every Boca and Boynton event, which is a listing page, not an
    agenda, and produced no items. A candidate must now look like a document
    URL (a PDF, ViewFile, DocumentCenter/View, AgendaViewer) AND mention
    "agenda" in its text or URL."""
    soup = BeautifulSoup(html, "html.parser")
    cands = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        text = a.get_text(" ", strip=True)
        text_hit = bool(_AGENDA_WORD.search(text))
        href_hit = bool(re.search(r"agenda", href, re.I))
        if not (text_hit or href_hit):
            continue
        if _MINUTES_WORD.search(text) and not text_hit:
            continue
        full = urljoin(base_url, href)
        if full.rstrip("/") == base_url.rstrip("/") or _AGENDACENTER_ROOT.search(full):
            continue
        lower = full.lower()
        if not _looks_like_document_url(lower):
            continue
        score = 3
        if text_hit:
            score += 2
        if href_hit:
            score += 1
        if "packet" in text.lower() or "packet" in lower:
            score -= 2  # the plain agenda first: short, and it names every item; the packet follows if room remains
        if score >= 3:
            cands[full] = max(cands.get(full, 0), score)
    ranked = sorted(cands.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:limit]]


_diag_printed = defaultdict(int)
_DIAG_PER_MUNI = 2


def _log_missing_agenda_link(event, html, link):
    """When a calendar page yields no agenda document, print what agenda-ish
    anchors the page DID have (first two per municipality per run) so the next
    fix can be built from real markup instead of guesses."""
    muni = event.get("muni_short") or "?"
    if _diag_printed[muni] >= _DIAG_PER_MUNI:
        return
    _diag_printed[muni] += 1
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        found = []
        for a in soup.find_all("a", href=True):
            t = a.get_text(" ", strip=True)
            h = a["href"].strip()
            if re.search(r"agenda|packet|document|\.pdf|minutes", f"{t} {h}", re.I):
                found.append(f"[{t[:40]}]({h[:110]})")
            if len(found) >= 8:
                break
        print(f"[Insights Docs] no agenda document link on {muni} {event.get('date')} {link} "
              f"(page text {len(_html_to_text(html or ''))} chars); agenda-like anchors: "
              f"{' | '.join(found) if found else 'none'}")
    except Exception as e:
        print(f"[Insights Docs] diagnostic failed for {muni}: {e}")


_HOST_MIN_GAP = 1.0           # seconds between requests to the same host
_last_host_time = {}
_fetch_transient_failure = False  # set when a fetch failed in a way worth retrying tomorrow
_ALT_HOSTS = {"discover.pbc.gov": "www.pbcgov.org"}
_UA = {"User-Agent": "Mozilla/5.0"}


def _throttle(url):
    host = urlparse(url).netloc
    wait = _HOST_MIN_GAP - (time.time() - _last_host_time.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_host_time[host] = time.time()


def _candidate_urls(link):
    """The link itself, then (for hosts known to block GitHub Actions) an
    alternate host with the same path. The alternate is a best guess and
    unverified: Palm Beach County's discover.pbc.gov WAF is why the scraper
    itself uses www.pbcgov.org."""
    urls = [link]
    host = urlparse(link).netloc
    if host in _ALT_HOSTS:
        urls.append(link.replace(host, _ALT_HOSTS[host], 1))
    return urls


def _fetch(link, timeout):
    """GET with per-host spacing (the first full run saw delraybeach.legistar.com
    drop every connection after a read timeout, a burst-rate symptom), one retry
    on a connection error or 5xx, and an alternate host where one is known.
    Returns a response or None. Sets _fetch_transient_failure when nothing
    usable came back, so the caller does not remember the meeting as 'has no
    document'."""
    global _fetch_transient_failure
    last = None
    for url in _candidate_urls(link):
        for attempt in (1, 2):
            _throttle(url)
            res = None
            try:
                res = sc.fetch_hardened(url, timeout=timeout)
                if res is None:
                    res = requests.get(url, timeout=timeout, headers=_UA)
            except Exception:
                res = None
            if res is not None and res.status_code == 200:
                return res
            if res is not None and res.status_code < 500:
                last = res      # a definite answer (404, 403...): do not retry this URL
                break
            if attempt == 1:
                time.sleep(3)
    if last is None or last.status_code >= 500:
        _fetch_transient_failure = True
    return last


def _is_pdf_response(res, link):
    if link.lower().split("?")[0].endswith(".pdf"):
        return True
    ctype = (getattr(res, "headers", {}) or {}).get("Content-Type", "") or ""
    if ctype.lower().startswith("application/pdf"):
        return True
    content = getattr(res, "content", b"") or b""
    return content[:4] == b"%PDF"


def _fetch_linked_text(url, timeout):
    """Text of a document reached by following a link (no further following)."""
    try:
        res = _fetch(url, timeout)
        if res is None or res.status_code != 200:
            return None
        if _is_pdf_response(res, url):
            return _extract_pdf_text(res.content)
        text = _html_to_text(res.text)
        return text[:MAX_DOC_CHARS] if len(text) >= 400 else None
    except Exception as e:
        print(f"[Insights Docs] Failed to read linked agenda {url}: {e}")
        return None


def fetch_document_text(event, timeout=25):
    """Best-effort agenda text behind an event's link; None (never a crash)
    on any failure."""
    if event.get("has_agenda") is False:
        return None
    link = event.get("link")
    if not link or not str(link).startswith("http"):
        return None
    try:
        res = _fetch(link, timeout)
        if res is None or res.status_code != 200:
            return None
        if _is_pdf_response(res, link):
            return _extract_pdf_text(res.content)

        html = res.text or ""
        page_text = _html_to_text(html)
        is_calendar_page = "calendar.aspx" in link.lower()

        parts = [] if is_calendar_page else ([page_text] if page_text else [])
        if is_calendar_page or len(page_text) < 4000:
            for alink in _find_agenda_links(html, link):
                sub = _fetch_linked_text(alink, timeout)
                if sub:
                    print(f"[Insights Docs] followed agenda link for "
                          f"{event.get('muni_short')} {event.get('date')}: {alink}")
                    parts.append(sub)
        text = "\n".join(parts).strip()
        if is_calendar_page:
            if not text:
                _log_missing_agenda_link(event, html, link)
            return text[:MAX_DOC_CHARS] if text else None
        return text[:MAX_DOC_CHARS] if len(text) >= 800 else None
    except Exception as e:
        global _fetch_transient_failure
        _fetch_transient_failure = True
        print(f"[Insights Docs] Failed to read document for "
              f"{event.get('muni_short')} {event.get('date')} "
              f"'{event.get('title')}': {e}")
        return None


# --- STEP 3: STRUCTURED ITEM EXTRACTION (LLM) -------------------------------

_gemini_unavailable = False   # set True on quota exhaustion or auth/config error
_gemini_checked_available = False
_last_gemini_call_time = 0.0


def gemini_available():
    global _gemini_checked_available
    if not _gemini_checked_available:
        _gemini_checked_available = True
        if not GEMINI_API_KEY:
            print("[Insights LLM] No GEMINI_API_KEY set - no new meetings will be analyzed this run.")
        else:
            print(f"[Insights LLM] Using Gemini model '{GEMINI_MODEL}' via Google AI Studio free tier.")
    return bool(GEMINI_API_KEY) and not _gemini_unavailable


def _extract_suggested_replacement_model(err_text):
    m = re.search(r"use models/([a-zA-Z0-9\-\.]+)", err_text)
    return m.group(1) if m else None


def _parse_json_array(raw):
    """Parses the model's JSON array. If the output was cut off mid-array
    (the old failure mode: ~half of calls), salvages every COMPLETE object
    before the cut. Returns (list, salvaged_flag); raises ValueError if
    nothing usable."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            for k in ("items", "results", "leads"):
                if isinstance(value.get(k), list):
                    value = value[k]
                    break
        if isinstance(value, list):
            return value, False
        raise ValueError("not a JSON array")
    except json.JSONDecodeError:
        pass
    start = raw.find("[")
    if start < 0:
        raise ValueError("no JSON array in response")
    dec = json.JSONDecoder()
    i, out = start + 1, []
    while True:
        while i < len(raw) and raw[i] in " \n\r\t,":
            i += 1
        if i >= len(raw) or raw[i] != "{":
            break
        try:
            obj, j = dec.raw_decode(raw, i)
        except ValueError:
            break
        out.append(obj)
        i = j
    if not out:
        raise ValueError("could not recover any complete item from truncated JSON")
    return out, True


def _call_gemini_once(prompt):
    """One Gemini call. Returns (raw_items, salvaged, None) or (None, False, exc)."""
    global _last_gemini_call_time
    elapsed = time.time() - _last_gemini_call_time
    if elapsed < GEMINI_MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(GEMINI_MIN_SECONDS_BETWEEN_CALLS - elapsed)
    try:
        resp = requests.post(
            _gemini_endpoint(),
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": MAX_LLM_RESPONSE_TOKENS,
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            },
            timeout=90,
        )
        _last_gemini_call_time = time.time()
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:300]}", response=resp)
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return [], False, None  # safety-blocked: nothing found, retrying will not help
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            return [], False, None
        items, salvaged = _parse_json_array(text)
        if cand.get("finishReason") == "MAX_TOKENS":
            salvaged = True
        return items, salvaged, None
    except Exception as e:
        _last_gemini_call_time = time.time()
        return None, False, e


def build_extraction_prompt(event, text):
    excerpt = text[:MAX_LLM_DOC_CHARS]
    kinds = ", ".join(ITEM_KINDS)
    return f"""You are a research assistant for a local REAL ESTATE and development reporter. Read this public government meeting document and list the line items a reporter covering land use, development and property deals would want to chase.

Municipality: {event.get('muni_full', '')}
Meeting: {event.get('title', '')}
Date: {event.get('date', '')}

Document (markers like [[PAGE 3]] show page breaks):
\"\"\"{excerpt}\"\"\"

Include only items about: rezonings, variances, special exceptions, site plans, plats, comprehensive plan amendments, annexations, development agreements, density or height changes, demolitions, historic designations, design/appearance/impact reviews of specific properties, sales, leases, purchases or easements of land or buildings, CRA or DDA property or incentive deals, tax abatements or impact-fee changes, and contracts for engineering, planning, architecture, surveying or construction services.
Exclude: budget lines unrelated to property, routine procedure (call to order, roll call, minutes, adjournment), proclamations, and general public comment.

For EACH item return an object with exactly these keys:
- "headline": plain English, 6 to 14 words, saying what is proposed. Never just an identifier.
- "kind": one of: {kinds}
- "address": street address as written in the document, or ""
- "case_no": application, case, permit, ordinance or resolution number as written, or ""
- "entities": private parties named in the item (developers, applicants, owners, vendors, engineers, architects, attorneys) as [{{"name": "...", "role": "developer|applicant|owner|vendor|engineer|architect|attorney|contractor"}}]. Never list governments, boards or agencies.
- "amount": dollar value of the item as a number, or null
- "action": what the body is asked to do (for example "first reading", "public hearing", "approve", "table")
- "consent": true if the item is on a consent agenda, otherwise false
- "summary": 1 to 2 factual sentences
- "evidence": ONE short verbatim quote (under 200 characters) copied exactly from the document that supports the item

Rules: copy addresses, numbers, names and quotes exactly as written. Never guess or infer a value that is not in the document; use "" or null instead. Return at most {MAX_ITEMS_PER_MEETING} items. If there are none, return [].
Return ONLY a JSON array."""


_GEMINI_5XX_RETRY_DELAYS = (10, 30)
_gemini_5xx_streak = 0
_GEMINI_5XX_STREAK_LIMIT = 3


def _call_gemini_with_retry(prompt):
    """The first full run logged 42 'HTTP 503 ... high demand' responses. Those
    are temporary, so retry twice with a pause. If three meetings in a row
    still fail this way the service is struggling: stop for this run."""
    global _gemini_unavailable, _gemini_5xx_streak
    items, salvaged, err = _call_gemini_once(prompt)
    for delay in _GEMINI_5XX_RETRY_DELAYS:
        status = getattr(getattr(err, "response", None), "status_code", None)
        if err is None or status not in (500, 502, 503, 504):
            break
        time.sleep(delay)
        items, salvaged, err = _call_gemini_once(prompt)
    status = getattr(getattr(err, "response", None), "status_code", None)
    if err is None:
        _gemini_5xx_streak = 0
    elif status in (500, 502, 503, 504):
        _gemini_5xx_streak += 1
        if _gemini_5xx_streak >= _GEMINI_5XX_STREAK_LIMIT and not _gemini_unavailable:
            print(f"[Insights LLM] Gemini returned server errors for {_gemini_5xx_streak} meetings in a "
                  f"row even after retries; skipping further LLM calls this run (they retry tomorrow).")
            _gemini_unavailable = True
    return items, salvaged, err


def _llm_items(event, text):
    """Raw item dicts from Gemini, or None on failure (the caller then does
    NOT cache, so the meeting is retried on the next run)."""
    global _gemini_unavailable, GEMINI_MODEL
    if not gemini_available():
        return None
    prompt = build_extraction_prompt(event, text)
    items, salvaged, err = _call_gemini_with_retry(prompt)

    if err is not None:
        err_text = str(err)
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 404:
            replacement = _extract_suggested_replacement_model(err_text)
            if replacement and replacement != GEMINI_MODEL:
                print(f"[Insights LLM] Model '{GEMINI_MODEL}' appears deprecated; Gemini suggested "
                      f"'{replacement}' - switching for the rest of this run. Consider updating "
                      f"INSIGHTS_LLM_MODEL permanently.")
                GEMINI_MODEL = replacement
                items, salvaged, err = _call_gemini_once(prompt)
                if err is not None:
                    err_text = str(err)
                    status = getattr(getattr(err, "response", None), "status_code", None)
        if err is not None:
            if status == 429 or "RESOURCE_EXHAUSTED" in err_text:
                if not _gemini_unavailable:
                    print(f"[Insights LLM] Gemini free-tier quota appears exhausted ({err_text[:200]}). "
                          f"Remaining meetings are skipped this run and retried tomorrow.")
                _gemini_unavailable = True
            elif status in (400, 401, 403, 404):
                if not _gemini_unavailable:
                    print(f"[Insights LLM] Gemini rejected the request ({err_text[:200]}). Likely a bad key "
                          f"or model name '{GEMINI_MODEL}'. Skipping further LLM calls this run.")
                _gemini_unavailable = True
            else:
                print(f"[Insights LLM] Extraction failed for {event.get('muni_full')} "
                      f"'{event.get('title')}' ({event.get('date')}): {err_text[:200]}")
            return None

    if salvaged:
        print(f"[Insights LLM] Response for {event.get('muni_short')} {event.get('date')} was cut off; "
              f"kept the {len(items)} complete item(s) recovered.")
    return items


# --- STEP 3b: VERIFY EVERYTHING AGAINST THE DOCUMENT ------------------------
# A lead is only useful to a journalist if every fact in it can be traced.
# Anything the model reports that cannot be found in the document text is
# dropped, not shown.

_ABBR = {
    "avenue": "ave", "street": "st", "drive": "dr", "road": "rd", "lane": "ln",
    "boulevard": "blvd", "court": "ct", "place": "pl", "terrace": "ter",
    "circle": "cir", "highway": "hwy", "parkway": "pkwy", "trail": "trl",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "north": "n", "south": "s", "east": "e", "west": "w",
}


def _canon(s):
    """Lowercase, punctuation to spaces, street words abbreviated, spaces
    collapsed. Used for addresses on both sides of a comparison."""
    toks = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    return " ".join(_ABBR.get(t, t) for t in toks)


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_UNIT_RE = re.compile(r"\b(?:unit|apt|apartment|suite|ste|#)\s*[\w-]+\s*$", re.I)


def normalize_address(addr):
    """Canonical address ('755 se 7th ave') or None if it has no street
    number. City/state/zip after the first comma and unit numbers are dropped."""
    if not addr:
        return None
    a = addr.split(",")[0].strip()
    a = _UNIT_RE.sub("", a).strip()
    c = _canon(a)
    return c if re.match(r"^\d+\s+\S", c) else None


def normalize_case(case_no):
    s = _squash(case_no)
    return s.upper() if len(s) >= 4 else None


_ENTITY_DROP = {"inc", "llc", "llp", "lp", "corp", "corporation", "co", "company",
                "ltd", "pa", "pllc", "pc", "the", "incorporated"}


def normalize_entity(name):
    toks = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(t for t in toks if t not in _ENTITY_DROP)


_PRIVATE_HINT_RE = re.compile(
    r"\b(llc|inc|corp|corporation|lp|llp|ltd|pllc|company|partners|associates|group|"
    r"holdings|properties|development|developers|builders|engineering|architects|"
    r"enterprises|investments|realty|homes)\b",
    re.I,
)


def is_government_entity(name):
    """True for governments/agencies. A name with a clear private-company
    marker (LLC, Inc, Partners...) is treated as private even if it contains
    a word like 'Town' or 'Village' (e.g. 'Village Square Partners LLC')."""
    name = name or ""
    return bool(_GOV_ENTITY_RE.search(name)) and not _PRIVATE_HINT_RE.search(name)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _split_pages(text):
    parts = PAGE_MARK_RE.split(text)
    if len(parts) == 1:
        return [(None, text)]
    pages = []
    if parts[0].strip():
        pages.append((None, parts[0]))
    for i in range(1, len(parts) - 1, 2):
        pages.append((int(parts[i]), parts[i + 1]))
    return pages


def _verified_amount(val, squashed_text):
    if val is None or isinstance(val, bool):
        return None
    try:
        amt = float(str(val).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    if amt <= 0 or amt > 1e11:
        return None
    digits = str(int(round(amt)))
    if len(digits) >= 4 and digits in squashed_text:
        return int(round(amt))
    if amt >= 1e6 and f"{amt / 1e6:g}million".replace(".", "") in squashed_text:
        return int(round(amt))
    return None


def normalize_items(raw_items, event, text, today_str):
    """Validates and verifies raw model items against `text`. Returns the
    list of clean item dicts (with evidence/page verification)."""
    plain = PAGE_MARK_RE.sub(" ", text)
    sq = _squash(plain)
    canon_text = f" {_canon(plain)} "
    pages = _split_pages(text)
    page_sq = [(n, _squash(t)) for n, t in pages]

    items = []
    for raw in (raw_items or [])[:MAX_ITEMS_PER_MEETING]:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in ITEM_KINDS:
            kind = "other"
        headline = _clean(raw.get("headline"), 140)
        summary = _clean(raw.get("summary"), 500)
        if not (headline or summary):
            continue

        address = _clean(raw.get("address"), 120)
        if address:
            canon_addr = normalize_address(address)
            if not canon_addr or f" {canon_addr} " not in canon_text:
                address = ""

        case_no = _clean(raw.get("case_no"), 60)
        if case_no and _squash(case_no) not in sq:
            case_no = ""

        entities = []
        for e in raw.get("entities") or []:
            if not isinstance(e, dict):
                continue
            name = _clean(e.get("name"), 100)
            role = _clean(e.get("role"), 20).lower()
            if (name and not is_government_entity(name) and role in ENTITY_ROLES
                    and _squash(name) in sq):
                entities.append({"name": name, "role": role})

        quote = _clean(raw.get("evidence"), 300)
        evidence = {"quote": quote, "page": None, "verified": False}
        sq_quote = _squash(quote)
        if len(sq_quote) >= 12 and sq_quote in sq:
            evidence["verified"] = True
            for n, psq in page_sq:
                if n is not None and sq_quote in psq:
                    evidence["page"] = n
                    break

        items.append({
            "headline": headline or summary[:100],
            "kind": kind,
            "address": address,
            "case_no": case_no,
            "entities": entities,
            "amount": _verified_amount(raw.get("amount"), sq),
            "action": _clean(raw.get("action"), 60),
            "consent": bool(raw.get("consent")),
            "summary": summary,
            "evidence": evidence,
            "muni_short": event.get("muni_short"),
            "muni_full": event.get("muni_full"),
            "body": event.get("title", ""),
            "date": event.get("date", ""),
            "link": event.get("link"),
        })
    return items


# --- ITEM CACHE -------------------------------------------------------------
# Past agendas do not change, so a successfully analyzed past meeting is
# never re-analyzed. Upcoming agendas can be amended, so they are re-fetched
# daily and re-analyzed only when the document text changed (hash). Failures
# are never cached.

def load_items_cache(path=ITEMS_CACHE_PATH):
    empty = {"schema": CACHE_SCHEMA, "events": {}, "nodoc": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return empty
    data.setdefault("events", {})
    data.setdefault("nodoc", {})
    return data


def save_items_cache(cache, path=ITEMS_CACHE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)


def _cache_key(event):
    return "|".join(sc.compute_change_key(event))


def _doc_hash(text):
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


def analyze_event(event, cache, today_str, is_upcoming):
    """Returns (items, status, had_document). status is one of: cache, llm,
    nodoc (no document exists or none could be found), fetch_failed (a
    transient fetch error, retried next run), pending (no LLM available / LLM
    call failed)."""
    key = _cache_key(event)
    entry = cache["events"].get(key)

    if entry and not is_upcoming:
        return entry["items"], "cache", True

    if not is_upcoming:
        tried = cache["nodoc"].get(key)
        if tried:
            try:
                age = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(tried, "%Y-%m-%d")).days
            except ValueError:
                age = NODOC_RETRY_DAYS
            if age < NODOC_RETRY_DAYS:
                return [], "nodoc", False

    global _fetch_transient_failure
    _fetch_transient_failure = False
    text = fetch_document_text(event)
    if not text:
        if _fetch_transient_failure:
            # Timeout, connection reset or 5xx: say nothing about whether a
            # document exists, so do not remember it as 'no document'.
            return (entry["items"] if entry else []), "fetch_failed", False
        if not is_upcoming:
            cache["nodoc"][key] = today_str
        return (entry["items"] if entry else []), "nodoc", False
    cache["nodoc"].pop(key, None)

    h = _doc_hash(text)
    if entry and entry.get("doc_hash") == h:
        return entry["items"], "cache", True

    raw = _llm_items(event, text)
    if raw is None:
        # LLM unavailable or failed: keep any older analysis, retry next run.
        return (entry["items"] if entry else []), "pending", True
    items = normalize_items(raw, event, text, today_str)
    cache["events"][key] = {"doc_hash": h, "analyzed_at": today_str, "items": items}
    return items, "llm", True


# --- STEP 4: GROUP ITEMS INTO LEADS -----------------------------------------

def _normalize_topic_key(text):
    text = re.sub(r"[^a-z0-9 ]", "", (text or "").lower())
    return set(w for w in text.split() if len(w) > 2)


def _similarity(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _body_key(body):
    try:
        return sc._normalize_title_for_key(body)
    except Exception:
        return re.sub(r"[^a-z0-9]", "", (body or "").lower())


def assign_item_keys(items):
    """Sets addr_norm / case_norm / key on each item. Keys are ALWAYS scoped
    to the municipality, so 'Ordinance 2026-03' in two towns is two
    different things. A case number seen with an address in the same town
    resolves to that address's key."""
    case_to_addr = {}
    for it in items:
        it["addr_norm"] = normalize_address(it.get("address"))
        it["case_norm"] = normalize_case(it.get("case_no"))
        if it["addr_norm"] and it["case_norm"]:
            case_to_addr[(it["muni_short"], it["case_norm"])] = it["addr_norm"]
    for it in items:
        m = it["muni_short"]
        if it["addr_norm"]:
            it["key"] = f"{m}|addr|{it['addr_norm']}"
        elif it["case_norm"]:
            a = case_to_addr.get((m, it["case_norm"]))
            it["key"] = f"{m}|addr|{a}" if a else f"{m}|case|{it['case_norm']}"
        else:
            it["key"] = None
    return items


def _street_core(addr_norm):
    return re.sub(r"^\d+\s+", "", addr_norm or "")


def _house_number(addr_norm):
    m = re.match(r"^(\d+)", addr_norm or "")
    return int(m.group(1)) if m else None


def _meeting_ids(items):
    return {(it["date"], _body_key(it["body"])) for it in items}


def _merge_adjacent(groups):
    """Groups of items for neighbouring same-side addresses (within 10 house
    numbers) that were handled at the same meeting are merged into one lead
    keyed by the lowest address. Returns {primary_key: {"items", "addresses"}}."""
    by_street = defaultdict(list)
    for key, its in groups.items():
        if "|addr|" not in key:
            continue
        muni, _, addr = key.split("|", 2)
        num = _house_number(addr)
        if num is not None:
            by_street[(muni, _street_core(addr))].append((num, key))

    parent = {k: k for k in groups}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, lst in by_street.items():
        lst.sort()
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (na, ka), (nb, kb) = lst[i], lst[j]
                if nb - na > 10:
                    break
                if (nb - na) % 2 != 0:
                    continue
                if _meeting_ids(groups[ka]) & _meeting_ids(groups[kb]):
                    parent[find(kb)] = find(ka)

    comps = defaultdict(list)
    for k in groups:
        comps[find(k)].append(k)

    merged = {}
    for root, keys in comps.items():
        def sort_key(k):
            muni, _, addr = k.split("|", 2) if "|addr|" in k else (k, "", "")
            n = _house_number(addr)
            return (n if n is not None else 10**9, k)
        keys.sort(key=sort_key)
        primary = keys[0]
        items = [it for k in keys for it in groups[k]]
        addresses = []
        for k in keys:
            if "|addr|" in k:
                addresses.append(k.split("|", 2)[2])
        merged[primary] = {"items": items, "addresses": addresses if len(keys) > 1 else [],
                           "adjacent": len(keys) > 1}
    return merged


def _dedupe_appearances(items):
    """One appearance per (date, body); keeps the richest summary and joins
    per-address summaries when several addresses share one meeting."""
    by_meeting = {}
    for it in sorted(items, key=lambda x: (x["date"], x["body"])):
        mid = (it["date"], _body_key(it["body"]))
        cur = by_meeting.get(mid)
        if cur is None:
            by_meeting[mid] = {**it, "_addresses": [it.get("address")] if it.get("address") else []}
            continue
        if it.get("address") and it["address"] not in cur["_addresses"]:
            cur["_addresses"].append(it["address"])
            cur["summary"] = f"{cur['summary']} / {it['summary']}"[:700]
        elif len(it.get("summary", "")) > len(cur.get("summary", "")):
            keep_addrs = cur["_addresses"]
            cur.update({k: it[k] for k in ("summary", "headline", "action", "consent", "evidence", "link")})
            cur["_addresses"] = keep_addrs
        if it.get("amount") and not cur.get("amount"):
            cur["amount"] = it["amount"]
        known = {e["name"] for e in cur["entities"]}
        cur["entities"] = cur["entities"] + [e for e in it["entities"] if e["name"] not in known]
    return sorted(by_meeting.values(), key=lambda x: x["date"])


_TABLED_RE = re.compile(r"\b(tabl|defer|continu|postpon|withdraw)", re.I)
_FDOT_RE = re.compile(r"\bFDOT\b|maintenance agreement|pedestrian clear zone", re.I)


def _fmt_money(n):
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"${v:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"${n / 1000:.0f}K"
    return f"${n:,}"


def _signals_for_appearances(apps, kind, today_str, adjacent_addresses):
    """Deterministic signals; every one is derived from extracted, verified
    fields, never from model opinion."""
    sigs = []
    hypothesis = None

    strong = kind in STRONG_KINDS
    sigs.append({"id": "kind", "label": KIND_LABELS.get(kind, "Property item"),
                 "tone": "warn" if strong else "info"})

    n = len(apps)
    if n >= 2:
        sigs.append({"id": "repeat", "label": f"On {n} agendas", "tone": "warn" if n >= 3 else "info"})

    upcoming = [a for a in apps if a["date"] >= today_str]
    if upcoming:
        sigs.append({"id": "upcoming", "label": f"On {_fmt_date(upcoming[0]['date'])} agenda", "tone": "success"})

    amounts = [a["amount"] for a in apps if a.get("amount")]
    if amounts:
        if len(set(amounts)) > 1:
            sigs.append({"id": "amount_changed",
                         "label": f"Amount changed {_fmt_money(amounts[0])} to {_fmt_money(amounts[-1])}",
                         "tone": "warn"})
        elif amounts[-1] >= 100_000:
            sigs.append({"id": "amount", "label": _fmt_money(amounts[-1]), "tone": "info"})

    if any(a.get("consent") for a in apps) and amounts and max(amounts) >= 100_000:
        sigs.append({"id": "consent", "label": "On consent agenda", "tone": "warn"})

    if any(_TABLED_RE.search(a.get("action") or "") for a in apps):
        sigs.append({"id": "tabled", "label": "Tabled or continued", "tone": "warn"})

    if n >= 2 and kind in CHANGE_KINDS:
        first, last = apps[0], apps[-1]
        q1, q2 = first["evidence"], last["evidence"]
        if q1.get("verified") and q2.get("verified"):
            sim = _similarity(_normalize_topic_key(q1["quote"]), _normalize_topic_key(q2["quote"]))
            if sim < 0.5:
                sigs.append({"id": "wording_changed", "label": "Agenda wording changed", "tone": "warn"})

    if adjacent_addresses:
        sigs.append({"id": "adjacent", "label": "Adjacent parcels", "tone": "warn"})
        hypothesis = ("Neighboring addresses handled together can mean one owner assembling lots. "
                      "Check ownership before treating this as fact.")
    elif kind == "easement" or any(_FDOT_RE.search(a.get("summary", "")) for a in apps):
        sigs.append({"id": "public_easement", "label": "Easements or state-road agreement", "tone": "info"})
        hypothesis = ("Easements and maintenance agreements are often conditions of a site plan "
                      "approval. Look for the project behind them.")
    return sigs, hypothesis


def _build_hook(apps, sigs, adjacent_addresses):
    latest = apps[-1]
    parts = [latest["summary"].rstrip(".") + "."]
    n = len(apps)
    if n >= 2:
        parts.append(f"On agendas {n} times since {_fmt_date(apps[0]['date'])}.")
    ids = {s["id"] for s in sigs}
    if "amount_changed" in ids:
        amts = [a["amount"] for a in apps if a.get("amount")]
        parts.append(f"The amount moved from {_fmt_money(amts[0])} to {_fmt_money(amts[-1])}.")
    if "wording_changed" in ids:
        parts.append(f"The agenda wording differs between {_fmt_date(apps[0]['date'])} and "
                     f"{_fmt_date(apps[-1]['date'])}. Compare the two entries.")
    if "tabled" in ids:
        parts.append("It was tabled or continued at least once.")
    if adjacent_addresses:
        parts.append("Handled together with neighboring addresses: " + ", ".join(adjacent_addresses) + ".")
    return " ".join(parts)


def _questions(kind, sigs, apps):
    qs = []
    ids = {s["id"] for s in sigs}
    if "wording_changed" in ids:
        qs.append(f"What changed between the {_fmt_date(apps[0]['date'])} and "
                  f"{_fmt_date(apps[-1]['date'])} versions of this item?")
    if "tabled" in ids:
        qs.append("Why was the item tabled or continued?")
    if "amount_changed" in ids:
        qs.append("Why did the dollar amount change, and who pays?")
    if "adjacent" in ids:
        qs.append("Do the neighboring parcels share an owner?")
    for q in QUESTIONS_BY_KIND.get(kind, QUESTIONS_BY_KIND["other"]):
        if len(qs) >= 4:
            break
        qs.append(q)
    return qs[:4]


def _score(kind, apps, sigs, today_str):
    s = KIND_WEIGHT.get(kind, 4)
    s += min(10 * (len(apps) - 1), 30)
    ids = {x["id"] for x in sigs}
    if "upcoming" in ids:
        s += 15
    amounts = [a["amount"] for a in apps if a.get("amount")]
    if amounts:
        s += 20 if max(amounts) >= 1_000_000 else (10 if max(amounts) >= 100_000 else 0)
    if "amount_changed" in ids or "wording_changed" in ids:
        s += 12
    if "tabled" in ids:
        s += 10
    if "adjacent" in ids:
        s += 8
    if "consent" in ids:
        s += 10
    return min(s, 100)


def _primary_kind(apps):
    for a in reversed(apps):
        if a["kind"] != "other":
            return a["kind"]
    return "other"


def _timeline(apps):
    return [{
        "date": a["date"], "body": a["body"], "action": a.get("action", ""),
        "summary": a["summary"], "link": a.get("link"),
        "addresses": a.get("_addresses", []),
    } for a in apps]


def _next_body_meeting(muni_short, body, upcoming_events, today_str):
    bk = _body_key(body)
    best = None
    for ev in upcoming_events:
        if ev.get("muni_short") != muni_short or (ev.get("date") or "") < today_str:
            continue
        if _body_key(ev.get("title")) != bk:
            continue
        if best is None or ev["date"] < best["date"]:
            best = ev
    if best:
        return {"date": best["date"], "title": best.get("title"), "link": best.get("link")}
    return None


def _evidence_for(apps):
    for a in reversed(apps):
        ev = a.get("evidence") or {}
        if ev.get("quote"):
            return {"quote": ev["quote"], "page": ev.get("page"), "verified": ev.get("verified", False),
                    "date": a["date"], "link": a.get("link")}
    return None


def _include(kind, apps, sigs):
    ids = {s["id"] for s in sigs}
    if "upcoming" in ids or len(apps) >= 2 or kind in STRONG_KINDS:
        return True
    if ids & {"amount", "amount_changed", "consent", "adjacent", "public_easement"}:
        return True
    return False


def build_address_leads(items, upcoming_events, today_str):
    groups = defaultdict(list)
    for it in items:
        k = it.get("key")
        if not k:
            continue
        if it["kind"] == "contract_vendor" and not it.get("addr_norm"):
            continue  # vendor-only items feed the entity leads
        groups[k].append(it)

    merged = _merge_adjacent(groups)
    leads = []
    for primary, info in merged.items():
        apps = _dedupe_appearances(info["items"])
        if not apps:
            continue
        kind = _primary_kind(apps)
        # Display addresses in the form the document used.
        shown = []
        for a in info["addresses"]:
            raw = next((it["address"] for it in info["items"] if it.get("addr_norm") == a and it.get("address")), a)
            shown.append(raw)
        sigs, hyp = _signals_for_appearances(apps, kind, today_str, shown if info["adjacent"] else [])
        if not _include(kind, apps, sigs):
            continue
        latest = apps[-1]
        addr_display = (latest.get("_addresses") or [None])[0] or next(
            (it["address"] for it in reversed(info["items"]) if it.get("address")), "") or ""
        title = latest["headline"]
        if info["adjacent"]:
            addr_display = " and ".join(shown)
            title = f"Neighboring addresses handled together: {addr_display}"
        entities = []
        seen_e = set()
        for a in apps:
            for e in a["entities"]:
                if e["name"] not in seen_e:
                    seen_e.add(e["name"])
                    entities.append(e)
        amounts = [a["amount"] for a in apps if a.get("amount")]
        upcoming_dates = [a["date"] for a in apps if a["date"] >= today_str]
        leads.append({
            "key": primary,
            "type": "address",
            "title": title,
            "address": addr_display or latest.get("case_no", ""),
            "munis": [latest["muni_short"]],
            "muni_names": latest["muni_full"],
            "body": latest["body"],
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, "Property item"),
            "group": KIND_GROUP.get(kind, "development"),
            "signals": sigs,
            "hypothesis": bool(hyp),
            "hypothesis_text": hyp,
            "hook": _build_hook(apps, sigs, shown if info["adjacent"] else []),
            "evidence": _evidence_for(apps),
            "timeline": _timeline(apps),
            "appearance_count": len(apps),
            "first_date": apps[0]["date"],
            "latest_date": latest["date"],
            "next_date": min(upcoming_dates) if upcoming_dates else None,
            "next_body_meeting": _next_body_meeting(latest["muni_short"], latest["body"],
                                                    upcoming_events, today_str),
            "entities": entities,
            "amount": max(amounts) if amounts else None,
            "questions": _questions(kind, sigs, apps),
            "score": _score(kind, apps, sigs, today_str),
            "web": {"checked": False, "mentions": []},
        })
    return leads


def _entity_item_relevant(item, ent):
    if item["kind"] in STRONG_KINDS or item["kind"] not in ("contract_vendor", "other"):
        return True
    if ent["role"] in ("developer", "applicant", "owner", "architect", "engineer", "attorney"):
        return True
    return bool(_ENTITY_RE_KEYWORDS.search(f"{item.get('headline', '')} {item.get('summary', '')}"))


def build_entity_leads(items, upcoming_events, today_str):
    """A private party (developer, applicant, engineer, vendor...) appearing
    in two or more municipalities."""
    by_entity = defaultdict(lambda: {"names": defaultdict(int), "items": [], "roles": set()})
    for it in items:
        for e in it["entities"]:
            if not _entity_item_relevant(it, e):
                continue
            norm = normalize_entity(e["name"])
            if len(norm) < 4:
                continue
            rec = by_entity[norm]
            rec["names"][e["name"]] += 1
            rec["items"].append(it)
            rec["roles"].add(e["role"])

    leads = []
    for norm, rec in by_entity.items():
        munis = sorted({it["muni_short"] for it in rec["items"]})
        if len(munis) < 2:
            continue
        apps = _dedupe_appearances(rec["items"])
        name = max(rec["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        latest = apps[-1]
        muni_names = sorted({it["muni_full"] for it in rec["items"]})
        amounts = [a["amount"] for a in apps if a.get("amount")]
        upcoming_dates = [a["date"] for a in apps if a["date"] >= today_str]
        role = sorted(rec["roles"])[0]
        sigs = [
            {"id": "cross_town", "label": f"{len(munis)} municipalities", "tone": "warn"},
            {"id": "kind", "label": role.capitalize(), "tone": "info"},
        ]
        if upcoming_dates:
            sigs.append({"id": "upcoming", "label": f"On {_fmt_date(min(upcoming_dates))} agenda", "tone": "success"})
        if amounts and max(amounts) >= 100_000:
            sigs.append({"id": "amount", "label": _fmt_money(max(amounts)), "tone": "info"})
        score = min(40 + 10 * (len(munis) - 2) + 8 * min(len(apps), 4)
                    + (15 if upcoming_dates else 0) + (10 if amounts and max(amounts) >= 100_000 else 0), 100)
        hook_bits = [f"{_fmt_date(a['date'])} ({a['muni_short']}): {a['summary'].rstrip('.')}."
                     for a in apps[-3:]]
        leads.append({
            "key": f"ENTITY|{norm}",
            "type": "entity",
            "title": f"{name} appears on agendas in {len(munis)} municipalities",
            "address": name,
            "munis": munis,
            "muni_names": " and ".join(muni_names),
            "body": latest["body"],
            "kind": "contract_vendor",
            "kind_label": role.capitalize(),
            "group": "entity",
            "signals": sigs,
            "hypothesis": False,
            "hypothesis_text": None,
            "hook": " ".join(hook_bits),
            "evidence": _evidence_for(apps),
            "timeline": [{**t, "muni": a["muni_short"]} for t, a in zip(_timeline(apps), apps)],
            "appearance_count": len(apps),
            "first_date": apps[0]["date"],
            "latest_date": latest["date"],
            "next_date": min(upcoming_dates) if upcoming_dates else None,
            "next_body_meeting": None,
            "entities": [{"name": name, "role": role}],
            "amount": max(amounts) if amounts else None,
            "questions": ENTITY_QUESTIONS[:],
            "score": score,
            "web": {"checked": False, "mentions": []},
        })
    return leads


def build_leads(items, upcoming_events, today_str):
    items = assign_item_keys(items)
    leads = build_address_leads(items, upcoming_events, today_str) + \
        build_entity_leads(items, upcoming_events, today_str)
    leads.sort(key=lambda l: (l["next_date"] is None, l["next_date"] or "", -l["score"], l["latest_date"]))
    return leads


# --- STEP 5: OPTIONAL WEB CHECK (Serper) ------------------------------------
# Previously every call came back HTTP 400 and the body was never logged, so
# the cause was invisible. Now the response body is logged, and the first
# failure of a kind that will repeat (400/401/403, which includes an account
# with no credits) stops further calls for the run instead of failing 27
# times. Only the top leads are checked.

_serper_unavailable = False


def _lead_search_query(lead):
    # For address leads this is the street address, for entity leads the name.
    return f"{lead['address']} {lead['muni_names'].split(' and ')[0]} Florida"[:150]


def _web_mention_matches(lead, source):
    text = f"{source.get('title', '')} {source.get('snippet', '')}".lower()
    if lead["type"] == "entity":
        want = _normalize_topic_key(normalize_entity(lead["address"]))
        return _similarity(want, _normalize_topic_key(text)) >= 0.3
    canon = _canon(lead["address"])
    return bool(canon) and f" {canon} " in f" {_canon(text)} "


def web_cross_reference(lead, num_results=5):
    global _serper_unavailable
    if not SERPER_API_KEY or _serper_unavailable:
        return {"checked": False, "mentions": []}
    query = _lead_search_query(lead)
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=15,
        )
        if resp.status_code != 200:
            body = (resp.text or "")[:300].replace("\n", " ")
            print(f"[Insights Search] Serper returned HTTP {resp.status_code} for query '{query}': {body}")
            if resp.status_code in (400, 401, 403, 429):
                print("[Insights Search] This status will repeat for every query (bad key, no credits, "
                      "or rate limit), so web checks are skipped for the rest of this run.")
                _serper_unavailable = True
            return {"checked": True, "mentions": [], "error": f"HTTP {resp.status_code}"}
        organic = resp.json().get("organic", [])[:num_results]
        mentions = [{"title": r.get("title"), "link": r.get("link")} for r in organic
                    if _web_mention_matches(lead, r)]
        return {"checked": True, "mentions": mentions[:3]}
    except Exception as e:
        print(f"[Insights Search] Serper request failed for query '{query}': {e}")
        return {"checked": True, "mentions": [], "error": str(e)}


# --- STEP 6: LEAD STATE ACROSS RUNS -----------------------------------------

def load_previous_leads(path=LEADS_OUTPUT_PATH):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def apply_first_seen(leads, previous, today_str):
    """first_seen is the date a lead first appeared in leads.json; is_new is
    true for leads that first appeared in this run (never on the very first
    run, when everything would otherwise be 'new')."""
    prev_map = {}
    if previous:
        prev_map = {l["key"]: l.get("first_seen") for l in previous.get("leads", [])}
    for l in leads:
        fs = prev_map.get(l["key"])
        l["first_seen"] = fs or today_str
        l["is_new"] = bool(previous) and not fs


# --- ORCHESTRATION -----------------------------------------------------------

def generate_leads_output():
    print("Starting Leads Engine...")
    print(f"[Insights] LLM key present: {bool(GEMINI_API_KEY)} (model={GEMINI_MODEL}) | "
          f"Web check key present: {bool(SERPER_API_KEY)}")
    today = _today_et()
    today_str = today.isoformat()

    historical = gather_historical_events()
    upcoming = load_upcoming_events(today_str)
    print(f"[Insights] {len(historical)} historical and {len(upcoming)} upcoming meeting(s) to consider.")

    cache = load_items_cache()
    valid = {_cache_key(ev) for ev in historical + upcoming}
    cache["events"] = {k: v for k, v in cache["events"].items() if k in valid}
    cache["nodoc"] = {k: v for k, v in cache["nodoc"].items() if k in valid}

    coverage = defaultdict(lambda: {"meetings": 0, "with_document": 0, "with_items": 0,
                                    "items": 0, "pending": 0})
    all_items = []
    counts = defaultdict(int)
    llm_since_save = 0

    todo = [(ev, False) for ev in historical] + [(ev, True) for ev in upcoming]
    for event, is_upcoming in todo:
        items, status, had_doc = analyze_event(event, cache, today_str, is_upcoming)
        counts[status] += 1
        c = coverage[event.get("muni_short") or "?"]
        c["meetings"] += 1
        c["with_document"] += 1 if had_doc else 0
        c["pending"] += 1 if status in ("pending", "fetch_failed") else 0
        if items:
            c["with_items"] += 1
            c["items"] += len(items)
        all_items.extend(items)
        if status == "llm":
            llm_since_save += 1
            if llm_since_save >= CACHE_SAVE_EVERY:
                save_items_cache(cache)
                llm_since_save = 0
        time.sleep(0.05)

    save_items_cache(cache)
    print(f"[Insights] Meetings: {counts['cache']} from cache, {counts['llm']} newly analyzed, "
          f"{counts['nodoc']} with no readable document, {counts['pending']} pending "
          f"(LLM unavailable or failed), {counts['fetch_failed']} fetch failure(s) (both retried next run).")
    for muni, c in sorted(coverage.items()):
        print(f"[Insights Coverage] {muni}: {c['meetings']} meeting(s), {c['with_document']} with "
              f"readable document, {c['with_items']} with items ({c['items']} item(s)), "
              f"{c['pending']} pending")

    leads = build_leads(all_items, upcoming, today_str)
    print(f"[Insights] Built {len(leads)} lead(s) from {len(all_items)} verified item(s).")

    if SERPER_API_KEY:
        for lead in sorted(leads, key=lambda l: -l["score"])[:WEB_CHECK_TOP_N]:
            lead["web"] = web_cross_reference(lead)
            time.sleep(0.2)

    leads = sorted(leads, key=lambda l: (l["next_date"] is None, l["next_date"] or "", -l["score"]))[:MAX_LEADS]
    apply_first_seen(leads, load_previous_leads(), today_str)

    output = {
        "schema": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today_str,
        "lookback_months": HISTORICAL_MONTHS_BACK,
        "meetings_scanned": len(historical) + len(upcoming),
        "meetings_pending_analysis": counts["pending"] + counts["fetch_failed"],
        "llm_available": bool(GEMINI_API_KEY) and not _gemini_unavailable,
        "coverage_by_municipality": dict(sorted(coverage.items())),
        "leads": leads,
    }
    with open(LEADS_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"[Insights] Wrote {len(leads)} lead(s) to {LEADS_OUTPUT_PATH}.")
    print("Leads Engine complete.")


# Kept so anything still calling the old entry point keeps working.
generate_insights_output = generate_leads_output


if __name__ == "__main__":
    generate_leads_output()
