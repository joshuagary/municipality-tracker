"""Local smoke test - not part of the shipped app. Exercises the leads engine
(insights_engine.py) against hand-built fixtures with every network call
mocked, since the development sandbox has no outbound access.

Fixtures reuse real agenda wording from the project's own insights.json
(Delray 755 SE 7th Ave, Juno Beach 401/403 Olympus Dr, Delray 802 SE 5th Ave,
Chen Moore & Associates in Jupiter Inlet Colony and North Palm Beach).

Run: python3 _smoke_test_insights.py
"""
import os
import io
import re
import json
import tempfile
import contextlib

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(tempfile.mkdtemp())

import sys
sys.path.insert(0, REPO)
import insights_engine as ie

TODAY = "2026-11-21"
passed = 0


def ok(name):
    global passed
    passed += 1
    print(f"Test passed: {name}")


# --- 1. Normalizers ---------------------------------------------------------
assert ie.normalize_address("755 SE 7th Avenue") == "755 se 7th ave"
assert ie.normalize_address("755 Southeast 7th Ave., Delray Beach, FL 33483") == "755 se 7th ave"
assert ie.normalize_address("401 Olympus Drive, Unit 4") == "401 olympus dr"
assert ie.normalize_address("Olympus Drive") is None
assert ie.normalize_address("") is None
assert ie.normalize_case("PZ-000669-2026") == "PZ0006692026"
assert ie.normalize_case("7") is None
assert ie.normalize_entity("Chen Moore & Associates, Inc.") == ie.normalize_entity("Chen Moore and Associates")
assert ie.is_government_entity("Town of Jupiter Inlet Colony")
assert ie.is_government_entity("Palm Beach County Commission on Ethics")
assert ie.is_government_entity("Florida Department of Transportation")
assert ie.is_government_entity("Village Square Partners LLC") is False
assert ie.is_government_entity("Chen Moore & Associates, Inc.") is False
ok("address, case, entity normalization and government-entity filter")

# --- 2. JSON parsing, including the truncated-response failure mode --------
arr, salv = ie._parse_json_array('[{"a": 1}, {"a": 2}]')
assert arr == [{"a": 1}, {"a": 2}] and not salv
arr, salv = ie._parse_json_array('```json\n[{"a": 1}]\n```')
assert arr == [{"a": 1}] and not salv
arr, salv = ie._parse_json_array('[{"a": 1}, {"a": 2}, {"a": "cut off mid-str')
assert arr == [{"a": 1}, {"a": 2}] and salv
arr, salv = ie._parse_json_array('{"items": [{"a": 1}]}')
assert arr == [{"a": 1}]
try:
    ie._parse_json_array("sorry, no json here")
    raise AssertionError("expected ValueError")
except ValueError:
    pass
assert ie._parse_json_array("[]") == ([], False)
ok("JSON parsing incl. truncated-output salvage")

# --- 3. Verification against the document ----------------------------------
DOC = (
    "\n[[PAGE 1]]\nBOARD OF ADJUSTMENT AGENDA\n"
    "\n[[PAGE 2]]\n6.B. PZ-000669-2026: Consideration of a variance request to reduce the minimum "
    "required off-street parking for a detached single-family dwelling from two spaces to zero, at "
    "755 SE 7th Avenue. Applicant: Harbor View Holdings LLC. Estimated cost $1,250,000.\n"
)
event = {"muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "Board of Adjustment",
         "date": "2026-09-03", "link": "https://example.com/a.pdf"}
raw = [
    {"headline": "Owner asks to waive parking for garage conversion", "kind": "variance",
     "address": "755 Southeast 7th Ave", "case_no": "PZ-000669-2026",
     "entities": [{"name": "Harbor View Holdings, LLC", "role": "applicant"},
                  {"name": "Phantom Builders Inc", "role": "developer"},
                  {"name": "City of Delray Beach", "role": "owner"}],
     "amount": 1250000, "action": "public hearing", "consent": False,
     "summary": "Variance to cut required parking from two spaces to zero.",
     "evidence": "variance request to reduce the minimum required off-street parking for a detached single-family dwelling"},
    {"headline": "Invented item", "kind": "rezoning", "address": "999 Nowhere Blvd", "case_no": "ZZ-9999",
     "entities": [], "amount": 5000000, "summary": "Not in the document.",
     "evidence": "this sentence does not appear anywhere in the agenda text"},
    "not a dict",
]
items = ie.normalize_items(raw, event, DOC, TODAY)
assert len(items) == 2
a, b = items
assert a["address"] == "755 Southeast 7th Ave"            # kept: real, matched after canonicalization
assert a["case_no"] == "PZ-000669-2026"
assert [e["name"] for e in a["entities"]] == ["Harbor View Holdings, LLC"]  # fabricated + government dropped
assert a["amount"] == 1250000
assert a["evidence"]["verified"] is True and a["evidence"]["page"] == 2
assert b["address"] == "" and b["case_no"] == "" and b["amount"] is None   # hallucinations stripped
assert b["evidence"]["verified"] is False and b["evidence"]["page"] is None
ok("evidence, address, case number, entity and amount are verified against the document")

