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

def fetch_hardened(url, referer=None, timeout=15, impersonate="chrome124"):
    """
    GET a URL using a real browser TLS/HTTP fingerprint (curl_cffi) when available,
    since plain `requests` is what's getting blocked by CivicPlus/Cloudflare WAFs on
    GitHub Actions IP ranges. Falls back to plain requests with browser-like headers
    if curl_cffi isn't installed, so this still runs somewhere without it.
    `impersonate` lets a caller try a different curl_cffi browser fingerprint when
    the default one is getting blocked (see fetch_hardened_retry below).
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
            # impersonate="chrome124" (default) gives us a genuine Chrome TLS/JA3
            # fingerprint, which is what actually gets past Cloudflare/CivicEngage bot
            # checks — spoofing the User-Agent string alone on plain `requests` does
            # not.
            res = cf_requests.get(url, headers=headers, impersonate=impersonate, timeout=timeout)
            return res
        except Exception as e:
            print(f"[fetch_hardened] curl_cffi ({impersonate}) failed for {url}: {e}")

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


def fetch_hardened_retry(url, referer=None, timeout=15, log_prefix="[fetch_hardened_retry]", attempts=None):
    """
    Like fetch_hardened(), but retries across a short list of curl_cffi browser
    fingerprints (and a brief backoff) before giving up - for sites like PBC whose
    WAF has been observed resetting the connection outright (TLS-level reset, not an
    HTTP error code) on the default fingerprint. Returns the first response with a
    real status_code, or None if every attempt failed to even connect.

    This does not solve WAF blocking that's based on GitHub Actions' IP ranges rather
    than TLS fingerprint - if that's the actual cause, every fingerprint here will
    fail the same way, and that fact (all attempts producing the same
    connection-reset error) is itself useful debugging signal to log and hand back.
    """
    import time
    if attempts is None:
        attempts = ["chrome124", "chrome120", "safari15_5", "edge101"]

    last_res = None
    for i, impersonate in enumerate(attempts):
        res = fetch_hardened(url, referer=referer, timeout=timeout, impersonate=impersonate)
        if res is not None and getattr(res, "status_code", None):
            print(f"{log_prefix} Attempt {i + 1}/{len(attempts)} (impersonate={impersonate}): HTTP {res.status_code}")
            if res.status_code == 200:
                return res
            last_res = res
        else:
            print(f"{log_prefix} Attempt {i + 1}/{len(attempts)} (impersonate={impersonate}): connection failed (no response at all).")
        if i < len(attempts) - 1:
            time.sleep(1.5)

    if last_res is not None:
        print(f"{log_prefix} All {len(attempts)} fingerprints connected but none returned HTTP 200; "
              f"returning the last response (HTTP {last_res.status_code}).")
        return last_res
    print(f"{log_prefix} All {len(attempts)} fingerprints failed to connect at all "
          f"(consistent connection-reset across fingerprints usually means an "
          f"IP-range block, not a fingerprint check - a fingerprint retry can't fix that).")
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
        r'\bTown Commission\b',  # Jupiter Inlet Colony uses "Town Commission" instead of Council
        r'\bVillage Council\b',  # Wellington is a Village, not City/Town - "Wellington
        # Village Council Meeting"/"...Workshop" wouldn't qualify without this.
        r'\bBoard of County Commissioners\b', r'\bBCC\b',
        # Redevelopment
        r'\bCommunity Redevelopment Agency\b', r'\bCRA\b',
        # Land use / zoning / planning
        r'\bPlanning (?:and|&)\s*Zoning\b', r'\bPlanning (?:Board|Commission)\b',
        r'\bLocal Planning Agency\b',  # Jupiter Inlet Colony has "Local Planning Agency Meeting"
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
    # Palm Beach County's agenda page is served from the main pbcgov.org domain
    # (discover.pbc.gov was blocked by WAF on GitHub Actions in an earlier session).
    # The page links straight to agenda PDFs under
    # /countycommissioners/Agenda_Master/YYYYMMDD.pdf, so we pull the meeting date
    # directly out of the filename instead of parsing text.
    #
    # As of this session, www.pbcgov.org started failing the *same* way
    # discover.pbc.gov originally did: a real GitHub Actions log showed
    # "Connection reset by peer" from curl_cffi AND "Connection aborted"/
    # RemoteDisconnected from the plain-requests fallback - a TLS-level reset on
    # both paths, not an HTTP error code, meaning PBC's WAF is rejecting the
    # connection outright rather than serving a 403/429. That's the same failure
    # signature as the original discover.pbc.gov block, just apparently extended to
    # www.pbcgov.org too. Switched to fetch_hardened_retry(), which tries a short
    # list of different curl_cffi browser TLS fingerprints (chrome124/chrome120/
    # safari15_5/edge101) with backoff, in case this is fingerprint-specific rather
    # than a straight IP-range block. UNCONFIRMED whether this actually gets past
    # PBC's WAF - this sandbox has no network route to pbcgov.org either, so this is
    # a reasonable next attempt, not a verified fix. Ask the user to run the workflow
    # and paste back the new "[PBC]"-prefixed log lines; if every fingerprint still
    # resets the connection the same way, it's very likely an IP-range block on
    # GitHub Actions runners specifically, which no fingerprint change can fix - that
    # would need a different mitigation (e.g. a proxy, or scraping via a different
    # source entirely) rather than another header/fingerprint tweak.
    events = []
    base_domain = "https://www.pbcgov.org"
    target_url = f"{base_domain}/countycommissioners/pages/agenda.aspx"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened_retry(target_url, log_prefix="[PBC]")
    if res is None:
        print("[PBC] Request failed after retrying multiple TLS fingerprints.")
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
    # the URL format provided), which server-renders a full month grid.
    #
    # Confirmed against a real GitHub Actions log dump of the actual raw HTML: each
    # event's day cell embeds a Schema.org microdata block -
    #   <div itemscope itemtype="http://schema.org/Event">
    #     <span itemprop="name">...</span>
    #     <span itemprop="startDate">2026-08-11T18:30:00</span>
    #     ...
    #   </div>
    # - which is far more reliable than text-scraping the visible tooltip markup: it
    # sidesteps freeform "Location" text entirely (one real event, the Village Council
    # Meeting, has a multi-sentence childcare sign-up disclaimer sitting between the
    # visible date and the visible title repeat - an earlier text-flattening regex
    # approach mangled that event's title because of it; this schema-based approach
    # never touches that text at all, since itemprop="name" and itemprop="startDate"
    # are read directly, independent of surrounding freeform content).
    #
    # NOTE: on one manual (non-CI) verification fetch, requesting CID=29 unexpectedly
    # returned a different calendar (CID=22, "Meetings") - a possible session/redirect
    # quirk that wasn't reproduced in the real GitHub Actions run used to build this
    # version, but worth watching for if event counts ever look off.
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

        soup = BeautifulSoup(res.text, "html.parser")
        event_divs = soup.find_all("div", itemtype="http://schema.org/Event")
        print(f"[Wellington] Found {len(event_divs)} schema.org event blocks for {y_val}-{m_val:02d}.")

        for ev_div in event_divs:
            name_span = ev_div.find("span", itemprop="name")
            date_span = ev_div.find("span", itemprop="startDate")
            if not name_span or not date_span:
                continue

            raw_title = name_span.get_text(strip=True)
            iso_datetime_str = date_span.get_text(strip=True)
            clean_title = clean_event_title(raw_title)

            if not is_qualifying_event(clean_title):
                continue

            try:
                dt = datetime.strptime(iso_datetime_str, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
            if not (current_month_start <= dt < lookahead_end):
                continue

            iso_date = dt.strftime("%Y-%m-%d")
            meeting_time = dt.strftime("%-I:%M %p")

            # Reconstruct the canonical event page link from the enclosing "monthItem"
            # div's id ("parentdiv{EID}_{sequence}") - more reliable than hunting for
            # the right "More Details" link when a day happens to have more than one
            # event, since each event's schema block sits inside its own monthItem.
            full_link = f"{base_domain}/Calendar.aspx"
            parent_item = ev_div.find_parent("div", class_="monthItem")
            if parent_item and parent_item.get("id"):
                eid_match = re.match(r'parentdiv(\d+)_', parent_item["id"])
                if eid_match:
                    eid = eid_match.group(1)
                    full_link = (
                        f"{base_domain}/Calendar.aspx?EID={eid}"
                        f"&month={dt.month}&year={dt.year}&day={dt.day}&calType=0"
                    )

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


# --- 8. WESTLAKE MODULE ---
def scrape_westlake():
    # Westlake runs MuniCode's "Meetings" portal (meetings.municode.com), a platform
    # not seen elsewhere in this project (not CivicPlus/Legistar/Granicus). Per a
    # WebFetch preview of https://www.westlakegov.com/meetings, the page is a single
    # server-rendered HTML table (columns: Date, Meeting, Agendas, Packets, Minutes,
    # Video/Audio, View) rather than a JS-driven widget - no iframe/API call was found,
    # data appeared directly in the table markup. Per this project's Key Methodological
    # Lesson, that WebFetch preview is a HYPOTHESIS, not confirmed ground truth: this
    # first pass is written defensively (multiple table-selector fallbacks, an
    # agenda-column-by-header lookup instead of a hardcoded index) and logs heavily so
    # a real run's output can confirm or correct the actual selectors/markup before
    # this is considered final. Ask the user to run the workflow (or run scraper.py
    # locally) and paste back the printed log if the extracted count/fields look wrong.
    #
    # Per explicit user instruction (applies project-wide, not just Westlake): an event
    # with confirmed date/time details but no agenda link yet is still included, with
    # has_agenda=False so the frontend can show a "No Agenda Available" hover instead
    # of a broken/missing "View Agenda" link.
    events = []
    base_domain = "https://www.westlakegov.com"
    target_url = f"{base_domain}/meetings"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url)
    if res is None:
        print("[Westlake] Request failed.")
        return events
    print(f"[Westlake] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    # Try to find the meetings table a few different ways, since the real id/class
    # names on MuniCode's grid are unconfirmed as of this first pass.
    table = soup.find("table", id=re.compile(r'meeting', re.I))
    if not table:
        table = soup.find("table", class_=re.compile(r'meeting', re.I))
    if not table:
        # Fall back to the largest table on the page (by row count) - meetings tables
        # are typically the dominant table on a page like this.
        candidate_tables = soup.find_all("table")
        if candidate_tables:
            table = max(candidate_tables, key=lambda t: len(t.find_all("tr")))

    if not table:
        print("[Westlake] No table found on the page at all. Dumping a slice of raw HTML "
              "around a known meeting-type string for debugging:")
        idx = res.text.find("City Council")
        print(res.text[max(0, idx - 200): idx + 500] if idx != -1 else res.text[:700])
        return events

    header_cells = table.find("tr")
    header_texts = [c.get_text(strip=True).lower() for c in header_cells.find_all(["th", "td"])] if header_cells else []
    agenda_col_idx = next((i for i, h in enumerate(header_texts) if "agenda" in h), None)
    print(f"[Westlake] Table header row: {header_texts} | agenda column index: {agenda_col_idx}")

    all_rows = table.find_all("tr")
    data_rows = all_rows[1:] if header_texts else all_rows
    print(f"[Westlake] Found {len(data_rows)} candidate data rows.")

    if data_rows:
        print(f"[Westlake] Sample first row raw HTML (for debugging column layout):\n{data_rows[0]}")

    seen_keys = set()

    for row in data_rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        row_text = row.get_text(separator=" ", strip=True)

        # Date+time cell observed as e.g. "09/01/2026 - 6:00pm" in the WebFetch preview.
        date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', row_text)
        if not date_match:
            continue
        m, d, y = date_match.groups()
        iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            continue
        if not (current_month_start <= dt < lookahead_end):
            continue

        time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.I)
        meeting_time = time_match.group(1).upper().replace(" ", "") if time_match else "6:00 PM"
        if len(meeting_time) > 2 and meeting_time[-2:] in ("AM", "PM") and meeting_time[-3] != " ":
            meeting_time = meeting_time[:-2] + " " + meeting_time[-2:]

        raw_title = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        clean_title = clean_event_title(raw_title)
        if not clean_title or not is_qualifying_event(clean_title):
            continue

        # Agenda availability: look in the identified "Agendas" column for this row;
        # fall back to scanning the whole row for any link whose text/href mentions
        # "agenda" if the header lookup didn't find a column index.
        agenda_link = None
        if agenda_col_idx is not None and agenda_col_idx < len(cells):
            agenda_link = cells[agenda_col_idx].find("a", href=True)
        if not agenda_link:
            agenda_link = row.find("a", href=True, string=re.compile(r'agenda', re.I))
        if not agenda_link:
            agenda_link = row.find("a", href=re.compile(r'agenda', re.I))

        has_agenda = agenda_link is not None
        if has_agenda:
            href = agenda_link.get("href", "").strip()
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
        else:
            full_link = target_url  # Fall back to the meetings page itself, not a dead link.

        dedup_key = (clean_title, iso_date, meeting_time)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"westlake-{iso_date}-{hash(clean_title + meeting_time)}",
            "muni_short": "WESTLAKE",
            "muni_full": "City of Westlake",
            "title": clean_title,
            "date": iso_date,
            "time": meeting_time,
            "link": full_link,
            "has_agenda": has_agenda,
            "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
        })

    print(f"[Westlake] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


# --- 10. CITY OF PALM BEACH MODULE ---
def scrape_palm_beach():
    # City of Palm Beach runs CivicClerk / CivicPlus "Meetings Select" - a JS SPA
    # portal (palmbeachfl.portal.civicclerk.com) backed by a JSON REST/OData API at
    # palmbeachfl.api.civicclerk.com. Confirmed directly by the user via their own
    # browser DevTools Network tab capture (not a guess/rendered-preview hypothesis -
    # this is the real request the portal's own JS makes):
    #   GET https://palmbeachfl.api.civicclerk.com/v1/Events
    #       ?$filter=startDateTime+lt+2026-08-30
    #       &$orderby=startDateTime+desc,+eventName+desc
    # returning an OData JSON body: {"@odata.context": ..., "value": [ {event}, ... ]}.
    # No auth header was needed - this is a public, unauthenticated read endpoint.
    #
    # Confirmed real fields on each event object (from the user's captured sample,
    # an "Architectural Commission Meeting" on 2026-08-26):
    #   id, eventName, eventDate, startDateTime (UTC, trailing "Z"), isDeleted,
    #   isPublished ("Published"/other), hasAgenda (bool - the API gives us this
    #   directly, no need to infer it from link presence like Westlake/DDA),
    #   eventLocation {address1, address2, city, state, zipCode},
    #   publishedFiles: [ {type: "Agenda"/"Agenda Packet"/"Supplemental Backup"/...,
    #                       url: "stream/PALMBEACHFL/<uuid>.pdf", ...}, ... ]
    #
    # Two things below are NOT yet directly confirmed and are handled defensively:
    #  1. publishedFiles[].url is relative (e.g. "stream/PALMBEACHFL/xxx.pdf") with no
    #     domain in the captured sample. Guessing it resolves against the API origin
    #     (api_domain + "/" + url) since the JSON itself came from that origin with no
    #     auth - if a real run's agenda links 404, this is the first thing to check
    #     against another DevTools capture (right-click the agenda link in the portal
    #     UI -> Copy Link, or Network-tab the actual PDF request).
    #  2. The exact portal URL pattern for a human-facing event detail page (used as
    #     the has_agenda=False fallback link, and if publishedFiles has no "Agenda"
    #     entry) - other CivicClerk portals were seen using `/event/{id}/files` and
    #     `/event/{id}/media` patterns during research, so `/event/{id}` (the portal
    #     event's base page) is used here as the safest general fallback.
    events = []
    base_domain = "https://palmbeachfl.portal.civicclerk.com"
    api_domain = "https://palmbeachfl.api.civicclerk.com"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()
    start_str = current_month_start.strftime("%Y-%m-%d")
    end_str = lookahead_end.strftime("%Y-%m-%d")

    # OData $filter/$orderby, URL-encoded the same way the real captured request was
    # (spaces as "+"). ge/lt bound the query to the current + next month window this
    # project uses everywhere else (get_dual_month_bounds()), ascending so the closest
    # upcoming meetings come first.
    odata_filter = f"startDateTime+ge+{start_str}+and+startDateTime+lt+{end_str}"
    odata_orderby = "startDateTime+asc"
    target_url = f"{api_domain}/v1/Events?$filter={odata_filter}&$orderby={odata_orderby}"

    res = fetch_hardened(target_url, referer=base_domain)
    if res is None:
        print("[Palm Beach] Request failed.")
        return events
    print(f"[Palm Beach] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        print(f"[Palm Beach] Non-200 response, first 400 chars: {(res.text or '')[:400]}")
        return events

    try:
        payload = json.loads(res.text)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[Palm Beach] Failed to parse JSON response: {e}. "
              f"First 400 chars: {(res.text or '')[:400]}")
        return events

    raw_events = payload.get("value", [])
    print(f"[Palm Beach] API returned {len(raw_events)} raw event(s) in this window "
          f"({start_str} to {end_str}).")
    if raw_events:
        print(f"[Palm Beach] Sample raw event keys: {sorted(raw_events[0].keys())}")

    # Eastern-time conversion for the UTC startDateTime the API returns. Uses stdlib
    # zoneinfo (Python 3.9+) when available; falls back to a manual US DST calculation
    # (2nd Sunday in March - 1st Sunday in November is EDT/UTC-4, else EST/UTC-5) if
    # the zoneinfo tzdata isn't present on the runner, so this doesn't hard-fail.
    def to_eastern(dt_utc):
        try:
            from zoneinfo import ZoneInfo
            return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        except Exception:
            year = dt_utc.year
            # 2nd Sunday of March
            d = datetime(year, 3, 1)
            d += timedelta(days=(6 - d.weekday()) % 7 + 7)
            dst_start = d
            # 1st Sunday of November
            d = datetime(year, 11, 1)
            d += timedelta(days=(6 - d.weekday()) % 7)
            dst_end = d
            offset_hours = 4 if dst_start <= dt_utc < dst_end else 5
            return dt_utc - timedelta(hours=offset_hours)

    seen_keys = set()

    for ev in raw_events:
        if ev.get("isDeleted"):
            continue
        if ev.get("isPublished") not in (None, "Published"):
            # Be permissive if the field is missing/unrecognized rather than dropping
            # everything, but skip anything explicitly marked as not published.
            continue

        raw_title = ev.get("eventName", "")
        clean_title = clean_event_title(raw_title)
        if not clean_title or not is_qualifying_event(clean_title):
            continue

        start_raw = ev.get("startDateTime") or ev.get("eventDate")
        if not start_raw:
            continue
        try:
            dt_utc = datetime.strptime(start_raw.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            print(f"[Palm Beach] Could not parse startDateTime '{start_raw}' for "
                  f"'{clean_title}', skipping.")
            continue

        dt_local = to_eastern(dt_utc)
        iso_date = dt_local.strftime("%Y-%m-%d")
        meeting_time = dt_local.strftime("%-I:%M %p") if os.name != "nt" else dt_local.strftime("%I:%M %p").lstrip("0")

        event_id = ev.get("id")
        has_agenda = bool(ev.get("hasAgenda"))

        # CONFIRMED (user-provided real example, Sept 1 event): the portal's actual
        # agenda deep-link is NOT built from publishedFiles[].url (that's a relative
        # blob-storage path, e.g. "stream/PALMBEACHFL/<uuid>.pdf", which is NOT what
        # the portal links to - that was an incorrect guess in the first pass and
        # produced a dead/wrong URL). The real, working link pattern is a portal page:
        #   {base_domain}/event/{event_id}/files/agenda/{fileId}
        # e.g. https://palmbeachfl.portal.civicclerk.com/event/11384/files/agenda/16662
        # where {fileId} is the "Agenda"-typed entry's `fileId` (NOT `id`, which is
        # always 0 on these sub-objects) from publishedFiles.
        agenda_file_id = None
        for f in (ev.get("publishedFiles") or []):
            if (f.get("type") or "").strip().lower() == "agenda" and f.get("fileId"):
                agenda_file_id = f["fileId"]
                break

        if has_agenda and agenda_file_id and event_id is not None:
            full_link = f"{base_domain}/event/{event_id}/files/agenda/{agenda_file_id}"
        elif event_id is not None:
            # hasAgenda True but no "Agenda"-typed file found (e.g. only an "Agenda
            # Packet"), or hasAgenda False - fall back to the event's portal page
            # rather than guessing a file URL. This fallback pattern
            # ({base_domain}/event/{id}) is still itself unconfirmed - flag if a real
            # run shows it 404ing too.
            full_link = f"{base_domain}/event/{event_id}"
        else:
            full_link = base_domain

        dedup_key = (clean_title, iso_date, meeting_time)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"palmbeach-{event_id if event_id is not None else iso_date}",
            "muni_short": "PALMBEACH",
            "muni_full": "Town of Palm Beach",
            "title": clean_title,
            "date": iso_date,
            "time": meeting_time,
            "link": full_link,
            "has_agenda": has_agenda,
            "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
        })

    print(f"[Palm Beach] Extracted {len(events)} qualifying event(s) "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


# --- 9. DOWNTOWN WPB DDA MODULE ---
def scrape_downtown_wpb_dda():
    # The Downtown West Palm Beach DDA (Downtown Development Authority) isn't on
    # CivicPlus/Legistar/Granicus/MuniCode - it's a WordPress page
    # (downtownwpb.com/dda/board-meetings/) that simply lists Board meeting packets as
    # a bulleted list of dates, grouped by fiscal year, most recent first. Per the user
    # (who can see the live page) and a WebFetch rendered-preview of it: meetings recur
    # on the 3rd Tuesday of each month at 8:30 a.m.; each date is a plain list item,
    # hyperlinked to an Issuu-hosted agenda packet (e.g.
    # ".../dda_board_agenda_packet_august_18_2026") once posted, and left as plain
    # unlinked text (e.g. "September 15, 2026") when the packet hasn't been posted yet.
    #
    # Per this project's Key Methodological Lessons: a WebFetch preview converts <a>
    # tags into markdown bracket-links, which is NOT proof of the real underlying <li>/
    # <a> structure - and this sandbox has no direct network route to downtownwpb.com
    # either (confirmed: a plain curl/requests attempt was rejected by the egress
    # proxy). So exactly like scrape_westlake(), this is an UNCONFIRMED first-pass
    # draft: written defensively (date-pattern text matching rather than assuming a
    # specific list/class structure) and logs heavily so a real run's log can confirm
    # or correct it. Ask the user to run the workflow and paste back the
    # "[Downtown WPB DDA]" log lines before trusting this fully.
    #
    # Title is hardcoded to include the literal phrase "Downtown Development Authority"
    # so it passes the existing is_qualifying_event() whitelist entry for that phrase -
    # no whitelist change needed, since the page itself never spells out a per-event
    # title (just dates).
    #
    # Per the project-wide "No Agenda Available" policy: an event whose date is known
    # but whose agenda packet isn't posted yet is still included, with has_agenda=False
    # and "link" pointing at the board-meetings page itself (never a guessed/dead URL).
    events = []
    base_domain = "https://downtownwpb.com"
    target_url = f"{base_domain}/dda/board-meetings/"
    clean_title = "Downtown Development Authority (DDA) Board Meeting"
    default_time = "8:30 AM"  # Per explicit user statement: "3rd Tuesday of each month at 8:30AM".

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url)
    if res is None:
        print("[Downtown WPB DDA] Request failed.")
        return events
    print(f"[Downtown WPB DDA] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    date_pattern = re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s*(\d{4})\b',
        re.I
    )

    # Primary strategy: each meeting date is expected to live in its own <li>. Scan
    # every <li> on the page (not just inside a specific <ul>, since the exact
    # container class is unconfirmed) for one that contains a "Month DD, YYYY" date.
    candidate_items = soup.find_all("li")
    print(f"[Downtown WPB DDA] Found {len(candidate_items)} <li> elements on the page.")

    date_items = [(li, date_pattern.search(li.get_text(" ", strip=True))) for li in candidate_items]
    date_items = [(li, m) for li, m in date_items if m]
    print(f"[Downtown WPB DDA] {len(date_items)} <li> elements contain a recognizable date.")

    if date_items:
        print(f"[Downtown WPB DDA] Sample matching <li> raw HTML:\n{date_items[0][0]}")

    # Fallback strategy: if the site isn't using <li> for these at all, fall back to
    # scanning every text node / link on the page directly for the same date pattern,
    # treating any date found inside an <a> as "has agenda" and any found in plain text
    # (outside a link) as "no agenda yet".
    if not date_items:
        print("[Downtown WPB DDA] No matching <li> elements found. Falling back to a "
              "page-wide date scan. Dumping a raw HTML slice anchored on '3rd Tuesday' "
              "for debugging:")
        idx = res.text.find("3rd Tuesday")
        if idx == -1:
            idx = res.text.lower().find("board meeting")
        print(res.text[max(0, idx - 200): idx + 500] if idx != -1 else res.text[:700])

        seen_fallback = set()
        for a in soup.find_all("a", href=True):
            m = date_pattern.search(a.get_text(" ", strip=True))
            if m and id(a) not in seen_fallback:
                seen_fallback.add(id(a))
                date_items.append((a, m))
        for text_node in soup.find_all(string=date_pattern):
            parent = text_node.parent
            if parent and parent.name != "a" and not parent.find("a"):
                m = date_pattern.search(text_node)
                if m:
                    date_items.append((parent, m))
        print(f"[Downtown WPB DDA] Fallback scan found {len(date_items)} date matches.")

    seen_keys = set()

    for elem, m in date_items:
        month_name, day, yr = m.groups()
        try:
            dt = datetime.strptime(f"{month_name} {day} {yr}", "%B %d %Y")
        except ValueError:
            continue
        iso_date = dt.strftime("%Y-%m-%d")

        if not (current_month_start <= dt < lookahead_end) or not is_qualifying_event(clean_title):
            continue

        # has_agenda: true if this element is (or contains/is contained by) a link to
        # an agenda packet; false if the date is plain, unlinked text.
        link_elem = elem if elem.name == "a" else elem.find("a", href=True)
        if not link_elem and elem.name != "a":
            parent_a = elem.find_parent("a", href=True)
            link_elem = parent_a

        has_agenda = link_elem is not None and link_elem.get("href")
        if has_agenda:
            href = link_elem.get("href", "").strip()
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
        else:
            full_link = target_url  # No agenda posted yet - point at the source page, not a dead link.

        dedup_key = iso_date
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"ddawpb-{iso_date}-{hash(clean_title + iso_date)}",
            "muni_short": "DDA-WPB",
            "muni_full": "Downtown WPB DDA",
            "title": clean_title,
            "date": iso_date,
            "time": default_time,
            "link": full_link,
            "has_agenda": bool(has_agenda),
            "summary": f"Official {clean_title}." if has_agenda else f"Official {clean_title}. No agenda posted yet.",
        })

    print(f"[Downtown WPB DDA] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


# --- 11. JUPITER MODULE (added this session — UNCONFIRMED, see comments) ---
def scrape_jupiter():
    # Town of Jupiter, FL - https://www.jupiter.fl.us/calendar.aspx?CID=35. The
    # calendar.aspx?CID=N URL shape is the same CivicPlus/CivicEngage pattern used by
    # PBG/Boca/Boynton (?view=list) and Wellington (?CID=29, startDate/enddate),
    # strongly suggesting this is another CivicPlus site - but which of the two
    # observed CivicPlus behaviors it uses (PBG/Boca/Boynton's full-month ?view=list,
    # or Wellington's single-day-drilldown grid requiring the Schema.org microdata
    # parse) is NOT confirmed. This session's sandbox had no outbound network route to
    # jupiter.fl.us at all - both a direct `curl` and the WebFetch rendered-preview
    # tool failed outright (WebFetch couldn't even fetch/parse robots.txt), so there
    # was no rendered preview to hypothesize from this time, per Key Methodological
    # Lesson #2 in handoff.md. Nothing below has been checked against real HTML.
    #
    # Written defensively to try both known CivicPlus shapes and log everything needed
    # to fix it from one real GitHub Actions run:
    #   1. Wellington-style: fetch calendar.aspx with startDate/enddate/CID=35 and look
    #      for <div itemscope itemtype="http://schema.org/Event"> blocks.
    #   2. If none found, fall back to the PBG/Boca/Boynton-style ?view=list&CID=35
    #      page and look for EID= row links (scrape_civicplus_calendar()'s approach,
    #      inlined here rather than reused since that helper doesn't accept a CID param).
    #   3. If neither yields anything, dump a raw HTML slice anchored on "Jupiter" (a
    #      guaranteed real string on the page) plus the row/block counts found by each
    #      strategy, so the real structure can be identified from the log.
    #
    # muni_full is set to "Town of Jupiter" - Jupiter, FL is legally a Town, not a
    # City, even though the user referred to it as "City of Jupiter" when requesting
    # it; flag to the user if "City of Jupiter" is actually the preferred display name.
    events = []
    base_domain = "https://www.jupiter.fl.us"
    calendar_cid = "35"

    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()
    next_month = 1 if curr_month == 12 else curr_month + 1
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    months_to_scrape = [
        {"year": curr_year, "month": curr_month},
        {"year": next_year, "month": next_month},
    ]

    seen_keys = set()

    for target in months_to_scrape:
        y_val, m_val = target["year"], target["month"]
        last_day = calendar.monthrange(y_val, m_val)[1]
        start_str = f"{m_val:02d}/01/{y_val}"
        end_str = f"{m_val:02d}/{last_day:02d}/{y_val}"

        # --- Strategy 1: Wellington-style schema.org microdata grid ---
        schema_url = (
            f"{base_domain}/calendar.aspx?Keywords=&startDate={start_str}"
            f"&enddate={end_str}&CID={calendar_cid}&showPastEvents=false"
        )
        res = fetch_hardened(schema_url, referer=f"{base_domain}/calendar.aspx")
        if res is None:
            print(f"[Jupiter] Request failed (schema.org attempt) for {y_val}-{m_val:02d}")
        else:
            print(f"[Jupiter] Fetching {y_val}-{m_val:02d} (schema.org attempt) | HTTP Status: {res.status_code}")

        found_this_month = False
        if res is not None and res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            event_divs = soup.find_all("div", itemtype="http://schema.org/Event")
            print(f"[Jupiter] Found {len(event_divs)} schema.org event blocks for {y_val}-{m_val:02d}.")

            for ev_div in event_divs:
                name_span = ev_div.find("span", itemprop="name")
                date_span = ev_div.find("span", itemprop="startDate")
                if not name_span or not date_span:
                    continue

                raw_title = name_span.get_text(strip=True)
                iso_datetime_str = date_span.get_text(strip=True)
                clean_title = clean_event_title(raw_title)

                if not is_qualifying_event(clean_title):
                    continue

                try:
                    dt = datetime.strptime(iso_datetime_str, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    continue
                if not (current_month_start <= dt < lookahead_end):
                    continue

                iso_date = dt.strftime("%Y-%m-%d")
                meeting_time = dt.strftime("%-I:%M %p")

                full_link = f"{base_domain}/Calendar.aspx"
                parent_item = ev_div.find_parent("div", class_="monthItem")
                if parent_item and parent_item.get("id"):
                    eid_match = re.match(r'parentdiv(\d+)_', parent_item["id"])
                    if eid_match:
                        eid = eid_match.group(1)
                        full_link = (
                            f"{base_domain}/Calendar.aspx?EID={eid}"
                            f"&month={dt.month}&year={dt.year}&day={dt.day}&calType=0"
                        )

                dedup_key = (clean_title, iso_date)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                found_this_month = True

                events.append({
                    "id": f"jup-{iso_date}-{hash(full_link)}",
                    "muni_short": "JUP",
                    "muni_full": "Town of Jupiter",
                    "title": clean_title,
                    "date": iso_date,
                    "time": meeting_time,
                    "link": full_link,
                    "summary": f"Official {clean_title} meeting."
                })

        # --- Strategy 2: PBG/Boca/Boynton-style ?view=list EID= row parse ---
        # Only attempted if strategy 1 found nothing for this month, since a real
        # schema.org match is the stronger signal when both happen to fire.
        if not found_this_month:
            list_url = f"{base_domain}/calendar.aspx?view=list&year={y_val}&month={m_val}&CID={calendar_cid}"
            res2 = fetch_hardened(list_url, referer=f"{base_domain}/calendar.aspx")
            if res2 is None:
                print(f"[Jupiter] Request failed (list-view attempt) for {y_val}-{m_val:02d}")
                continue
            print(f"[Jupiter] Fetching {y_val}-{m_val:02d} (list-view attempt) | HTTP Status: {res2.status_code}")
            if res2.status_code != 200:
                continue

            soup2 = BeautifulSoup(res2.text, "html.parser")
            event_rows = soup2.find_all(lambda tag: tag.name in ["tr", "li", "div"] and tag.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h)))
            print(f"[Jupiter] Found {len(event_rows)} EID= row matches (list-view attempt) for {y_val}-{m_val:02d}.")

            if not event_rows:
                # Nothing from either strategy - dump a raw slice for debugging,
                # anchored on a guaranteed real string rather than the page head.
                anchor_idx = res2.text.find("Jupiter")
                if anchor_idx == -1:
                    anchor_idx = 0
                print(f"[Jupiter] DEBUG raw HTML slice (list-view attempt): "
                      f"{res2.text[anchor_idx:anchor_idx + 1500]}")
                continue

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
                meeting_time = time_match.group(1).strip().upper() if time_match else "6:00 PM"
                if "AM" not in meeting_time and "PM" not in meeting_time:
                    meeting_time += " PM"

                if not iso_date or not is_qualifying_event(clean_title):
                    continue

                dt = datetime.strptime(iso_date, "%Y-%m-%d")
                if current_month_start <= dt < lookahead_end:
                    dedup_key = (clean_title, iso_date)
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        events.append({
                            "id": f"jup-{iso_date}-{hash(full_link)}",
                            "muni_short": "JUP",
                            "muni_full": "Town of Jupiter",
                            "title": clean_title,
                            "date": iso_date,
                            "time": meeting_time,
                            "link": full_link,
                            "summary": f"Official {clean_title} meeting."
                        })

    print(f"[Jupiter] Extracted {len(events)} events.")
    return events


# --- 12. RIVIERA BEACH MODULE (added this session — real markup confirmed by user,
#          sandbox execution still unconfirmed, see comments) ---
def scrape_riviera_beach():
    # City of Riviera Beach - https://www.rivierabch.com/ccm. The city's own site is
    # QScend, not any platform previously seen in this project - but its meetings list
    # isn't served there at all. /ccm embeds an <iframe> pointing directly at
    # https://rivierabeach.granicus.com/ViewPublisher.php?view_id=1, a standard
    # Granicus "Legislative Management" ViewPublisher page (a different Granicus
    # product from WPB's OpenCities per-series static pages - this is the classic
    # ViewPublisher agenda/minutes table). This is a sixth distinct platform for this
    # project.
    #
    # Unlike Westlake/Jupiter, this table's real HTML was NOT hypothesized from a
    # WebFetch rendered preview - the user pasted the literal raw <tr> from
    # View Page Source on the live granicus.com page directly, e.g.:
    #
    #   <tr class="listingRow">
    #     <td class="listItem" headers="Name" id="City-Council" scope="row">City Council</td>
    #     <td class="listItem" headers="Date City-Council">Aug&nbsp;19,&nbsp;2026 - 06:00&nbsp;PM</td>
    #     <td class="listItem" headers="Duration City-Council">04h&nbsp;00m</td>
    #     <td class="listItem"><a href="//rivierabeach.granicus.com/AgendaViewer.php?view_id=1&event_id=626" target="_blank">Agenda</a></td>
    #     <td class="listItem">&nbsp;</td>  <!-- Minutes column, blank when not posted -->
    #   </tr>
    #
    # So the column layout (Name, Date, Duration, Agenda, Minutes) and the real agenda
    # link pattern (`//rivierabeach.granicus.com/AgendaViewer.php?view_id=1&event_id=N`,
    # protocol-relative) are both confirmed ground truth, not a guess - the same
    # standard as PBC/Delray/Wellington/Palm Beach's confirmed pieces.
    #
    # What's NOT confirmed: this session's sandbox has zero outbound network route to
    # granicus.com (a plain curl was rejected by the egress policy, and a WebFetch
    # rendered-preview attempt was separately blocked by that domain's robots.txt) -
    # so fetch_hardened() below has never actually been executed against the real
    # page. Written defensively per Key Methodological Lesson #2: agenda-link
    # detection uses a href-pattern search (not a hardcoded column index) so it
    # survives column reordering, and heavy debug logging is included so the first
    # real GitHub Actions run's `[Riviera Beach]`-prefixed log lines can confirm or
    # correct anything below. Ask the user to run the workflow and paste that back.
    #
    # Per the project-wide "No Agenda Available" policy: a real, dated/timed meeting
    # with no Agenda link yet (blank Minutes-style cell, no <a> match) is still
    # included with has_agenda=False, and "link" falls back to the ViewPublisher page
    # itself rather than a guessed/dead URL.
    #
    # Whitelist note (flag to user, don't silently add): real titles seen in the
    # user's screenshot include "Utility Special District", "Utility Special District
    # Budget Workshop", "FY2027 Budget Workshop", and "Community Awards and
    # Presentations Program" - none of these match any current is_qualifying_event
    # pattern and will be silently excluded, consistent with Westlake's Education
    # Advisory Board / Palm Beach's Architectural Commission precedent. "City Council",
    # "City Council Budget Workshop" (contains "City Council"), "Community
    # Redevelopment Agency", and "Planning and Zoning Board Meeting" all already
    # qualify under the existing whitelist with no changes needed.
    events = []
    base_domain = "https://rivierabeach.granicus.com"
    target_url = f"{base_domain}/ViewPublisher.php?view_id=1"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url, referer="https://www.rivierabch.com/ccm")
    if res is None:
        print("[Riviera Beach] Request failed.")
        return events
    print(f"[Riviera Beach] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    rows = soup.find_all("tr", class_="listingRow")
    if not rows:
        # Fall back to any <tr> containing a "listItem" cell, in case the real class
        # name on "listingRow" differs slightly from the one confirmed row.
        rows = soup.find_all(lambda tag: tag.name == "tr" and tag.find("td", class_="listItem"))
    print(f"[Riviera Beach] Found {len(rows)} candidate meeting rows.")

    if not rows:
        idx = res.text.find("listingRow")
        if idx == -1:
            idx = res.text.find("AgendaViewer")
        if idx == -1:
            idx = res.text.find("City Council")
        print(f"[Riviera Beach] No rows found. DEBUG raw HTML slice: "
              f"{res.text[max(0, idx - 200): idx + 1000] if idx != -1 else res.text[:1000]}")
        return events

    print(f"[Riviera Beach] Sample first row raw HTML (for debugging column layout):\n{rows[0]}")

    seen_keys = set()

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        raw_title = cells[0].get_text(strip=True)
        clean_title = clean_event_title(raw_title)
        if not clean_title or not is_qualifying_event(clean_title):
            continue

        date_text = re.sub(r'\s+', ' ', cells[1].get_text(separator=" ", strip=True)).strip()
        date_match = re.search(
            r'([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{4}).*?(\d{1,2}:\d{2}\s*[AP]M)',
            date_text, re.I
        )
        if not date_match:
            print(f"[Riviera Beach] Could not parse date/time from cell text: {date_text!r} - skipping row.")
            continue
        month_str, day_str, year_str, time_str = date_match.groups()
        time_str_compact = re.sub(r'\s+', '', time_str.upper())  # "06:00PM"

        dt = None
        for fmt in ("%b %d %Y %I:%M%p", "%B %d %Y %I:%M%p"):
            try:
                dt = datetime.strptime(f"{month_str} {day_str} {year_str} {time_str_compact}", fmt)
                break
            except ValueError:
                continue
        if dt is None:
            print(f"[Riviera Beach] Date parse failed for groups {date_match.groups()} - skipping row.")
            continue

        if not (current_month_start <= dt < lookahead_end):
            continue

        iso_date = dt.strftime("%Y-%m-%d")
        meeting_time = dt.strftime("%-I:%M %p")

        # Agenda link: search by href pattern rather than a fixed column index, since
        # the confirmed sample only shows one real row and column order beyond
        # Name/Date isn't guaranteed stable (Duration/Agenda/Minutes/Video).
        agenda_link_elem = row.find("a", href=re.compile(r'AgendaViewer\.php', re.I))
        has_agenda = agenda_link_elem is not None
        if has_agenda:
            href = agenda_link_elem.get("href", "").strip()
            if href.startswith("//"):
                full_link = f"https:{href}"
            elif href.startswith("http"):
                full_link = href
            else:
                full_link = f"{base_domain}/{href.lstrip('/')}"
        else:
            full_link = target_url  # Source page itself, never a guessed/dead link.

        dedup_key = (clean_title, iso_date, meeting_time)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"riv-{iso_date}-{hash(clean_title + meeting_time)}",
            "muni_short": "RIVBEACH",
            "muni_full": "City of Riviera Beach",
            "title": clean_title,
            "date": iso_date,
            "time": meeting_time,
            "link": full_link,
            "has_agenda": has_agenda,
            "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
        })

    print(f"[Riviera Beach] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


# --- 13. TOWN OF JUNO BEACH MODULE ---
def scrape_juno_beach():
    # Town of Juno Beach - IMPORTANT: the town's own page,
    # https://www.juno-beach.fl.us/1203/Agendas-Minutes, does NOT contain the meetings
    # table in its server-rendered HTML at all. This was discovered on a real GitHub
    # Actions run: HTTP 200, 78,832 bytes returned, but no table and no "Town Council"
    # string anywhere in the raw response - the debug dump showed only <head>
    # boilerplate (GTM snippet, meta tags). The user then inspected the live page and
    # found the real content is loaded via a same-page wrapper that embeds:
    #   <iframe id="child-iframe" src="https://legacyjuno-beach.teammunicode.com/meetings" ...>
    # So the real source to scrape is that iframe's target directly - MuniCode's
    # "Meetings" portal on a "teammunicode.com" subdomain, the same underlying
    # platform/product as Westlake (meetings.municode.com), just a different hostname
    # scheme. This is confirmed by the user's own live inspection (real DOM, not a
    # rendered-preview hypothesis) - this project's Key Methodological Lesson #1
    # standard (page-wrapper JS content isn't visible in the wrapper's own raw HTML).
    #
    # The user also confirmed a real Agenda PDF link, which matches MuniCode's blob
    # storage pattern exactly (same host family as Westlake's mccmeetings.blob... URLs):
    #   https://mccmeetings.blob.core.usgovcloudapi.net/jnobeachfl-pubu/MEET-Agenda-<hash>.pdf
    #
    # Per the user's screenshot of the rendered table, the columns are:
    #   Date | Meeting | Agenda | Agenda Packet | Minutes | Video | View
    # with the Agenda AND Agenda Packet columns each showing two icons (PDF, HTML)
    # once a document is posted, and both blank when nothing is posted yet. Per
    # explicit user instruction: only the "Agenda" column's PDF link should be used -
    # never "Agenda Packet", and never the HTML version.
    #
    # Per Key Methodological Lesson #2: this session had ZERO outbound access to
    # legacyjuno-beach.teammunicode.com either - a direct curl from the sandbox shell
    # was rejected outright by the egress/org policy (connect_rejected), and a WebFetch
    # attempt was blocked by that host's robots.txt. So the actual raw HTML of
    # /meetings on the teammunicode.com host has still never been seen this session -
    # only the wrapper page's failure (confirmed real) and the user's screenshot of the
    # rendered table (still a hypothesis about markup, per Lesson #1). This function
    # now targets the CORRECT real host, which is a major improvement over the first
    # pass (which was fetching a page that could never have worked), but the table
    # markup itself is still unconfirmed. Written defensively and logging heavily so a
    # real run's log can confirm/correct these assumptions.
    events = []
    base_domain = "https://legacyjuno-beach.teammunicode.com"
    target_url = f"{base_domain}/meetings"
    # Kept for the "no agenda yet" fallback link and for user-facing context - this is
    # the page a human should land on, even though it's not what gets scraped.
    public_page_url = "https://www.juno-beach.fl.us/1203/Agendas-Minutes"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url, referer=public_page_url)
    if res is None:
        print("[Juno Beach] Request failed.")
        return events
    print(f"[Juno Beach] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    # Same defensive table-finding approach as scrape_westlake(): try id/class hints
    # first, then fall back to the largest table on the page by row count.
    table = soup.find("table", id=re.compile(r'meeting', re.I))
    if not table:
        table = soup.find("table", class_=re.compile(r'meeting', re.I))
    if not table:
        candidate_tables = soup.find_all("table")
        if candidate_tables:
            table = max(candidate_tables, key=lambda t: len(t.find_all("tr")))

    if not table:
        print("[Juno Beach] No table found on the page at all. Dumping a slice of raw "
              "HTML around a known meeting-type string for debugging:")
        idx = res.text.find("Town Council")
        print(res.text[max(0, idx - 200): idx + 500] if idx != -1 else res.text[:700])
        return events

    header_cells = table.find("tr")
    header_texts = [c.get_text(strip=True).lower() for c in header_cells.find_all(["th", "td"])] if header_cells else []
    # Match the "Agenda" column exactly (not "Agenda Packet", which also contains the
    # substring "agenda" - the two are visually adjacent columns per the user's
    # screenshot and must not be confused).
    agenda_col_idx = next(
        (i for i, h in enumerate(header_texts) if h.strip() == "agenda"), None
    )
    if agenda_col_idx is None:
        # Fallback: first column whose header contains "agenda" but not "packet".
        agenda_col_idx = next(
            (i for i, h in enumerate(header_texts) if "agenda" in h and "packet" not in h),
            None,
        )
    print(f"[Juno Beach] Table header row: {header_texts} | agenda column index: {agenda_col_idx}")

    all_rows = table.find_all("tr")
    data_rows = all_rows[1:] if header_texts else all_rows
    print(f"[Juno Beach] Found {len(data_rows)} candidate data rows.")

    if data_rows:
        print(f"[Juno Beach] Sample first row raw HTML (for debugging column layout):\n{data_rows[0]}")

    seen_keys = set()

    for row in data_rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        row_text = row.get_text(separator=" ", strip=True)

        # Date+time observed in the screenshot as e.g. "09/23/2026 - 5:30pm".
        date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', row_text)
        if not date_match:
            continue
        m, d, y = date_match.groups()
        iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            continue
        if not (current_month_start <= dt < lookahead_end):
            continue

        time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.I)
        meeting_time = time_match.group(1).upper().replace(" ", "") if time_match else "6:00 PM"
        if len(meeting_time) > 2 and meeting_time[-2:] in ("AM", "PM") and meeting_time[-3] != " ":
            meeting_time = meeting_time[:-2] + " " + meeting_time[-2:]

        raw_title = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        clean_title = clean_event_title(raw_title)
        if not clean_title or not is_qualifying_event(clean_title):
            continue

        # Agenda availability: look ONLY in the identified "Agenda" column (never
        # "Agenda Packet") for a link that points at a PDF - MuniCode's real confirmed
        # PDF links look like
        # https://mccmeetings.blob.core.usgovcloudapi.net/jnobeachfl-pubu/MEET-Agenda-<hash>.pdf,
        # while the HTML-version icon in the same cell links to an HTML document
        # instead - per explicit user instruction, only the PDF link should be used.
        agenda_link = None
        if agenda_col_idx is not None and agenda_col_idx < len(cells):
            agenda_cell = cells[agenda_col_idx]
            # Prefer a link that explicitly points at a .pdf.
            agenda_link = agenda_cell.find("a", href=re.compile(r'\.pdf(\?|$)', re.I))
            if not agenda_link:
                # Fall back to any link in the cell whose text/title/icon suggests PDF
                # (e.g. an <img alt="PDF">) rather than HTML.
                for a in agenda_cell.find_all("a", href=True):
                    label = a.get_text(strip=True).lower()
                    img_alt = " ".join(img.get("alt", "") for img in a.find_all("img")).lower()
                    if "pdf" in label or "pdf" in img_alt:
                        agenda_link = a
                        break
            if not agenda_link:
                # Last resort: take the first link in the cell, but only if there's no
                # sign it's specifically an HTML-labeled one (to avoid grabbing the
                # wrong icon when the PDF-detection heuristics above don't match).
                candidates = agenda_cell.find_all("a", href=True)
                non_html_candidates = [
                    a for a in candidates
                    if "html" not in a.get_text(strip=True).lower()
                    and "html" not in " ".join(img.get("alt", "") for img in a.find_all("img")).lower()
                ]
                if non_html_candidates:
                    agenda_link = non_html_candidates[0]

        has_agenda = agenda_link is not None
        if has_agenda:
            href = agenda_link.get("href", "").strip()
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
        else:
            # Fall back to the town's own public Agendas & Minutes page (not the raw
            # teammunicode.com scrape target) - that's the page a human visitor
            # actually lands on and can browse from, never a dead/internal link.
            full_link = public_page_url

        dedup_key = (clean_title, iso_date, meeting_time)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"junobeach-{iso_date}-{hash(clean_title + meeting_time)}",
            "muni_short": "JUNOBEACH",
            "muni_full": "Town of Juno Beach",
            "title": clean_title,
            "date": iso_date,
            "time": meeting_time,
            "link": full_link,
            "has_agenda": has_agenda,
            "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
        })

    print(f"[Juno Beach] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


# --- 14. JUPITER INLET COLONY MODULE ---
def scrape_jupiter_inlet_colony():
    # Town of Jupiter Inlet Colony - https://www.jupiterinletcolony.gov/AgendaCenter
    # This is an AgendaCenter platform (a new platform for this project, not CivicPlus/
    # Legistar/Granicus/MuniCode/CivicClerk/WordPress/Granicus). The page displays a
    # searchable agenda/minutes interface with collapsible sections per governing body.
    #
    # From the user's screenshot (real AgendaCenter page capture, not a rendered-preview
    # hypothesis): the main content shows a "Town Commission" section with a table listing
    # meetings by date. Each row contains:
    #   - Date (e.g. "Aug 19, 2026")
    #   - Meeting Title (e.g. "Town Commission Budget Workshop", "Regular Town Commission Meeting")
    #   - Agenda link (in "Agenda" column, with a "Download" button or link when posted)
    #   - Minutes column (when available)
    #   - Media column (when available)
    #   - Download button (for the agenda document)
    #
    # This first-pass scraper is UNCONFIRMED against real raw HTML from a GitHub Actions run.
    # Per Key Methodological Lesson #1/2 in handoff.md: a screenshot is a hypothesis about
    # markup, not ground truth. This is written defensively with heavy logging so a real run's
    # output can confirm or correct the selectors/structure. Ask the user to run the workflow
    # and paste back the [Jupiter Inlet Colony]-prefixed log lines if the extracted count
    # looks wrong.
    #
    # Whitelist note: "Local Planning Agency Meeting" is included per explicit user request
    # (added to is_qualifying_event whitelist above).
    events = []
    base_domain = "https://www.jupiterinletcolony.gov"
    target_url = f"{base_domain}/AgendaCenter"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url)
    if res is None:
        print("[Jupiter Inlet Colony] Request failed.")
        return events
    print(f"[Jupiter Inlet Colony] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    # AgendaCenter typically structures agendas in a table or list. Look for rows/items
    # that contain meeting information. Since the exact structure is unconfirmed, try
    # multiple selectors: look for table rows first, then fall back to list items or divs.

    # Strategy 1: Look for table rows (most likely based on the screenshot showing a table)
    rows = soup.find_all("tr")
    if not rows:
        # Strategy 2: Look for list items (if it's a list-based layout instead)
        rows = soup.find_all("li")
    if not rows:
        # Strategy 3: Look for divs that might contain meeting data (generic fallback)
        rows = soup.find_all("div", class_=re.compile(r'meeting|agenda|row', re.I))

    print(f"[Jupiter Inlet Colony] Found {len(rows)} candidate rows/items on the page.")

    if not rows:
        print("[Jupiter Inlet Colony] No rows found. Dumping a slice of raw HTML "
              "around a known meeting-type string for debugging:")
        idx = res.text.find("Town Commission")
        print(res.text[max(0, idx - 200): idx + 500] if idx != -1 else res.text[:700])
        return events

    if rows:
        print(f"[Jupiter Inlet Colony] Sample first row raw HTML (for debugging):\n{rows[0]}")

    seen_keys = set()
    date_pattern = re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
        r'\.?\s+(\d{1,2}),?\s*(\d{4})\b',
        re.I
    )

    for row in rows:
        row_text = row.get_text(separator=" ", strip=True)
        if not row_text or len(row_text) < 5:
            continue

        # Try to extract a date in "Month DD, YYYY" format from the row
        date_match = date_pattern.search(row_text)
        if not date_match:
            continue

        try:
            month_name, day, year = date_match.groups()
            # Normalize abbreviated month names to full names for strptime
            month_map = {
                'jan': 'January', 'feb': 'February', 'mar': 'March', 'apr': 'April',
                'may': 'May', 'jun': 'June', 'jul': 'July', 'aug': 'August',
                'sep': 'September', 'oct': 'October', 'nov': 'November', 'dec': 'December'
            }
            month_full = month_map.get(month_name.lower()[:3], month_name)
            dt = datetime.strptime(f"{month_full} {day} {year}", "%B %d %Y")
        except ValueError:
            continue

        iso_date = dt.strftime("%Y-%m-%d")
        if not (current_month_start <= dt < lookahead_end):
            continue

        # Extract the meeting title - look for text between the date and any link/button
        # In AgendaCenter, the title typically appears right after the date
        clean_title = "Town Commission Meeting"  # Default fallback

        # Try to find a more specific title from the row text
        # Look for common patterns like "Regular Town Commission Meeting", "Special Town Commission Meeting", etc.
        title_patterns = [
            r'(?:Regular|Special|Emergency)?\s*Town Commission\s+(?:Meeting|Hearing|Workshop|Retreat)',
            r'Town Commission\s+(?:Budget\s+)?Workshop',
            r'Local Planning Agency\s+Meeting',
        ]
        for pattern_str in title_patterns:
            title_match = re.search(pattern_str, row_text, re.I)
            if title_match:
                clean_title = clean_event_title(title_match.group(0))
                break

        if not clean_title or not is_qualifying_event(clean_title):
            continue

        # Try to extract time if present; AgendaCenter may or may not show times prominently
        time_match = re.search(r'(\d{1,2}):(\d{2})\s*([AP]M)', row_text, re.I)
        meeting_time = time_match.group(0).upper() if time_match else "6:00 PM"

        # Check if an agenda is available by looking for a link in the row
        agenda_link = row.find("a", href=True) if hasattr(row, 'find') else None
        has_agenda = agenda_link is not None

        if has_agenda:
            href = agenda_link.get("href", "").strip()
            full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
        else:
            full_link = target_url  # Fall back to the main AgendaCenter page

        dedup_key = (clean_title, iso_date, meeting_time)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append({
            "id": f"juic-{iso_date}-{hash(clean_title + meeting_time)}",
            "muni_short": "JUIC",
            "muni_full": "Town of Jupiter Inlet Colony",
            "title": clean_title,
            "date": iso_date,
            "time": meeting_time,
            "link": full_link,
            "has_agenda": has_agenda,
            "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
        })

    print(f"[Jupiter Inlet Colony] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
    return events


def scrape_manalapan():
    # Town of Manalapan - https://manalapan.org/agenda-minutes/
    # WordPress site (meta-generator: "WordPress Download Manager 3.3.68") - a new
    # WordPress instance for this project, distinct from Downtown WPB DDA's WordPress
    # page (that one's a bulleted <li> date list; this one is HTML <table>s grouped by
    # month, per a WebFetch rendered preview). No iframe / third-party civic platform
    # detected.
    #
    # Per two WebFetch rendered-preview passes of the live page (UNCONFIRMED against
    # raw HTML - see Key Methodological Lesson #1 in handoff.md; this sandbox also has
    # zero direct network route to manalapan.org, confirmed via a plain curl rejected
    # by the egress policy - Lesson #2):
    #   - Table columns: Description | Meeting Date | Agenda | Packet | Minutes | Recording
    #   - A sample row's Description cell reads like "01/08/2026 – Architectural
    #     Commission Meeting" (date prefix + en-dash + body/meeting name); the separate
    #     Meeting Date cell repeats the date in "Month D, YYYY" form.
    #   - Agenda/Packet/Minutes/Recording cells contain a "Download" link to a
    #     wp-content/uploads/YYYY/MM/*.pdf URL when posted, or an em-dash "—" placeholder
    #     when not.
    #   - Real body/meeting names seen on the page: "Architectural Commission Meeting",
    #     "Town Commission Meeting", "Pension Board Meeting", "Special Magistrate
    #     Hearing", "First Budget Hearing", "Final Budget Hearing Meeting".
    #
    # Written defensively per Lesson #2: table selection tries id/class hints first,
    # then falls back to the largest <table> by row count; the Agenda column is located
    # by header text (never a hardcoded index, matching the Westlake/Juno Beach
    # pattern) and explicitly excludes any header containing "packet" so the Packet
    # column's link is never grabbed by mistake. Heavy debug logging (header row, row
    # count, first raw row) is included so a real GitHub Actions run's
    # "[Manalapan]"-prefixed log lines can confirm or correct this before it's trusted.
    # Treat this as an UNCONFIRMED first-pass draft, same category as Westlake/Juno
    # Beach at the time they were added.
    #
    # Whitelist note - NOT resolved, flagged rather than guessed: of the six real
    # meeting/body names above, only "Town Commission Meeting" currently matches
    # is_qualifying_event() (via the existing \bTown Commission\b pattern). "Architectural
    # Commission", "Pension Board", "First/Final Budget Hearing" don't match anything,
    # and "Special Magistrate Hearing" matches a body name this project's whitelist has
    # explicitly and deliberately excluded before (Special Magistrate, dropped per past
    # user request) - so it is intentionally left out here too, consistent with that
    # standing decision. Ask the user which (if any) of the other four should be added
    # before extending the whitelist.
    events = []
    base_domain = "https://manalapan.org"
    target_url = f"{base_domain}/agenda-minutes/"

    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    res = fetch_hardened(target_url)
    if res is None:
        print("[Manalapan] Request failed.")
        return events
    print(f"[Manalapan] HTTP Status: {res.status_code}, body length: {len(res.text) if res.text else 0}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")

    tables = soup.find_all("table")
    print(f"[Manalapan] Found {len(tables)} <table> elements on the page.")

    seen_keys = set()
    date_pattern = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')

    for table in tables:
        header_row = table.find("tr")
        header_texts = (
            [c.get_text(strip=True).lower() for c in header_row.find_all(["th", "td"])]
            if header_row else []
        )
        agenda_col_idx = next(
            (i for i, h in enumerate(header_texts) if "agenda" in h and "packet" not in h),
            None
        )

        all_rows = table.find_all("tr")
        data_rows = all_rows[1:] if header_texts else all_rows
        if not data_rows:
            continue

        print(f"[Manalapan] Table header row: {header_texts} | agenda column index: {agenda_col_idx} "
              f"| {len(data_rows)} candidate data rows.")
        print(f"[Manalapan] Sample first row raw HTML (for debugging column layout):\n{data_rows[0]}")

        for row in data_rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            row_text = row.get_text(separator=" ", strip=True)
            date_match = date_pattern.search(row_text)
            if not date_match:
                continue
            mo, day, yr = date_match.groups()
            try:
                dt = datetime(int(yr), int(mo), int(day))
            except ValueError:
                continue
            iso_date = dt.strftime("%Y-%m-%d")
            if not (current_month_start <= dt < lookahead_end):
                continue

            # Description cell is expected to hold "MM/DD/YYYY – Body Meeting"; strip
            # any leading date + separator, leaving the meeting/body name.
            desc_text = cells[0].get_text(strip=True)
            title_text = date_pattern.sub("", desc_text).lstrip(" -–—:").strip()
            clean_title = clean_event_title(title_text) if title_text else clean_event_title(row_text)
            if not clean_title or not is_qualifying_event(clean_title):
                continue

            time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', row_text, re.I)
            meeting_time = time_match.group(1).upper().replace(" ", "") if time_match else "6:00 PM"
            if len(meeting_time) > 2 and meeting_time[-2:] in ("AM", "PM") and meeting_time[-3] != " ":
                meeting_time = meeting_time[:-2] + " " + meeting_time[-2:]

            agenda_link = None
            if agenda_col_idx is not None and agenda_col_idx < len(cells):
                agenda_link = cells[agenda_col_idx].find("a", href=True)
            if not agenda_link:
                agenda_link = row.find(
                    "a", href=re.compile(r'agenda(?!.*packet)', re.I)
                )

            has_agenda = agenda_link is not None
            if has_agenda:
                href = agenda_link.get("href", "").strip()
                full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
            else:
                full_link = target_url  # No agenda posted yet - point at the source page.

            dedup_key = (clean_title, iso_date, meeting_time)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            events.append({
                "id": f"manalapan-{iso_date}-{hash(clean_title + meeting_time)}",
                "muni_short": "MANALAPAN",
                "muni_full": "Town of Manalapan",
                "title": clean_title,
                "date": iso_date,
                "time": meeting_time,
                "link": full_link,
                "has_agenda": has_agenda,
                "summary": f"Official {clean_title} meeting." if has_agenda else f"Official {clean_title} meeting. No agenda posted yet.",
            })

    if not tables or not events:
        print("[Manalapan] No qualifying events extracted. Dumping a slice of raw HTML "
              "around a known meeting-type string for debugging:")
        idx = res.text.find("Town Commission")
        if idx == -1:
            idx = res.text.find("Agenda")
        print(res.text[max(0, idx - 200): idx + 500] if idx != -1 else res.text[:700])

    print(f"[Manalapan] Extracted {len(events)} events "
          f"({sum(1 for e in events if e['has_agenda'])} with agendas, "
          f"{sum(1 for e in events if not e['has_agenda'])} without).")
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
    all_events.extend(scrape_westlake())
    all_events.extend(scrape_downtown_wpb_dda())
    all_events.extend(scrape_palm_beach())
    all_events.extend(scrape_jupiter())
    all_events.extend(scrape_riviera_beach())
    all_events.extend(scrape_juno_beach())
    all_events.extend(scrape_jupiter_inlet_colony())
    all_events.extend(scrape_manalapan())

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2)

    print(f"Execution complete. Saved {len(all_events)} unique matching events to data.json.")

if __name__ == "__main__":
    main()
