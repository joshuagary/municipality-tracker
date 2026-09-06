"""Local smoke test - not part of the shipped app. Exercises
insights_engine's clustering/scoring/history/filtering logic against
hand-built fixtures, since this sandbox has no network access to test the
real Gemini/Serper/live-site calls. Run: python3 _smoke_test_insights.py
"""
import os
import json
import tempfile

os.chdir(tempfile.mkdtemp())

import insights_engine as ie

# --- Test 1: heuristic topic extraction (no Gemini key configured) ---
ie.GEMINI_API_KEY = None
ie._gemini_checked_available = False

fake_event_1 = {"muni_short": "BOCA", "muni_full": "City of Boca Raton",
                "title": "Community Redevelopment Agency Meeting", "date": "2026-04-10",
                "link": "https://example.com/a.pdf"}
topics_1, used_llm_1 = ie.extract_meeting_topics(fake_event_1, "The board discussed CRA funding for the downtown streetscape project and a new zoning variance request.")
assert used_llm_1 is False
assert len(topics_1) >= 1
assert any(t["category"] == "Community Redevelopment (CRA)" for t in topics_1), topics_1
print("Test 1 (heuristic extraction) passed:", topics_1)

fake_event_2 = {"muni_short": "WPB", "muni_full": "City of West Palm Beach",
                "title": "City Commission Meeting", "date": "2026-05-02", "link": None}
topics_2, _ = ie.extract_meeting_topics(fake_event_2, "Discussion of a proposed data center rezoning application downtown.")
assert any(t["category"] == "Data Centers" for t in topics_2), topics_2
print("Test 2 (data center heuristic) passed:", topics_2)

fake_event_3 = {"muni_short": "PBG", "muni_full": "City of Palm Beach Gardens",
                "title": "Planning and Zoning Board Meeting", "date": "2026-03-15", "link": None}
topics_3, _ = ie.extract_meeting_topics(fake_event_3, "The board reviewed a new zoning variance for a mixed-use residential development.")
print("Test 3 (cross-muni zoning topic) passed:", topics_3)

# --- Test 2: clustering across municipalities ---
all_topics = topics_1 + topics_2 + topics_3
themes = ie.cluster_recurring_themes(all_topics)
assert len(themes) >= 1
print(f"Test 4 (clustering) passed: {len(themes)} theme(s):")
for t in themes:
    print("  -", t["theme_title"], "|", t["category"], "| munis:", t["municipalities"], "| count:", t["occurrence_count"])

# --- Test 3: confidence scoring ---
for t in themes:
    fake_web = {"checked": True, "corroborated": True, "sources": [{"title": "x", "link": "y"}]} if t["cross_municipality"] else {"checked": False, "corroborated": False, "sources": []}
    t["web_notability"] = fake_web
    t["confidence"] = ie.compute_confidence(t, fake_web)
themes.sort(key=lambda t: t["confidence"], reverse=True)
for i, t in enumerate(themes):
    t["rank"] = i + 1
print("Test 5 (confidence scoring) passed:")
for t in themes:
    print(f"  rank={t['rank']} confidence={t['confidence']} cross_muni={t['cross_municipality']} theme={t['theme_title']}")
assert themes[0]["confidence"] >= themes[-1]["confidence"]

# --- Test 4: history + starring across multiple simulated days ---
hist_path = "insights_history.json"
ie.INSIGHTS_HISTORY_PATH = hist_path

hist = ie.load_insight_history(hist_path)
ie.apply_star_flags(themes, hist)
assert all(t["starred"] is False for t in themes), "Nothing should be starred on day 1"
ie.save_insight_history(themes)
hist = ie.load_insight_history(hist_path)
assert len(hist["runs"]) == 1

hist = ie.load_insight_history(hist_path)
hist["runs"][-1]["date"] = "2026-09-04"
with open(hist_path, "w") as f:
    json.dump(hist, f)

hist = ie.load_insight_history(hist_path)
ie.apply_star_flags(themes, hist)
starred_count_day2 = sum(1 for t in themes if t["starred"])
print(f"Test 6 (starring on 2nd occurrence): {starred_count_day2} starred theme(s) out of {len(themes)}")
assert starred_count_day2 >= 1, "Expected at least one theme to be starred on its 2nd appearance"