# --- 4. Keys never merge across municipalities -----------------------------
def mk(muni, muni_full, body, date, kind, headline, summary, address="", case_no="", entities=None,
       amount=None, action="", consent=False, quote="", verified=True, page=None):
    return {
        "headline": headline, "kind": kind, "address": address, "case_no": case_no,
        "entities": entities or [], "amount": amount, "action": action, "consent": consent,
        "summary": summary,
        "evidence": {"quote": quote or summary, "page": page, "verified": verified},
        "muni_short": muni, "muni_full": muni_full, "body": body, "date": date,
        "link": f"https://example.com/{muni}/{date}",
    }

juic_ord = mk("JUIC", "Town of Jupiter Inlet Colony", "Town Commission", "2026-09-14", "other",
              "Building code changes", "Ordinance amending Chapter 4 relating to buildings.",
              case_no="Ordinance 2026-03")
west_ord = mk("WESTLAKE", "City of Westlake", "City Council", "2026-06-02", "other",
              "Solid waste assessment changes", "Ordinance amending Chapter 18 Solid Waste.",
              case_no="Ordinance 2026-03")
keyed = ie.assign_item_keys([juic_ord, west_ord])
assert keyed[0]["key"] != keyed[1]["key"]
assert keyed[0]["key"].startswith("JUIC|") and keyed[1]["key"].startswith("WESTLAKE|")
leads = ie.build_leads([dict(juic_ord), dict(west_ord)], [], TODAY)
assert leads == [], "unrelated ordinances with the same number must not become a lead"
ok("same ordinance number in two towns is never merged (the old #1 false insight)")

# case number resolves to the address seen alongside it in the same town
x = mk("DELRAY", "City of Delray Beach", "Board of Adjustment", "2026-09-03", "variance", "h", "s",
       address="755 SE 7th Avenue", case_no="PZ-000669-2026")
y = mk("DELRAY", "City of Delray Beach", "City Commission", "2026-11-09", "variance", "h", "s",
       case_no="PZ-000669-2026")
k = ie.assign_item_keys([x, y])
assert k[0]["key"] == k[1]["key"] == "DELRAY|addr|755 se 7th ave"
ok("case number resolves to the address key within a town")

# --- 5. Lead building and signals ------------------------------------------
delray_sep = mk("DELRAY", "City of Delray Beach", "Board of Adjustment", "2026-09-03", "variance",
                "Owner asks to waive parking for garage conversion",
                "Variance request to reduce required parking from two spaces to zero after converting the attached garage.",
                address="755 SE 7th Avenue", case_no="PZ-000669-2026", action="public hearing",
                quote="reduce the minimum required off-street parking from two spaces to zero")
delray_nov = mk("DELRAY", "City of Delray Beach", "Board of Adjustment", "2026-11-05", "variance",
                "Parking variance request revised",
                "Revised variance request to allow one of the two required spaces to be located elsewhere on the lot.",
                address="755 SE 7th Avenue", case_no="PZ-000669-2026", action="public hearing",
                quote="allow one of the two required off-street parking spaces to be located within the setback")
olympus_401a = mk("JUNOBEACH", "Town of Juno Beach", "Planning and Zoning Board", "2026-08-03", "design_review",
                  "Appearance review for 401 Olympus Drive", "Appearance review for 401 Olympus Drive.",
                  address="401 Olympus Drive")
olympus_403a = mk("JUNOBEACH", "Town of Juno Beach", "Planning and Zoning Board", "2026-08-03", "design_review",
                  "Appearance review for 403 Olympus Drive", "Appearance review for 403 Olympus Drive.",
                  address="403 Olympus Drive")
olympus_401b = mk("JUNOBEACH", "Town of Juno Beach", "Planning and Zoning Board", "2026-09-21", "design_review",
                  "Appearance review for 401 Olympus Drive", "Board consideration of an appearance review for 401 Olympus Drive.",
                  address="401 Olympus Drive")
olympus_403b = mk("JUNOBEACH", "Town of Juno Beach", "Planning and Zoning Board", "2026-09-21", "design_review",
                  "Appearance review for 403 Olympus Drive", "Board consideration of an appearance review for 403 Olympus Drive.",
                  address="403 Olympus Drive")
