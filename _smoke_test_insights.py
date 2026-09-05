"""Local smoke test - not part of the shipped app. Exercises
insights_engine's clustering/scoring/history logic against hand-built
fixtures, since this sandbox has no network access to test the real HF/
Serper/live-site calls. Run: python3 _smoke_test_insights.py
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
topics_1 = ie.extract_meeting_topics(fake_event_1, "The board discussed CRA funding for the downtown streetscape project and a new zoning variance request.")
assert len(topics_1) >= 1
assert any(t["category"] == "Community Redevelopment (CRA)" for t in topics_1), topics_1
print("Test 1 (heuristic extraction) passed:", topics_1)

fake_event_2 = {"muni_short": "WPB", "muni_full": "City of West Palm Beach",
                "title": "City Commission Meeting", "date": "2026-05-02", "link": None}
topics_2 = ie.extract_meeting_topics(fake_event_2, "Discussion of a proposed data center rezoning application downtown.")
assert any(t["category"] == "Data Centers" for t in topics_2), topics_2
print("Test 2 (data center heuristic) passed:", topics_2)

fake_event_3 = {"muni_short": "PBG", "muni_full": "City of Palm Beach Gardens",
                "title": "Planning and Zoning Board Meeting", "date": "2026-03-15", "link": None}
topics_3 = ie.extract_meeting_topics(fake_event_3, "The board reviewed a new zoning variance for a mixed-use residential development.")
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

# Day 1 (mirrors generate_insights_output's real order: star BEFORE saving)
hist = ie.load_insight_history(hist_path)
ie.apply_star_flags(themes, hist)
assert all(t["starred"] is False for t in themes), "Nothing should be starred on day 1"
ie.save_insight_history(themes)
hist = ie.load_insight_history(hist_path)
assert len(hist["runs"]) == 1

# Simulate day 1 actually being a distinct prior date (not "today"), so a
# real day-2 run can be appended rather than deduped as the same-day rerun.
hist = ie.load_insight_history(hist_path)
hist["runs"][-1]["date"] = "2026-09-04"
with open(hist_path, "w") as f:
    json.dump(hist, f)

# Day 2: re-run with the SAME themes (simulating a recurring topic) - should now star
hist = ie.load_insight_history(hist_path)
ie.apply_star_flags(themes, hist)
starred_count_day2 = sum(1 for t in themes if t["starred"])
print(f"Test 6 (starring on 2nd occurrence): {starred_count_day2} starred theme(s) out of {len(themes)}")
assert starred_count_day2 >= 1, "Expected at least one theme to be starred on its 2nd appearance"

ie.save_insight_history(themes)
hist = ie.load_insight_history(hist_path)
assert len(hist["runs"]) == 2, f"Expected 2 distinct days, got {len(hist['runs'])}"

# Simulate 8 days total to confirm the 7-day retention trims correctly
for day in range(3, 10):
    ie.save_insight_history(themes)
    # force a distinct date so each save isn't deduped as "today"
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

print("\nALL SMOKE TESTS PASSED")
