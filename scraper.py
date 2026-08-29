import json
import re
import os
import io
import requests
from bs4 import BeautifulSoup
import pdfplumber

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

# --- 1. BOCA RATON (HTML Calendar Scraper) ---
def scrape_boca_raton():
    events = []
    url = "https://www.myboca.us/calendar.aspx?view=list&CID=0"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=12)
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
                            "summary": "Official meeting parsed from City of Boca Raton calendar."
                        })
    except Exception as e:
        print(f"Error scraping Boca Raton: {e}")
    return events

# --- 2. PALM BEACH COUNTY (BCC Agenda HTML/PDF Scraper) ---
def scrape_palm_beach_county():
    events = []
    url = "https://discover.pbc.gov/countycommissioners/pages/agendaarchive-html.aspx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            # Locate agenda links in the archive list
            for a_tag in soup.select("a[href*='Agenda']"):
                title = a_tag.text.strip()
                href = a_tag['href']
                
                if is_qualifying_event(title) or "BCC" in title:
                    full_link = href if href.startswith("http") else f"https://discover.pbc.gov{href}"
                    
                    # Extract date string from title or link text
                    date_match = re.search(r'([A-Za-z]+\s+\d{1,2},\s+\d{4})', title)
                    event_date = date_match.group(1) if date_match else "Upcoming Agenda"

                    events.append({
                        "id": f"pbc-{hash(full_link)}",
                        "muni_short": "PBC",
                        "muni_full": "Palm Beach County Board of Commissioners",
                        "title": f"BCC Meeting - {title}",
                        "date": event_date,
                        "time": "9:30 AM",
                        "link": full_link,
                        "summary": "Official Board of County Commissioners Agenda."
                    })
    except Exception as e:
        print(f"Error scraping Palm Beach County: {e}")
    return events

# --- 3. WEST PALM BEACH (HTML & PDF Agenda Scraper) ---
def scrape_west_palm_beach():
    events = []
    url = "https://www.wpb.org/Our-City/Calendars/Meetings"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for item in soup.select("a[href*='City-Commission']"):
                title = item.text.strip()
                href = item['href']
                
                if is_qualifying_event(title) or "Commission" in title:
                    full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                    
                    events.append({
                        "id": f"wpb-{hash(full_link)}",
                        "muni_short": "WPB",
                        "muni_full": "City of West Palm Beach",
                        "title": title if title else "City Commission Meeting",
                        "date": "Monthly Scheduled",
                        "time": "5:00 PM",
                        "link": full_link,
                        "summary": "Regular City Commission Meeting parsed from City calendar."
                    })
    except Exception as e:
        print(f"Error scraping West Palm Beach: {e}")
    return events

# --- 4. GENERIC PDF PARSER FUNCTION ---
def parse_pdf_agenda(pdf_url):
    """Downloads a PDF agenda and parses text content using pdfplumber."""
    try:
        response = requests.get(pdf_url, timeout=10)
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
            # Simple extractor example for meeting summaries
            summary = text[:300].replace("\n", " ") + "..."
            return summary
    except Exception as e:
        print(f"PDF parsing error on {pdf_url}: {e}")
        return "PDF Agenda available at source link."

# --- MAIN CONTROLLER ---
def run():
    existing_events = {e["id"]: e for e in load_existing_data()}
    
    # Run all live web scrapers
    scraped_data = []
    scraped_data.extend(scrape_boca_raton())
    scraped_data.extend(scrape_palm_beach_county())
    scraped_data.extend(scrape_west_palm_beach())

    # Update master records while preserving history
    for event in scraped_data:
        existing_events[event["id"]] = event

    final_list = list(existing_events.values())
    save_data(final_list)
    print(f"Scraper execution complete. Updated data.json with {len(final_list)} total live events.")

if __name__ == "__main__":
    run()
