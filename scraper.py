import json
import re
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

DATA_FILE = "data.json"

# Strict filtering keywords for qualifying public governance meetings
KEYWORDS = [
    "city commission", "county commissioners", "bcc", "city council", 
    "town council", "regular meeting", "special meeting", "workshop", "budget hearing"
]

# Non-qualifying events to filter out
EXCLUDE_KEYWORDS = [
    "parks", "recreation", "code compliance", "pension", "police", "firefighters", "advisory"
]

def save_data(data):
    """Overwrites data.json completely with fresh results."""
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
        match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', date_str)
        if match:
            month, day, year = match.groups()
            dt = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None

# --- 1. BOCA RATON (HTML Calendar Scraper) ---
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
                        iso_date = normalize_date(raw_date) or raw_date
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
        print(f"Error scraping Boca Raton: {e}")
    return events

 # --- 2. PALM BEACH COUNTY (Targeted Meeting Scraper) ---
def scrape_palm_beach_county():
    events = []
    url = "https://discover.pbc.gov/countycommissioners/Pages/Agenda.aspx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Target hyperlinks inside content areas containing explicit date strings
            for a_tag in soup.select("a[href]"):
                text = a_tag.text.strip()
                href = a_tag['href']
                
                # Ignore top-level menu/navigation links
                if text.lower() in ["bcc agenda", "bcc meeting agendas", "agenda", "home"]:
                    continue

                # Search for specific meeting date pattern (e.g. "September 1, 2026")
                date_match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
                
                if date_match:
                    full_link = href if href.startswith("http") else f"https://discover.pbc.gov{href}"
                    iso_date = normalize_date(text)
                    
                    if iso_date:
                        events.append({
                            "id": f"pbc-{hash(full_link)}",
                            "muni_short": "PBC",
                            "muni_full": "Palm Beach County Board of Commissioners",
                            "title": f"BCC Regular Meeting - {date_match.group(0)}",
                            "date": iso_date,
                            "time": "9:30 AM",
                            "link": full_link,
                            "summary": "Official Palm Beach County Board of Commissioners Agenda."
                        })
    except Exception as e:
        print(f"Error scraping Palm Beach County: {e}")
    return events

# --- 3. WEST PALM BEACH (HTML Agenda Scraper) ---
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
                    iso_date = normalize_date(title) or title
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
        print(f"Error scraping West Palm Beach: {e}")
    return events

# --- MAIN CONTROLLER ---
def run():
    # Fresh initialization: Start with an empty list for every run
    fresh_events = []
    
    # Run scrapers
    fresh_events.extend(scrape_boca_raton())
    fresh_events.extend(scrape_palm_beach_county())
    fresh_events.extend(scrape_west_palm_beach())

    # Completely overwrite data.json
    save_data(fresh_events)
    print(f"Wiped old data and saved {len(fresh_events)} fresh scraped events to {DATA_FILE}.")

if __name__ == "__main__":
    run()