lone_review = mk("JUIC", "Town of Jupiter Inlet Colony", "Town Commission", "2026-10-15", "design_review",
                 "Impact review for new 4,098 sq ft home", "Impact Review for a proposed two-story residence.",
                 address="234 Shelter Lane")
easement_oct = mk("DELRAY", "City of Delray Beach", "City Commission", "2026-10-06", "easement",
                  "City accepts pedestrian easement", "Acceptance of an easement agreement for a pedestrian clear zone at 802 SE 5th Avenue.",
                  address="802 SE 5th Avenue")
easement_nov = mk("DELRAY", "City of Delray Beach", "City Commission", "2026-11-02", "easement",
                  "City accepts landscape and FDOT agreements", "Acceptance of a landscape maintenance agreement and an FDOT landscape maintenance agreement for part of 802 SE 5th Avenue.",
                  address="802 SE 5th Avenue")
future = mk("BOCA", "City of Boca Raton", "City Council", "2026-12-08", "rezoning",
            "Rezoning of waterfront parcel to allow 120 units", "First reading of an ordinance rezoning 1200 N Federal Hwy.",
            address="1200 N Federal Highway", action="first reading", amount=45_000_000)
cm_juic = mk("JUIC", "Town of Jupiter Inlet Colony", "Town Commission", "2026-10-19", "contract_vendor",
             "Town hires engineering firm", "Agreement with Chen Moore and Associates for general engineering services.",
             entities=[{"name": "Chen Moore and Associates", "role": "engineer"}])
cm_npb = mk("NPB", "Village of North Palm Beach", "Village Council", "2026-10-22", "contract_vendor",
            "Village raises engineering firm hourly rates", "Sixth Amendment to the professional services agreement with Chen Moore & Associates, Inc. to modify the hourly rate schedule.",
            entities=[{"name": "Chen Moore & Associates, Inc.", "role": "engineer"}])

all_items = [delray_sep, delray_nov, olympus_401a, olympus_403a, olympus_401b, olympus_403b, lone_review,
             easement_oct, easement_nov, future, cm_juic, cm_npb, juic_ord, west_ord]
upcoming_events = [
    {"muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "Board of Adjustment",
     "date": "2026-12-03", "link": "https://example.com/boa-dec"},
    {"muni_short": "BOCA", "muni_full": "City of Boca Raton", "title": "City Council", "date": "2026-12-08",
     "link": "https://example.com/boca-dec"},
]
leads = ie.build_leads([dict(i) for i in all_items], upcoming_events, TODAY)
by_key = {l["key"]: l for l in leads}
sig_ids = lambda l: {s["id"] for s in l["signals"]}

# Delray parking variance: repeat + wording changed, next body meeting surfaced
dv = by_key["DELRAY|addr|755 se 7th ave"]
assert {"repeat", "wording_changed"} <= sig_ids(dv), sig_ids(dv)
assert dv["appearance_count"] == 2 and dv["first_date"] == "2026-09-03" and dv["latest_date"] == "2026-11-05"
assert dv["next_body_meeting"]["date"] == "2026-12-03"
assert dv["evidence"]["quote"].startswith("allow one of the two")
assert any("What changed between" in q for q in dv["questions"])
assert dv["next_date"] is None
ok("repeat review + changed agenda wording + next body meeting")

# Adjacent parcels merge into one lead keyed by the lowest address, flagged as hypothesis
adj = by_key["JUNOBEACH|addr|401 olympus dr"]
assert "adjacent" in sig_ids(adj) and adj["hypothesis"] is True
assert adj["address"] == "401 Olympus Drive and 403 Olympus Drive"
assert adj["appearance_count"] == 2
assert "JUNOBEACH|addr|403 olympus dr" not in by_key
ok("adjacent parcels merged into one hypothesis lead")

# Easement / FDOT hypothesis
ea = by_key["DELRAY|addr|802 se 5th ave"]
assert "public_easement" in sig_ids(ea) and ea["hypothesis"] is True
ok("easement/FDOT hypothesis")

# Upcoming rezoning floats to the top with a countdown date and big-amount signal
assert leads[0]["key"] == "BOCA|addr|1200 n federal hwy"
assert leads[0]["next_date"] == "2026-12-08" and {"upcoming", "amount"} <= sig_ids(leads[0])
ok("upcoming rezoning sorts first with upcoming and amount signals")

# A lone, weak, past design review is not a lead
assert "JUIC|addr|234 shelter ln" not in by_key
ok("single weak past item is not a lead")

