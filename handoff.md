# Municipality Tracker — Session Handoff

## Your Role
You are a coding assistant helping build and debug **`municipality-tracker`**, a Python
scraper + static HTML frontend that tracks public government meetings across South
Florida municipalities (plus Westlake, added this session — see below). The person
you're working with is non-technical-ish but hands-on: they paste real GitHub Actions
logs, spot specific wrong meetings by name, and want precise, tested fixes — not
guesses. Verify claims (fetch real pages, test regex against real HTML, not just a
rendered preview of it) before asserting something works, and be upfront when
something can't be verified without a live browser or the user's own CI logs. See
"Key Methodological Lessons" below before writing any new scraper — it covers real
mistakes made (and caught) across sessions that are worth not repeating.

## Project Overview
`scraper.py` runs daily via GitHub Actions (`.github/workflows/scrape.yml`), fetches
public meeting schedules for each municipality, filters for governance-relevant
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
| Wellington | CivicPlus (`wellingtonfl.gov`) | Own function, `scrape_wellington()` — Schema.org microdata | ✅ Working (confirmed 3 events, CIP Workshop correctly excluded) |
| **Westlake** | **MuniCode (`meetings.municode.com`, jurisdiction `WESTLAKEFL`)** | **Own function, `scrape_westlake()` — HTML table parse, see below** | ⚠️ **First-pass draft — NOT yet confirmed against a real GitHub Actions log (see below)** |

### Key Methodological Lessons (read before building the next scraper)

**Lesson 1 — a web-fetch/browsing tool's rendered output is not raw HTML.**
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

**Lesson 2 — the coding sandbox itself may have no outbound network access at all**
(discovered when adding Westlake). Even a plain `curl` to the target domain from the
assistant's own shell can fail outright (connection rejected by an egress/organization
policy), meaning *neither* the rendered-preview tool's output *nor* a live raw-HTML
fetch may be available to build against in a given session — only the rendered-preview
tool. When that happens, say so explicitly, write the scraper defensively (multiple
selector/fallback strategies, generous debug `print()`s of header rows / row counts /
a sample raw row), and be explicit with the user that the function is an **unconfirmed
first-pass draft** until they run it for real (GitHub Actions or locally) and paste
back the log. Don't imply a scraper "works" when its only grounding is a rendered
preview — this is the same lesson as #1, just for a session where even the raw fetch
you'd normally use to double-check the preview isn't available either.

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

### Wellington (added in an earlier session)
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

### Westlake (added this session — UNCONFIRMED, see Goals for Next Session)
- **City of Westlake**, `westlakegov.com/meetings`. Identified via a rendered-preview
  fetch as running **MuniCode's meetings portal** (`meetings.municode.com`, jurisdiction
  slug `WESTLAKEFL`) — evidence: "a municode design" in the footer, document URLs on
  `mccmeetings.blob.core.usgovcloudapi.net`, agenda HTML links of the form
  `meetings.municode.com/adaHtmlDocument/index?cc=WESTLAKEFL&me=<meeting_id>&ip=...`.
  This is a brand-new platform for this project (not CivicPlus/Legistar/Granicus).
- **Per the rendered preview** (unconfirmed — see Lesson 2 above): the page is a single
  server-rendered HTML `<table>` (no iframe, no separate JS-driven API call was
  detected) with columns Date, Meeting, Agendas, Packets, Minutes, Video/Audio, View.
  Sample row: `09/01/2026 - 6:00pm | City Council Regular Meeting | [agenda links] | ...`.
- **`scrape_westlake()` was written defensively** against this unconfirmed structure:
  it tries several table-selector fallbacks (id/class containing "meeting", else the
  largest `<table>` on the page by row count), locates the "Agendas" column by matching
  header cell text rather than a hardcoded column index, and — if no table is found at
  all — dumps a raw HTML slice anchored on the string `"City Council"` to the log for
  debugging. It also prints the detected header row, row count, and the first data
  row's raw HTML unconditionally, specifically so the first real run's log can confirm
  or correct these assumptions.
- **This session's sandbox had no direct network route to `westlakegov.com`** (a plain
  `curl` from the assistant's shell was rejected by an egress/org policy) — meaning
  `scrape_westlake()` could not be checked against real raw HTML at all this session,
  only against a rendered-preview tool's summary of the page. **Treat it as an
  unconfirmed first draft.** See Goals for Next Session, item 1.