ie.save_insight_history(themes)
hist = ie.load_insight_history(hist_path)
assert len(hist["runs"]) == 2, f"Expected 2 distinct days, got {len(hist['runs'])}"

for day in range(3, 10):
    ie.save_insight_history(themes)
    hist = ie.load_insight_history(hist_path)
    hist["runs"][-1]["date"] = f"2026-08-{day:02d}"
    with open(hist_path, "w") as f:
        json.dump(hist, f)

hist = ie.load_insight_history(hist_path)
assert len(hist["runs"]) == ie.HISTORY_RETENTION_RUNS, f"Expected {ie.HISTORY_RETENTION_RUNS} runs retained, got {len(hist['runs'])}"
print(f"Test 7 (7-day retention trim) passed: {len(hist['runs'])} runs retained.")

# --- Test 5: web_cross_reference gracefully degrades with no API key ---
ie.SERPER_API_KEY = None
result = ie.web_cross_reference(themes[0])
assert result == {"checked": False, "corroborated": False, "sources": []}
print("Test 8 (search graceful degradation, no key) passed.")

# --- Test 6: Gemini path gracefully degrades with no API key ---
ie.GEMINI_API_KEY = None
ie._gemini_checked_available = False
assert ie.gemini_available() is False
print("Test 9 (LLM graceful degradation, no key) passed.")

# --- Test 7: topic cache round-trips and only persists LLM-derived results ---
cache_path = "topic_cache.json"
ie.TOPIC_CACHE_PATH = cache_path

fake_event_llm = {"muni_short": "TEST1", "muni_full": "Test City", "title": "Test Meeting A", "date": "2026-01-01", "link": None}
fake_event_heur = {"muni_short": "TEST2", "muni_full": "Test City 2", "title": "Test Meeting B", "date": "2026-01-02", "link": None}

llm_topics = [{"topic_title": "Real LLM topic", "category": "Other Governance Matters", "description": "x", "method": "llm"}]

cache = {}
key_llm = ie._cache_key(fake_event_llm)
key_heur = ie._cache_key(fake_event_heur)

cache[key_llm] = llm_topics  # simulates generate_insights_output's write-on-used_llm logic
# key_heur deliberately never written - simulates a heuristic-fallback result

assert key_llm in cache
assert key_heur not in cache
ie.save_topic_cache(cache, cache_path)
reloaded = ie.load_topic_cache(cache_path)
assert reloaded == cache
print("Test 10 (topic cache only persists LLM-derived results) passed.")

# --- Test 8: prune_topic_cache drops keys outside the current window ---
pruned = ie.prune_topic_cache({key_llm: llm_topics, "stale|key|here": [{"x": 1}]}, {key_llm})
assert key_llm in pruned and "stale|key|here" not in pruned
print("Test 11 (cache pruning drops aged-out keys) passed.")

# --- Test 9: unavailable flag short-circuits further LLM calls ---
ie._gemini_unavailable = True
result = ie._llm_topics("Test City", "Some Meeting", "2026-01-01", "text")
assert result is None
print("Test 12 (gemini-unavailable short-circuit) passed.")
ie._gemini_unavailable = False  # reset for cleanliness

# --- Test 10: cancelled meetings are detected and excluded ---
assert ie.is_cancelled_event("**CANCELED - City Council Regular Meeting") is True
assert ie.is_cancelled_event("CANCELLED - Planning Board") is True
assert ie.is_cancelled_event("City Council Regular Meeting") is False
print("Test 13 (cancellation detection) passed.")

# --- Test 11: schedule-deviation notes are stripped from titles ---
stripped = ie.strip_schedule_notes(
    "Boynton Beach Community Redevelopment Agency (BBCRA) Board Monthly Meeting "
    "(March meeting is on a Monday, not the usual Tuesday)"
)
assert "usual" not in stripped.lower() and "Monday" not in stripped
assert "Boynton Beach Community Redevelopment Agency" in stripped
print(f"Test 14 (schedule-note stripping) passed: '{stripped}'")

