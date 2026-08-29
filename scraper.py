import json
import re
import os
import io
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from PyPDF2 import PdfReader

DATA_FILE = "data.json"

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

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
            return re.sub(r'\s+', ' ', extracted_text).strip().lower()
    except Exception as e:
        print(f"PDF extraction error on {url}: {e}")
    return None

# --- 1. BOCA RATON MODULE ---
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
                    if iso_date:
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

# --- 2. PALM BEACH COUNTY MODULE ---
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

 # --- 3. WEST PALM BEACH MODULE (LLM PARSER WITH SERVER-RENDERED URL) ---
def scrape_west_palm_beach():
    events = []
    # Using the server-rendered calendar feed URL so JavaScript execution isn't required
    url = "https://www.wpb.org/Our-City/Calendars/Meetings"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    hf_token = os.environ.get("HF_TOKEN")

    try:
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code != 200:
            return events

        clean_soup = BeautifulSoup(res.text, "html.parser")
        for tag in clean_soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        # Extract textual content including links for the LLM
        page_content = []
        for elem in clean_soup.find_all(['a', 'div', 'p', 'span', 'li']):
            text = elem.get_text(strip=True)
            href = elem.get('href') if elem.name == 'a' else None
            if text and len(text) > 2:
                if href:
                    page_content.append(f"Text: '{text}' | Link: '{href}'")
                else:
                    page_content.append(text)

        cleaned_text = "\n".join(page_content[:2000])

        if hf_token:
            router_url = "https://api-inference.huggingface.co/models/meta-llama/Meta-Llama-3.1-8B-Instruct/v1/chat/completions"
            payload = {
                "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise data extractor. Extract municipal meetings and return ONLY a valid JSON array. Do not include markdown ticks, conversational text, or explanations."
                    },
                    {
                        "role": "user",
                        "content": f"""Extract all municipal public meetings from this text.
Target boards: City Commission, Zoning Board of Appeals, Planning Board, Downtown Action Committee, Plans & Plats Review Committee.

Schema format:
[
  {{"title": "Clean Title", "date": "YYYY-MM-DD", "time": "HH:MM AM/PM", "link": "URL"}}
]

Text:
{cleaned_text}"""
                    }
                ],
                "temperature": 0.01,
                "max_tokens": 1200
            }

            hf_headers = {
                "Authorization": f"Bearer {hf_token}",
                "Content-Type": "application/json"
            }

            hf_res = requests.post(router_url, headers=hf_headers, json=payload, timeout=15)
            if hf_res.status_code == 200:
                content = hf_res.json()['choices'][0]['message']['content'].strip()
                content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
                content = re.sub(r'\s*```$', '', content, flags=re.MULTILINE)
                
                llm_data = json.loads(content)

                for item in llm_data:
                    href = item.get("link", "")
                    full_link = href if href.startswith("http") else f"https://www.wpb.org{href}"
                    events.append({
                        "id": f"wpb-{item['date']}-{hash(full_link)}",
                        "muni_short": "WPB",
                        "muni_full": "City of West Palm Beach",
                        "title": item["title"],
                        "date": item["date"],
                        "time": item.get("time", "5:00 PM"),
                        "link": full_link,
                        "summary": f"Official {item['title']} parsed via Llama 3.1 engine."
                    })
                print(f"Llama 3.1 successfully returned {len(events)} events for WPB.")

    except Exception as e:
        print(f"Error in LLM WPB parsing: {e}")

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
