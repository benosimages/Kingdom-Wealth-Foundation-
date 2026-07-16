"""Night watch — 24/7 market intelligence scanner.

Runs hourly (own GitHub Action). Scans all markets + Bitcoin, detects big moves
and volatility spikes, checks the real economic calendar, and pushes alerts to
your phone via ntfy.

Env: OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV, NTFY_TOPIC
"""
import os
import time
from datetime import datetime

import requests

TOKEN = os.environ["OANDA_TOKEN"]
ACCOUNT = os.environ["OANDA_ACCOUNT"]
ENV = os.environ.get("OANDA_ENV", "practice")
NTFY = os.environ.get("NTFY_TOPIC", "")
BASE = "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
H = {"Authorization": f"Bearer {TOKEN}"}

WATCH = [
    ("SPX500_USD", "S&P 500"), ("NAS100_USD", "Nasdaq"), ("US30_USD", "Dow"),
    ("XAU_USD", "Gold"), ("WTICO_USD", "Oil"), ("NATGAS_USD", "NatGas"),
    ("BTC_USD", "Bitcoin"), ("XAG_USD", "Silver"), ("EUR_USD", "EUR/USD"),
]
MOVE_ALERT = 1.0   # % move on the day that triggers a push
SPIKE_ALERT = 2.5  # last-hour range vs 20-hour average that flags a spike


def notify(msg):
    if not NTFY:
        print(f"(no NTFY_TOPIC) {msg}")
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY}", data=msg.encode(), timeout=10)
        print(f"pushed: {msg}")
    except requests.RequestException as e:
        print(f"push failed: {e}")


def candles(inst, gran, count):
    r = requests.get(f"{BASE}/v3/instruments/{inst}/candles",
                     headers=H, params={"granularity": gran, "count": count, "price": "M"},
                     timeout=15)
    r.raise_for_status()
    return r.json().get("candles", [])


def upcoming_news(hours=12):
    """High-impact USD events in the next N hours (ForexFactory free feed)."""
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        r.raise_for_status()
        out = []
        now = time.time()
        for e in r.json():
            if e.get("impact") != "High" or e.get("country") not in ("USD", "ALL"):
                continue
            try:
                ts = datetime.fromisoformat(e["date"]).timestamp()
            except (KeyError, ValueError):
                continue
            if 0 <= ts - now <= hours * 3600:
                out.append(f"{e.get('title', '?')} at {datetime.fromtimestamp(ts):%I:%M %p} ET-ish")
        return out[:4]
    except (requests.RequestException, ValueError):
        return []


def main():
    alerts = []
    for sym, name in WATCH:
        try:
            day = candles(sym, "D", 2)
            if day:
                c = day[-1]["mid"]
                chg = (float(c["c"]) - float(c["o"])) / float(c["o"]) * 100
                if abs(chg) >= MOVE_ALERT:
                    alerts.append(f"{'📈' if chg > 0 else '📉'} {name} {chg:+.2f}% today ({float(c['c']):.2f})")
            hours = candles(sym, "H1", 21)
            if len(hours) >= 21:
                ranges = [float(h["mid"]["h"]) - float(h["mid"]["l"]) for h in hours]
                avg = sum(ranges[:-1]) / len(ranges[:-1])
                if avg > 0 and ranges[-1] / avg >= SPIKE_ALERT:
                    alerts.append(f"⚡ {name} volatility spike — last hour {ranges[-1]/avg:.1f}x normal")
        except requests.RequestException as e:
            print(f"{sym}: {e}")
    news = upcoming_news()
    if news:
        alerts.append("📅 Upcoming high-impact: " + " · ".join(news))
    if alerts:
        notify("Night watch:\n" + "\n".join(alerts[:6]))
    else:
        print("Night watch: all quiet.")


if __name__ == "__main__":
    main()