stripped2 = ie.strip_schedule_notes(
    "Boynton Beach Community Redevelopment Agency (BBCRA) Board Monthly Meeting "
    "(Will Follow the Joint Meeting)"
)
assert "Will Follow" not in stripped2
print(f"Test 14b (schedule-note stripping, second phrasing) passed: '{stripped2}'")

# --- Test 12: LLM returning an empty list is a SUCCESS, not a fallback trigger ---
# Mock requests.post since _llm_topics now calls the Gemini REST API directly
# rather than going through an SDK client object.
class FakeGeminiResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text
    def json(self):
        return self._json_body

def _gemini_text_response(text):
    return FakeGeminiResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})

ie._gemini_unavailable = False
ie._last_gemini_call_time = 0.0
ie.GEMINI_MIN_SECONDS_BETWEEN_CALLS = 0  # don't actually sleep during tests

ie.requests.post = lambda *a, **kw: _gemini_text_response("[]")
empty_result = ie._llm_topics("Test City", "Routine Meeting", "2026-01-01", "Approval of minutes. Adjournment.")
assert empty_result == [], f"Expected empty list (success, nothing specific), got {empty_result}"
print("Test 15 (LLM empty result treated as success, not failure) passed.")

ie.requests.post = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should never be called - no doc text"))
no_doc_result = ie._llm_topics("Test City", "Some Meeting", "2026-01-01", None)
assert no_doc_result == [], "No document text should short-circuit to [] without calling the LLM"
print("Test 16 (no document text -> empty result, no fabricated topic) passed.")

ie.requests.post = lambda *a, **kw: _gemini_text_response(
    '[{"topic_title": "Ordinance No. 4316", "category": "Other Governance Matters", "description": "Grants a franchise to a utility company."}]'
)
real_result = ie._llm_topics(
    "City of Riviera Beach", "City Council Meeting", "2026-08-19",
    "The council considered Ordinance No. 4316 granting a franchise to Florida Public Utilities."
)
assert len(real_result) == 1 and real_result[0]["topic_title"] == "Ordinance No. 4316"
assert real_result[0]["method"] == "llm"
print("Test 17 (specific real LLM extraction parses correctly) passed.")

# --- Test 17b: error classification correctly distinguishes quota exhaustion
# from an auth/config problem (bad key or bad model name) ---
ie._gemini_unavailable = False
ie.requests.post = lambda *a, **kw: FakeGeminiResponse(429, text="RESOURCE_EXHAUSTED: quota exceeded")
result = ie._llm_topics("Test City", "Some Meeting", "2026-01-01", "some real document text")
assert result is None
assert ie._gemini_unavailable is True
print("Test 17c (429 quota exhaustion correctly short-circuits further calls) passed.")

ie._gemini_unavailable = False
ie.requests.post = lambda *a, **kw: FakeGeminiResponse(404, text="model not found")
result = ie._llm_topics("Test City", "Some Meeting", "2026-01-01", "some real document text")
assert result is None
assert ie._gemini_unavailable is True
print("Test 17d (404 model-not-found correctly short-circuits further calls) passed.")
ie._gemini_unavailable = False

# --- Test 17e: self-healing retry when Gemini's 404 names the exact
# replacement model (this happened for real: gemini-2.5-flash-lite was
# retired mid-project and the error named gemini-3.5-flash-lite as the fix) ---
ie.GEMINI_MODEL = "gemini-2.5-flash-lite"  # simulate a stale/deprecated default
call_log = []
def _self_heal_mock(*a, **kw):
    call_log.append(1)
    if len(call_log) == 1:
        return FakeGeminiResponse(
            404,
            text='{"error": {"code": 404, "message": "This model models/gemini-2.5-flash-lite '
                 'is no longer available to new users. Please update your code to use '
                 'models/gemini-3.5-flash-lite for this."}}'
        )
    return _gemini_text_response('[{"topic_title": "Resolution No. 99", "category": "Other Governance Matters", "description": "x"}]')

