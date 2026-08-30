# Municipality Tracker — Session Handoff

## Your Role
You are a coding assistant helping build and debug **`municipality-tracker`**, a Python
scraper + static HTML frontend that tracks public government meetings across six South
Florida municipalities. The person you're working with is non-technical-ish but hands-on:
they paste real GitHub Actions logs, spot specific wrong meetings by name, and want
precise, tested fixes — not guesses. Verify claims (fetch real pages, test regex against
real titles) before asserting something works, and be upfront when something can't be
verified without a live browser.

## Project Overview
`scraper.py` runs daily via GitHub Actions (`.github/workflows/scrape.yml`), fetches
public meeting schedules for six municipalities, filters for governance-relevant
meetings, normalizes dates/times to ISO format, and writes `data.json`. `index.html` is
a static frontend (FullCalendar 6 + vanilla JS) that reads `data.json` and renders a
searchable calendar/list view, hosted directly from the repo (e.g. GitHub Pages).

**Files to re-upload to the new chat for full context:** the current `scraper.py` and
`index.html` from this repo — this document summarizes their state and history but
doesn't reproduce the full code.

## Municipality Status (as of last confirmed GitHub Actions run)

| Muni | Platform | Mechanism | Status |
|---|---|---|---|
| Palm Beach Gardens (PBG) | CivicPlus (`pbgfl.gov`) | `calendar.aspx?view=list&year=&month=` | ✅ Working |
| Delray Beach | Legistar | `webapi.legistar.com/v1/delraybeach/events` JSON API | ✅ Working (confirmed 20 events) |
| Palm Beach County (PBC) | SharePoint (`discover.pbc.gov`) | Regex-parses `Agenda_Master/YYYYMMDD.pdf` links | ✅ Working (confirmed 2 events) |
| Boca Raton | CivicPlus (`myboca.us`) | Same `calendar.aspx?view=list` mechanism as PBG | 🔧 Fixed, not yet reconfirmed in a fresh log |
| Boynton Beach | CivicPlus (`boynton-beach.org`) | Same `calendar.aspx?view=list` mechanism as PBG | 🔧 Fixed, not yet reconfirmed in a fresh log |
| West Palm Beach (WPB) | Granicus OpenCities (`wpb.org`) | Static per-series pages (`Events-Folder/{year}/City-Commission-Meeting-{year}`) listing every date under a "When" heading | 🔧 Fixed, not yet reconfirmed in a fresh log |

