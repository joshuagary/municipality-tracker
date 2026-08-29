import json
import re
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

DATA_FILE = "data.json"

KEYWORDS = [
    "commission", "commissioners", "bcc", "council", "meeting", "agenda",
    "workshop", "hearing", "zoning", "planning", "p&z", "pz", "site plan",
    "plat", "plats", "pprc", "redevelopment", "cra", "action", "cac"
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
    """Finds dates formatted as 'Month DD, YYYY' or 'YYYY-MM-DD' or 'MM/DD/YYYY'"""
    if not text:
        return None
        
    # Match "September 1, 2026" or "Sep 1, 2026"
    m1 = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
    if m1:
        try:
            dt = datetime.strptime(f"{m1.group(1)} {m1.group(2)} {m1.group(3)}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(f"{m1.group(1)} {m1.group(2)} {m1.group(3)}", "%b %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Match "2026-09-01" or "20260901" in URL links
    m2 = re.search(r'(20\d{2})[-_]?([0-1]\d)[-_]?([0-3]\d)', text)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"

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
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Look inside container rows/cells for adjacent text + link pairs
            for a_tag in soup.select("a[href]"):
                href = a_tag['href']
                link_text = a_tag.text.strip()
                
                # Check link text, parent tag text, or link URL for date
                parent_text = a_tag.parent.text.strip() if a_tag.parent else ""
                full_context = f"{link_text} {parent_text} {href}"
                
                iso_date = extract_date_from_text(full_context)
                
                if iso_date and ("agenda" in full_context.lower() or "bcc" in full_context.lower()):
                    full_link = href if href.startswith("http") else f"http://www.pbcgov.com{href}" if href.startswith("/pubInf") else f"https://discover.pbc.gov{href}"
                    events.append({
                        "id": f"pbc-{iso_date}",
                        "muni_short": "PBC",
                        "muni_full": "Palm Beach County Board of Commissioners",
                        "title": f"BCC Regular Meeting Agenda",
                        "date": iso_date,
                        "time": "9:30 AM",
                        "link": full_link,
                        "summary": "Official Palm Beach County Board of County Commissioners Agenda."
                    })
    except Exception as e:
        print(f"Error scraping Palm Beach County: {e}")
    return events

# --- 3. WEST PALM BEACH SCRAPER ---
def scrape_west_palm_beach():
    events = []
    url = "https://www.wpb.org/Our-City/Calendars/Meetings"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for a_tag in soup.select("a[href]"):
                title = a_tag.text.strip()
                href = a_tag['href']
                parent_text = a_tag.parent.text.strip() if a_tag.parent else ""
                
                iso_date = extract_date_from_text(title) or extract_date_from_text(parent_text) or extract_date_from_text(href)
                
                if is_qualifying_event(title) and iso_date:
                    full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                    events.append({
                        "id": f"wpb-{hash(full_link)}",
                        "muni_short": "WPB",
                        "muni_full": "City of West Palm Beach",
                        "title": title if len(title) > 3 else "City Commission / Board Meeting",
                        "date": iso_date,
                        "time": "5:00 PM",
                        "link": full_link,
                        "summary": "Public meeting parsed from City of West Palm Beach portal."
                    })
    except Exception as e:
        print(f"Error scraping West Palm Beach: {e}")
    return events

# --- MAIN CONTROLLER ---
def run():
    raw_events = []
    raw_events.extend(scrape_boca_raton())
    raw_events.extend(scrape_palm_beach_county())
    raw_events.extend(scrape_west_palm_beach())

    # Deduplicate based on Municipality + Date + Title
    unique_events = {}
    for event in raw_events:
        clean_title = re.sub(r'\s+', ' ', event['title']).strip().lower()
        dedup_key = f"{event['muni_short']}_{event['date']}_{clean_title}"
        unique_events[dedup_key] = event

    final_list = list(unique_events.values())
    save_data(final_list)
    print(f"Container extraction complete. Saved {len(final_list)} unique matching events to {DATA_FILE}.")

if __name__ == "__main__":
    run()
