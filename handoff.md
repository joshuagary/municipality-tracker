# Municipality Tracker — Session Handoff {#municipality-tracker-session-handoff}

## Your Role {#your-role}

You are a coding assistant helping build and debug **`municipality-tracker`**, a Python
scraper \+ static HTML frontend that tracks public government meetings across South
Florida municipalities (plus Westlake, added this session — see below). The person
you're working with is non\-technical\-ish but hands\-on: they paste real GitHub Actions
logs, spot specific wrong meetings by name, and want precise, tested fixes — not
guesses. Verify claims (fetch real pages, test regex against real HTML, not just a
rendered preview of it) before asserting something works, and be upfront when
something can't be verified without a live browser or the user's own CI logs. See
"Key Methodological Lessons" below before writing any new scraper — it covers real
mistakes made (and caught) across sessions that are worth not repeating.

## Project Overview {#project-overview}

`scraper.py` runs daily via GitHub Actions (`.github/workflows/scrape.yml`), fetches
public meeting schedules for each municipality, filters for governance\-relevant
meetings, normalizes dates/times to ISO format, and writes `data.json`. `index.html` is
a static frontend (FullCalendar 6 \+ vanilla JS) that reads `data.json` and renders a
searchable calendar/list view, hosted directly from the repo (e.g. GitHub Pages).

**Files to re\-upload to the new chat for full context:** the current `scraper.py` and
`index.html` from this repo — this document summarizes their state and history but
doesn't reproduce the full code.

## Municipality Status (as of last confirmed GitHub Actions run) {#municipality-status-as-of-last-confirmed-github-actions-run}

\| Muni | Platform | Mechanism | Status |\\n| \-\-\- | \-\-\- | \-\-\- | \-\-\- |\\n| Palm Beach Gardens (PBG) | CivicPlus (`pbgfl.gov`) | `calendar.aspx?view=list&year=&month=` | ✅ Working |\\n| Delray Beach | Legistar | `webapi.legistar.com/v1/delraybeach/events` JSON API | ✅ Working (confirmed 9 events) |\\n| Palm Beach County (PBC) | SharePoint (`discover.pbc.gov`) | Regex\-parses `Agenda_Master/YYYYMMDD.pdf` links | ✅ Working (confirmed 2 events) |\\n| Boca Raton | CivicPlus (`myboca.us`) | Same `calendar.aspx?view=list` mechanism as PBG | ✅ Working (confirmed 16 events) |\\n| Boynton Beach | CivicPlus (`boynton-beach.org`) | Same `calendar.aspx?view=list` mechanism as PBG | ✅ Working (confirmed 12 events) |\\n| West Palm Beach (WPB) | Granicus OpenCities (`wpb.org`) | Static per\-series pages listing every date under a "When" heading | ✅ Working (confirmed 8 events) |\\n| Wellington | CivicPlus (`wellingtonfl.gov`) | Own function, `scrape_wellington()` — Schema.org microdata | ✅ Working (confirmed 3 events, CIP Workshop correctly excluded) |\\n| **Westlake** | **MuniCode (`meetings.municode.com`, jurisdiction `WESTLAKEFL`)** | **Own function, `scrape_westlake()` — HTML table parse, see below** | ⚠️ **First\-pass draft — NOT yet confirmed against a real GitHub Actions log (see below)** |\\n| **Downtown WPB DDA** | **WordPress (`downtownwpb.com/dda/board-meetings/`)** | **Own function, `scrape_downtown_wpb_dda()` — `<li>` date\-list parse, see below** | ⚠️ **First\-pass draft — NOT yet confirmed against a real GitHub Actions log (see below)** |\\n| **City of Palm Beach** (Town of Palm Beach) | **CivicClerk / CivicPlus "Meetings Select" portal (`palmbeachfl.portal.civicclerk.com`)** | **Own function, `scrape_palm_beach()` — real OData JSON API \+ user\-confirmed agenda\-link pattern, see below** | ⚠️ **Events \+ agenda links confirmed working by the user for real events (Sept 1 example) — fallback link for events with no agenda yet still unconfirmed; not yet confirmed against a full real GitHub Actions log** |\\n| **Town of Jupiter** | **CivicPlus (`jupiter.fl.us`), exact behavior unknown** | **Own function, `scrape_jupiter()` — tries both known CivicPlus shapes, see below** | ⚠️ **Least\-confirmed scraper in the project — zero network access of any kind this session, not even a rendered preview** |\\n| **City of Riviera Beach** | **Granicus ViewPublisher (`rivierabeach.granicus.com`, embedded via iframe from `rivierabch.com/ccm`)** | **Own function, `scrape_riviera_beach()` — HTML table row parse (`listingRow`/`AgendaViewer.php`), see below** | ⚠️ **Row markup \+ agenda\-link pattern confirmed real by the user via View Page Source — sandbox never had network access to execute the fetch; not yet confirmed against a real GitHub Actions log** |\\n

### Key Methodological Lessons (read before building the next scraper) {#key-methodological-lessons-read-before-building-the-next-scraper}

**Lesson 1 — a web\-fetch/browsing tool's rendered output is not raw HTML.**
While adding Wellington, an early attempt built a regex parser by fetching the live
page through a **web\-fetch/browsing tool** and reasoning about the markdown\-rendered
text it returned (e.g. `[Event Title](url)` bracket\-link syntax). That regex found 0
matches once it actually ran against the real scraper's output. Root cause: **a
web\-fetch tool's markdown conversion is not the raw HTML.** It synthesizes bracket\-link
syntax for every `<a>` tag that never exists in real HTML — `BeautifulSoup(...).get_text()`
on the real raw response strips tags entirely and produces different text with no
brackets, parens, or embedded hrefs at all. Any regex/structure reasoning built purely
from a web\-fetch tool's rendered preview needs to be treated as a *hypothesis*, not
ground truth, until confirmed against the literal HTML the real scraper receives.

**The reliable way to debug a new municipality's HTML structure going forward:**
temporarily add debug `print()` statements inside the relevant scraper function that
dump a slice of the real `res.text` (raw response body) to the GitHub Actions log —
anchored on a known real, specific piece of content (an actual meeting title, a known
category label) rather than the first N characters (which is usually all
`<head>`/analytics/anti\-forgery boilerplate) or a generic string like `EID=` (which can
match unrelated content elsewhere on the page, e.g. a "Featured Events" carousel
widget). Ask the user to run the workflow once and paste back the new log output, then
build the parser from that literal HTML. This is exactly how Wellington's real
mechanism was found: **CivicPlus embeds a Schema.org microdata block per event**
(`<div itemscope itemtype="http://schema.org/Event">` containing
`<span itemprop="name">` and `<span itemprop="startDate">2026-08-11T18:30:00</span>`),
which is what `scrape_wellington()` now parses directly via BeautifulSoup tag/attribute
selectors — far more reliable than any text\-flattening regex, since it sidesteps
freeform "Location" text entirely (one real Wellington event has a multi\-sentence
childcare sign\-up disclaimer sitting between the visible date and the visible title
repeat, which broke an earlier regex\-based attempt by sweeping that whole disclaimer
into the "title" capture).