- No new whitelist entries were needed — Westlake's visible meeting types (City
  Council Regular/Special Meeting, City Council Workshop, City Council Budget
  Workshop/Hearing, Education Advisory Board Meeting, Local Planning Agency Meeting)
  are covered by the existing `City Council` / `Planning ... ` patterns already in
  `is_qualifying_event`. **Education Advisory Board** and **Local Planning Agency**
  meetings will currently be *excluded* by the whitelist as written (neither matches
  any existing pattern) — worth asking the user whether either should qualify, since
  they did appear on Westlake's real meetings list.

## Global Feature: "No Agenda Available" events (added this session, applies project-wide)
Per explicit user instruction, this is a project-wide policy, not Westlake-specific:
**an event with confirmed date/time/title but no agenda document yet posted should
still be included on the calendar**, not dropped — flagged so the frontend can be
honest about it instead of showing a dead/broken link.

- **Data contract**: every event dict in `scraper.py` may include `"has_agenda":
  True/False`. All pre-existing scrapers (PBG/Boca/Boynton/Delray/PBC/WPB/Wellington)
  don't set this field and are treated as `True` by default in the frontend, since they
  have always resolved to a usable link. `scrape_westlake()` sets it explicitly per
  event based on whether a real `<a>` link was found in that row's Agendas column.
- **Critical detail — the fallback link, not just the flag**: when `has_agenda` is
  `False`, `"link"` must NOT be left pointing at a dead/guessed agenda URL. It should
  point at **the general source page the event was found on** (e.g. for Westlake,
  `https://www.westlakegov.com/meetings` itself) so the event stays genuinely
  clickable and useful instead of leading to a bad page. This was a real bug caught and
  fixed this session: an earlier version made the frontend not-clickable when there was
  no agenda, which was actually *worse* than just pointing at the source page.
- **Frontend (`index.html`)**: `has_agenda !== false` gates all of this (defends
  against old scrapers that never set the field). When false:
  - The FullCalendar event's `url` is still set to `e.link` (the source page) — it
    stays clickable, it just doesn't go to a specific agenda doc.
  - The list view renders the link as `<a>` text reading "No Agenda Available" instead
    of "View Agenda →", styled muted/italic via a `.event-link-disabled` class — still
    a real, clickable anchor to `e.link`, not a disabled/dead span.
  - The custom hover tooltip's bottom line reads "No Agenda Available" instead of the
    agenda hint.