# Cross-town entity lead; no government entities; both munis present
ent = by_key["ENTITY|chen moore and associates"]
assert set(ent["munis"]) == {"JUIC", "NPB"} and ent["type"] == "entity" and ent["group"] == "entity"
assert "cross_town" in sig_ids(ent)
ok("cross-town entity lead (Chen Moore)")

# Nothing from the ordinance false-match survived
assert not any(l["key"].endswith("2026-03") or "ORDINANCE" in l["key"] for l in leads)
ok("no ordinance-number leads")

# JSON-serializable and no private fields leak
json.dumps(leads)
for l in leads:
    for t in l["timeline"]:
        assert set(t) >= {"date", "body", "summary", "link"}
ok("leads serialize cleanly")

# --- 6. analyze_event: caching, failure handling, nodoc, upcoming rehash ---
ie.GEMINI_API_KEY = "test-key"
ie._gemini_checked_available = False
ie._gemini_unavailable = False
calls = {"llm": 0, "fetch": 0}
state = {"text": DOC, "response": raw[:1], "fail": False}


def fake_fetch(ev, timeout=25):
    calls["fetch"] += 1
    return state["text"]


def fake_gemini(prompt):
    calls["llm"] += 1
    if state["fail"]:
        return None, False, RuntimeError("boom")
    return state["response"], False, None


ie.fetch_document_text = fake_fetch
ie._call_gemini_once = fake_gemini
cache = {"schema": ie.CACHE_SCHEMA, "events": {}, "nodoc": {}}

its, status, had = ie.analyze_event(event, cache, TODAY, False)
assert status == "llm" and had and len(its) == 1 and calls["llm"] == 1
its, status, had = ie.analyze_event(event, cache, TODAY, False)
assert status == "cache" and calls["llm"] == 1 and calls["fetch"] == 1   # past meeting: no refetch, no LLM
ok("past meeting analyzed once, then served from cache")

state["fail"] = True
ev2 = dict(event, date="2026-09-17")
its, status, had = ie.analyze_event(ev2, cache, TODAY, False)
assert status == "pending" and its == [] and ie._cache_key(ev2) not in cache["events"]
ok("LLM failure is not cached (retried next run)")

state["text"] = None
ev3 = dict(event, date="2026-09-18")
its, status, had = ie.analyze_event(ev3, cache, TODAY, False)
assert status == "nodoc" and not had and ie._cache_key(ev3) in cache["nodoc"]
fetches = calls["fetch"]
its, status, had = ie.analyze_event(ev3, cache, TODAY, False)
assert status == "nodoc" and calls["fetch"] == fetches      # negative cache: no refetch within 7 days
later = "2026-11-29"
its, status, had = ie.analyze_event(ev3, cache, later, False)
assert calls["fetch"] == fetches + 1                         # retried after NODOC_RETRY_DAYS
ok("no-document meetings are negatively cached and retried weekly")

state.update(text=DOC, fail=False)
up = dict(event, date="2026-12-03")
before = calls["llm"]
ie.analyze_event(up, cache, TODAY, True)
assert calls["llm"] == before + 1
ie.analyze_event(up, cache, TODAY, True)
assert calls["llm"] == before + 1                            # same document hash: no new LLM call
state["text"] = DOC + "\n7.A. Supplemental item added 11/30."
ie.analyze_event(up, cache, TODAY, True)
assert calls["llm"] == before + 2                            # amended agenda: re-analyzed
ok("upcoming agenda re-analyzed only when its text changes")

# quota exhaustion trips the breaker and stops further calls
ie._gemini_unavailable = False
calls["llm"] = 0


def quota_gemini(prompt):
    calls["llm"] += 1
    class R:  # noqa
        status_code = 429
    err = ie.requests.HTTPError("HTTP 429: RESOURCE_EXHAUSTED", response=R())
    return None, False, err


ie._call_gemini_once = quota_gemini
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r1 = ie._llm_items(event, DOC)
    r2 = ie._llm_items(event, DOC)
assert r1 is None and r2 is None and calls["llm"] == 1 and ie._gemini_unavailable
ie._gemini_unavailable = False
ok("quota exhaustion stops further LLM calls for the run")

# --- 7. Serper: body is logged and the first repeating failure stops calls --
ie.SERPER_API_KEY = "serper-test"
ie._serper_unavailable = False
posts = {"n": 0}


class FakeResp:
    def __init__(self, code, body, payload=None):
        self.status_code, self.text, self._p = code, body, payload

    def json(self):
        return self._p


def serper_400(*a, **k):
    posts["n"] += 1
    return FakeResp(400, '{"message":"Not enough credits","statusCode":400}')


