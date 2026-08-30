# Municipality Tracker — Session Handoff

## Your Role
You are a coding assistant helping build and debug **`municipality-tracker`**, a Python
scraper + static HTML frontend that tracks public government meetings across seven
South Florida municipalities. The person you're working with is non-technical-ish but
hands-on: they paste real GitHub Actions logs, spot specific wrong meetings by name,
and want precise, tested fixes — not guesses. Verify claims (fetch real pages, test
regex against real HTML, not just a rendered preview of it) before asserting something
works, and be upfront when something can't be verified without a live browser or the
user's own CI logs. See "Key Methodological Lesson" below before writing any new
scraper — it covers a real mistake made (and caught) this session that's worth not
repeating.

## Project Overview
`scraper.py` runs daily via GitHub Actions (`.github/workflows/scrape.yml`), fetches
public meeting schedules for seven municipalities, filters for governance-relevant
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
| Delray Beach | Legistar | `webapi.legistar.com/v1/delraybeach/events` JSON API | ✅ Working (confirmed 9 events) |
| Palm Beach County (PBC) | SharePoint (`discover.pbc.gov`) | Regex-parses `Agenda_Master/YYYYMMDD.pdf` links | ✅ Working (confirmed 2 events) |
| Boca Raton | CivicPlus (`myboca.us`) | Same `calendar.aspx?view=list` mechanism as PBG | ✅ Working (confirmed 16 events) |
| Boynton Beach | CivicPlus (`boynton-beach.org`) | Same `calendar.aspx?view=list` mechanism as PBG | ✅ Working (confirmed 12 events) |
| West Palm Beach (WPB) | Granicus OpenCities (`wpb.org`) | Static per-series pages listing every date under a "When" heading | ✅ Working (confirmed 8 events) |
| **Wellington** | **CivicPlus (`wellingtonfl.gov`)** | **Own function, `scrape_wellington()` - see below** | ✅ **Working (confirmed 3 events, CIP Workshop correctly excluded)** |

### Key Methodological Lesson From This Session (read before building the next scraper)
While adding Wellington, an early attempt built a regex parser by fetching the live
page through a **web-fetch/browsing tool** and reasoning about the markdown-rendered
text it returned (e.g. `[Event Title](url)` bracket-link syntax). That regex found 0
matches once it actually ran against the real scraper's output. Root cause: **a
web-fetch tool's markdown conversion is not the raw HTML.** It synthesizes bracket-link
syntax for every `<a>` tag that never exists in real HTML — `BeautifulSoup(...).get_text()`
on the real raw response strips tags entirely and produces different text with no
brackets, parens, or embedded hrefs at all. Any regex/structure reasoning built purely
from a web-fetch tool's rendered preview needs to be treated as a *hypothesis*, not
ground truth, until confirmed against the literal HTML the real scraper receives.

**The reliable way to debug a new municipality's HTML structure going forward:**
temporarily add debug `print()` statements inside the relevant scraper function that
dump a slice of the real `res.text` (raw response body) to the GitHub Actions log —
anchored on a known real, specific piece of content (an actual meeting title, a known
category label) rather than the first N characters (which is usually all
`<head>`/analytics/anti-forgery boilerplate) or a generic string like `EID=` (which can
match unrelated content elsewhere on the page, e.g. a "Featured Events" carousel
widget). Ask the user to run the workflow once and paste back the new log output, then
build the parser from that literal HTML. This is exactly how Wellington's real
mechanism was found: **CivicPlus embeds a Schema.org microdata block per event**
(`<div itemscope itemtype="http://schema.org/Event">` containing
`<span itemprop="name">` and `<span itemprop="startDate">2026-08-11T18:30:00</span>`),
which is what `scrape_wellington()` now parses directly via BeautifulSoup tag/attribute
selectors — far more reliable than any text-flattening regex, since it sidesteps
freeform "Location" text entirely (one real Wellington event has a multi-sentence
childcare sign-up disclaimer sitting between the visible date and the visible title
repeat, which broke an earlier regex-based attempt by sweeping that whole disclaimer
into the "title" capture).