- **Tooltip hint wording lesson (also caught and fixed this session)**: the hover
  tooltip is a non-interactive popup — the user's mouse cannot move onto it before it
  closes (it hides on the underlying event's `mouseleave`), so anything in it that
  *looks* like a clickable link/button is deceptive. The `.tt-hint` line originally
  read "View Agenda →" styled in the same blue as real links, which read as a broken
  clickable element. Fixed by (a) rewording to **"Click to view agenda"** — describing
  the action on the actual calendar event, not implying the tooltip itself is
  clickable — and (b) restyling `.tt-hint` to muted/italic (`var(--text-muted)`)
  instead of the link-blue (`var(--primary-hover)`), so nothing inside the tooltip
  visually implies it's interactive. **Apply this same standard to any other
  tooltip/hint text added in the future**: never word or style static, non-hoverable
  UI as if it's clickable.

## Filtering Logic (`is_qualifying_event`)
Inclusive whitelist only - matches specific named governance bodies instead of generic
words, to stay maintainable (an earlier broad-keyword + exclude-list approach was
abandoned as an unmaintainable whack-a-mole). Current whitelist:
- City Council, City Commission, Town Council, Village Council (added for Wellington)
- Board of County Commissioners, BCC
- Community Redevelopment Agency, CRA
- Planning and Zoning, Planning Board/Commission, Zoning Board/Commission/Board of
  Appeals, Board of Adjustment
- Downtown Development Authority, Housing Authority, Airport Authority
- Council/Commission Workshop, Council/Commission Agenda Review (added for
  Wellington), Mayor/Commission Work Session, Public Hearing, Town Hall (all qualified
  by the body name so bare "Workshop"/"Hearing"/"Agenda Review" can't match alone)
- **Explicitly dropped per user request - not to be re-added unless asked:** Special
  Magistrate, Code Enforcement Board, Historic Preservation Board, Community Appearance
  Board, CIP Workshop (Wellington-specific)
- Tested against 24+ real titles from PBG/Boca/Boynton/PBC/Delray in an earlier session,
  plus all 4 real Wellington Council-calendar titles (3 qualify, CIP Workshop correctly
  excluded) - verified via an end-to-end mocked test of `scrape_wellington()`, not just
  the regex in isolation.
- **Not yet resolved**: Westlake's real "Education Advisory Board Meeting" and "Local
  Planning Agency Meeting" titles don't match any current pattern and will be silently
  excluded as of this handoff — flag to the user rather than guessing whether to add
  patterns for these.

## Frontend (`index.html`) Features
- **`dayMaxEvents: 3` + `moreLinkClick: 'popover'`** - days with more than 3 meetings
  collapse into a "+N more" link with a popover, instead of listing everything inline.
- **Custom styled hover tooltip** (not FullCalendar's native/browser tooltip) - shows
  muni badge, full title, date/time, summary, and a status hint ("Click to view
  agenda" or "No Agenda Available"), positioned near the cursor and clamped to stay
  within the viewport. Wired via `eventMouseEnter`/`eventMouseLeave`, using
  `extendedProps` attached to each calendar event. **This tooltip is not itself
  hoverable/interactive** - see the "No Agenda Available" section above before adding
  any new content to it that might read as clickable.
- **Sticky calendar header:** the month/year toolbar (`.fc-header-toolbar`) is confirmed
  working with `position: sticky; top: 0`. The weekday row (Sun-Sat) fix (targeting
  `.fc-scrollgrid-section-header` directly instead of assuming a `<td>` wrapper) was
  applied but **still not yet visually confirmed by the user** - if raised again, ask
  for the actual class name(s) on the weekday row from browser dev tools rather than
  guessing further.

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
  `res.text` a real scraper run receives (see Key Methodological Lesson #1 above). This
  bit Wellington once already, and is the reason Westlake's scraper is flagged
  unconfirmed.
- **The coding sandbox's own shell may have zero outbound network access**, separate
  from and in addition to the above - confirmed this session when a plain `curl` to
  `westlakegov.com` was rejected outright by an egress/org policy. Don't assume a
  direct fetch is possible just because a rendered-preview tool is; check explicitly,
  and say plainly when neither raw-HTML fetch nor test-execution against the live site
  was possible. The most trustworthy confirmations (PBC, Delray, Wellington) all came
  from the user's own GitHub Actions log output, not from the assistant's own test
  runs, and that remains the reliable path.

## Goals for Next Session
1. **Confirm or fix `scrape_westlake()` against a real run.** Ask the user to run the
   GitHub Actions workflow (or `python scraper.py` locally) and paste back the
   `[Westlake]`-prefixed log lines. The function logs the detected table header row,
   row count, agenda-column index, and a sample raw row specifically for this purpose.
   If the real structure differs from what's assumed, rebuild the selectors from that
   real log output, not from another rendered-preview guess.
2. Resolve whether Westlake's "Education Advisory Board Meeting" and "Local Planning
   Agency Meeting" should be added to the whitelist - they're real recurring meeting
   types on Westlake's calendar that don't currently qualify. Ask the user rather than
   guessing.
3. Add the next new municipality - **use Key Methodological Lessons #1 and #2 above
   from the start**: don't assume a web-fetch tool's rendered preview reflects the real
   HTML, and don't assume the sandbox has live network access to the target site either
   - check both explicitly. If the platform/mechanism is at all unclear, build in a
   debug-print step early and confirm against a real GitHub Actions log before writing
   the "final" parser. When adding an event with no agenda link, follow the "No Agenda
   Available" pattern above: set `has_agenda: False` and point `link` at the general
   source page, never a broken/guessed URL.
4. If Wellington's event count ever looks wrong (too few, too many, or the CID=22 vs
   CID=29 mix-up resurfaces), revisit the CID=29-vs-CID=22 scope question directly with
   the user rather than guessing which calendar is intended.
5. Confirm the weekday-header sticky fix actually renders correctly in-browser; if not,
   get the actual class name(s) on the weekday row from browser dev tools and target
   that directly instead of guessing further.
6. Keep an eye out for new false-positive/false-negative meeting titles as any
   municipality's calendar gets scraped over time - extend the whitelist additively
   (never re-add a dropped keyword, including Wellington's CIP Workshop, without being
   asked).
7. General code hygiene: `scrape_civicplus_calendar()` is shared by PBG/Boca/Boynton;
   `scrape_wellington()` and `scrape_westlake()` are their own functions since their
   mechanisms don't match the generic CivicPlus list view. If a new CivicPlus
   municipality's `?view=list&year=&month=` genuinely returns a full month list (like
   PBG/Boca/Boynton), prefer reusing `scrape_civicplus_calendar()` with an optional
   parameter over forking it again; if it behaves like Wellington's (single-day
   drilldown), a bespoke function following `scrape_wellington()`'s Schema.org-microdata
   approach is the proven pattern to reach for first. If a new municipality turns out
   to run MuniCode's meetings portal (like Westlake), reuse/generalize
   `scrape_westlake()`'s table-parsing approach once it's confirmed working, rather
   than writing a third HTML-table parser from scratch.
