import os
import re
import json
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
    governance_keywords = [
        r'\bCouncil\b', r'\bCommission\b', r'\bBoard\b', r'\bCommittee\b',
        r'\bAuthority\b', r'\bAgency\b', r'\bCRA\b', r'\bZoning\b', r'\bPlanning\b',
        r'\bHistoric\b', r'\bSpecial Magistrate\b', r'\bCode Enforcement\b',
        r'\bTask Force\b', r'\bTown Hall\b', r'\bHearing\b', r'\bWorkshop\b', r'\bBCC\b'
    ]
    pattern = re.compile('|'.join(governance_keywords), re.I)
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


# --- 1. WEST PALM BEACH MODULE ---
def scrape_west_palm_beach():
    # The old target (wpb.org/government/city-commission-agendas) is stale and returns
    # a 403 from a WAF challenge page, not a real content 403. WPB runs the same
    # CivicPlus platform as Palm Beach Gardens, so try the same calendar list-view
    # mechanism first (clean EID links + dates, no PDF parsing needed). If that comes
    # back empty, fall back to the City Clerk's "Commission & CRA Agendas" hub, which
    # lists agenda PDFs named like "04_13_26_FINAL-City-Commission-Agenda.pdf".
    events = []
    base_domain = "https://www.wpb.org"
    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()
    next_month = 1 if curr_month == 12 else curr_month + 1
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    months_to_scrape = [
        {"year": curr_year, "month": curr_month},
        {"year": next_year, "month": next_month},
    ]
    seen_keys = set()

    # --- Attempt 1: CivicPlus calendar list view (mirrors the working PBG mechanism) ---
    for target in months_to_scrape:
        y_val, m_val = target["year"], target["month"]
        url = f"{base_domain}/calendar.aspx?view=list&year={y_val}&month={m_val}"
        res = fetch_hardened(url, referer=f"{base_domain}/calendar.aspx")
        if res is None:
            print(f"[WPB Calendar] Request failed for {y_val}-{m_val:02d}")
            continue
        print(f"[WPB Calendar] Fetching {y_val}-{m_val:02d} | HTTP Status: {res.status_code}")
        if res.status_code != 200:
            continue

        soup = BeautifulSoup(res.text, "html.parser")
        event_rows = soup.find_all(lambda tag: tag.name in ["tr", "li", "div"] and tag.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h)))
        if not event_rows:
            event_rows = soup.select(".calendarItem, .eventRow, table.calendarList tr, ol.calendarList > li")

        for row in event_rows:
            row_text = row.text.strip()
            if not row_text:
                continue
            link_elem = row.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h))
            if not link_elem:
                continue

            raw_title = link_elem.text.strip() or (link_elem.parent.text.strip() if link_elem.parent else "")
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

            time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)', row_text)
            meeting_time = time_match.group(1).strip().upper() if time_match else "5:00 PM"
            if "AM" not in meeting_time and "PM" not in meeting_time:
                meeting_time += " PM"

            if iso_date and is_qualifying_event(clean_title):
                dt = datetime.strptime(iso_date, "%Y-%m-%d")
                if current_month_start <= dt < lookahead_end:
                    dedup_key = (clean_title, iso_date)
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        events.append({
                            "id": f"wpb-{iso_date}-{hash(full_link)}",
                            "muni_short": "WPB",
                            "muni_full": "City of West Palm Beach",
                            "title": clean_title,
                            "date": iso_date,
                            "time": meeting_time,
                            "link": full_link,
                            "summary": f"Official {clean_title} meeting."
                        })

    if events:
        print(f"[WPB] Extracted {len(events)} events via calendar list view.")
        return events

    # --- Attempt 2 (fallback): City Clerk Commission & CRA Agendas hub, PDF filenames ---
    print("[WPB] Calendar list view returned nothing, falling back to Agendas hub.")
    hub_url = f"{base_domain}/Our-City/City-Clerk/Commission-CRA-Agendas/City-Commission-Agendas"
    res = fetch_hardened(hub_url)
    if res is None:
        print("[WPB Agendas] Request failed.")
        return events
    print(f"[WPB Agendas] HTTP Status: {res.status_code}")
    if res.status_code != 200:
        return events

    soup = BeautifulSoup(res.text, "html.parser")
    for a in soup.find_all("a", href=re.compile(r'\.pdf', re.I)):
        href = a.get("href", "").strip()
        # Filenames look like: 04_13_26_FINAL-City-Commission-Agenda.pdf
        fname_match = re.search(r'(\d{2})_(\d{2})_(\d{2})[_-].*(?:Commission|CRA|Agenda)', href, re.I)
        if not fname_match:
            continue
        mm, dd, yy = fname_match.groups()
        iso_date = f"20{yy}-{mm}-{dd}"
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            continue
        if not (current_month_start <= dt < lookahead_end):
            continue

        clean_title = "City Commission Meeting"
        full_link = href if href.startswith("http") else f"{base_domain}/{href.lstrip('/')}"
        dedup_key = (clean_title, iso_date)
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            events.append({
                "id": f"wpb-{iso_date}-{hash(full_link)}",
                "muni_short": "WPB",
                "muni_full": "City of West Palm Beach",
                "title": clean_title,
                "date": iso_date,
                "time": "5:00 PM",
                "link": full_link,
                "summary": f"Official {clean_title} meeting."
            })

    print(f"[WPB] Extracted {len(events)} events via Agendas hub fallback.")
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
    return scrape_legistar_portal("BOCA", "City of Boca Raton", "https://bocaraton.legistar.com/")