### Key history / root causes fixed in prior sessions
- **Boca & Boynton were never Legistar clients.** The original code (from an earlier
  Gemini session) wired them up as Legistar, which is why they returned 500s/404s/0
  events. Both actually run CivicPlus - confirmed live ("Government Websites by
  CivicPlus®" appears directly on Boca's site). Both share one generic
  `scrape_civicplus_calendar()` function with PBG instead of three separate paths.
- **WPB is on a third platform entirely** (Granicus OpenCities, not CivicPlus), and its
  main calendar page is JS-rendered client-side ("Please wait while we load this
  calendar..." with no server-side content) - a plain fetch can't read it. Its
  individual meeting-series pages are static, though, and list the full year's dates.
- **PBC's domain moved**: `discover.pbcgov.org` → `discover.pbc.gov`. The new page has
  no clean event list, so the scraper pulls the date straight from agenda PDF filenames.
- **`fetch_hardened()` helper**: uses `curl_cffi` (already in
  `requirements`/workflow deps) to impersonate a real Chrome TLS fingerprint, since
  plain `requests` gets blocked by some of these sites' WAFs on GitHub Actions IP
  ranges. Falls back to plain `requests` if `curl_cffi` isn't available.

### This session: Wellington added
- **Confirmed live**: Wellington runs standard CivicPlus ("Government Websites by
  CivicPlus®" in the footer), using the exact `calendar.aspx?startDate=&enddate=&CID=&
  showPastEvents=false` URL format the user provided.
- **But its mechanism doesn't match `scrape_civicplus_calendar()`**: Wellington's
  `?view=list` is a single-day drilldown (clicking a date goes to `...&day=6`), not a
  full-month list like PBG/Boca/Boynton's. The reliable single-request-per-month source
  turned out to be the plain calendar page (which server-renders a full month grid
  regardless of the `view=` param in practice), parsed via the Schema.org microdata
  blocks described above. Given the mechanism mismatch, Wellington got its own bespoke
  function, `scrape_wellington()`, the same way WPB and PBC did.
- **CID=29** ("Council Meetings" calendar) is what's being scraped, per the user's
  original URL. Note: **Wellington also has a broader "Meetings" calendar at CID=22**
  that includes additional governance-adjacent meeting types (Education Committee,
  Equestrian Preserve Committee, Planning/Zoning & Adjustment Board, Architectural
  Review Board, Code Special Magistrate Hearing, etc.) that CID=29 does NOT include.
  This was surfaced to the user but not switched to - worth asking again if broader
  coverage of Wellington's other boards/committees is ever wanted.
- **Whitelist additions made** (see Filtering Logic below): `Village Council` (Wellington
  is a Village, not a City/Town - none of the existing body-name patterns covered its
  actual meeting titles) and `Council/Commission Agenda Review` (added per explicit user
  request, for "Wellington Council Agenda Review Meeting").
- **Explicitly NOT added, per user decision**: `CIP Workshop` - a real recurring
  Wellington meeting type that does not currently qualify. Do not add without being
  asked again, consistent with the project's whitelist discipline.
- **One unresolved observation, not yet reproduced in a real CI run**: during early
  manual (non-CI) verification of Wellington via a web-fetch tool, one request for
  `CID=29` unexpectedly came back as a different calendar (`CID=22`). This wasn't seen
  in the real GitHub Actions run that ultimately confirmed the working scraper, so it
  may have been an artifact of that tool rather than a real site behavior - but worth
  keeping an eye on if Wellington's event count ever looks off (e.g. events appearing
  from the "Meetings" calendar that shouldn't be in scope, or events disappearing).

## Filtering Logic (`is_qualifying_event`)
Inclusive whitelist only - matches specific named governance bodies instead of generic
words, to stay maintainable (an earlier broad-keyword + exclude-list approach was
abandoned as an unmaintainable whack-a-mole). Current whitelist:
- City Council, City Commission, Town Council, **Village Council** (added for Wellington)
- Board of County Commissioners, BCC
- Community Redevelopment Agency, CRA
- Planning and Zoning, Planning Board/Commission, Zoning Board/Commission/Board of
  Appeals, Board of Adjustment
- Downtown Development Authority, Housing Authority, Airport Authority
- Council/Commission Workshop, **Council/Commission Agenda Review** (added for
  Wellington), Mayor/Commission Work Session, Public Hearing, Town Hall (all qualified
  by the body name so bare "Workshop"/"Hearing"/"Agenda Review" can't match alone)
- **Explicitly dropped per user request - not to be re-added unless asked:** Special
  Magistrate, Code Enforcement Board, Historic Preservation Board, Community Appearance
  Board, **CIP Workshop** (Wellington-specific)
- Tested against 24+ real titles from PBG/Boca/Boynton/PBC/Delray in an earlier session,
  plus all 4 real Wellington Council-calendar titles this session (3 qualify, CIP
  Workshop correctly excluded) - verified via an end-to-end mocked test of
  `scrape_wellington()`, not just the regex in isolation.

## Frontend (`index.html`) Features (unchanged this session - carried from prior handoff)
- **`dayMaxEvents: 3` + `moreLinkClick: 'popover'`** - days with more than 3 meetings
  collapse into a "+N more" link with a popover, instead of listing everything inline.
- **Custom styled hover tooltip** (not FullCalendar's native/browser tooltip) - shows
  muni badge, full title, date/time, summary, and a "View Agenda →" hint, positioned
  near the cursor and clamped to stay within the viewport. Wired via
  `eventMouseEnter`/`eventMouseLeave`, using `extendedProps` attached to each calendar
  event.
- **Sticky calendar header:** the month/year toolbar (`.fc-header-toolbar`) is confirmed
  working with `position: sticky; top: 0`. The weekday row (Sun-Sat) fix (targeting
  `.fc-scrollgrid-section-header` directly instead of assuming a `<td>` wrapper) was
  applied but **still not yet visually confirmed by the user** as of last handoff - if
  raised again, ask for the actual class name(s) on the weekday row from browser dev
  tools rather than guessing further.

## Known Limitations / Things I Can't Verify From My End
- No live browser available in my working environment - all frontend changes are
  verified via Node.js syntax-checking the extracted `<script>` block and manual DOM
  reasoning based on FullCalendar's documented/stable class names, not actual visual
  rendering. Visual/interaction bugs need the user to check in-browser and report back
  with specifics (ideally: exact class names from browser inspector, screenshots, or
  console errors).
- **Any web-fetch/browsing tool used to "verify live" is rendering a converted preview
  (e.g. markdown), not raw HTML** - treat anything learned that way as a hypothesis
  about page structure, not a confirmed fact, until it's checked against the literal
  `res.text` a real scraper run receives (see Key Methodological Lesson above). This
  bit Wellington once already this session.
- No live network access from the sandboxed code-execution tool (bash) - all research
  into actual site structures was done via web search/fetch tools or the user's own
  real GitHub Actions logs, not by running the scraper live end-to-end myself. The most
  trustworthy confirmations (PBC, Delray, and ultimately Wellington) all came from the
  user's own GitHub Actions log output, not from my own test runs.

## Goals for Next Session
1. Add the next new municipality - **use the Key Methodological Lesson above from the
   start**: don't assume a web-fetch tool's rendered preview reflects the real HTML;
   if the platform/mechanism is at all unclear, build in a debug-print step early and
   confirm against a real GitHub Actions log before writing the "final" parser.
2. If Wellington's event count ever looks wrong (too few, too many, or the CID=22 vs
   CID=29 mix-up resurfaces), revisit the CID=29-vs-CID=22 scope question directly with
   the user rather than guessing which calendar is intended.
3. Confirm the weekday-header sticky fix actually renders correctly in-browser; if not,
   get the actual class name(s) on the weekday row from browser dev tools and target
   that directly instead of guessing further.
4. Keep an eye out for new false-positive/false-negative meeting titles as any
   municipality's calendar gets scraped over time - extend the whitelist additively
   (never re-add a dropped keyword, including Wellington's CIP Workshop, without being
   asked).
5. General code hygiene: `scrape_civicplus_calendar()` is shared by PBG/Boca/Boynton;
   `scrape_wellington()` is its own function since Wellington's list-view mechanism
   doesn't match. If a new CivicPlus municipality's `?view=list&year=&month=` genuinely
   returns a full month list (like PBG/Boca/Boynton), prefer reusing
   `scrape_civicplus_calendar()` with an optional parameter over forking it again; if it
   behaves like Wellington's (single-day drilldown), a bespoke function following
   `scrape_wellington()`'s Schema.org-microdata approach is the proven pattern to reach
   for first.