orig_post = ie.requests.post
ie.requests.post = serper_400
lead_a = {"type": "address", "address": "755 SE 7th Avenue", "muni_names": "City of Delray Beach"}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    w1 = ie.web_cross_reference(lead_a)
    w2 = ie.web_cross_reference(lead_a)
log = buf.getvalue()
assert "Not enough credits" in log, log
assert posts["n"] == 1 and w1["error"] == "HTTP 400" and w2["checked"] is False
ok("Serper 400 body is logged and the run stops calling after the first failure")

ie._serper_unavailable = False


def serper_ok(*a, **k):
    return FakeResp(200, "", {"organic": [
        {"title": "Delray variance at 755 SE 7th Avenue", "link": "https://news.example/a", "snippet": ""},
        {"title": "Unrelated parking story", "link": "https://news.example/b", "snippet": "no address"},
    ]})


ie.requests.post = serper_ok
w = ie.web_cross_reference(lead_a)
assert [m["link"] for m in w["mentions"]] == ["https://news.example/a"]
ie.requests.post = orig_post
ok("web mentions only count when they contain the address")

# --- 8. Following agenda links from a calendar page ------------------------
CAL_HTML = """
<html><body>
<a href="/AgendaCenter">Agenda Center</a>
<a href="/AgendaCenter/ViewFile/Agenda/_11092026-73">Agenda</a>
<a href="/DocumentCenter/View/1234/Packet.pdf">Agenda Packet</a>
<a href="/AgendaCenter/ViewFile/Minutes/_10122026-70">Minutes</a>
<a href="mailto:clerk@example.com">Email agenda questions</a>
<a href="/Calendar.aspx">Back to calendar</a>
<a href="http://www.myboca.us/129/Agendas-Minutes">Agendas &amp; Minutes</a>
<a href="/129/Agendas-Minutes">Agendas and Minutes</a>
</body></html>
"""
links = ie._find_agenda_links(CAL_HTML, "https://www.myboca.us/Calendar.aspx?EID=1&month=11")
assert links[0] == "https://www.myboca.us/AgendaCenter/ViewFile/Agenda/_11092026-73", links
assert all("Minutes" not in l and "mailto" not in l for l in links)
assert "https://www.myboca.us/AgendaCenter" not in links
assert not any("Agendas-Minutes" in l for l in links), links   # site-wide nav landing page (first real run's bug)
assert any("Packet.pdf" in l for l in links) and links.index(next(l for l in links if "ViewFile" in l)) == 0 or len(links) == 1
ok("agenda link finder picks the agenda document, skips minutes, mailto, the center root and nav landing pages")


class FakeHttp:
    def __init__(self, status, text="", content=b"", headers=None):
        self.status_code, self.text, self.content, self.headers = status, text, content, headers or {}


real_fetch = ie._fetch
real_pdf = ie._extract_pdf_text
pages_served = {
    "https://www.myboca.us/Calendar.aspx?EID=1&month=11": FakeHttp(200, CAL_HTML, headers={"Content-Type": "text/html"}),
    "https://www.myboca.us/AgendaCenter/ViewFile/Agenda/_11092026-73": FakeHttp(
        200, content=b"%PDF-1.4 fake", headers={"Content-Type": "application/pdf"}),
    "https://www.myboca.us/DocumentCenter/View/1234/Packet.pdf": FakeHttp(
        200, content=b"%PDF-1.4 fake", headers={"Content-Type": "application/pdf"}),
}
ie._fetch = lambda url, timeout: pages_served.get(url)
ie._extract_pdf_text = lambda b: "\n[[PAGE 1]]\nRezoning of 1200 N Federal Highway first reading."
ev_cal = {"muni_short": "BOCA", "muni_full": "City of Boca Raton", "title": "City Council",
          "date": "2026-12-08", "link": "https://www.myboca.us/Calendar.aspx?EID=1&month=11"}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    txt = real_fetch_doc = None
    # restore the real fetch_document_text for this test (it was replaced in section 6)
    import importlib
    importlib.reload(ie)
    ie._fetch = lambda url, timeout: pages_served.get(url)
    ie._extract_pdf_text = lambda b: "\n[[PAGE 1]]\nRezoning of 1200 N Federal Highway first reading."
    txt = ie.fetch_document_text(ev_cal)
assert txt and "Rezoning of 1200 N Federal Highway" in txt
assert "followed agenda link" in buf.getvalue()
# a calendar page with no agenda link yields no document (and no junk text)
pages_served["https://www.myboca.us/Calendar.aspx?EID=2"] = FakeHttp(
    200, "<html><body>Nav Home Contact About</body></html>", headers={"Content-Type": "text/html"})
