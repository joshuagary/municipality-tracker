import os
import requests
from bs4 import BeautifulSoup

def run_diagnostic():
    url = "https://www.wpb.org/Our-City/Calendars/Meetings"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    print("--- STARTING WPB DIAGNOSTIC CHECK ---")
    try:
        res = requests.get(url, headers=headers, timeout=10)
        print(f"HTTP Status Code: {res.status_code}")
        print(f"Response Length: {len(res.text)} characters")

        soup = BeautifulSoup(res.text, "html.parser")
        text_sample = soup.get_text(separator=" ", strip=True)

        print("\n--- FIRST 1000 CHARACTERS OF FETCHED HTML ---")
        print(text_sample[:1000])

        print("\n--- KEYWORD SEARCH IN HTML ---")
        keywords = ["Zoning", "Planning", "Commission", "Downtown", "Plats"]
        for kw in keywords:
            found = kw.lower() in text_sample.lower()
            print(f"Keyword '{kw}' present: {found}")

    except Exception as e:
        print(f"Diagnostic Error: {e}")

if __name__ == "__main__":
    run_diagnostic()
