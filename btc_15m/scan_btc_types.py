import requests
import json

GAMMA_API_URL = "https://gamma-api.polymarket.com/events"

def scan_btc_markets():
    print("🔍 Scanning for Bitcoin Markets...")
    try:
        resp = requests.get(GAMMA_API_URL, params={
            "limit": 100, "active": "true", "archived": "false", "closed": "false",
            "order": "volume24hr", "ascending": "false"
        })
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"Error: {e}")
        return

    count = 0
    for event in events:
        markets = event.get("markets", [])
        for market in markets:
            question = market.get("question", "")
            if "Up" in question or "Down" in question or "15m" in question:
                print(f"- {question}")
                count += 1
                if count >= 30: return

if __name__ == "__main__":
    scan_btc_markets()
