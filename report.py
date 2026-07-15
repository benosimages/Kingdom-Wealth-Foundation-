"""Nightly performance report — quantstats tear sheet from OANDA trade history.

Run after each session (the workflow calls it after bot.py exits):
    python bot/report.py

Writes reports/tearsheet.html (Sharpe, Sortino, max drawdown, win rate, daily P&L)
and prints a plain-text summary into the Actions log.
"""
import os
from datetime import datetime, timezone

import requests

try:
    import pandas as pd
    import quantstats as qs
    HAS_QS = True
except ImportError:
    HAS_QS = False

TOKEN = os.environ["OANDA_TOKEN"]
ACCOUNT = os.environ["OANDA_ACCOUNT"]
ENV = os.environ.get("OANDA_ENV", "practice")
BASE = "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
H = {"Authorization": f"Bearer {TOKEN}"}


def closed_trades(count=500):
    r = requests.get(f"{BASE}/v3/accounts/{ACCOUNT}/trades",
                     headers=H, params={"state": "CLOSED", "count": count}, timeout=20)
    r.raise_for_status()
    return r.json().get("trades", [])


def main():
    trades = closed_trades()
    if not trades:
        print("No closed trades yet.")
        return
    rows = [(t["closeTime"][:10], float(t.get("realizedPL", 0))) for t in trades]
    wins = sum(1 for _, pl in rows if pl >= 0)
    total = sum(pl for _, pl in rows)
    print(f"Closed trades: {len(rows)} · win rate {wins/len(rows)*100:.0f}% · net P&L {total:+.2f}")

    if HAS_QS:
        df = pd.DataFrame(rows, columns=["date", "pl"])
        daily = df.groupby("date")["pl"].sum()
        daily.index = pd.to_datetime(daily.index)
        # convert P&L to returns against a nominal base so quantstats ratios make sense
        base = 5000.0
        returns = daily / base
        os.makedirs("reports", exist_ok=True)
        qs.reports.html(returns, output="reports/tearsheet.html",
                        title=f"AutoTrader — {datetime.now(timezone.utc):%Y-%m-%d}")
        print("Wrote reports/tearsheet.html")
    else:
        print("quantstats not installed — text summary only.")


if __name__ == "__main__":
    main()