assert ie.fetch_document_text(dict(ev_cal, link="https://www.myboca.us/Calendar.aspx?EID=2")) is None
ok("calendar page: follows the agenda link; no link means no document")

# --- 9. PDF text carries page markers (only if a PDF writer is available) --
importlib.reload(ie)  # drop the stubbed _fetch/_extract_pdf_text from section 8
try:
    from reportlab.pdfgen import canvas  # type: ignore
    buf_pdf = io.BytesIO()
    c = canvas.Canvas(buf_pdf)
    c.drawString(72, 720, "Page one text about 755 SE 7th Avenue")
    c.showPage()
    c.drawString(72, 720, "Page two text")
    c.save()
    t = ie._extract_pdf_text(buf_pdf.getvalue())
    assert "[[PAGE 1]]" in t and "[[PAGE 2]]" in t
    ok("PDF extraction emits page markers")
except ImportError:
    print("Skipped: PDF page-marker test (reportlab not installed here)")

# --- 10. End to end through generate_leads_output ---------------------------
importlib.reload(ie)
ie.GEMINI_API_KEY = "test-key"
ie.SERPER_API_KEY = None
ie._gemini_checked_available = False
ie._gemini_unavailable = False

with open("data.json", "w") as f:
    json.dump([
        {"id": "x", "muni_short": "BOCA", "muni_full": "City of Boca Raton", "title": "City Council",
         "date": "2026-12-08", "time": "6:00 PM", "link": "https://example.com/boca-dec", "summary": ""},
        {"id": "y", "muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "Board of Adjustment",
         "date": "2026-12-03", "time": "6:00 PM", "link": "https://example.com/boa-dec", "summary": ""},
    ], f)

hist_events = [
    {"muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "Board of Adjustment",
     "date": "2026-09-03", "link": "https://example.com/d1"},
    {"muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "Board of Adjustment",
     "date": "2026-11-05", "link": "https://example.com/d2"},
    {"muni_short": "WPB", "muni_full": "City of West Palm Beach", "title": "City Commission Meeting",
     "date": "2026-10-01", "link": "https://example.com/wpb-series"},
]
ie.gather_historical_events = lambda months_back=6: [dict(e) for e in hist_events]
docs = {
    "https://example.com/d1": "\n[[PAGE 1]]\nVariance request to reduce required off-street parking from two spaces to zero at 755 SE 7th Avenue. PZ-000669-2026.",
    "https://example.com/d2": "\n[[PAGE 3]]\nVariance request to allow one of the two required off-street parking spaces within the setback at 755 SE 7th Avenue. PZ-000669-2026.",
    "https://example.com/boa-dec": "\n[[PAGE 1]]\nVariance request to allow one of the two required off-street parking spaces within the setback at 755 SE 7th Avenue. PZ-000669-2026.",
    "https://example.com/boca-dec": "\n[[PAGE 5]]\nFirst reading of an ordinance rezoning 1200 N Federal Highway to allow 120 units, valued at $45,000,000.",
}
ie.fetch_document_text = lambda ev, timeout=25: docs.get(ev.get("link"))


def e2e_gemini(prompt):
    if "755 SE 7th Avenue" in prompt:
        quote = ("variance request to reduce required off-street parking from two spaces to zero"
                 if "from two spaces to zero" in prompt else
                 "variance request to allow one of the two required off-street parking spaces within the setback")
        return [{"headline": "Parking variance request at 755 SE 7th Avenue", "kind": "variance",
                 "address": "755 SE 7th Avenue", "case_no": "PZ-000669-2026", "entities": [], "amount": None,
                 "action": "public hearing", "consent": False,
                 "summary": "Variance to the off-street parking requirement.", "evidence": quote}], False, None
    return [{"headline": "Rezoning to allow 120 units on Federal Highway", "kind": "rezoning",
             "address": "1200 N Federal Highway", "case_no": "", "entities": [], "amount": 45000000,
             "action": "first reading", "consent": False,
             "summary": "First reading of an ordinance rezoning the parcel.",
             "evidence": "First reading of an ordinance rezoning 1200 N Federal Highway to allow 120 units"}], False, None


