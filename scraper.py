import os
import re
import json
import calendar
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

try:
    from curl_cffi import requests as cf_requests
    HAVE_CURL_CFFI = True
except ImportError:
    HAVE_CURL_CFFI = False

# --- HELPER FUNCTIONS ---

def fetch_hardened(url, referer=None, timeout=15):
    """
    GET a URL using a real browser TLS/HTTP fingerprint (curl_cffi) when available,
    since plain `requests` is what's getting blocked by CivicPlus/Cloudflare WAFs on
    GitHub Actions IP ranges. Falls back to plain requests with browser-like headers
    if curl_cffi isn't installed, so this still runs somewhere without it.
    Returns a response-like object with .status_code and .text, or None on failure.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer

    if HAVE_CURL_CFFI:
        try:
            # impersonate="chrome124" gives us a genuine Chrome TLS/JA3 fingerprint,
            # which is what actually gets past Cloudflare/CivicEngage bot checks —
            # spoofing the User-Agent string alone on plain `requests` does not.
            res = cf_requests.get(url, headers=headers, impersonate="chrome124", timeout=timeout)
            return res
        except Exception as e:
            print(f"[fetch_hardened] curl_cffi failed for {url}: {e}")

    # Fallback: plain requests with a modern browser UA
    try:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        return requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        print(f"[fetch_hardened] requests fallback failed for {url}: {e}")
        return None

def clean_event_title(title):
    if not title:
        return "Public Meeting"
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def is_qualifying_event(title):
    # Inclusive whitelist: only match specific, named governance bodies rather than
    # generic words like "Board" or "Committee", which sweep in every advisory board
    # a city happens to run (Parks & Recreation Board, Financial Advisory Board,
    # Library Board, etc.). This is more maintainable than trying to enumerate every
    # non-governance board that might exist - add a new line here only when a real
    # governance body is missing, not when a false positive shows up.
    qualifying_keywords = [
        # Core elected legislative bodies
        r'\bCity Council\b', r'\bCity Commission\b', r'\bTown Council\b',
        r'\bVillage Council\b',  # Wellington is a Village, not City/Town - "Wellington
        # Village Council Meeting"/"...Workshop" wouldn't qualify without this.
        r'\bBoard of County Commissioners\b', r'\bBCC\b',
        # Redevelopment
        r'\bCommunity Redevelopment Agency\b', r'\bCRA\b',
        # Land use / zoning
        r'\bPlanning (?:and|&)\s*Zoning\b', r'\bPlanning (?:Board|Commission)\b',
        r'\bZoning (?:Board|Commission|Board of Appeals)\b',
        r'\bBoard of Adjustment\b',
        # Quasi-governmental authorities tied to city/county government
        r'\bDowntown Development Authority\b', r'\bHousing Authority\b',
        r'\bAirport Authority\b',
        # Council/Commission-specific sessions (qualified by the body name so bare
        # "Workshop" or "Hearing" alone can't match an unrelated event)
        r'\b(?:Council|Commission)\s+Workshop\b',
        r'\b(?:Council|Commission)\s+Agenda Review\b',  # Wellington's "Council Agenda
        # Review Meeting" - added per user request; CIP Workshop intentionally left out.
        r'\bMayor\s*/?\s*Commission Work\s*Session\b',
        r'\bPublic Hearing\b', r'\bTown Hall\b',
    ]
    pattern = re.compile('|'.join(qualifying_keywords), re.I)
    return bool(pattern.search(title))

def get_dual_month_bounds():
    now = datetime.now()
    curr_year = now.year
    curr_month = now.month

    current_month_start = datetime(curr_year, curr_month, 1)

    if curr_month == 11:
        lookahead_end = datetime(curr_year + 1, 1, 1)
    elif curr_month == 12:
        lookahead_end = datetime(curr_year + 1, 2, 1)
    else:
        lookahead_end = datetime(curr_year, curr_month + 2, 1)

    return current_month_start, lookahead_end, curr_year, curr_month


# --- GENERIC CIVICPLUS "calendar.aspx?view=list" SCRAPER ---
# Palm Beach Gardens, Boca Raton (myboca.us), and Boynton Beach (boynton-beach.org)
# all turned out to run the same CivicPlus/CivicEngage platform - Boca and Boynton
# were originally (wrongly) wired up as Legistar clients, which is why they always
# returned 0 events. This one function now backs all three.
def scrape_civicplus_calendar(muni_code, muni_name, base_domain, default_time="6:00 PM", exclude_pattern=None):
    events = []
    calendar_base_url = f"{base_domain}/calendar.aspx"

    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()
    next_month = 1 if curr_month == 12 else curr_month + 1
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    months_to_scrape = [
        {"year": curr_year, "month": curr_month},
        {"year": next_year, "month": next_month}
    ]

    seen_keys = set()

    for target in months_to_scrape:
        y_val, m_val = target["year"], target["month"]
        url = f"{calendar_base_url}?view=list&year={y_val}&month={m_val}"

        res = fetch_hardened(url, referer=calendar_base_url)
        if res is None:
            print(f"[{muni_name} List] Request failed for {y_val}-{m_val:02d}")
            continue
        print(f"[{muni_name} List] Fetching {y_val}-{m_val:02d} | HTTP Status: {res.status_code}")
        if res.status_code != 200:
            continue

        soup = BeautifulSoup(res.text, "html.parser")
        event_rows = soup.find_all(lambda tag: tag.name in ["tr", "li", "div"] and tag.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h)))
        if not event_rows:
            event_rows = soup.select(".calendarItem, .eventRow, table.calendarList tr, ol.calendarList > li")

        print(f"[{muni_name} List] Found {len(event_rows)} matching event rows for {y_val}-{m_val:02d}.")

        for row in event_rows:
            row_text = row.text.strip()
            if not row_text:
                continue

            link_elem = row.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h))
            if not link_elem:
                continue

            raw_title = link_elem.text.strip()
            if not raw_title and link_elem.parent:
                raw_title = link_elem.parent.text.strip()
            raw_title = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*', '', raw_title).strip()
            clean_title = clean_event_title(raw_title)

            href = link_elem.get("href", "").strip()
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"

            iso_date = None
            date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', row_text)
            if date_match:
                m, d, y = date_match.groups()
                iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
            else:
                text_date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})', row_text, re.I)
                if text_date_match:
                    try:
                        dt_parsed = datetime.strptime(text_date_match.group(0).replace(",", ""), "%B %d %Y")
                        iso_date = dt_parsed.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

            if not iso_date:
                day_match = re.search(r'\b(\d{1,2})\b', row_text)
                if day_match:
                    d_num = int(day_match.group(1))
                    if 1 <= d_num <= 31:
                        iso_date = f"{y_val}-{m_val:02d}-{d_num:02d}"

            time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)', row_text)
            meeting_time = time_match.group(1).strip().upper() if time_match else default_time
            if "AM" not in meeting_time and "PM" not in meeting_time:
                meeting_time += " PM"

            if not iso_date or not is_qualifying_event(clean_title):
                continue
            if exclude_pattern and re.search(exclude_pattern, clean_title, re.I):
                continue

            dt = datetime.strptime(iso_date, "%Y-%m-%d")
            if current_month_start <= dt < lookahead_end:
                dedup_key = (clean_title, iso_date)
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    events.append({
                        "id": f"{muni_code.lower()}-{iso_date}-{hash(full_link)}",
                        "muni_short": muni_code,
                        "muni_full": muni_name,
                        "title": clean_title,
                        "date": iso_date,
                        "time": meeting_time,
                        "link": full_link,
                        "summary": f"Official {clean_title} meeting."
                    })

    print(f"[{muni_name} List] Extracted {len(events)} events.")
    return events


# --- 1. WEST PALM BEACH MODULE ---
def scrape_west_palm_beach():
    # WPB runs Granicus OpenCities, NOT CivicPlus - a different product from PBG/Boca/
    # Boynton, which is why calendar.aspx 404s here. OpenCities' main calendar page
    # ("Our-City/Calendars/Meetings") is JS-rendered client-side and returns no usable
    # HTML. However each recurring meeting series has its own static, server-rendered
    # page (e.g. Events-Folder/2026/City-Commission-Meeting-2026) that lists every
    # occurrence for the year under a "When" section as plain text - no JS needed.
    events = []
    base_domain = "https://www.wpb.org"
    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    years_to_check = sorted(set([curr_year, next_year]))

    # Slugs for the recurring governance meeting series known to exist on this pattern.
    series = [
        ("City-Commission-Meeting", "City Commission Meeting"),
        ("Community-Redevelopment-Agency-Meeting", "Community Redevelopment Agency Meeting"),
        ("MayorCommission-Work-Session", "Mayor / Commission Work Session"),
    ]

    seen_keys = set()

    for slug, fallback_title in series:
        for year in years_to_check:
            url = f"{base_domain}/Events-Folder/{year}/{slug}-{year}"
            res = fetch_hardened(url)
            if res is None:
                print(f"[WPB {fallback_title}] Request failed for {year}")
                continue
            if res.status_code == 404:
                # This series may not exist under this slug/year - not a failure.
                continue
            print(f"[WPB {fallback_title}] Fetching {year} | HTTP Status: {res.status_code}")
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            page_title_elem = soup.find("h1")
            clean_title = clean_event_title(page_title_elem.text.strip()) if page_title_elem else fallback_title
            # Strip a trailing year off the title, e.g. "City Commission Meeting 2026"
            clean_title = re.sub(r'\s+\d{4}$', '', clean_title).strip() or fallback_title

            page_text = soup.get_text(separator=' ')
            # Matches lines like: "Monday, August 31, 2026 | 05:00 PM"
            date_pattern = re.compile(
                r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
                r'(January|February|March|April|May|June|July|August|September|October|November|December)'
                r'\s+(\d{1,2}),\s*(\d{4})\s*\|\s*(\d{1,2}:\d{2}\s*[AP]M)',
                re.I
            )

            for match in date_pattern.finditer(page_text):
                month_name, day, yr, time_str = match.groups()
                try:
                    dt = datetime.strptime(f"{month_name} {day} {yr}", "%B %d %Y")
                except ValueError:
                    continue
                iso_date = dt.strftime("%Y-%m-%d")

                if not (current_month_start <= dt < lookahead_end):
                    continue
                if not is_qualifying_event(clean_title):
                    continue

                dedup_key = (clean_title, iso_date)
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    events.append({
                        "id": f"wpb-{iso_date}-{hash(url + clean_title)}",
                        "muni_short": "WPB",
                        "muni_full": "City of West Palm Beach",
                        "title": clean_title,
                        "date": iso_date,
                        "time": time_str.strip().upper(),
                        "link": url,
                        "summary": f"Official {clean_title} meeting."
                    })

    print(f"[WPB] Extracted {len(events)} events.")
    return events


# --- 2. PALM BEACH COUNTY MODULE ---
def scrape_palm_beach_county():
    # discover.pbcgov.org is dead (404/timeout) - the county migrated this site to
    # discover.pbc.gov. That page also doesn't have a clean event list; it links
    # straight to agenda PDFs under /countycommissioners/Agenda_Master/YYYYMMDD.pdf,
    # so we pull the meeting date directly out of the filename instead of parsing text.
    events = []
    base_domain = "https://discover.pbc.gov"
    target_url = f"{base_domain}/countycommissioners/Pages/Agenda.aspx"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url)
    if res is None:
        print("[PBC] Request failed.")
        return events
    print(f"[PBC] HTTP Status: {res.status_code}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")
    seen_keys = set()

    for a in soup.find_all("a", href=re.compile(r'Agenda_Master/\d{8}\.pdf', re.I)):
        href = a.get("href", "").strip()
        date_match = re.search(r'(\d{4})(\d{2})(\d{2})\.pdf', href, re.I)
        if not date_match:
            continue
        y, m, d = date_match.groups()
        iso_date = f"{y}-{m}-{d}"
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            continue

        if current_month_start <= dt < lookahead_end:
            clean_title = "Board of County Commissioners Meeting"
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
            dedup_key = (clean_title, iso_date)
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                events.append({
                    "id": f"pbc-{iso_date}-{hash(full_link)}",
                    "muni_short": "PBC",
                    "muni_full": "Palm Beach County",
                    "title": clean_title,
                    "date": iso_date,
                    "time": "9:30 AM",
                    "link": full_link,
                    "summary": f"Official {clean_title} meeting."
                })

    print(f"[PBC] Extracted {len(events)} events.")
    return events


# --- HELPER FOR LEGISTAR PORTALS (JSON API, THEN RSS, THEN HTML GRID AS FALLBACKS) ---
def scrape_legistar_portal(muni_code, muni_name, base_url):
    events = []
    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    # 0. Primary Attempt: Legistar's official JSON Web API (webapi.legistar.com).
    # This is a separate, documented REST endpoint - not the same server as the
    # HTML calendar - so it avoids the "This Week" grid/postback-state problem
    # entirely. The client slug is normally identical to the InSite subdomain
    # (e.g. "https://delraybeach.legistar.com/" -> client "delraybeach").
    client_match = re.search(r'https?://([^.]+)\.legistar\.com', base_url)
    client_slug = client_match.group(1) if client_match else None

    if client_slug:
        start_str = current_month_start.strftime("%Y-%m-%d")
        end_str = lookahead_end.strftime("%Y-%m-%d")
        api_url = (
            f"https://webapi.legistar.com/v1/{client_slug}/events"
            f"?$filter=EventDate+ge+datetime'{start_str}'+and+EventDate+lt+datetime'{end_str}'"
        )
        try:
            res = requests.get(api_url, headers=headers, timeout=15)
            print(f"[{muni_name} API] Status: {res.status_code}")
            if res.status_code == 200:
                data = res.json()
                for item in data:
                    raw_title = item.get("EventBodyName") or ""
                    clean_title = clean_event_title(raw_title)
                    if not is_qualifying_event(clean_title):
                        continue

                    event_date_raw = item.get("EventDate")  # e.g. "2026-09-15T00:00:00-04:00"
                    if not event_date_raw:
                        continue
                    try:
                        dt = datetime.strptime(event_date_raw[:10], "%Y-%m-%d")
                    except ValueError:
                        continue
                    if not (current_month_start <= dt < lookahead_end):
                        continue
                    iso_date = dt.strftime("%Y-%m-%d")

                    full_link = item.get("EventInSiteURL") or f"{base_url}Calendar.aspx"
                    meeting_time = (item.get("EventTime") or "6:00 PM").strip() or "6:00 PM"

                    events.append({
                        "id": f"{muni_code.lower()}-{iso_date}-{hash(full_link)}",
                        "muni_short": muni_code,
                        "muni_full": muni_name,
                        "title": clean_title,
                        "date": iso_date,
                        "time": meeting_time,
                        "link": full_link,
                        "summary": f"Official {clean_title} meeting."
                    })
                if events:
                    print(f"[{muni_name}] Extracted {len(events)} events via Legistar Web API.")
                    return events
                else:
                    print(f"[{muni_name} API] Returned 200 but no qualifying events; falling back.")
            else:
                print(f"[{muni_name} API] Non-200 response; falling back.")
        except Exception as e:
            print(f"[{muni_name} API] Failed: {e}")

    # 1. Secondary Attempt: Legistar RSS Feed Endpoint
    rss_url = f"{base_url}Calendar.ashx?Mode=RSS"
    try:
        res = requests.get(rss_url, headers=headers, timeout=10)
        print(f"[{muni_name} RSS] Status: {res.status_code}")
        if res.status_code == 200 and res.text.strip().startswith("<?xml"):
            root = ET.fromstring(res.text)
            for item in root.findall(".//item"):
                title_elem = item.find("title")
                link_elem = item.find("link")
                pubdate_elem = item.find("pubDate")

                raw_title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                clean_title = clean_event_title(raw_title)

                full_link = link_elem.text.strip() if link_elem is not None and link_elem.text else rss_url

                iso_date = None
                if pubdate_elem is not None and pubdate_elem.text:
                    try:
                        # Parse RFC 822 date format from RSS
                        dt_parsed = datetime.strptime(pubdate_elem.text[:16], "%a, %d %b %Y")
                        iso_date = dt_parsed.strftime("%Y-%m-%d")
                    except Exception:
                        pass

                if iso_date and is_qualifying_event(clean_title):
                    dt = datetime.strptime(iso_date, "%Y-%m-%d")
                    if current_month_start <= dt < lookahead_end:
                        events.append({
                            "id": f"{muni_code.lower()}-{iso_date}-{hash(full_link)}",
                            "muni_short": muni_code,
                            "muni_full": muni_name,
                            "title": clean_title,
                            "date": iso_date,
                            "time": "6:00 PM",
                            "link": full_link,
                            "summary": f"Official {clean_title} meeting."
                        })
            if events:
                print(f"[{muni_name}] Extracted {len(events)} events via RSS.")
                return events
    except Exception as e:
        print(f"[{muni_name} RSS] Failed: {e}")

    # 2. Tertiary Fallback: Standard HTML Calendar Parsing
    target_url = f"{base_url}Calendar.aspx"
    try:
        res = requests.get(target_url, headers=headers, timeout=10)
        print(f"[{muni_name} HTML] Status: {res.status_code}")
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table", id=re.compile(r'.*gridCalendar.*'))
            if table:
                rows = table.find_all("tr")[1:]
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) >= 6:
                        raw_title = cols[0].text.strip()
                        date_str = cols[1].text.strip()
                        time_str = cols[3].text.strip()

                        clean_title = clean_event_title(raw_title)

                        link_a = cols[5].find("a")
                        href = link_a.get("href", "").strip() if link_a else ""
                        full_link = f"{base_url}{href}" if href and not href.startswith("http") else (href or target_url)

                        try:
                            dt = datetime.strptime(date_str, "%m/%d/%Y")
                            iso_date = dt.strftime("%Y-%m-%d")

                            if current_month_start <= dt < lookahead_end and is_qualifying_event(clean_title):
                                events.append({
                                    "id": f"{muni_code.lower()}-{iso_date}-{hash(full_link)}",
                                    "muni_short": muni_code,
                                    "muni_full": muni_name,
                                    "title": clean_title,
                                    "date": iso_date,
                                    "time": time_str or "6:00 PM",
                                    "link": full_link,
                                    "summary": f"Official {clean_title} meeting."
                                })
                        except ValueError:
                            continue
        print(f"[{muni_name} HTML] Extracted {len(events)} events.")
    except Exception as e:
        print(f"[{muni_name} HTML] Error: {e}")

    return events


def scrape_boca_raton():
    # Boca Raton runs CivicPlus (myboca.us), not Legistar - "Government Websites by
    # CivicPlus" is stated right on the site, and myboca.us/calendar.aspx?view=list
    # works exactly like PBG's. The original Legistar wiring here was simply wrong,
    # which is why it always returned 0 events.
    return scrape_civicplus_calendar("BOCA", "City of Boca Raton", "https://www.myboca.us", default_time="6:00 PM")

def scrape_boynton_beach():
    # Same story as Boca: Boynton Beach runs CivicPlus/CivicEngage at boynton-beach.org
    # with the same calendar.aspx?view=list mechanism, not Legistar.
    return scrape_civicplus_calendar("BOYNTON", "City of Boynton Beach", "https://www.boynton-beach.org", default_time="6:00 PM")

def scrape_delray_beach():
    # Delray Beach is the one municipality here that's genuinely on Legistar.
    return scrape_legistar_portal("DELRAY", "City of Delray Beach", "https://delraybeach.legistar.com/")


# --- 6. PALM BEACH GARDENS MODULE ---
def scrape_palm_beach_gardens():
    return scrape_civicplus_calendar(
        "PBG", "City of Palm Beach Gardens", "https://www.pbgfl.gov",
        default_time="6:00 PM", exclude_pattern=r'\b(ITB|RFP|RFQ|Bid)\b'
    )


# --- 7. WELLINGTON MODULE ---
def scrape_wellington():
    # Wellington runs CivicPlus (confirmed live: "Government Websites by CivicPlus"
    # appears on wellingtonfl.gov), but its calendar behaves differently from PBG/Boca/
    # Boynton's: ?view=list is a single-day drilldown here, not a full-month list, so
    # scrape_civicplus_calendar() doesn't fit. The reliable single-request-per-month
    # source is the plain calendar page (startDate/enddate/CID/showPastEvents, matching
    # the URL format provided) - CivicPlus embeds each event's title, time, and a clean
    # ISO 8601 timestamp directly in the page's HTML (as hover/click-popup content) even
    # though it renders visually as a month grid, no JS execution needed to read it.
    #
    # NOTE: on one manual verification fetch, requesting CID=29 unexpectedly returned a
    # different calendar (CID=22, "Meetings") instead - a possible session/redirect
    # quirk on Wellington's CivicPlus install that couldn't be fully diagnosed without
    # raw HTML/browser access. Confirm with a real GitHub Actions log before trusting
    # this scraper the way PBG/Delray/PBC are already trusted.
    events = []
    base_domain = "https://www.wellingtonfl.gov"
    calendar_cid = "29"  # "Council Meetings" calendar, per the user-provided URL

    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()
    next_month = 1 if curr_month == 12 else curr_month + 1
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    months_to_scrape = [
        {"year": curr_year, "month": curr_month},
        {"year": next_year, "month": next_month},
    ]

    # Each event's title appears cleanly right after the day number, in a
    # "[<Title>Category: <Calendar Name>](#)" anchor - BEFORE any freeform "Location"
    # text (which can be empty, or a long multi-sentence disclaimer, as with the
    # Village Council Meeting's childcare sign-up notice). Extracting the title from
    # this early anchor, then searching only within that event's own slice of text for
    # the "More Details" link and the trailing ISO 8601 timestamp, avoids an earlier bug
    # where long Location text got swept into the title capture for events that had it.
    title_delim_pattern = re.compile(r'\[([^\]]*?)\s*Category:\s*[^\]]+\]\(#\)')
    link_pattern = re.compile(r'\[More Details\]\((https://www\.wellingtonfl\.gov/Calendar\.aspx\?EID=\d+[^\)]*)\)')
    iso_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})')

    seen_keys = set()

    for target in months_to_scrape:
        y_val, m_val = target["year"], target["month"]
        last_day = calendar.monthrange(y_val, m_val)[1]
        start_str = f"{m_val:02d}/01/{y_val}"
        end_str = f"{m_val:02d}/{last_day:02d}/{y_val}"
        url = (
            f"{base_domain}/calendar.aspx?Keywords=&startDate={start_str}"
            f"&enddate={end_str}&CID={calendar_cid}&showPastEvents=false"
        )

        res = fetch_hardened(url, referer=f"{base_domain}/calendar.aspx")
        if res is None:
            print(f"[Wellington] Request failed for {y_val}-{m_val:02d}")
            continue
        print(f"[Wellington] Fetching {y_val}-{m_val:02d} | HTTP Status: {res.status_code}")
        if res.status_code != 200:
            continue

        # --- TEMPORARY DEBUG ---
        # First attempt dumped the first 4000 chars, which turned out to be all page
        # boilerplate (GA4 tag manager, anti-forgery token JS) before any real event
        # markup - confirmed the event data (Council/EID=/ISO timestamps) genuinely
        # exists in the raw response, just further in. Dumping a window around the
        # first real event marker instead this time. Remove this block once
        # scrape_wellington() is confirmed working end-to-end.
        print(f"[Wellington DEBUG] Response length: {len(res.text)} chars")
        eid_pos = res.text.find("EID=")
        print(f"[Wellington DEBUG] First 'EID=' at char offset: {eid_pos}")
        if eid_pos != -1:
            window_start = max(0, eid_pos - 1500)
            window_end = min(len(res.text), eid_pos + 2500)
            print(f"[Wellington DEBUG] Raw HTML window [{window_start}:{window_end}]:\n{res.text[window_start:window_end]}")
        # --- END TEMPORARY DEBUG ---

        soup = BeautifulSoup(res.text, "html.parser")
        page_text = soup.get_text(separator=' ')

        title_starts = list(title_delim_pattern.finditer(page_text))
        print(f"[Wellington] Found {len(title_starts)} raw event blocks for {y_val}-{m_val:02d}.")

        for i, title_match in enumerate(title_starts):
            raw_title = title_match.group(1)
            block_start = title_match.end()
            block_end = title_starts[i + 1].start() if i + 1 < len(title_starts) else len(page_text)
            block_text = page_text[block_start:block_end]

            link_match = link_pattern.search(block_text)
            iso_match = iso_pattern.search(block_text)
            if not link_match or not iso_match:
                continue

            full_link = link_match.group(1)
            iso_date, iso_time_raw = iso_match.groups()
            clean_title = clean_event_title(raw_title)

            if not is_qualifying_event(clean_title):
                continue

            try:
                dt = datetime.strptime(iso_date, "%Y-%m-%d")
            except ValueError:
                continue
            if not (current_month_start <= dt < lookahead_end):
                continue

            try:
                time_obj = datetime.strptime(iso_time_raw, "%H:%M:%S")
                meeting_time = time_obj.strftime("%-I:%M %p")
            except ValueError:
                meeting_time = "6:00 PM"

            dedup_key = (clean_title, iso_date)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            events.append({
                "id": f"wellington-{iso_date}-{hash(full_link)}",
                "muni_short": "WELL",
                "muni_full": "Village of Wellington",
                "title": clean_title,
                "date": iso_date,
                "time": meeting_time,
                "link": full_link,
                "summary": f"Official {clean_title} meeting."
            })

    print(f"[Wellington] Extracted {len(events)} events.")
    return events


# --- MAIN ENGINE RUNNER ---
def main():
    all_events = []
    
    print("Starting Municipal Scraper Engine...")
    
    all_events.extend(scrape_west_palm_beach())
    all_events.extend(scrape_palm_beach_county())
    all_events.extend(scrape_boca_raton())
    all_events.extend(scrape_boynton_beach())
    all_events.extend(scrape_delray_beach())
    all_events.extend(scrape_palm_beach_gardens())
    all_events.extend(scrape_wellington())

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2)

    print(f"Execution complete. Saved {len(all_events)} unique matching events to data.json.")

if __name__ == "__main__":
    main()