**Lesson 2 — the coding sandbox itself may have no outbound network access at all**
(discovered when adding Westlake). Even a plain `curl` to the target domain from the
assistant's own shell can fail outright (connection rejected by an egress/organization
policy), meaning *neither* the rendered\-preview tool's output *nor* a live raw\-HTML
fetch may be available to build against in a given session — only the rendered\-preview
tool. When that happens, say so explicitly, write the scraper defensively (multiple
selector/fallback strategies, generous debug `print()`s of header rows / row counts /
a sample raw row), and be explicit with the user that the function is an **unconfirmed
first\-pass draft** until they run it for real (GitHub Actions or locally) and paste
back the log. Don't imply a scraper "works" when its only grounding is a rendered
preview — this is the same lesson as \#1, just for a session where even the raw fetch
you'd normally use to double\-check the preview isn't available either.

### Key history / root causes fixed in prior sessions {#key-history-root-causes-fixed-in-prior-sessions}

- **Boca & Boynton were never Legistar clients.** The original code (from an earlier
  Gemini session) wired them up as Legistar, which is why they returned 500s/404s/0
  events. Both actually run CivicPlus \- confirmed live ("Government Websites by
  CivicPlus®" appears directly on Boca's site). Both share one generic
  `scrape_civicplus_calendar()` function with PBG instead of three separate paths.
- **WPB is on a third platform entirely** (Granicus OpenCities, not CivicPlus), and its
  main calendar page is JS\-rendered client\-side ("Please wait while we load this
  calendar..." with no server\-side content) \- a plain fetch can't read it. Its
  individual meeting\-series pages are static, though, and list the full year's dates.
- **PBC's domain moved**\: `discover.pbcgov.org` → `discover.pbc.gov`. The new page has
  no clean event list, so the scraper pulls the date straight from agenda PDF filenames.
- **`fetch_hardened()` helper**\: uses `curl_cffi` (already in
  `requirements`/workflow deps) to impersonate a real Chrome TLS fingerprint, since
  plain `requests` gets blocked by some of these sites' WAFs on GitHub Actions IP
  ranges. Falls back to plain `requests` if `curl_cffi` isn't available.

### Wellington (added in an earlier session) {#wellington-added-in-an-earlier-session}

- **Confirmed live**\: Wellington runs standard CivicPlus ("Government Websites by
  CivicPlus®" in the footer), using the exact `calendar.aspx?startDate=&enddate=&CID=& showPastEvents=false` URL format the user provided.
- **But its mechanism doesn't match `scrape_civicplus_calendar()`**\: Wellington's
  `?view=list` is a single\-day drilldown (clicking a date goes to `...&day=6`), not a
  full\-month list like PBG/Boca/Boynton's. The reliable single\-request\-per\-month source
  turned out to be the plain calendar page (which server\-renders a full month grid
  regardless of the `view=` param in practice), parsed via the Schema.org microdata
  blocks described above. Given the mechanism mismatch, Wellington got its own bespoke
  function, `scrape_wellington()`, the same way WPB and PBC did.
- **CID\=29** ("Council Meetings" calendar) is what's being scraped, per the user's
  original URL. Note: **Wellington also has a broader "Meetings" calendar at CID\=22**
  that includes additional governance\-adjacent meeting types (Education Committee,
  Equestrian Preserve Committee, Planning/Zoning & Adjustment Board, Architectural
  Review Board, Code Special Magistrate Hearing, etc.) that CID\=29 does NOT include.
  This was surfaced to the user but not switched to \- worth asking again if broader
  coverage of Wellington's other boards/committees is ever wanted.
- **Whitelist additions made** (see Filtering Logic below): `Village Council` (Wellington
  is a Village, not a City/Town \- none of the existing body\-name patterns covered its
  actual meeting titles) and `Council/Commission Agenda Review` (added per explicit user
  request, for "Wellington Council Agenda Review Meeting").
- **Explicitly NOT added, per user decision**\: `CIP Workshop` \- a real recurring
  Wellington meeting type that does not currently qualify. Do not add without being
  asked again, consistent with the project's whitelist discipline.
- **One unresolved observation, not yet reproduced in a real CI run**\: during early
  manual (non\-CI) verification of Wellington via a web\-fetch tool, one request for
  `CID=29` unexpectedly came back as a different calendar (`CID=22`). This wasn't seen
  in the real GitHub Actions run that ultimately confirmed the working scraper, so it
  may have been an artifact of that tool rather than a real site behavior \- but worth
  keeping an eye on if Wellington's event count ever looks off (e.g. events appearing
  from the "Meetings" calendar that shouldn't be in scope, or events disappearing).

### Westlake (added this session — UNCONFIRMED, see Goals for Next Session) {#westlake-added-this-session-unconfirmed-see-goals-for-next-session}

- **City of Westlake**, `westlakegov.com/meetings`. Identified via a rendered\-preview
  fetch as running **MuniCode's meetings portal** (`meetings.municode.com`, jurisdiction
  slug `WESTLAKEFL`) — evidence: "a municode design" in the footer, document URLs on
  `mccmeetings.blob.core.usgovcloudapi.net`, agenda HTML links of the form
  `meetings.municode.com/adaHtmlDocument/index?cc=WESTLAKEFL&me=<meeting_id>&ip=...`.
  This is a brand\-new platform for this project (not CivicPlus/Legistar/Granicus).
- **Per the rendered preview** (unconfirmed — see Lesson 2 above): the page is a single
  server\-rendered HTML `<table>` (no iframe, no separate JS\-driven API call was
  detected) with columns Date, Meeting, Agendas, Packets, Minutes, Video/Audio, View.
  Sample row: `09/01/2026 - 6:00pm | City Council Regular Meeting | [agenda links] | ...`.
- **`scrape_westlake()` was written defensively** against this unconfirmed structure:
  it tries several table\-selector fallbacks (id/class containing "meeting", else the
  largest `<table>` on the page by row count), locates the "Agendas" column by matching
  header cell text rather than a hardcoded column index, and — if no table is found at
  all — dumps a raw HTML slice anchored on the string `"City Council"` to the log for
  debugging. It also prints the detected header row, row count, and the first data
  row's raw HTML unconditionally, specifically so the first real run's log can confirm
  or correct these assumptions.
- **This session's sandbox had no direct network route to `westlakegov.com`** (a plain
  `curl` from the assistant's shell was rejected by an egress/org policy) — meaning
  `scrape_westlake()` could not be checked against real raw HTML at all this session,
  only against a rendered\-preview tool's summary of the page. **Treat it as an
  unconfirmed first draft.** See Goals for Next Session, item 1.
- No new whitelist entries were needed — Westlake's visible meeting types (City
  Council Regular/Special Meeting, City Council Workshop, City Council Budget
  Workshop/Hearing, Education Advisory Board Meeting, Local Planning Agency Meeting)
  are covered by the existing `City Council` / `Planning ... ` patterns already in
  `is_qualifying_event`. **Education Advisory Board** and **Local Planning Agency**
  meetings will currently be *excluded* by the whitelist as written (neither matches
  any existing pattern) — worth asking the user whether either should qualify, since
  they did appear on Westlake's real meetings list.

### Downtown WPB DDA (added this session — UNCONFIRMED, see Goals for Next Session) {#downtown-wpb-dda-added-this-session-unconfirmed-see-goals-for-next-session}

- **Downtown West Palm Beach DDA** (Downtown Development Authority),
  `downtownwpb.com/dda/board-meetings/` — a WordPress page, not any platform used
  elsewhere in this project. Meets the **3rd Tuesday of each month at 8:30 a.m.**, per
  the user (who confirmed this live from the page).
- **Per the user \+ a rendered\-preview fetch** (unconfirmed against raw HTML — see
  Lesson 1/2 above): the page lists Board meeting packets as a bulleted list of dates,
  grouped by fiscal year. Each date is hyperlinked to an Issuu\-hosted agenda packet
  (e.g. `issuu.com/westpalmdda/docs/dda_board_agenda_packet_august_18_2026`) once
  posted, and left as plain unlinked text (e.g. "September 15, 2026") when the packet
  hasn't been posted yet — as of this session, August has an agenda, September doesn't.
- **This session's sandbox again had no direct network route to `downtownwpb.com`**
  (plain `curl`/`requests` rejected by the egress proxy, same as Westlake) — so
  `scrape_downtown_wpb_dda()` could only be built against a WebFetch rendered preview,
  not real raw HTML. **Treat it as an unconfirmed first draft**, exactly like Westlake.
- **Written defensively**\: primary strategy scans every `<li>` on the page for a
  "Month DD, YYYY" text pattern (not assuming a specific `<ul>`/class); if none match,
  it falls back to a page\-wide scan of `<a>` tags and bare text nodes for the same date
  pattern, and dumps a raw HTML slice anchored on "3rd Tuesday" (or "board meeting") to
  the log for debugging. Logs the `<li>` count, matched\-date count, and a sample raw
  `<li>` unconditionally so the first real run's log can confirm or correct this.
- **Title is hardcoded** to `"Downtown Development Authority (DDA) Board Meeting"`
  (the page itself never states a per\-event title, only dates) — chosen deliberately to
  contain the literal phrase "Downtown Development Authority" so it passes the
  *existing* whitelist entry for that phrase with no `is_qualifying_event` changes
  needed.
- **Time is hardcoded to "8:30 AM"** per the user's direct statement — this is the one
  piece of this scraper resting on a stated fact rather than an unverified page\-structure
  guess.
- Follows the project\-wide "No Agenda Available" pattern: `has_agenda: False` \+
  `link` pointing at the board\-meetings page itself (never a guessed/dead Issuu URL)
  when a date's packet isn't posted yet.
- **Mock\-tested end\-to\-end** against a hand\-built HTML fixture matching the
  hypothesized structure (one unlinked `<li>` date, two linked `<li>` dates) —
  confirmed it correctly extracts has\_agenda True/False, the right ISO dates, the
  8:30 AM time, and the fixed title. This confirms the *code logic*, not that the real
  site's HTML actually matches the hypothesis.

### City of Palm Beach (added this session — real API \+ real agenda\-link pattern confirmed) {\#city\-of\-palm\-beach\-added\-this\-session\-discovery\-mode\-no\-real\-scraper\-yet}\\n\\n\- **URL**\: `https://palmbeachfl.portal.civicclerk.com/`. Officially the **Town of\\n  Palm Beach** (the `eventLocation` in the real API response says "Palm Beach, FL";\\n  its governing body is the Town Council per the sample event's location — "Council\\n  Chambers"), not a City — `muni_full` is set to `\"Town of Palm Beach\"` in the code.\\n  Flag to the user if that's not the intended display name.\\n\- **Platform**\: CivicClerk, specifically CivicPlus's newer "Meetings Select" Angular\\n  SPA public portal product — a **different CivicPlus product** from the CivicEngage\\n  `calendar.aspx` sites used by PBG/Boca/Boynton/Wellington, and different again from\\n  MuniCode (Westlake) or WordPress (Downtown WPB DDA) — a fourth distinct platform for\\n  this project. The portal itself is a pure client\-side\-rendered SPA with **no\\n  server\-rendered HTML at all** (confirmed via WebFetch preview: raw page is just\\n  "You need to enable JavaScript to run this app") — unlike Westlake, there was no\\n  markup to hypothesize a parser from.\\n\- **Real API confirmed directly by the user via their own browser DevTools Network tab\\n  capture** (not a guess, not a rendered\-preview hypothesis — this is ground truth,\\n  the same standard as the project's most trusted confirmations like PBC/Delray/\\n  Wellington):\\n  `\n  GET https://palmbeachfl.api.civicclerk.com/v1/Events\n      ?$filter=startDateTime+lt+2026-08-30\n      &$orderby=startDateTime+desc,+eventName+desc\n  `\\n  returns an **unauthenticated** OData JSON body: `{\"@odata.context\": ..., \"value\": [\n  {event}, ... ]}`. No API token/login was needed.\\n\- **Confirmed real fields** on each event object (from the user's captured sample, an\\n  "Architectural Commission Meeting" on 2026\-08\-26): `id`, `eventName`, `eventDate`,\\n  `startDateTime` (UTC, trailing `Z`), `isDeleted`, `isPublished` (`\"Published\"`/\\n  other), **`hasAgenda`** (boolean — the API gives this directly, no need to infer it\\n  from link presence the way Westlake/DDA do), `eventLocation` (address/city/state/\\n  zip), and `publishedFiles`\: a list of `{fileId, type: \"Agenda\"/\"Agenda Packet\"/\n  \"Supplemental Backup\"/..., url: \"stream/PALMBEACHFL/<uuid>.pdf\", ...}`.\\n\- **Agenda link pattern CONFIRMED by the user with a real working example** (Sept 1\\n  event): `https://palmbeachfl.portal.civicclerk.com/event/11384/files/agenda/16662`.\\n  This corrected a wrong first\-pass guess — `publishedFiles[].url` (the relative\\n  blob\-storage path) is **not** what the portal actually links to; the real pattern is\\n  a **portal page**, not a direct file/blob URL:\\n  `\n  {base_domain}/event/{event_id}/files/agenda/{fileId}\n  `\\n  where `{fileId}` is the "Agenda"\-typed entry's **`fileId`** field from\\n  `publishedFiles` (not its `id`, which is always `0` on these sub\-objects). Verified\\n  in code with the user's exact real example (event 11384, fileId 16662) via a smoke\\n  test that reproduces the confirmed URL exactly.\\n\- **`scrape_palm_beach()` is now a real parser**, built directly from the confirmed\\n  API \+ confirmed link pattern:\\n  \- Queries `$filter=startDateTime ge {month-start} and startDateTime lt\n    {lookahead-end}` (reusing the project's existing `get_dual_month_bounds()` window),\\n    `$orderby=startDateTime asc`.\\n  \- Skips `isDeleted` events and anything not `\"Published\"` (permissive if that field\\n    is missing/unrecognized, to avoid over\-dropping).\\n  \- Converts `startDateTime` from UTC to America/New\_York using stdlib `zoneinfo`,\\n    with a manual DST\-rule fallback (2nd Sunday March – 1st Sunday November \= EDT) if\\n    `zoneinfo`'s tzdata isn't available on the runner — this project's other scrapers\\n    never needed timezone conversion since their source pages already show local wall\-\\n    clock time, so this is new logic, worth double\-checking against the real log's\\n    dates/times on the first run.\\n  \- Uses the API's own `hasAgenda` boolean directly (more reliable than Westlake/DDA's\\n    link\-presence inference).\\n  \- Builds the agenda link as `{base_domain}/event/{event_id}/files/agenda/{fileId}`\\n    (confirmed pattern, see above) when an "Agenda"\-typed file is present; otherwise\\n    falls back to `{base_domain}/event/{event_id}` (the event's general portal page —\\n    this fallback pattern itself is still **unconfirmed**, worth checking on a real\\n    run for an event that has `hasAgenda: false`).\\n  \- Runs through the existing `is_qualifying_event()` whitelist like every other\\n    scraper — the one real sample event seen so far, "Architectural Commission\\n    Meeting," does **not** currently qualify (doesn't match any existing pattern) and\\n    will be silently excluded. Not added without asking — same discipline as\\n    Westlake's Education Advisory Board/Local Planning Agency question. "Town Council"\\n    meetings, if/when they appear in a real run, will already qualify via the existing\\n    `Town Council` pattern.\\n  \- **Logic smoke\-tested** against a mocked version of the real captured JSON shape,\\n    including a reproduction of the user's own confirmed Sept 1 example (event 11384,\\n    fileId 16662) — the generated link matches\\n    `https://palmbeachfl.portal.civicclerk.com/event/11384/files/agenda/16662` exactly.\\n    Also confirmed correct whitelist filtering, correct UTC→Eastern conversion\\n    (09:00 UTC → 5:00 AM EDT; 18:00 UTC → 2:00 PM EDT), and correct dedup. This\\n    confirms the code logic against the real samples seen, not that every event in a\\n    full real run will parse cleanly — still worth confirming against a real GitHub\\n    Actions log, particularly the `hasAgenda: false` fallback link, which remains\\n    unconfirmed.\\n\- Wired into `main()` already, so it runs as part of the normal daily job.\\n {#city-of-palm-beach-added-this-session-real-api-real-agenda-link-pattern-confirmed-city-of-palm-beach-added-this-session-discovery-mode-no-real-scraper-yetnn-url-httpspalmbeachflportalcivicclerkcom-officially-the-town-ofn-palm-beach-the-eventlocation-in-the-real-api-response-says-palm-beach-fln-its-governing-body-is-the-town-council-per-the-sample-events-location-counciln-chambers-not-a-city-muni_full-is-set-to-town-of-palm-beach-in-the-coden-flag-to-the-user-if-thats-not-the-intended-display-namen-platform-civicclerk-specifically-civicpluss-newer-meetings-select-angularn-spa-public-portal-product-a-different-civicplus-product-from-the-civicengagen-calendaraspx-sites-used-by-pbgbocaboyntonwellington-and-different-again-fromn-municode-westlake-or-wordpress-downtown-wpb-dda-a-fourth-distinct-platform-forn-this-project-the-portal-itself-is-a-pure-client-side-rendered-spa-with-non-server-rendered-html-at-all-confirmed-via-webfetch-preview-raw-page-is-justn-you-need-to-enable-javascript-to-run-this-app-unlike-westlake-there-was-non-markup-to-hypothesize-a-parser-fromn-real-api-confirmed-directly-by-the-user-via-their-own-browser-devtools-network-tabn-capture-not-a-guess-not-a-rendered-preview-hypothesis-this-is-ground-truthn-the-same-standard-as-the-projects-most-trusted-confirmations-like-pbcdelrayn-wellingtonn-n-get-httpspalmbeachflapicivicclerkcomv1eventsn-filterstartdatetimelt2026-08-30n-orderbystartdatetimedesceventnamedescn-n-returns-an-unauthenticated-odata-json-body-odatacontext-value-n-event-no-api-tokenlogin-was-neededn-confirmed-real-fields-on-each-event-object-from-the-users-captured-sample-ann-architectural-commission-meeting-on-2026-08-26-id-eventname-eventdaten-startdatetime-utc-trailing-z-isdeleted-ispublished-publishedn-other-hasagenda-boolean-the-api-gives-this-directly-no-need-to-infer-itn-from-link-presence-the-way-westlakedda-do-eventlocation-addresscitystaten-zip-and-publishedfiles-a-list-of-fileid-type-agendaagenda-packetn-supplemental-backup-url-streampalmbeachfluuidpdf-n-agenda-link-pattern-confirmed-by-the-user-with-a-real-working-example-sept-1n-event-httpspalmbeachflportalcivicclerkcomevent11384filesagenda16662n-this-corrected-a-wrong-first-pass-guess-publishedfilesurl-the-relativen-blob-storage-path-is-not-what-the-portal-actually-links-to-the-real-pattern-isn-a-portal-page-not-a-direct-fileblob-urln-n-base_domaineventevent_idfilesagendafileidn-n-where-fileid-is-the-agenda-typed-entrys-fileid-field-fromn-publishedfiles-not-its-id-which-is-always-0-on-these-sub-objects-verifiedn-in-code-with-the-users-exact-real-example-event-11384-fileid-16662-via-a-smoken-test-that-reproduces-the-confirmed-url-exactlyn-scrape_palm_beach-is-now-a-real-parser-built-directly-from-the-confirmedn-api-confirmed-link-patternn-queries-filterstartdatetime-ge-month-start-and-startdatetime-ltn-lookahead-end-reusing-the-projects-existing-get_dual_month_bounds-windown-orderbystartdatetime-ascn-skips-isdeleted-events-and-anything-not-published-permissive-if-that-fieldn-is-missingunrecognized-to-avoid-over-droppingn-converts-startdatetime-from-utc-to-americanew_york-using-stdlib-zoneinfon-with-a-manual-dst-rule-fallback-2nd-sunday-march-1st-sunday-november-edt-ifn-zoneinfos-tzdata-isnt-available-on-the-runner-this-projects-other-scrapersn-never-needed-timezone-conversion-since-their-source-pages-already-show-local-wall-n-clock-time-so-this-is-new-logic-worth-double-checking-against-the-real-logsn-datestimes-on-the-first-runn-uses-the-apis-own-hasagenda-boolean-directly-more-reliable-than-westlakeddasn-link-presence-inferencen-builds-the-agenda-link-as-base_domaineventevent_idfilesagendafileidn-confirmed-pattern-see-above-when-an-agenda-typed-file-is-present-otherwisen-falls-back-to-base_domaineventevent_id-the-events-general-portal-page-n-this-fallback-pattern-itself-is-still-unconfirmed-worth-checking-on-a-realn-run-for-an-event-that-has-hasagenda-falsen-runs-through-the-existing-is_qualifying_event-whitelist-like-every-othern-scraper-the-one-real-sample-event-seen-so-far-architectural-commissionn-meeting-does-not-currently-qualify-doesnt-match-any-existing-pattern-andn-will-be-silently-excluded-not-added-without-asking-same-discipline-asn-westlakes-education-advisory-boardlocal-planning-agency-question-town-counciln-meetings-ifwhen-they-appear-in-a-real-run-will-already-qualify-via-the-existingn-town-council-patternn-logic-smoke-tested-against-a-mocked-version-of-the-real-captured-json-shapen-including-a-reproduction-of-the-users-own-confirmed-sept-1-example-event-11384n-fileid-16662-the-generated-link-matchesn-httpspalmbeachflportalcivicclerkcomevent11384filesagenda16662-exactlyn-also-confirmed-correct-whitelist-filtering-correct-utceastern-conversionn-0900-utc-500-am-edt-1800-utc-200-pm-edt-and-correct-dedup-thisn-confirms-the-code-logic-against-the-real-samples-seen-not-that-every-event-in-an-full-real-run-will-parse-cleanly-still-worth-confirming-against-a-real-githubn-actions-log-particularly-the-hasagenda-false-fallback-link-which-remainsn-unconfirmedn-wired-into-main-already-so-it-runs-as-part-of-the-normal-daily-jobn}

### Town of Jupiter (added this session — UNCONFIRMED, zero network access at all) {#town-of-jupiter-added-this-session-unconfirmed}

- **URL**\: `https://www.jupiter.fl.us/calendar.aspx?CID=35`. The user referred to it as
  "City of Jupiter," but Jupiter, FL is legally a **Town** — `muni_full` is set to
  `"Town of Jupiter"` in the code. Flag to the user if "City of Jupiter" is actually
  the preferred display name.
- **Platform**\: the `calendar.aspx?CID=N` URL shape strongly suggests CivicPlus/
  CivicEngage, the same platform family as PBG/Boca/Boynton/Wellington — but which of
  the two known CivicPlus behaviors it uses is **not confirmed**.
- **This session had zero outbound network access to jupiter.fl.us at all** — worse
  than the Westlake/Downtown WPB DDA sessions, which at least had a WebFetch rendered
  preview to hypothesize from. Here, both a direct `curl` (rejected by an egress/WAF
  policy, HTTP 403 at the proxy) **and** the WebFetch tool failed outright — WebFetch
  couldn't even fetch/parse the site's `robots.txt` (confirmed WebFetch itself works
  fine generally, e.g. against `example.com`, so this is specific to jupiter.fl.us).
  **No rendered preview and no raw HTML were available this session — nothing about
  the real page structure has been observed.**
- **`scrape_jupiter()` was written defensively** to try both known CivicPlus shapes
  per month and fall back gracefully:
  1. Wellington\-style: fetch `calendar.aspx?startDate=&enddate=&CID=35` and look for
     `<div itemscope itemtype="http://schema.org/Event">` blocks.
  2. If none found, fall back to the PBG/Boca/Boynton\-style
     `?view=list&year=&month=&CID=35` page and look for `EID=` row links.
  3. If neither yields anything, dumps a raw HTML slice anchored on the literal string
     "Jupiter" (guaranteed to appear somewhere real on the page) plus block/row counts
     for both attempts, so the real structure can be identified from one real log.
- **Logic smoke\-tested** against two hand\-built HTML fixtures (one schema.org\-style,
  one EID\-list\-style) — confirmed correct whitelist filtering (a "Town Council"\-style
  title qualifies, a "Financial Advisory Board"/"Parks Advisory Board" title is
  correctly excluded), correct date/time extraction, and correct link construction for
  both strategies. **This confirms only the code logic against hypothesized shapes —
  it says nothing about whether jupiter.fl.us actually uses either shape.** Treat this
  as the least\-confirmed scraper in the project so far — even more speculative than
  Westlake/DDA — until a real GitHub Actions run's `[Jupiter]`\-prefixed log lines come
  back.
- Wired into `main()` already, so it runs as part of the normal daily job (it will
  either extract real events, extract nothing, or hit the debug dump — all three are
  informative outcomes for the next session).
- **Known limitation, confirmed by the user in this session — `has_agenda` is never
  set.** Unlike Westlake/Downtown WPB DDA/Palm Beach, `scrape_jupiter()` does not set
  the `"has_agenda"` field on its events at all. The frontend's `has_agenda !== false`
  gate treats any event without the field as **having** an agenda (this default exists
  so the original pre\-"No Agenda Available" scrapers \- PBG/Boca/Boynton/Delray/PBC/
  WPB \- keep working, since for them the assumption was always true). For Jupiter this
  default is wrong: the user confirmed live that none of Jupiter's events currently
  have a posted agenda, yet the calendar/tooltip still shows "Click to view agenda" for
  them. Root cause: `scrape_jupiter()` was written completely blind (no raw HTML, no
  rendered preview \- see above) and there was no way to know what an
  agenda\-not\-yet\-posted row even looks like on Jupiter's real page, so the field was
  left out rather than guessed. **Explicit user decision this session: leave as\-is for
  now** \- the user doesn't know Jupiter's real "no agenda" markup either, so there's
  nothing to build the detection from yet. Do not silently hardcode `has_agenda: False`
  or guess a detection rule without asking first. The fix, when someone can provide it:
  either (a) a raw HTML/DevTools snippet of an agenda\-less Jupiter row so a real
  `has_agenda` check can be added the way Westlake/DDA/Palm Beach have one, or (b) the
  first real GitHub Actions log dump (see above), which may reveal it incidentally.

### City of Riviera Beach (added this session — real markup confirmed by user, sandbox execution still unconfirmed) {#city-of-riviera-beach-added-this-session}

- **URL**: `https://www.rivierabch.com/ccm`. The city's own site runs QScend (not a
  platform seen elsewhere in this project), but it doesn't serve the meetings list
  itself — `/ccm` embeds an `<iframe>` pointing directly at
  `https://rivierabeach.granicus.com/ViewPublisher.php?view_id=1`, a standard
  Granicus "Legislative Management" **ViewPublisher** page — a different Granicus
  product from WPB's OpenCities per-series static pages. This is a **sixth distinct
  platform** for this project (CivicPlus/CivicEngage, Legistar, SharePoint/PDF,
  Granicus OpenCities, MuniCode, WordPress, CivicClerk, and now Granicus
  ViewPublisher).
