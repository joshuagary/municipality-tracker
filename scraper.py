import json
import re
import os
import io
from datetime import datetime
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader
from curl_cffi import requests

DATA_FILE = "data.json"

# Global Keyword Matcher (Shared across municipalities)
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
    """Strips HTML whitespace, dates, locations, and metadata from extracted titles."""
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
        print(f"PDF extraction error on {url}: {e}")
    return None

# --- 1. BOCA RATON MODULE (DYNAMIC URL & SHARED KEYWORD LOGIC) ---
def scrape_boca_raton():
    events = []
    
    # Dynamically compute year and month URL for current execution date
    now = datetime.now()
    url = f"https://www.myboca.us/calendar.aspx?view=month&year={now.year}&month={now.month}&CID=0"
    current_month_start = datetime(now.year, now.month, 1)

    try:
        res = requests.get(url, impersonate="chrome124", timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            elements = soup.select(".calendarRow, .detail-list-item, .calendarEvent, tr, li")
            
            for item in elements:
                title_elem = item.select_one(".calendarTitle a, a[href*='calendar']")
                date_elem = item.select_one(".calendarDate, .date")
                
                if not title_elem:
                    continue

                raw_title = title_elem.text.strip()
                clean_title = clean_event_title(raw_title)
                href = title_elem['href']
                raw_context = item.get_text(separator=" ", strip=True)

                # Shared Keyword & Exclude Matching (Same as WPB logic)
                if (is_qualifying_event(clean_title) or is_qualifying_event(raw_context)) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', clean_title, re.I):
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

            print(f"Boca Raton Scraper dynamically loaded ({url}) and extracted {len(events)} events.")
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

# --- 3. WEST PALM BEACH MODULE (CLEAN TITLES & SHARED KEYWORDS) ---
def scrape_west_palm_beach():
    events = []
    url = "https://www.wpb.org/Our-City/Meetings-Agendas"
    hf_token = os.environ.get("HF_TOKEN")
    current_month_start = datetime(datetime.now().year, datetime.now().month, 1)

    try:
        res = requests.get(url, impersonate="chrome124", timeout=15)
        if res.status_code != 200:
            return events

        soup = BeautifulSoup(res.text, "html.parser")

        # --- ENGINE A: Llama 3.1 LLM ---
        if hf_token:
            try:
                clean_soup = BeautifulSoup(res.text, "html.parser")
                for tag in clean_soup(["script", "style", "nav", "footer"]):
                    tag.decompose()

                page_text = clean_soup.get_text(separator="\n", strip=True)[:4500]

                router_url = "https://api-inference.huggingface.co/models/meta-llama/Meta-Llama-3.1-8B-Instruct/v1/chat/completions"
                payload = {
                    "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a JSON parser. Output ONLY a raw JSON array matching the requested schema. No code blocks, intros, or explanations."
                        },
                        {
                            "role": "user",
                            "content": f"""Extract public meetings ONLY for municipal boards matching these topics:
Commission, Zoning Board of Appeals, Planning Board, Downtown Action Committee, Plans & Plats Review Committee, CRA, Development.

Do NOT extract bid openings, ITB/RFP notices, or general town events.

Schema format:
[
  {{"title": "Clean Short Title", "date": "YYYY-MM-DD", "time": "HH:MM AM/PM", "link": "Relative or Absolute URL"}}
]

Text:
{page_text}"""
                        }
                    ],
                    "temperature": 0.01,
                    "max_tokens": 1200
                }

                hf_headers = {
                    "Authorization": f"Bearer {hf_token}",
                    "Content-Type": "application/json"
                }

                hf_res = requests.post(router_url, headers=hf_headers, json=payload, timeout=20)
                if hf_res.status_code == 200:
                    content = hf_res.json()['choices'][0]['message']['content'].strip()
                    content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
                    content = re.sub(r'\s*```$', '', content, flags=re.MULTILINE)

                    llm_data = json.loads(content)

                    for item in llm_data:
                        raw_title = item.get("title", "")
                        clean_title = clean_event_title(raw_title)

                        if is_qualifying_event(clean_title) and not re.search(r'\b(ITB|RFP|RFQ|Bid)\b', clean_title, re.I):
                            href = item.get("link", "")
                            full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                            events.append({
                                "id": f"wpb-{item['date']}-{hash(full_link)}",
                                "muni_short": "WPB",
                                "muni_full": "City of West Palm Beach",
                                "title": clean_title,
                                "date": item["date"],
                                "time": item.get("time", "5:00 PM"),
                                "link": full_link,
                                "summary": f"Official {clean_title} meeting."
                            })
                    if len(events) > 0:
                        print(f"Llama 3.1 extracted {len(events)} clean events for WPB.")
                        return events
            except Exception as e:
                print(f"Llama 3.1 Engine failed ({e}). Falling back to BeautifulSoup...")

        # --- ENGINE B: CLEAN BEAUTIFULSOUP FALLBACK ---
        print("Running Clean BeautifulSoup fallback for WPB...")
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

# --- DEDUPLICATION CONTROLLER ---
def run():
    raw_events = []
    raw_events.extend(scrape_boca_raton())
    raw_events.extend(scrape_palm_beach_county())
    raw_events.extend(scrape_west_palm_beach())

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
