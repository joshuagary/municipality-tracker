import os
import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# --- HELPER FUNCTIONS ---

def clean_event_title(title):
    """Clean up extra whitespaces, newlines, and common artifacts from titles."""
    if not title:
        return "Public Meeting"
    title = re.sub(r'\s+', ' ', title).strip()
    return title

def is_qualifying_event(title):
    """Filter for official governance board, council, commission, and committee meetings."""
    governance_keywords = [
        r'\bCouncil\b', r'\bCommission\b', r'\bBoard\b', r'\bCommittee\b',
        r'\bAuthority\b', r'\bAgency\b', r'\bCRA\b', r'\bZoning\b', r'\bPlanning\b',
        r'\bHistoric\b', r'\bSpecial Magistrate\b', r'\bCode Enforcement\b',
        r'\bTask Force\b', r'\bTown Hall\b', r'\bHearing\b', r'\bWorkshop\b', r'\bBCC\b'
    ]
    pattern = re.compile('|'.join(governance_keywords), re.I)
    return bool(pattern.search(title))

def get_dual_month_bounds():
    """Returns datetime objects for the start of the current month and the upper bound (1st of month after next)."""
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


# --- 1. WEST PALM BEACH MODULE (RESTORED ORIGINAL LOGIC) ---
def scrape_west_palm_beach():
    events = []
    base_domain = "https://www.wpb.org"
    target_url = f"{base_domain}/government/city-commission-agendas"
    
    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(target_url, headers=headers, timeout=15)
        print(f"[WPB] HTTP Status: {res.status_code}")
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            seen_keys = set()
            
            for a in soup.find_all("a"):
                href = a.get("href", "").strip()
                if not href:
                    continue
                
                text = a.text.strip()
                parent_text = a.parent.text.strip() if a.parent else text
                context = f"{text} {parent_text}"

                if is_qualifying_event(context) or "agenda" in href.lower() or "pdf" in href.lower():
                    raw_title = text if len(text) > 3 else parent_text.split("\n")[0].strip()
                    clean_title = clean_event_title(raw_title)

                    date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', context)
                    iso_date = None
                    if date_match:
                        m, d, y = date_match.groups()
                        iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
                    else:
                        text_date = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})', context, re.I)
                        if text_date:
                            try:
                                dt_parsed = datetime.strptime(text_date.group(0).replace(",", ""), "%B %d %Y")
                                iso_date = dt_parsed.strftime("%Y-%m-%d")
                            except ValueError:
                                pass

                    if iso_date and is_qualifying_event(clean_title):
                        dt = datetime.strptime(iso_date, "%Y-%m-%d")
                        if current_month_start <= dt < lookahead_end:
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
        print(f"[WPB] Extracted {len(events)} events.")
    except Exception as e:
        print(f"[WPB] Error: {e}")

    return events


# --- 2. PALM BEACH COUNTY MODULE (RESTORED ORIGINAL LOGIC) ---
def scrape_palm_beach_county():
    events = []
    base_domain = "https://discover.pbcgov.org"
    target_url = f"{base_domain}/countycommission/Pages/BCC-Meeting-Agendas.aspx"
    
    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(target_url, headers=headers, timeout=15)
        print(f"[PBC] HTTP Status: {res.status_code}")
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            seen_keys = set()

            for a in soup.find_all("a"):
                href = a.get("href", "").strip()
                if not href:
                    continue
                
                text = a.text.strip()
                parent_text = a.parent.text.strip() if a.parent else text
                context = f"{text} {parent_text}"

                date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', context)
                if not date_match:
                    date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})', context, re.I)

                if date_match:
                    if len(date_match.groups()) == 3:
                        m, d, y = date_match.groups()
                        iso_date = f"{y}-{int(m):02d}-{int(d):02d}"
                    else:
                        try:
                            dt_parsed = datetime.strptime(date_match.group(0).replace(",", ""), "%B %d %Y")
                            iso_date = dt_parsed.strftime("%Y-%m-%d")
                        except ValueError:
                            continue

                    raw_title = text if len(text) > 3 else "Board of County Commissioners Meeting"
                    clean_title = clean_event_title(raw_title)

                    if is_qualifying_event(clean_title) or "BCC" in context:
                        dt = datetime.strptime(iso_date, "%Y-%m-%d")
                        if current_month_start <= dt < lookahead_end:
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
    except Exception as e:
        print(f"[PBC] Error: {e}")

    return events


# --- 3. BOCA RATON MODULE (RESTORED ORIGINAL LOGIC) ---
def scrape_boca_raton():
    events = []
    base_url = "https://bocaraton.legistar.com/"
    target_url = f"{base_url}Calendar.aspx"
    
    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(target_url, headers=headers, timeout=15)
        print(f"[Boca Raton] HTTP Status: {res.status_code}")
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
                                    "id": f"boca-{iso_date}-{hash(full_link)}",
                                    "muni_short": "BOCA",
                                    "muni_full": "City of Boca Raton",
                                    "title": clean_title,
                                    "date": iso_date,
                                    "time": time_str or "6:00 PM",
                                    "link": full_link,
                                    "summary": f"Official {clean_title} meeting."
                                })
                        except ValueError:
                            continue
        print(f"[Boca Raton] Extracted {len(events)} events.")
    except Exception as e:
        print(f"[Boca Raton] Error: {e}")

    return events