- **Real markup confirmed directly by the user via View Page Source on the live
  granicus.com page** (not a WebFetch rendered-preview hypothesis — same standard as
  PBC/Delray/Wellington/Palm Beach's confirmed pieces). The user pasted the literal
  raw row:
  ```html
  <tr class="listingRow">
    <td class="listItem" headers="Name" id="City-Council" scope="row">City Council</td>
    <td class="listItem" headers="Date City-Council">Aug&nbsp;19,&nbsp;2026 - 06:00&nbsp;PM</td>
    <td class="listItem" headers="Duration City-Council">04h&nbsp;00m</td>
    <td class="listItem"><a href="//rivierabeach.granicus.com/AgendaViewer.php?view_id=1&event_id=626" target="_blank">Agenda</a></td>
    <td class="listItem">&nbsp;</td>  <!-- Minutes column, blank when not posted -->
  </tr>
  ```
  This confirms the column layout (Name, Date, Duration, Agenda, Minutes) and the
  real agenda-link pattern: `//rivierabeach.granicus.com/AgendaViewer.php?view_id=1&event_id={N}`
  (protocol-relative).
- **What's NOT confirmed**: this session's sandbox has zero outbound network route to
  `granicus.com` at all — a plain `curl` was rejected by the egress policy, and a
  separate WebFetch rendered-preview attempt was blocked by that domain's
  `robots.txt`. So `fetch_hardened()` in `scrape_riviera_beach()` has never actually
  been executed against the real page — only logic-tested against a fixture built
  from the exact real row above (plus a synthetic "no agenda yet" row and an
  out-of-range row), confirming correct date/time parsing, agenda-link construction,
  the "No Agenda Available" fallback link, month-range filtering, and whitelist
  filtering. **Treat this as an unconfirmed-by-execution first draft**, same
  category as Westlake/Jupiter, but with stronger markup grounding than either.
- **Written defensively** per Key Methodological Lesson #2: agenda detection
  searches for an `<a href>` matching `AgendaViewer\.php` anywhere in the row rather
  than a hardcoded column index, so it survives column reordering (Duration/Agenda/
  Minutes/Video order isn't guaranteed stable beyond the one confirmed sample). Falls
  back to matching any `<td class="listItem">`-containing row if the exact
  `listingRow` class doesn't match, and dumps a raw HTML slice anchored on
  `"listingRow"`/`"AgendaViewer"`/`"City Council"` if nothing is found at all. Logs
  HTTP status, row count, and the first row's raw HTML unconditionally.
- **Whitelist note — not yet resolved, flag to user rather than guessing**: real
  meeting-name values seen in the user's screenshot include "Utility Special
  District", "Utility Special District Budget Workshop", "FY2027 Budget Workshop",
  and "Community Awards and Presentations Program" — none of these match any current
  `is_qualifying_event` pattern and will be silently excluded, same discipline as
  Westlake's Education Advisory Board / Palm Beach's Architectural Commission
  questions. "City Council", "City Council Budget Workshop" (contains "City
  Council"), "Community Redevelopment Agency", and "Planning and Zoning Board
  Meeting" all already qualify with no whitelist changes needed.
- `muni_short` is `"RIVBEACH"`, `muni_full` is `"City of Riviera Beach"`.
- Wired into `main()` already, so it runs as part of the normal daily job.

## Global Feature: "No Agenda Available" events (added this session, applies project\-wide) {#global-feature-no-agenda-available-events-added-this-session-applies-project-wide}

Per explicit user instruction, this is a project\-wide policy, not Westlake\-specific:
**an event with confirmed date/time/title but no agenda document yet posted should
still be included on the calendar**, not dropped — flagged so the frontend can be
honest about it instead of showing a dead/broken link.

- **Data contract**\: every event dict in `scraper.py` may include `"has_agenda": True/False`. All pre\-existing scrapers (PBG/Boca/Boynton/Delray/PBC/WPB/Wellington)
  don't set this field and are treated as `True` by default in the frontend, since they
  have always resolved to a usable link. `scrape_westlake()` sets it explicitly per
  event based on whether a real `<a>` link was found in that row's Agendas column.
- **Critical detail — the fallback link, not just the flag**\: when `has_agenda` is
  `False`, `"link"` must NOT be left pointing at a dead/guessed agenda URL. It should
  point at **the general source page the event was found on** (e.g. for Westlake,
  `https://www.westlakegov.com/meetings` itself) so the event stays genuinely
  clickable and useful instead of leading to a bad page. This was a real bug caught and
  fixed this session: an earlier version made the frontend not\-clickable when there was
  no agenda, which was actually *worse* than just pointing at the source page.
- **Frontend (`index.html`)**\: `has_agenda !== false` gates all of this (defends
  against old scrapers that never set the field). When false:
  - The FullCalendar event's `url` is still set to `e.link` (the source page) — it
    stays clickable, it just doesn't go to a specific agenda doc.
  - The list view renders the link as `<a>` text reading "No Agenda Available" instead
    of "View Agenda →", styled muted/italic via a `.event-link-disabled` class — still
    a real, clickable anchor to `e.link`, not a disabled/dead span.
  - The custom hover tooltip's bottom line reads "No Agenda Available" instead of the
    agenda hint.
- **Tooltip hint wording lesson (also caught and fixed this session)**\: the hover
  tooltip is a non\-interactive popup — the user's mouse cannot move onto it before it
  closes (it hides on the underlying event's `mouseleave`), so anything in it that
  *looks* like a clickable link/button is deceptive. The `.tt-hint` line originally
  read "View Agenda →" styled in the same blue as real links, which read as a broken
  clickable element. Fixed by (a) rewording to **"Click to view agenda"** — describing
  the action on the actual calendar event, not implying the tooltip itself is
  clickable — and (b) restyling `.tt-hint` to muted/italic (`var(--text-muted)`)
  instead of the link\-blue (`var(--primary-hover)`), so nothing inside the tooltip
  visually implies it's interactive. **Apply this same standard to any other
  tooltip/hint text added in the future**\: never word or style static, non\-hoverable
  UI as if it's clickable.

## Filtering Logic (`is_qualifying_event`) {#filtering-logic-is_qualifying_event}

Inclusive whitelist only \- matches specific named governance bodies instead of generic
words, to stay maintainable (an earlier broad\-keyword \+ exclude\-list approach was
abandoned as an unmaintainable whack\-a\-mole). Current whitelist:

- City Council, City Commission, Town Council, Village Council (added for Wellington)
- Board of County Commissioners, BCC
- Community Redevelopment Agency, CRA
- Planning and Zoning, Planning Board/Commission, Zoning Board/Commission/Board of
  Appeals, Board of Adjustment
- Downtown Development Authority, Housing Authority, Airport Authority
- Council/Commission Workshop, Council/Commission Agenda Review (added for
  Wellington), Mayor/Commission Work Session, Public Hearing, Town Hall (all qualified
  by the body name so bare "Workshop"/"Hearing"/"Agenda Review" can't match alone)
- **Explicitly dropped per user request \- not to be re\-added unless asked:** Special
  Magistrate, Code Enforcement Board, Historic Preservation Board, Community Appearance
  Board, CIP Workshop (Wellington\-specific)
- Tested against 24\+ real titles from PBG/Boca/Boynton/PBC/Delray in an earlier session,
  plus all 4 real Wellington Council\-calendar titles (3 qualify, CIP Workshop correctly
  excluded) \- verified via an end\-to\-end mocked test of `scrape_wellington()`, not just
  the regex in isolation.
- **Not yet resolved**\: Westlake's real "Education Advisory Board Meeting" and "Local
  Planning Agency Meeting" titles don't match any current pattern and will be silently
  excluded as of this handoff — flag to the user rather than guessing whether to add
  patterns for these.

## Frontend (`index.html`) Features {#frontend-indexhtml-features}

- **`dayMaxEvents: 3` \+ `moreLinkClick: 'popover'`** \- days with more than 3 meetings
  collapse into a "\+N more" link with a popover, instead of listing everything inline.
- **Custom styled hover tooltip** (not FullCalendar's native/browser tooltip) \- shows
  muni badge, full title, date/time, summary, and a status hint ("Click to view
  agenda" or "No Agenda Available"), positioned near the cursor and clamped to stay
  within the viewport. Wired via `eventMouseEnter`/`eventMouseLeave`, using
  `extendedProps` attached to each calendar event. **This tooltip is not itself
  hoverable/interactive** \- see the "No Agenda Available" section above before adding
  any new content to it that might read as clickable.
- **Sticky calendar header:** the month/year toolbar (`.fc-header-toolbar`) is confirmed
  working with `position: sticky; top: 0`. The weekday row (Sun\-Sat) fix (targeting
  `.fc-scrollgrid-section-header` directly instead of assuming a `<td>` wrapper) was
  applied but **still not yet visually confirmed by the user** \- if raised again, ask
  for the actual class name(s) on the weekday row from browser dev tools rather than
  guessing further.

## Known Limitations / Things I Can't Verify From My End {#known-limitations-things-i-cant-verify-from-my-end}

- No live browser available in my working environment \- all frontend changes are
  verified via Node.js syntax\-checking the extracted `<script>` block and manual DOM
  reasoning based on FullCalendar's documented/stable class names, not actual visual
  rendering. Visual/interaction bugs need the user to check in\-browser and report back
  with specifics (ideally: exact class names from browser inspector, screenshots, or
  console errors).
- **Any web\-fetch/browsing tool used to "verify live" is rendering a converted preview
  (e.g. markdown), not raw HTML** \- treat anything learned that way as a hypothesis
  about page structure, not a confirmed fact, until it's checked against the literal
  `res.text` a real scraper run receives (see Key Methodological Lesson \#1 above). This
  bit Wellington once already, and is the reason Westlake's scraper is flagged
  unconfirmed.
- **The coding sandbox's own shell may have zero outbound network access**, separate
  from and in addition to the above \- confirmed this session when a plain `curl` to
  `westlakegov.com` was rejected outright by an egress/org policy. Don't assume a
  direct fetch is possible just because a rendered\-preview tool is; check explicitly,
  and say plainly when neither raw\-HTML fetch nor test\-execution against the live site
  was possible. The most trustworthy confirmations (PBC, Delray, Wellington) all came
  from the user's own GitHub Actions log output, not from the assistant's own test
  runs, and that remains the reliable path.

## Goals for Next Session {#goals-for-next-session}

0. **Confirm `scrape_palm_beach()` against a full real GitHub Actions run.** The API
   endpoint/shape AND the agenda\-link pattern are now both confirmed by the user with
   real working examples (see the Palm Beach section above), and the parser is
   logic\-tested to reproduce that exact confirmed link. What's still unconfirmed:
   whether the `has_agenda: False` fallback link (`{base_domain}/event/{id}`) is a
   real working portal page, and whether the UTC\-\>Eastern time conversion produces
   the right wall\-clock times across a full real run (not just the one hand\-checked
   sample). Ask the user to run the workflow and paste back the `[Palm Beach]`\-
   prefixed log lines. Also ask the user whether "Architectural Commission Meeting"
   (the one real title seen so far, currently excluded by the whitelist) or any other
   non\-Town\-Council meeting type should qualify.
1. **Confirm or fix `scrape_westlake()` against a real run.** Ask the user to run the
   GitHub Actions workflow (or `python scraper.py` locally) and paste back the
   `[Westlake]`\-prefixed log lines. The function logs the detected table header row,
   row count, agenda\-column index, and a sample raw row specifically for this purpose.
   If the real structure differs from what's assumed, rebuild the selectors from that
   real log output, not from another rendered\-preview guess.
   1b. **Confirm or fix `scrape_downtown_wpb_dda()` against a real run**, the same way.
   Ask the user to run the workflow and paste back the `[Downtown WPB DDA]`\-prefixed
   log lines; expect 2 events (August with an Issuu agenda link, September with
   `has_agenda: False`) once September's packet is posted, expect it to flip to
   `has_agenda: True` on a subsequent run. If the real `<li>` structure differs, rebuild
   from the real log output.
2. Resolve whether Westlake's "Education Advisory Board Meeting" and "Local Planning
   Agency Meeting" should be added to the whitelist \- they're real recurring meeting
   types on Westlake's calendar that don't currently qualify. Ask the user rather than
   guessing.
3. Add the next new municipality \- **use Key Methodological Lessons \#1 and \#2 above
   from the start**\: don't assume a web\-fetch tool's rendered preview reflects the real
   HTML, and don't assume the sandbox has live network access to the target site either
   - check both explicitly. If the platform/mechanism is at all unclear, build in a
     debug\-print step early and confirm against a real GitHub Actions log before writing
     the "final" parser \- or, per what worked for Palm Beach this session, ask the user
     for a browser DevTools Network\-tab capture directly when the site is a JS SPA with
     no server\-rendered HTML to hypothesize from at all. When Palm Beach's first\-pass
     agenda\-link guess turned out wrong even with a confirmed API response, the fix was
     asking the user for one more concrete real example (a working URL) rather than
     guessing a second time \- the same instinct applies to any single field whose exact
     meaning/URL\-building isn't obvious from a JSON sample alone. When adding an event
     with no agenda link, follow the "No Agenda Available" pattern above: set
     `has_agenda: False` and point `link` at the general source page, never a
     broken/guessed URL.
4. If Wellington's event count ever looks wrong (too few, too many, or the CID\=22 vs
   CID\=29 mix\-up resurfaces), revisit the CID\=29\-vs\-CID\=22 scope question directly with
   the user rather than guessing which calendar is intended.
5. Confirm the weekday\-header sticky fix actually renders correctly in\-browser; if not,
   get the actual class name(s) on the weekday row from browser dev tools and target
   that directly instead of guessing further.
6. Keep an eye out for new false\-positive/false\-negative meeting titles as any
   municipality's calendar gets scraped over time \- extend the whitelist additively
   (never re\-add a dropped keyword, including Wellington's CIP Workshop, without being
   asked).
7. General code hygiene: `scrape_civicplus_calendar()` is shared by PBG/Boca/Boynton;
   `scrape_wellington()` and `scrape_westlake()` are their own functions since their
   mechanisms don't match the generic CivicPlus list view. If a new CivicPlus
   municipality's `?view=list&year=&month=` genuinely returns a full month list (like
   PBG/Boca/Boynton), prefer reusing `scrape_civicplus_calendar()` with an optional
   parameter over forking it again; if it behaves like Wellington's (single\-day
   drilldown), a bespoke function following `scrape_wellington()`'s Schema.org\-microdata
   approach is the proven pattern to reach for first. If a new municipality turns out
   to run MuniCode's meetings portal (like Westlake), reuse/generalize
   `scrape_westlake()`'s table\-parsing approach once it's confirmed working, rather
   than writing a third HTML\-table parser from scratch. If a new municipality turns out
   to run CivicClerk/CivicPlus "Meetings Select" (like Palm Beach), reuse/generalize
   `scrape_palm_beach()`'s OData API\-query approach once it's confirmed working \-
   including its `{base_domain}/event/{id}/files/agenda/{fileId}` agenda\-link pattern,
   which is CivicClerk\-specific and confirmed real, not a guess.
8. **Confirm or fix `scrape_jupiter()` against a real run** \- this session had zero
   network access of any kind to jupiter.fl.us (not even a WebFetch rendered preview),
   so it's the least\-confirmed scraper in the project. Ask the user to run the workflow
   and paste back the `[Jupiter]`\-prefixed log lines; rebuild from whichever strategy's
   debug output actually fired (schema.org blocks found, EID\= rows found, or the raw
   HTML dump). Separately, **`has_agenda` detection is still an open item, left
   unresolved by explicit user decision** this session: Jupiter's events currently show
   "Click to view agenda" even though the user confirmed none of them have a posted
   agenda yet, because `scrape_jupiter()` never sets `has_agenda` at all (the frontend
   defaults a missing field to "has an agenda," which is correct for the older
   pre\-No\-Agenda\-Available scrapers but wrong here). Don't guess a detection rule \-
   wait for either a raw HTML/DevTools snippet of an agenda\-less Jupiter row, or
   whatever the first real GitHub Actions log happens to reveal, then add real
   `has_agenda` logic the way Westlake/DDA/Palm Beach have it.
9. **Confirm `scrape_riviera_beach()` against a real run.** The row markup and the
   agenda-link pattern are both confirmed real (pasted directly from the user's View
   Page Source on `rivierabeach.granicus.com/ViewPublisher.php?view_id=1`), and the
   parser is logic-tested against a fixture built from that exact real row — but the
   sandbox never had network access to actually execute `fetch_hardened()` against
   the live page this session. Ask the user to run the workflow and paste back the
   `[Riviera Beach]`-prefixed log lines. Also ask the user whether "Utility Special
   District" (+ its Budget Workshop variant), "FY2027 Budget Workshop", or "Community
   Awards and Presentations Program" — real meeting types seen in the user's
   screenshot, currently excluded by the whitelist — should qualify. If a future
   municipality turns out to run Granicus ViewPublisher too, reuse/generalize
   `scrape_riviera_beach()`'s `listingRow`/`AgendaViewer.php`-href approach rather
   than writing a new Granicus parser from scratch.