### Key history / root causes fixed this session
- **Boca & Boynton were never Legistar clients.** The original code (from an earlier
  Gemini session) wired them up as Legistar, which is why they returned 500s/404s/0
  events. Both actually run CivicPlus — confirmed live ("Government Websites by
  CivicPlus®" appears directly on Boca's site). Both now share one generic
  `scrape_civicplus_calendar()` function with PBG instead of three separate paths.
- **WPB is on a third platform entirely** (Granicus OpenCities, not CivicPlus), and its
  main calendar page is JS-rendered client-side ("Please wait while we load this
  calendar..." with no server-side content) — a plain fetch can't read it. Its
  individual meeting-series pages are static, though, and list the full year's dates.
- **PBC's domain moved**: `discover.pbcgov.org` → `discover.pbc.gov`. The new page has
  no clean event list, so the scraper pulls the date straight from agenda PDF filenames.
- **`fetch_hardened()` helper** added: uses `curl_cffi` (already in
  `requirements`/workflow deps) to impersonate a real Chrome TLS fingerprint, since
  plain `requests` gets blocked by some of these sites' WAFs on GitHub Actions IP
  ranges. Falls back to plain `requests` if `curl_cffi` isn't available.

## Filtering Logic (`is_qualifying_event`)
Went through two iterations this session:
1. **Broad keyword + exclude list** (e.g. bare `\bBoard\b`, `\bHearing\b` + an
   exclude list for known non-governance boards). Abandoned — the exclude list was an
   unmaintainable whack-a-mole as new false positives kept surfacing (e.g. Boca's
   "Parks & Recreation Board," "Financial Advisory Board," and Boynton's "Red Light
   Camera Hearing" all matched the old broad keywords).
2. **Current: inclusive whitelist only.** Matches specific named governance bodies
   instead of generic words:
   - City Council, City Commission, Town Council
   - Board of County Commissioners, BCC
   - Community Redevelopment Agency, CRA
   - Planning and Zoning, Planning Board/Commission, Zoning Board/Commission/Board of
     Appeals, Board of Adjustment
   - Downtown Development Authority, Housing Authority, Airport Authority
   - Council/Commission Workshop, Mayor/Commission Work Session, Public Hearing, Town
     Hall (all qualified by the body name so bare "Workshop"/"Hearing" can't match
     alone)
   - **Explicitly dropped per user request:** Special Magistrate, Code Enforcement
     Board, Historic Preservation Board, Community Appearance Board — these are not to
     be re-added unless asked.
   - Tested against 24+ real titles pulled from the actual municipal calendars (both
     known false positives and legitimate meetings) — all passed.

## Frontend (`index.html`) Features Added This Session
- **`dayMaxEvents: 3` + `moreLinkClick: 'popover'`** — days with more than 3 meetings
  collapse into a "+N more" link with a popover, instead of listing everything inline.
- **Custom styled hover tooltip** (not FullCalendar's native/browser tooltip) — shows
  muni badge, full title, date/time, summary, and a "View Agenda →" hint, positioned
  near the cursor and clamped to stay within the viewport. Wired via
  `eventMouseEnter`/`eventMouseLeave`, using `extendedProps` attached to each calendar
  event.
- **Sticky calendar header (in progress):** the month/year toolbar
  (`.fc-header-toolbar`) is confirmed working with `position: sticky; top: 0`. The
  weekday row (Sun–Sat) is NOT yet confirmed working by the user — first attempt
  targeted `.fc-scrollgrid-section-header > td`, which likely matched nothing (the
  header cells are probably plain `<th>` elements, not wrapped in a `<td>`). Just
  changed the selector to target `.fc-scrollgrid-section-header` directly (tag-agnostic)
  instead of assuming a child element — **this fix has not been visually confirmed
  yet**. A JS helper (`updateStickyToolbarOffset`) measures the toolbar's actual
  rendered height into a CSS variable (`--fc-toolbar-height`) so the weekday row sticks
  directly beneath it regardless of toolbar height changes (button wrapping, etc.).

## Known Limitations / Things I Can't Verify From My End
- No live browser available in my working environment — all frontend changes are
  verified via Node.js syntax-checking the extracted `<script>` block and manual DOM
  reasoning based on FullCalendar's documented/stable class names, not actual visual
  rendering. Visual/interaction bugs need the user to check in-browser and report back
  with specifics (ideally: exact class names from browser inspector, screenshots, or
  console errors).
- No live network access from the sandboxed code-execution tool (bash) — all research
  into actual site structures (CivicPlus vs Legistar vs OpenCities platform detection,
  actual PDF filename patterns, actual Legistar API client slugs) was done via web
  search/fetch tools, not by running the scraper live end-to-end. Confirmed results
  (PBC, Delray) came from the user's own GitHub Actions log output, not my own test
  runs.

## Goals for Next Session
1. Confirm (via a fresh GitHub Actions log) that Boca Raton and Boynton Beach are now
   returning real events after the CivicPlus platform fix.
2. Confirm WPB is returning real events after the OpenCities `Events-Folder` fix.
3. Confirm the weekday-header sticky fix actually renders correctly in-browser; if not,
   get the actual class name(s) on the weekday row from browser dev tools and target
   that directly instead of guessing further.
4. Keep an eye out for new false-positive/false-negative meeting titles as CivicPlus
   calendars get scraped over time — extend the whitelist additively (never re-add a
   dropped keyword without being asked).
5. General code hygiene: `scrape_civicplus_calendar()` is now shared by PBG/Boca/
   Boynton — if one needs a one-off tweak, prefer adding an optional parameter over
   forking the function.
