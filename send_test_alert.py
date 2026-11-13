#!/usr/bin/env python3
"""Imitate TradingView webhooks against the local server.

Order alert (strategy.pine):   python3 send_test_alert.py buy AAPL 227.50
Price push (price_feed.pine):  python3 send_test_alert.py prices AAPL=358.2 NYSE:KO=99.1
"""
import json
import os
import sys
import urllib.error
import urllib.request

secret = os.environ.get("WEBHOOK_SECRET", "change-me")
if sys.argv[1] == "prices":
    prices = {k: float(v) for k, v in (a.split("=") for a in sys.argv[2:])}
    body = {"secret": secret, "type": "prices", "bar_seconds": 60, "prices": prices}
else:
    action, ticker, price = sys.argv[1], sys.argv[2], float(sys.argv[3])
    body = {"secret": secret, "ticker": ticker, "action": action, "price": price, "time": "test"}
url = f"http://localhost:{os.environ.get('PORT', '8080')}/webhook"
req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