ie.requests.post = _self_heal_mock
result = ie._llm_topics("Test City", "Some Meeting", "2026-01-01", "some real document text")
assert result is not None and len(result) == 1 and result[0]["topic_title"] == "Resolution No. 99", result
assert ie.GEMINI_MODEL == "gemini-3.5-flash-lite", f"Expected auto-switch to the suggested model, got {ie.GEMINI_MODEL}"
assert ie._gemini_unavailable is False, "A successful self-heal must NOT mark Gemini unavailable"
assert len(call_log) == 2, "Expected exactly one retry after the self-heal"
print("Test 17e (self-heals to Gemini's suggested replacement model on a deprecated-model 404) passed.")
ie._gemini_unavailable = False

# --- Test 13: insight-worthiness filter - the core behavior change requested ---
noise_theme = {"occurrence_count": 1, "cross_municipality": False, "all_heuristic": True,
               "web_notability": {"checked": True, "corroborated": False, "sources": []}}
assert ie.is_insight_worthy(noise_theme) is False
print("Test 18 (single uncorroborated mention is dropped) passed.")

notable_singleton = {"occurrence_count": 1, "cross_municipality": False, "all_heuristic": False,
                      "web_notability": {"checked": True, "corroborated": True, "sources": [{"title": "x", "link": "y"}]}}
assert ie.is_insight_worthy(notable_singleton) is True
ie.assign_reason(notable_singleton)
assert "corroborated_by_web_search" in notable_singleton["reasons"]
assert "recurring_over_time" not in notable_singleton["reasons"]
print("Test 19 (corroborated LLM-derived singleton is kept, reason correctly assigned) passed.")

heuristic_singleton_corroborated = {"occurrence_count": 1, "cross_municipality": False, "all_heuristic": True,
                                     "web_notability": {"checked": True, "corroborated": True, "sources": [{"title": "x", "link": "y"}]}}
assert ie.is_insight_worthy(heuristic_singleton_corroborated) is False, (
    "A generic heuristic singleton must NOT be kept just because it trivially "
    "'corroborates' (e.g. 'City Council Regular Meeting' always finds search "
    "results) - this is the exact bug seen on the real run where an LLM outage "
    "forced all-heuristic extraction and 54/54 themes came back 'corroborated'."
)
print("Test 19b (heuristic-only singleton is dropped even if trivially 'corroborated') passed.")

recurring_theme = {"occurrence_count": 3, "cross_municipality": False, "all_heuristic": True,
                    "web_notability": {"checked": True, "corroborated": False, "sources": []}}
assert ie.is_insight_worthy(recurring_theme) is True
ie.assign_reason(recurring_theme)
assert "recurring_over_time" in recurring_theme["reasons"]
print("Test 20 (recurring-but-uncorroborated theme is kept regardless of method) passed.")

cross_muni_theme = {"occurrence_count": 2, "cross_municipality": True, "all_heuristic": True,
                     "web_notability": {"checked": False, "corroborated": False, "sources": []}}
ie.assign_reason(cross_muni_theme)
assert "recurring_across_municipalities" in cross_muni_theme["reasons"]
print("Test 21 (cross-municipality recurrence reason) passed.")

# --- Test 14: citation_summary is built correctly for recurring themes ---
entries = [
    {"muni_full": "City of Riviera Beach", "muni_short": "RIVBEACH", "date": "2026-08-19", "event_title": "City Council Meeting", "link": None, "description": None, "method": "llm"},
    {"muni_full": "City of Riviera Beach", "muni_short": "RIVBEACH", "date": "2026-07-15", "event_title": "City Council Meeting", "link": None, "description": None, "method": "llm"},
    {"muni_full": "City of Riviera Beach", "muni_short": "RIVBEACH", "date": "2026-06-03", "event_title": "City Council Meeting", "link": None, "description": None, "method": "llm"},
]
summary = ie._build_citation_summary(entries)
assert "2026-07-15" in summary and "2026-06-03" in summary and "2026-08-19" not in summary
print(f"Test 22 (citation summary cites prior occurrences, excludes the most recent) passed: '{summary}'")

single_entry_summary = ie._build_citation_summary(entries[:1])
assert single_entry_summary is None
print("Test 23 (no citation summary for a single occurrence) passed.")

print("\nALL SMOKE TESTS PASSED")
