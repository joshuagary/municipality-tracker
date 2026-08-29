import json
import re
import os
import requests
from bs4 import BeautifulSoup

DATA_FILE = "data.json"
KEYWORDS = [
    "city commission", "county commissioners", "bcc", "city council", 
    "town council", "regular meeting", "special meeting", "budget hearing"
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
    return any(keyword in title_lower for keyword in KEYWORDS)

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
                    if is_qualifying_event(title):
                        events.append({
                            "id": f"boca-{hash(title_elem['href'])}",
                            "muni_short": "BOCA",
                            "muni_full": "City of Boca Raton",
                            "title": title,
                            "date": date_elem.text.strip(),
                            "time": "1:30 PM",
                            "link": f"https://www.myboca.us{title_elem['href']}",
                            "summary": "Official meeting parsed from City of Boca Raton calendar portal."
                        })
    except Exception as e:
        print(f"Error parsing Boca Raton: {e}")
    return events

def get_seed_data():
    """Generates structured seed data representing Florida Municipality Calendars"""
    return [
        {
            "id": "wpb-2026-08-31",
            "muni_short": "WPB",
            "muni_full": "City of West Palm Beach",
            "title": "City Commission Meeting",
            "date": "August 31, 2026",
            "time": "5:00 PM",
            "link": "https://www.wpb.org/Our-City/Meetings-Agendas",
            "summary": "Regular City Commission Meeting held in Commission Chambers, 401 Clematis Street."
        },
        {
            "id": "pbc-2026-09-01",
            "muni_short": "PBC",
            "muni_full": "Palm Beach County",
            "title": "BCC Regular Meeting",
            "date": "September 1, 2026",
            "time": "9:30 AM",
            "link": "https://discover.pbc.gov/countycommissioners/pages/meeting-dates.aspx",
            "summary": "Board of County Commissioners Regular Meeting, Governmental Center."
        },
        {
            "id": "wpb-2026-09-08",
            "muni_short": "WPB",
            "muni_full": "City of West Palm Beach",
            "title": "Special City Commission Meeting: First Public Hearing",
            "date": "September 8, 2026",
            "time": "5:01 PM",
            "link": "https://www.wpb.org/Our-City/Meetings-Agendas",
            "summary": "First Public Hearing for Fiscal Year Budget & Millage Rate."
        },
        {
            "id": "boca-2026-09-15",
            "muni_short": "BOCA",
            "muni_full": "City of Boca Raton",
            "title": "City Council Workshop",
            "date": "September 15, 2026",
            "time": "1:30 PM",
            "link": "https://www.myboca.us/calendar.aspx",
            "summary": "City Council Workshop meeting at City Hall Council Chamber."
        }
    ]

def run():
    existing = {e["id"]: e for e in load_existing_data()}
    
    # 1. Load seed records
    for event in get_seed_data():
        existing[event["id"]] = event
        
    # 2. Add scraped records
    for event in scrape_boca_raton():
        existing[event["id"]] = event
        
    final_list = list(existing.values())
    save_data(final_list)
    print(f"Successfully output {len(final_list)} events to {DATA_FILE}")

if __name__ == "__main__":
    run()