# --- 4. BOYNTON BEACH MODULE ---
def scrape_boynton_beach():
    events = []
    base_url = "https://boyntonbeach.legistar.com/"
    target_url = f"{base_url}Calendar.aspx"
    
    current_month_start, lookahead_end, _, _ = get_dual_month_bounds()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }

    try:
        res = requests.get(target_url, headers=headers, timeout=15)
        print(f"[Boynton Beach] HTTP Status: {res.status_code}")
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
                                    "id": f"boynton-{iso_date}-{hash(full_link)}",
                                    "muni_short": "BOYNTON",
                                    "muni_full": "City of Boynton Beach",
                                    "title": clean_title,
                                    "date": iso_date,
                                    "time": time_str or "6:00 PM",
                                    "link": full_link,
                                    "summary": f"Official {clean_title} meeting."
                                })
                        except ValueError:
                            continue
        print(f"[Boynton Beach] Extracted {len(events)} events.")
    except Exception as e:
        print(f"[Boynton Beach] Error: {e}")

    return events


# --- 5. DELRAY BEACH MODULE (STABLE CURRENT-MONTH SCRAPER) ---
def scrape_delray_beach():
    events = []
    url = "https://delraybeach.legistar.com/Calendar.aspx"
    
    now = datetime.now()
    # Lock lower bound to the 1st day of the current month
    current_month_start = datetime(now.year, now.month, 1)

    try:
        res = requests.get(url, impersonate="chrome124", timeout=15)
        print(f"[Delray] HTTP Status Code: {res.status_code}")
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            rows = soup.find_all("tr")

            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                raw_title = cols[0].text.strip()
                clean_title = clean_event_title(raw_title)
                
                raw_date = cols[1].text.strip() if len(cols) > 1 else ""
                raw_time = cols[2].text.strip() if len(cols) > 2 else ""

                # Target direct PDF Agenda link (View.ashx?M=A), fallback to Meeting Details page
                href = ""
                agenda_a = row.select_one("a[href*='View.ashx?M=A']")
                if agenda_a and agenda_a.get('href'):
                    href = agenda_a['href'].strip()
                else:
                    detail_a = row.select_one("a[href*='MeetingDetail.aspx']")
                    if detail_a and detail_a.get('href'):
                        href = detail_a['href'].strip()

                if not href:
                    href = "Calendar.aspx"

                # Standard M/D/YYYY date extraction
                iso_date = None
                date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', raw_date)
                if date_match:
                    m, d, y = date_match.groups()
                    iso_date = f"{y}-{int(m):02d}-{int(d):02d}"

                # Apply qualification filter for clean current-month dataset
                if iso_date and is_qualifying_event(clean_title) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', clean_title, re.I):
                    dt = datetime.strptime(iso_date, "%Y-%m-%d")
                    
                    if dt >= current_month_start:
                        time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)', raw_time)
                        meeting_time = time_match.group(1).strip().upper() if time_match else "4:00 PM"
                        if "AM" not in meeting_time and "PM" not in meeting_time:
                            meeting_time += " PM"

                        full_link = href if href.startswith("http") else f"https://delraybeach.legistar.com/{href.lstrip('/')}"

                        events.append({
                            "id": f"delray-{iso_date}-{hash(full_link)}",
                            "muni_short": "DELRAY",
                            "muni_full": "City of Delray Beach",
                            "title": clean_title,
                            "date": iso_date,
                            "time": meeting_time,
                            "link": full_link,
                            "summary": f"Official {clean_title} meeting."
                        })

            print(f"[Delray] Successfully saved {len(events)} current-month events.")
    except Exception as e:
        print(f"[Delray] Exception: {e}")

    return events


# --- 6. PALM BEACH GARDENS MODULE (CURRENT WORKING DUAL-MONTH SCRAPER) ---
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
    
    # 1. West Palm Beach
    all_events.extend(scrape_west_palm_beach())

    # 2. Palm Beach County
    all_events.extend(scrape_palm_beach_county())

    # 3. Boca Raton
    all_events.extend(scrape_boca_raton())
    
    # 4. Boynton Beach
    all_events.extend(scrape_boynton_beach())

    # 5. Delray Beach
    all_events.extend(scrape_delray_beach())
    
    # 6. Palm Beach Gardens
    all_events.extend(scrape_palm_beach_gardens())

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(all_events, f, indent=2)

    print(f"Execution complete. Saved {len(all_events)} unique matching events to data.json.")

if __name__ == "__main__":
    main()
