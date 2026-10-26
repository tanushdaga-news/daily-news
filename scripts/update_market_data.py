#!/usr/bin/env python3
"""Refresh THE 5IVE's market strip from Yahoo Finance daily candles.

Scheduled after Indian market close by GitHub Actions. This is end-of-day data,
not an exchange-authorised real-time feed.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TICKERS = [
    ("^BSESN", "Sensex", "Sensex"),
    ("^NSEI", "Nifty 50", "Nifty 50"),
    ("^NSEBANK", "Bank Nifty", "Bank Nifty"),
    ("^CNXIT", "Nifty IT", "Nifty IT"),
    ("^INDIAVIX", "India VIX", "India VIX"),
]
OUT = "market-data.json"
IST = ZoneInfo("Asia/Kolkata")

def fetch_chart(ticker):
    symbol = urllib.parse.quote(ticker, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=7d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 THE5IVE-market-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"No chart result for {ticker}")
    r = result[0]
    quotes = r.get("indicators", {}).get("quote", [{}])[0]
    closes = quotes.get("close", [])
    timestamps = r.get("timestamp", [])
    points = [(ts, close) for ts, close in zip(timestamps, closes) if close is not None]
    if len(points) < 2:
        raise RuntimeError(f"Not enough daily closes for {ticker}")
    ts, close = points[-1]
    _, previous = points[-2]
    return int(ts), float(close), float(previous)

def main():
    now = datetime.now(IST)
    items = []
    market_dates = []
    errors = []
    for ticker, label, query in TICKERS:
        try:
            ts, close, previous = fetch_chart(ticker)
            market_day = datetime.fromtimestamp(ts, IST).date()
            market_dates.append(market_day)
            change = (close / previous - 1) * 100 if previous else 0
            direction = "up" if change > 0 else ("down" if change < 0 else "")
            items.append({
                "n": f"{close:,.2f}",
                "l": f"{label} · close {market_day.strftime('%d %b %Y')}",
                "d": f"{change:+.2f}%",
                "dir": direction,
                "u": "https://www.google.com/search?q=" + urllib.parse.quote(query + " latest close")
            })
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")

    # Never overwrite a valid strip with an empty or partial response.
    if len(items) < 2 or not any(x["l"].startswith("Sensex") for x in items) or not any(x["l"].startswith("Nifty 50") for x in items):
        raise RuntimeError("Required index data missing; preserving the last published market-data.json. " + "; ".join(errors))

    newest_day = max(market_dates).strftime("%Y-%m-%d")
    data = {
        "updatedAt": now.strftime("%d %b %Y, %I:%M %p IST"),
        "marketDate": newest_day,
        "source": "Yahoo Finance daily candles; may be delayed and is not an exchange-authorised live feed",
        "items": items,
        "warnings": errors,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Wrote {len(items)} market items for latest session {newest_day}.")
    if errors:
        print("Warnings: " + "; ".join(errors))

if __name__ == "__main__":
    main()