ie._call_gemini_once = e2e_gemini
ie.GEMINI_MIN_SECONDS_BETWEEN_CALLS = 0
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ie.generate_leads_output()
out = json.load(open("leads.json"))
assert out["schema"] == 2 and out["llm_available"] is True
assert out["meetings_scanned"] == 5 and out["meetings_pending_analysis"] == 0
cov = out["coverage_by_municipality"]
assert cov["WPB"] == {"meetings": 1, "with_document": 0, "with_items": 0, "items": 0, "pending": 0}
assert cov["DELRAY"]["meetings"] == 3 and cov["DELRAY"]["with_items"] == 3
lk = {l["key"]: l for l in out["leads"]}
assert [l["key"] for l in out["leads"][:2]] == ["DELRAY|addr|755 se 7th ave", "BOCA|addr|1200 n federal hwy"]  # soonest vote first
d = lk["DELRAY|addr|755 se 7th ave"]
assert d["appearance_count"] == 3 and d["next_date"] == "2026-12-03"
assert d["evidence"]["verified"] and d["evidence"]["page"] is not None
assert all(l["is_new"] is False for l in out["leads"])                      # first ever run: nothing is 'new'
assert all(l["first_seen"] == out["today"] for l in out["leads"])
assert os.path.exists("agenda_items_cache.json")
ok("end to end: leads.json, coverage report, upcoming-first ordering, first-run state")

# second run: nothing re-analyzed, a new lead is flagged new, an old one keeps first_seen
llm_calls = {"n": 0}
orig = ie._call_gemini_once


def counting(prompt):
    llm_calls["n"] += 1
    return orig(prompt)


ie._call_gemini_once = counting
hist_events.append({"muni_short": "DELRAY", "muni_full": "City of Delray Beach",
                    "title": "City Commission", "date": "2026-11-19", "link": "https://example.com/d3"})
docs["https://example.com/d3"] = ("\n[[PAGE 2]]\nRezoning of 802 SE 5th Avenue to allow a mixed-use "
                                  "project. First reading.")


def gem3(prompt):
    llm_calls["n"] += 1
    if "802 SE 5th Avenue" in prompt:
        return [{"headline": "Rezoning request at 802 SE 5th Avenue", "kind": "rezoning",
                 "address": "802 SE 5th Avenue", "case_no": "", "entities": [], "amount": None,
                 "action": "first reading", "consent": False, "summary": "Rezoning for a mixed-use project.",
                 "evidence": "Rezoning of 802 SE 5th Avenue to allow a mixed-use project"}], False, None
    return orig(prompt)


ie._call_gemini_once = gem3
with contextlib.redirect_stdout(io.StringIO()):
    ie.generate_leads_output()
out2 = json.load(open("leads.json"))
lk2 = {l["key"]: l for l in out2["leads"]}
assert llm_calls["n"] == 1, llm_calls            # only the one new past meeting hit the LLM; upcoming docs were unchanged
assert lk2["DELRAY|addr|802 se 5th ave"]["is_new"] is True
assert lk2["BOCA|addr|1200 n federal hwy"]["is_new"] is False
ok("second run: cache honoured, brand-new lead flagged is_new")

