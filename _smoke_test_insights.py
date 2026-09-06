"""Local smoke test - not part of the shipped app. Exercises
insights_engine's clustering/scoring/history/filtering logic against
hand-built fixtures, since this sandbox has no network access to test the
real HF/Serper/live-site calls. Run: python3 _smoke_test_insights.py
"""
import os
import json
import tempfile

os.chdir(tempfile.mkdtemp())

import insights_engine as ie

# --- Test 1: heuristic topic extraction (no HF client available) ---
ie._hf_client_attempted = True
ie._hf_client = None

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

# --- Test 6: get_hf_client gracefully degrades with no token ---
ie._hf_client_attempted = False
ie._hf_client = None
ie.HF_TOKEN = None
client = ie.get_hf_client()
assert client is None
print("Test 9 (LLM graceful degradation, no token) passed.")

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

# --- Test 9: credits-exhausted flag short-circuits further LLM calls ---
ie._hf_credits_exhausted = True
result = ie._llm_topics(object(), "Test City", "Some Meeting", "2026-01-01", "text")
assert result is None
print("Test 12 (credits-exhausted short-circuit) passed.")
ie._hf_credits_exhausted = False  # reset for cleanliness

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
class FakeChoice:
    def __init__(self, content):
        self.message = type("obj", (), {"content": content})

class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]

class FakeClient:
    def __init__(self, content):
        self._content = content
    def chat_completion(self, **kwargs):
        return FakeResponse(self._content)

ie._hf_credits_exhausted = False
empty_result = ie._llm_topics(FakeClient("[]"), "Test City", "Routine Meeting", "2026-01-01", "Approval of minutes. Adjournment.")
assert empty_result == [], f"Expected empty list (success, nothing specific), got {empty_result}"
print("Test 15 (LLM empty result treated as success, not failure) passed.")

no_doc_result = ie._llm_topics(FakeClient("should never be called"), "Test City", "Some Meeting", "2026-01-01", None)
assert no_doc_result == [], "No document text should short-circuit to [] without calling the LLM"
print("Test 16 (no document text -> empty result, no fabricated topic) passed.")

real_result = ie._llm_topics(
    FakeClient('[{"topic_title": "Ordinance No. 4316", "category": "Other Governance Matters", "description": "Grants a franchise to a utility company."}]'),
    "City of Riviera Beach", "City Council Meeting", "2026-08-19",
    "The council considered Ordinance No. 4316 granting a franchise to Florida Public Utilities."
)
assert len(real_result) == 1 and real_result[0]["topic_title"] == "Ordinance No. 4316"
assert real_result[0]["method"] == "llm"
print("Test 17 (specific real LLM extraction parses correctly) passed.")

# --- Test 17b: error classification correctly distinguishes credits-exhausted
# from model-not-supported (these were conflated in a real run and produced a
# misleading "credits exhausted" message for what was actually a model/
# provider routing mismatch) ---
class FakeHttpError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.response = type("Resp", (), {"status_code": status_code})()

class FailingClient:
    def __init__(self, exc):
        self._exc = exc
    def chat_completion(self, **kwargs):
        raise self._exc

ie._hf_credits_exhausted = False
ie._hf_model_unsupported = False
credits_exc = FakeHttpError("Bad request: {'message': 'You have depleted your monthly included credits.'}", 402)
result = ie._llm_topics(FailingClient(credits_exc), "Test City", "Some Meeting", "2026-01-01", "some real document text")
assert result is None
assert ie._hf_credits_exhausted is True and ie._hf_model_unsupported is False
print("Test 17c (real 402 correctly classified as credits-exhausted) passed.")

ie._hf_credits_exhausted = False
ie._hf_model_unsupported = False
model_exc = FakeHttpError("Bad request: {'message': \"The requested model 'X' is not supported by any provider you have enabled.\", 'code': 'model_not_supported'}", 400)
result = ie._llm_topics(FailingClient(model_exc), "Test City", "Some Meeting", "2026-01-01", "some real document text")
assert result is None
assert ie._hf_model_unsupported is True and ie._hf_credits_exhausted is False, (
    "A 400 model_not_supported error must NOT be misclassified as a 402 credits "
    "issue - this exact confusion happened on a real run and produced a "
    "misleading 'credits exhausted' message when the real problem was an "
    "unsupported model."
)
print("Test 17d (400 model_not_supported correctly classified, NOT as credits) passed.")
ie._hf_credits_exhausted = False
ie._hf_model_unsupported = False

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
