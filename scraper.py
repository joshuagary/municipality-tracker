import json
import re
import os
import io
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from PyPDF2 import PdfReader

DATA_FILE = "data.json"

KEYWORDS = [
    "commission", "commissioners", "bcc", "council", "meeting", "agenda",
    "workshop", "hearing", "zoning", "planning", "p&z", "pz", "site plan",
    "plat", "plats", "pprc", "redevelopment", "cra", "action", "cac", "work session"
]

EXCLUDE_KEYWORDS = [
    "code compliance", "pension", "police", "firefighters"
]

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

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

def extract_pdf_first_pages_text(url):
    """Downloads a PDF and extracts text from the first 2 pages for content comparison."""
    if not url.lower().endswith(".pdf"):
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            pdf_file = io.BytesIO(res.content)
            reader = PdfReader(pdf_file)
            extracted_text = ""
            pages_to_read = min(2, len(reader.pages))
            for i in range(pages_to_read):
                text = reader.pages[i].extract_text()
                if text:
                    extracted_text += text
            # Normalize whitespace for clean comparison
            return re.sub(r'\s+', ' ', extracted_text).strip().lower()
    except Exception as e:
        print(f"PDF extraction error on {url}: {e}")
    return None

# --- 1. BOCA RATON SCRAPER ---
def scrape_boca_raton():
    events = []
    url = "https://www.myboca.us/calendar.aspx?view=list&CID=0"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for item in soup.select(".calendarRow"):
                title_elem = item.select_one(".calendarTitle a")
                date_elem = item.select_one(".calendarDate")
                if title_elem:
                    title = title_elem.text.strip()
                    raw_date = date_elem.text.strip() if date_elem else ""
                    iso_date = extract_date_from_text(raw_date) or extract_date_from_text(title)
                    if is_qualifying_event(title) and iso_date:
                        events.append({
                            "id": f"boca-{hash(title_elem['href'])}",
                            "muni_short": "BOCA",
                            "muni_full": "City of Boca Raton",
                            "title": title,
                            "date": iso_date,
                            "time": "1:30 PM",
                            "link": f"https://www.myboca.us{title_elem['href']}",
                            "summary": "Official meeting parsed from City of Boca Raton portal."
                        })
    except Exception as e:
        print(f"Error scraping Boca Raton: {e}")
    return events

# --- 2. PALM BEACH COUNTY SCRAPER ---
def scrape_palm_beach_county():
    events = []
    url = "https://discover.pbc.gov/countycommissioners/Pages/Agenda.aspx"
    headers = {"User-Agent": "Mozilla/5.0"}
    current_month_start = datetime(datetime.now().year, datetime.now().month, 1)

    try:
        res = requests.get(url, headers=headers, timeout=12)
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

# --- 3. WEST PALM BEACH SCRAPER ---
def scrape_west_palm_beach():
    events = []
    urls = [
        "https://www.wpb.org/Our-City/Meetings-Agendas",
        "https://www.wpb.org/Our-City/Calendars/Meetings"
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    current_month_start = datetime(datetime.now().year, datetime.now().month, 1)

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=12)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for a_tag in soup.select("a[href]"):
                    title = a_tag.text.strip()
                    href = a_tag['href']
                    parent = a_tag.find_parent(["li", "tr", "div", "article"])
                    parent_text = parent.text.strip() if parent else ""
                    full_text = f"{title} {parent_text} {href}"

                    if is_qualifying_event(title) or is_qualifying_event(parent_text):
                        iso_date = extract_date_from_text(full_text)
                        if iso_date:
                            event_date = datetime.strptime(iso_date, "%Y-%m-%d")
                            if event_date >= current_month_start:
                                target_href = href
                                if parent:
                                    pdf_child = parent.select_one("a[href*='.pdf']")
                                    if pdf_child:
                                        target_href = pdf_child['href']

                                full_link = target_href if target_href.startswith("http") else f"https://www.wpb.org{target_href}"
                                display_title = title if len(title) > 3 else parent_text.split('\n')[0]
                                if len(display_title) > 80:
                                    display_title = display_title[:77] + "..."

                                events.append({
                                    "id": f"wpb-{hash(full_link)}",
                                    "muni_short": "WPB",
                                    "muni_full": "City of West Palm Beach",
                                    "title": display_title if display_title else "Public Governance Meeting",
                                    "date": iso_date,
                                    "time": "5:00 PM",
                                    "link": full_link,
                                    "summary": "Public meeting & agenda parsed from West Palm Beach portal."
                                })
        except Exception as e:
            print(f"Error scraping West Palm Beach from {url}: {e}")
    return events

# --- DEEP CONTENT DEDUPLICATION CONTROLLER ---
def run():
    raw_events = []
    raw_events.extend(scrape_boca_raton())
    raw_events.extend(scrape_palm_beach_county())
    raw_events.extend(scrape_west_palm_beach())

    # Group events by (muni_short, date, time, clean_title)
    grouped_events = {}
    for event in raw_events:
        clean_title = re.sub(r'\s+', ' ', event['title']).strip().lower()
        group_key = (event['muni_short'], event['date'], event['time'], clean_title)
        
        if group_key not in grouped_events:
            grouped_events[group_key] = []
        grouped_events[group_key].append(event)

    final_list = []

    # Inspect groups with potential duplicates
    for group_key, items in grouped_events.items():
        if len(items) == 1:
            final_list.append(items[0])
        else:
            print(f"Comparing {len(items)} matching candidates for {group_key[0]} on {group_key[1]}...")
            unique_in_group = []
            
            for item in items:
                # Extract content signature from first 2 pages if PDF
                content_text = extract_pdf_first_pages_text(item['link'])
                
                is_duplicate = False
                for existing in unique_in_group:
                    existing_text = existing.get('_pdf_content')
                    
                    # If both PDFs have extracted text and share identical or 95%+ similar content
                    if content_text and existing_text:
                        # Check substring/exact match on the first 500 characters
                        if content_text[:500] == existing_text[:500]:
                            is_duplicate = True
                            print(f"Eliminated duplicate agenda PDF: {item['link']}")
                            break
                    # Fallback link-equality check
                    elif item['link'] == existing['link']:
                        is_duplicate = True
                        break

                if not is_duplicate:
                    item['_pdf_content'] = content_text  # Store temporary signature
                    unique_in_group.append(item)

            # Strip temporary content signature before saving
            for u in unique_in_group:
                u.pop('_pdf_content', None)
                final_list.append(u)

    save_data(final_list)
    print(f"Deep comparison complete. Saved {len(final_list)} unique events to {DATA_FILE}.")

if __name__ == "__main__":
    run()