# --- 10b. fixes from the first real run (2026-11-21 log) --------------------
import time as _time
importlib.reload(ie)
_real_sleep = _time.sleep
_time.sleep = lambda s: None           # no real waiting in these tests
try:
    # (a) a calendar page with only a nav landing page logs what it did find, and yields no document
    cal_only_nav = ("<html><body><a href='/129/Agendas-Minutes'>Agendas &amp; Minutes</a>"
                    "<a href='/Calendar.aspx'>Calendar</a></body></html>")
    ie._fetch = lambda url, timeout: FakeHttp(200, cal_only_nav, headers={"Content-Type": "text/html"})
    ev_nav = {"muni_short": "JUP", "muni_full": "Town of Jupiter", "title": "Town Council", "date": "2026-11-03",
              "link": "https://www.jupiter.fl.us/Calendar.aspx?EID=9"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert ie.fetch_document_text(ev_nav) is None
        ie.fetch_document_text(dict(ev_nav, date="2026-11-04"))
        ie.fetch_document_text(dict(ev_nav, date="2026-11-05"))
    out = buf.getvalue()
    assert out.count("no agenda document link on JUP") == 2, out      # capped at two per municipality per run
    assert "/129/Agendas-Minutes" in out
    ok("calendar page with no agenda document logs the anchors it saw (capped per municipality)")

    # (b) _fetch: retries a 503 once, spaces requests per host, falls back to the alternate PBC host
    importlib.reload(ie)
    seen_urls = []
    answers = {"n": 0}

    def flaky(url, timeout=15, **k):
        seen_urls.append(url)
        answers["n"] += 1
        return FakeHttp(503) if answers["n"] == 1 else FakeHttp(200, "ok")
    ie.sc.fetch_hardened = flaky
    res = ie._fetch("https://delraybeach.legistar.com/MeetingDetail.aspx?LEGID=1", 10)
    assert res.status_code == 200 and len(seen_urls) == 2 and ie._fetch_transient_failure is False
    ok("fetch retries a transient 503 once")

    importlib.reload(ie)
    seen_urls = []

    def pbc(url, timeout=15, **k):
        seen_urls.append(url)
        if "discover.pbc.gov" in url:
            return None
        return FakeHttp(200, "pdf-bytes")
    ie.sc.fetch_hardened = pbc
    ie.requests.get = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("blocked"))
    res = ie._fetch("https://discover.pbc.gov/countycommissioners/Agenda_Master/20261117.pdf", 10)
    assert res.status_code == 200
    assert seen_urls[-1] == "https://www.pbcgov.org/countycommissioners/Agenda_Master/20261117.pdf"
    ok("PBC: falls back to the www.pbcgov.org host when discover.pbc.gov is blocked")

    # (c) a transient fetch failure is NOT remembered as 'no document'
    importlib.reload(ie)
    ie.GEMINI_API_KEY = "k"
    cache = {"schema": ie.CACHE_SCHEMA, "events": {}, "nodoc": {}}

    def failing_fetch(ev, timeout=25):
        ie._fetch_transient_failure = True
        return None
    ie.fetch_document_text = failing_fetch
    ev_t = {"muni_short": "DELRAY", "muni_full": "City of Delray Beach", "title": "City Commission",
            "date": "2026-08-18", "link": "https://delraybeach.legistar.com/x"}
    its, status, had = ie.analyze_event(ev_t, cache, TODAY, False)
    assert status == "fetch_failed" and ie._cache_key(ev_t) not in cache["nodoc"]
    ie.fetch_document_text = lambda ev, timeout=25: None
    its, status, had = ie.analyze_event(ev_t, cache, TODAY, False)
    assert status == "nodoc" and ie._cache_key(ev_t) in cache["nodoc"]   # a clean 'nothing there' is remembered
    ok("transient fetch failures are retried next run; genuine no-document is remembered")

    # (d) Gemini 503: retried and recovered; a streak of failures trips the breaker
    importlib.reload(ie)
    ie.GEMINI_API_KEY = "k"
    ie._gemini_checked_available = False
    class R503:  # noqa
        status_code = 503
    outcomes = [(None, False, ie.requests.HTTPError("HTTP 503: high demand", response=R503())),
                ([], False, None)]
    ie._call_gemini_once = lambda prompt: outcomes.pop(0)
    with contextlib.redirect_stdout(io.StringIO()):
        got = ie._llm_items(event, DOC)
    assert got == [] and not outcomes and ie._gemini_unavailable is False
    ok("Gemini 503 is retried and recovers within the same run")

    importlib.reload(ie)
    ie.GEMINI_API_KEY = "k"
    ie._gemini_checked_available = False
    n503 = {"n": 0}

    def always_503(prompt):
        n503["n"] += 1
        return None, False, ie.requests.HTTPError("HTTP 503", response=R503())
    ie._call_gemini_once = always_503
    with contextlib.redirect_stdout(io.StringIO()) as buf3:
        for _ in range(4):
            ie._llm_items(event, DOC)
    assert ie._gemini_unavailable is True
    assert n503["n"] == 3 * 3, n503     # 3 meetings x (1 try + 2 retries), then the breaker stops further calls
    ok("three meetings in a row failing with 5xx stops LLM calls for the run")
finally:
    _time.sleep = _real_sleep
    importlib.reload(ie)

# --- 11. every scraper main() runs is also covered by the engine's lists ---
# Guards against the drift that once left WPB Planning, WPB Plans & Plats and the PBC Zoning
# calendar out of the historical lookup: a scraper added to scraper.main() but forgotten in
# insights_engine's two lists would silently contribute no leads from past meetings.
import inspect
import scraper as _sc
_main_scrapers = set(re.findall(r"all_events\.extend\((scrape_\w+)\(\)\)", inspect.getsource(_sc.main)))
_covered = {f.__name__ for f in ie.MULTI_MONTH_LOOP_SCRAPERS + ie.SINGLE_WINDOW_SCRAPERS}
assert _main_scrapers, "could not find any scrapers in main()"
_missing = _main_scrapers - _covered
assert not _missing, f"scrapers in main() but missing from insights_engine lists: {sorted(_missing)}"
assert len(ie.MULTI_MONTH_LOOP_SCRAPERS + ie.SINGLE_WINDOW_SCRAPERS) == len(_covered), "a scraper is listed twice"
ok(f"all {len(_main_scrapers)} scrapers in main() are covered by insights_engine")

print(f"\nAll {passed} test groups passed.")
