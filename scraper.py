import json
import re
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

DATA_FILE = "data.json"

KEYWORDS = [
    "city commission", "county commissioners", "bcc", "city council", 
    "town council", "regular meeting", "special meeting", "workshop", "budget hearing"
]

EXCLUDE_KEYWORDS = [
    "parks", "recreation", "code compliance", "pension", "police", "firefighters", "advisory"
]

def load_existing_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def is_qualifying_event(title):
    title_lower = title.lower()
    if any(ex in title_lower for ex in EXCLUDE_KEYWORDS):
        return False
    return any(kw in title_lower for kw in KEYWORDS)

def normalize_date(date_str):
    """Converts various date text formats into ISO format YYYY-MM-DD"""
    try:
        # Match pattern like "August 29, 2026" or "Aug 29, 2026"
        match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', date_str)
        if match:
            month, day, year = match.groups()
            dt = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # Fallback default to current date if unparseable
    return datetime.now().strftime("%Y-%m-%d")

# --- BOCA RATON SCRAPER ---
def scrape_boca_raton():
    events = []
    url = "https://www.myboca.us/calendar.aspx?view=list&CID=0"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for item in soup.select(".calendarRow"):
                title_elem = item.select_one(".calendarTitle a")
                date_elem = item.select_one(".calendarDate")
                if title_elem and date_elem:
                    title = title_elem.text.strip()
                    raw_date = date_elem.text.strip()
                    if is_qualifying_event(title):
                        iso_date = normalize_date(raw_date)
                        events.append({
                            "id": f"boca-{hash(title_elem['href'])}",
                            "muni_short": "BOCA",
                            "muni_full": "City of Boca Raton",
                            "title": title,
                            "date": iso_date,
                            "time": "1:30 PM",
                            "link": f"https://www.myboca.us{title_elem['href']}",
                            "summary": "Parsed dynamically from City of Boca Raton calendar."
                        })
    except Exception as e:
        print(f"Boca scraper error: {e}")
    return events

# --- PALM BEACH COUNTY SCRAPER ---
def scrape_palm_beach_county():
    events = []
    url = "https://discover.pbc.gov/countycommissioners/pages/agendaarchive-html.aspx"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for a_tag in soup.select("a[href*='Agenda']"):
                title = a_tag.text.strip()
                href = a_tag['href']
                if is_qualifying_event(title) or "BCC" in title:
                    full_link = href if href.startswith("http") else f"https://discover.pbc.gov{href}"
                    iso_date = normalize_date(title)
                    events.append({
                        "id": f"pbc-{hash(full_link)}",
                        "muni_short": "PBC",
                        "muni_full": "Palm Beach County Board of Commissioners",
                        "title": f"BCC Meeting - {title}",
                        "date": iso_date,
                        "time": "9:30 AM",
                        "link": full_link,
                        "summary": "Board of County Commissioners Agenda Item."
                    })
    except Exception as e:
        print(f"PBC scraper error: {e}")
    return events

# --- WEST PALM BEACH SCRAPER ---
def scrape_west_palm_beach():
    events = []
    url = "https://www.wpb.org/Our-City/Calendars/Meetings"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for item in soup.select("a[href*='City-Commission']"):
                title = item.text.strip()
                href = item['href']
                if is_qualifying_event(title) or "Commission" in title:
                    full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                    iso_date = normalize_date(title)
                    events.append({
                        "id": f"wpb-{hash(full_link)}",
                        "muni_short": "WPB",
                        "muni_full": "City of West Palm Beach",
                        "title": title if title else "City Commission Meeting",
                        "date": iso_date,
                        "time": "5:00 PM",
                        "link": full_link,
                        "summary": "Regular City Commission Meeting parsed from City calendar."
                    })
    except Exception as e:
        print(f"WPB scraper error: {e}")
    return events

def run():
    existing_events = {e["id"]: e for e in load_existing_data()}
    scraped_data = []
    
    scraped_data.extend(scrape_boca_raton())
    scraped_data.extend(scrape_palm_beach_county())
    scraped_data.extend(scrape_west_palm_beach())

    for event in scraped_data:
        existing_events[event["id"]] = event

    final_list = list(existing_events.values())
    save_data(final_list)
    print(f"Scraper execution complete. Saved {len(final_list)} normalized events to {DATA_FILE}.")

if __name__ == "__main__":
    run()
