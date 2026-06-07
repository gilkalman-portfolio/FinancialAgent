"""
Debug script — run directly to inspect sec-api.io raw response
python debug_sec_api.py
"""
import os, json, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("SEC_API_KEY", "")
BASE_URL = "https://api.sec-api.io"

since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

payload = {
    "query": {
        "query_string": {
            "query": f'periodOfReport:[{since} TO *] AND documentType:"4"'
        }
    },
    "from": "0",
    "size": "3",
    "sort": [{"filedAt": {"order": "desc"}}],
}

print(f"API_KEY found: {'YES' if API_KEY else 'NO'}")
print(f"Query since: {since}")
print()

resp = requests.post(
    f"{BASE_URL}/insider-trading",
    params={"token": API_KEY},
    json=payload,
    timeout=15,
)

print(f"Status: {resp.status_code}")
print()

data = resp.json()

# הדפס את המפתחות ברמה הראשונה
print("Top-level keys:", list(data.keys()))
print("Total:", data.get("total"))
print()

transactions = data.get("transactions", [])
print(f"Transactions returned: {len(transactions)}")
print()

if transactions:
    print("=== First filing (raw) ===")
    print(json.dumps(transactions[0], indent=2)[:3000])
