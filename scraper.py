import json
import re
import os
import io
from datetime import datetime
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader
from curl_cffi import requests

DATA_FILE = "data.json"

KEYWORDS = [
    "commission", "commissioners", "bcc", "council", "meeting", "agenda",
    "workshop", "hearing", "zoning", "planning", "p&z", "pz", "site plan",
    "plat", "plats", "pprc", "redevelopment", "cra", "action", "cac", "work session",
    "board of appeals", "downtown action", "board of adjustment", "building board"
]

EXCLUDE_KEYWORDS = [
    "code compliance", "pension", "police", "firefighters", "art", "library", "parks"
]

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def clean_event_title(raw_text):
    if not raw_text:
        return "Municipal Governance Meeting"
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    first_line = lines[0] if lines else raw_text
    cleaned = re.sub(r'(?i)(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}.*', '', first_line)
    cleaned = re.sub(r'(?i)Tagged as:.*', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if len(cleaned) > 3 else "Municipal Governance Meeting"

def is_qualifying_event(title):
    if not title:
        return False
    title_lower = title.lower()
    if any(ex in title_lower for ex in EXCLUDE_KEYWORDS):
        return False
    return any(kw in title_lower for kw in KEYWORDS)

def extract_date_from_text(text):
    if not text:
        return None
    m1 = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
    if m1:
        month_str, day_str, year_str = m1.group(1), m1.group(2), m1.group(3)
        for fmt in ("%B %d %Y", "%b %d %Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(f"{month_str} {int(day_str)} {year_str}", "%B %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    m2 = re.search(r'(20\d{2})[-_]?([0-1]\d)[-_]?([0-3]\d)', text)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return None

def normalize_title_for_grouping(title):
    if not title:
        return ""
    title_clean = title.lower().strip()
    title_clean = re.sub(r'([a-z]+)\s+0(\d)', r'\1 \2', title_clean)
    return re.sub(r'\s+', ' ', title_clean)

def extract_pdf_first_pages_text(url):
    if not url.lower().endswith(".pdf"):
        return None
    try:
        res = requests.get(url, impersonate="chrome124", timeout=10)
        if res.status_code == 200:
            pdf_file = io.BytesIO(res.content)
            reader = PdfReader(pdf_file)
            extracted_text = ""
            pages_to_read = min(2, len(reader.pages))
            for i in range(pages_to_read):
                text = reader.pages[i].extract_text()
                if text:
                    extracted_text += text
            return re.sub(r'\s+', ' ', extracted_text).strip().lower()
    except Exception as e:
        pass
    return None

# --- 1. BOCA RATON MODULE ---
def scrape_boca_raton():
    events = []
    now = datetime.now()
    current_month_start = datetime(now.year, now.month, 1)

    curr_year = now.year
    curr_month = now.month
    next_year = curr_year + 1 if curr_month == 12 else curr_year
    next_month = 1 if curr_month == 12 else curr_month + 1

    urls = [
        f"https://www.myboca.us/calendar.aspx?view=list&year={curr_year}&month={curr_month}&CID=0",
        f"https://www.myboca.us/calendar.aspx?view=list&year={next_year}&month={next_month}&CID=0"
    ]

    STRICT_GOVERNANCE_KEYWORDS = [
        "city council", "planning & zoning", "planning and zoning",
        "community redevelopment agency", "cra", "zoning board of adjustment",
        "building board of adjustment", "planning board", "zoning board",
        "downtown action", "plans & plats", "plans and plats"
    ]

    try:
        for url in urls:
            res = requests.get(url, impersonate="chrome124", timeout=12)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            elements = soup.select(".calendarRow, .calendarCell, .calendarEvent, .detail-list-item, tr, li")
            
            for item in elements:
                title_elem = item if (item.name == 'a' and 'calendar' in item.get('href', '')) else item.select_one("a[href*='calendar.aspx?EID='], a[href*='Calendar.aspx?EID='], .calendarTitle a")
                date_elem = item.select_one(".calendarDate, .date, .calendarDay")
                
                if not title_elem:
                    continue

                raw_title = title_elem.text.strip()
                clean_title = clean_event_title(raw_title)
                href = title_elem['href'] if title_elem.name == 'a' else title_elem.get('href', '')
                raw_context = item.get_text(separator=" ", strip=True)

                title_lower = clean_title.lower()
                is_target_governance = any(kw in title_lower for kw in STRICT_GOVERNANCE_KEYWORDS)

                if is_target_governance and not re.search(r'\b(ITB|RFP|RFQ|Bid|Advisory|Airport|Library|Parks)\b', clean_title, re.I):
                    raw_date = date_elem.text.strip() if date_elem else raw_context
                    iso_date = extract_date_from_text(raw_date) or extract_date_from_text(clean_title)

                    if iso_date:
                        dt = datetime.strptime(iso_date, "%Y-%m-%d")
                        if dt >= current_month_start:
                            time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', raw_context)
                            meeting_time = time_match.group(1).upper() if time_match else "1:30 PM"
                            full_link = href if href.startswith("http") else f"https://www.myboca.us{href}"

                            events.append({
                                "id": f"boca-{iso_date}-{hash(full_link)}",
                                "muni_short": "BOCA",
                                "muni_full": "City of Boca Raton",
                                "title": clean_title,
                                "date": iso_date,
                                "time": meeting_time,
                                "link": full_link,
                                "summary": f"Official {clean_title} meeting."
                            })

        print(f"Boca Raton Scraper finished multi-month lookahead ({curr_month}/{curr_year} & {next_month}/{next_year}). Extracted {len(events)} total events.")
    except Exception as e:
        print(f"Error scraping Boca Raton: {e}")

    return events

# --- 2. PALM BEACH COUNTY MODULE ---
def scrape_palm_beach_county():
    events = []
    url = "https://discover.pbc.gov/countycommissioners/Pages/Agenda.aspx"
    current_month_start = datetime(datetime.now().year, datetime.now().month, 1)

    try:
        res = requests.get(url, impersonate="chrome124", timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for a_tag in soup.select("a[href]"):
                href = a_tag['href']
                link_text = a_tag.text.strip()
                parent_text = a_tag.parent.text.strip() if a_tag.parent else ""
                full_context = f"{link_text} {parent_text} {href}"
                iso_date_str = extract_date_from_text(full_context)
                
                if iso_date_str:
                    event_date = datetime.strptime(iso_date_str, "%Y-%m-%d")
                    if event_date >= current_month_start:
                        if "agenda" in full_context.lower() or "bcc" in full_context.lower():
                            full_link = href if href.startswith("http") else f"http://www.pbcgov.com{href}" if href.startswith("/pubInf") else f"https://discover.pbc.gov{href}"
                            date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', full_context)
                            m_label = date_match.group(0) if date_match else iso_date_str

                            events.append({
                                "id": f"pbc-{iso_date_str}-{hash(full_link)}",
                                "muni_short": "PBC",
                                "muni_full": "Palm Beach County Board of Commissioners",
                                "title": f"BCC Meeting - {m_label}",
                                "date": iso_date_str,
                                "time": "9:30 AM",
                                "link": full_link,
                                "summary": "Official Palm Beach County Board of Commissioners Agenda."
                            })
    except Exception as e:
        print(f"Error scraping Palm Beach County: {e}")
    return events

# --- 3. WEST PALM BEACH MODULE ---
def scrape_west_palm_beach():
    events = []
    url = "https://www.wpb.org/Our-City/Meetings-Agendas"
    current_month_start = datetime(datetime.now().year, datetime.now().month, 1)

    try:
        res = requests.get(url, impersonate="chrome124", timeout=15)
        if res.status_code != 200:
            return events

        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.find_all(["div", "li", "tr", "article"])

        for card in cards:
            card_text = card.get_text(separator=" ", strip=True)
            title_elem = card.select_one("h2, h3, h4, .title, .item-title, a[href]")
            if not title_elem:
                continue

            raw_title = title_elem.text.strip()
            clean_title = clean_event_title(raw_title)

            a_tag = card.select_one("a[href]")
            href = a_tag['href'] if a_tag else ""

            if (is_qualifying_event(clean_title) or is_qualifying_event(card_text)) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', card_text, re.I):
                iso_date = extract_date_from_text(card_text)
                if iso_date:
                    dt = datetime.strptime(iso_date, "%Y-%m-%d")
                    if dt >= current_month_start:
                        full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                        time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', card_text)
                        m_time = time_match.group(1).upper() if time_match else "5:00 PM"

                        events.append({
                            "id": f"wpb-{iso_date}-{hash(full_link)}",
                            "muni_short": "WPB",
                            "muni_full": "City of West Palm Beach",
                            "title": clean_title,
                            "date": iso_date,
                            "time": m_time,
                            "link": full_link,
                            "summary": f"Official {clean_title} meeting."
                        })
    except Exception as e:
        print(f"Error scraping West Palm Beach: {e}")

    return events

# --- 4. DELRAY BEACH MODULE (STABLE CURRENT-MONTH SCRAPER) ---
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

# --- 5. PALM BEACH GARDENS MODULE (DIRECT CIVICPLUS API SCRAPER) ---
def scrape_palm_beach_gardens():
    events = []
    base_url = "https://www.pbgfl.gov"
    api_url = "https://www.pbgfl.gov/Calendar.aspx/GetEvents"
    
    now = datetime.now()
    curr_year = now.year
    curr_month = now.month

    # Date Range: Current Month (Aug 1) to End of Next Month (Sept 30)
    start_date = datetime(curr_year, curr_month, 1)
    
    if curr_month == 11:
        lookahead_end = datetime(curr_year + 1, 1, 1)
    elif curr_month == 12:
        lookahead_end = datetime(curr_year + 1, 2, 1)
    else:
        lookahead_end = datetime(curr_year, curr_month + 2, 1)

    end_date = lookahead_end - timedelta(days=1)

    # Payload matching CivicPlus calendar API request structure
    payload = {
        "startDate": start_date.strftime("%Y-%m-%dT00:00:00"),
        "endDate": end_date.strftime("%Y-%m-%dT23:59:59"),
        "categoryIDs": "41,42,37,44,45,46,47,48,43"
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }

    try:
        # POST request to fetch calendar events payload directly
        res = requests.post(api_url, json=payload, headers=headers, timeout=15)
        print(f"[PBG API] HTTP Status Code: {res.status_code}")

        items = []
        if res.status_code == 200:
            data = res.json()
            # CivicPlus ASP.NET web services wrap data inside a 'd' container
            items = data.get("d", []) if isinstance(data, dict) else data
            if isinstance(items, str):
                items = json.loads(items)
        
        # Fallback to GET endpoint if POST JSON wrapper is unavailable
        if not items:
            fallback_url = f"https://www.pbgfl.gov/Calendar.aspx?CID=41,42,37,44,45,46,47,48,43&startDate={start_date.strftime('%m/%d/%Y')}&endDate={end_date.strftime('%m/%d/%Y')}"
            res_fallback = requests.get(fallback_url, headers=headers, timeout=15)
            if res_fallback.status_code == 200:
                soup = BeautifulSoup(res_fallback.text, "html.parser")
                # Parse calendar event links (EID=) off rendered fallback structure
                for a in soup.select("a[href*='Calendar.aspx?EID='], a[href*='calendar.aspx?EID=']"):
                    title_text = a.text.strip()
                    href_val = a.get("href", "").strip()
                    if title_text and href_val:
                        # Extract parent element text for date/time context
                        parent_text = a.parent.text if a.parent else title_text
                        items.append({
                            "title": title_text,
                            "link": href_val,
                            "raw_context": parent_text
                        })

        print(f"[PBG API] Retrieved {len(items)} raw event records.")
        seen_keys = set()

        for item in items:
            raw_title = item.get("title") or item.get("Title") or item.get("eventName") or ""
            clean_title = clean_event_title(raw_title)

            # Date Extraction
            start_str = item.get("startDate") or item.get("Start") or item.get("Date") or item.get("raw_context") or ""
            iso_date = None
            meeting_time = "6:00 PM"

            if "T" in str(start_str):
                try:
                    dt_obj = datetime.fromisoformat(str(start_str).split(".")[0].replace("Z", ""))
                    iso_date = dt_obj.strftime("%Y-%m-%d")
                    meeting_time = dt_obj.strftime("%I:%M %p").lstrip("0")
                except ValueError:
                    pass

            if not iso_date:
                date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(start_str))
                if date_match:
                    m, d, y = date_match.groups()
                    iso_date = f"{y}-{int(m):02d}-{int(d):02d}"

            # Link Construction
            href = item.get("link") or item.get("Link") or item.get("url") or ""
            if href:
                full_link = href if href.startswith("http") else f"{base_url}/{href.lstrip('/')}"
            else:
                eid = item.get("eventID") or item.get("id")
                full_link = f"{base_url}/calendar.aspx?EID={eid}" if eid else "https://www.pbgfl.gov/calendar.aspx"

            # Filter for Current + Next Month
            if iso_date and is_qualifying_event(clean_title) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', clean_title, re.I):
                dt = datetime.strptime(iso_date, "%Y-%m-%d")

                if start_date <= dt < lookahead_end:
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

        print(f"[PBG API] Successfully saved {len(events)} qualifying events across August and September.")

    except Exception as e:
        print(f"[PBG API] Error scraping PBG via API endpoint: {e}")

    return events





# --- DEDUPLICATION & MAIN CONTROLLER ---
def run():
    raw_events = []
    
    print("Starting Municipal Scraper Engine...")
    raw_events.extend(scrape_boca_raton())
    raw_events.extend(scrape_palm_beach_county())
    raw_events.extend(scrape_west_palm_beach())
    
    print("Executing Delray Beach Scraper...")
    raw_events.extend(scrape_delray_beach())

    grouped_events = {}
    for event in raw_events:
        clean_title = normalize_title_for_grouping(event['title'])
        group_key = (event['muni_short'], event['date'], event['time'], clean_title)

        if group_key not in grouped_events:
            grouped_events[group_key] = []
        grouped_events[group_key].append(event)

    final_list = []

    for group_key, items in grouped_events.items():
        if len(items) == 1:
            final_list.append(items[0])
        else:
            unique_in_group = []
            for item in items:
                clean_link = item['link'].replace("http://www.pbcgov.com/pubInf/Agenda", "https://discover.pbc.gov/countycommissioners/Agenda_Master")

                is_duplicate = False
                for existing in unique_in_group:
                    existing_clean_link = existing['link'].replace("http://www.pbcgov.com/pubInf/Agenda", "https://discover.pbc.gov/countycommissioners/Agenda_Master")

                    if clean_link == existing_clean_link:
                        is_duplicate = True
                        break

                    content_text = extract_pdf_first_pages_text(item['link'])
                    existing_text = existing.get('_pdf_content')
                    if content_text and existing_text and (content_text[:500] == existing_text[:500]):
                        is_duplicate = True
                        break

                if not is_duplicate:
                    item['_pdf_content'] = extract_pdf_first_pages_text(item['link'])
                    unique_in_group.append(item)

            for u in unique_in_group:
                u.pop('_pdf_content', None)
                final_list.append(u)

    save_data(final_list)
    print(f"Execution complete. Saved {len(final_list)} unique matching events to {DATA_FILE}.")

if __name__ == "__main__":
    run()