def scrape_boynton_beach():
    return scrape_legistar_portal("BOYNTON", "City of Boynton Beach", "https://boyntonbeach.legistar.com/")

def scrape_delray_beach():
    return scrape_legistar_portal("DELRAY", "City of Delray Beach", "https://delraybeach.legistar.com/")


# --- 6. PALM BEACH GARDENS MODULE ---
def scrape_palm_beach_gardens():
    events = []
    base_domain = "https://www.pbgfl.gov"
    calendar_base_url = "https://www.pbgfl.gov/calendar.aspx"
    
    current_month_start, lookahead_end, curr_year, curr_month = get_dual_month_bounds()

    next_month = 1 if curr_month == 12 else curr_month + 1
    next_year = curr_year + 1 if curr_month == 12 else curr_year

    months_to_scrape = [
        {"year": curr_year, "month": curr_month},
        {"year": next_year, "month": next_month}
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pbgfl.gov/calendar.aspx",
        "Connection": "keep-alive"
    }

    seen_keys = set()

    for target in months_to_scrape:
        y_val = target["year"]
        m_val = target["month"]

        url = f"{calendar_base_url}?view=list&year={y_val}&month={m_val}"

        try:
            res = requests.get(url, headers=headers, timeout=15)
            print(f"[PBG List] Fetching {y_val}-{m_val:02d} | HTTP Status: {res.status_code}")

            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")

                event_rows = soup.find_all(lambda tag: tag.name in ["tr", "li", "div"] and tag.find("a", href=lambda h: h and ("EID=" in h or "eid=" in h)))
                if not event_rows:
                    event_rows = soup.select(".calendarItem, .eventRow, table.calendarList tr, ol.calendarList > li")

                print(f"[PBG List] Found {len(event_rows)} matching event rows for {y_val}-{m_val:02d}.")

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

                    if iso_date and is_qualifying_event(clean_title) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', clean_title, re.I):
                        dt = datetime.strptime(iso_date, "%Y-%m-%d")
                        if current_month_start <= dt < lookahead_end:
                            dedup_key = (clean_title, iso_date)
                            if dedup_key not in seen_keys:
                                seen_keys.add(dedup_key)
                                events.append({
                                    "id": f"pbg-{iso_date}-{hash(full_link)}",
                                    "muni_short": "PBG",
                                    "muni_full": "City of Palm Beach Gardens",
                                    "title": clean_title,
                                    "date": iso_date,
                                    "time": meeting_time,
                                    "link": full_link,
                                    "summary": f"Official {clean_title} meeting."
                                })

        except Exception as e:
            print(f"[PBG List] Error scraping {y_val}-{m_val:02d}: {e}")

    print(f"[PBG List] Extracted {len(events)} events.")
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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2)

    print(f"Execution complete. Saved {len(all_events)} unique matching events to data.json.")

if __name__ == "__main__":
    main()
