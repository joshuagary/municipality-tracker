import os
from curl_cffi import requests
from bs4 import BeautifulSoup

def run_diagnostic():
    url = "https://www.wpb.org/Our-City/Meetings-Agendas"
    print("--- STARTING WPB AKAMAI BYPASS DIAGNOSTIC ---")
    try:
        # Impersonate Chrome 124 browser handshake
        res = requests.get(url, impersonate="chrome124", timeout=15)
        print(f"HTTP Status Code: {res.status_code}")
        print(f"Response Length: {len(res.text)} characters")

        soup = BeautifulSoup(res.text, "html.parser")
        text_sample = soup.get_text(separator=" ", strip=True)

        keywords = ["Zoning", "Planning", "Commission", "Downtown", "Plats"]
        for kw in keywords:
            found = kw.lower() in text_sample.lower()
            print(f"Keyword '{kw}' present: {found}")

    except Exception as e:
        print(f"Diagnostic Error: {e}")

if __name__ == "__main__":
    run_diagnostic()
