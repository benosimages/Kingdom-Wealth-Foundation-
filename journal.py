"""Trade journal builder — closes the #1 named data gap.

Runs after each session (same GitHub Actions job). Parses the session log for
TAKE lines (score/grade/RR/notes), fetches the session's closed trades from
OANDA, joins them by instrument + time order, and appends rows to journal.csv,
which the workflow commits back to the repo. Over time this builds the
persistent score→outcome record the rolling 100-trade window can't provide.

Usage: python journal.py session.log
Env: OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV
"""
import csv, os, re, sys
from datetime import datetime, timezone

import requests

TOKEN = os.environ["OANDA_TOKEN"]
ACCOUNT = os.environ["OANDA_ACCOUNT"]
ENV = os.environ.get("OANDA_ENV", "practice")
BASE = "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
H = {"Authorization": f"Bearer {TOKEN}"}
JOURNAL = "journal.csv"
FIELDS = ["date", "instrument", "direction", "grade", "score", "rr_planned",
          "risk_usd", "entry", "exit", "realized_pl", "outcome", "notes"]

TAKE_RE = re.compile(
    r"TAKE (\S+) grade (\S+) \((\d+)/100\) risk \$([\d.]+) RR ([\d.]+) — (.*)")
ENTER_RE = re.compile(r"ENTER (\S+) (LONG|SHORT) (\d+)u @ ([\d.]+)")


def parse_log(path):
    takes, enters = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = TAKE_RE.search(line)
            if m:
                takes.append({"instrument": m[1], "grade": m[2], "score": int(m[3]),
                              "risk_usd": float(m[4]), "rr": float(m[5]), "notes": m[6].strip()})
            m = ENTER_RE.search(line)
            if m:
                enters.append({"instrument": m[1], "direction": m[2], "entry": float(m[4])})
    return takes, enters


def todays_closed():
    r = requests.get(f"{BASE}/v3/accounts/{ACCOUNT}/trades",
                     params={"state": "CLOSED", "count": 100}, headers=H, timeout=15)
    r.raise_for_status()
    today = datetime.now(timezone.utc).date().isoformat()
    out = [t for t in r.json().get("trades", []) if (t.get("closeTime") or "").startswith(today)]
    out.sort(key=lambda t: t.get("openTime", ""))
    return out


def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else "session.log"
    takes, enters = ([], []) if not os.path.exists(log_path) else parse_log(log_path)
    closed = todays_closed()
    if not closed and not takes:
        print("journal: nothing to record today")
        return

    # join closed trades to TAKE metadata: same instrument, in order
    by_inst = {}
    for t in takes:
        by_inst.setdefault(t["instrument"], []).append(t)

    new_file = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for tr in closed:
            inst = tr.get("instrument", "")
            meta = (by_inst.get(inst) or [{}]) and (by_inst[inst].pop(0) if by_inst.get(inst) else {})
            pl = float(tr.get("realizedPL", 0))
            units = float(tr.get("initialUnits", 0))
            w.writerow({
                "date": (tr.get("closeTime") or "")[:10],
                "instrument": inst,
                "direction": "LONG" if units > 0 else "SHORT",
                "grade": meta.get("grade", ""),
                "score": meta.get("score", ""),
                "rr_planned": meta.get("rr", ""),
                "risk_usd": meta.get("risk_usd", ""),
                "entry": tr.get("price", ""),
                "exit": tr.get("averageClosePrice", ""),
                "realized_pl": f"{pl:.2f}",
                "outcome": "win" if pl >= 0 else "loss",
                "notes": meta.get("notes", ""),
            })
    print(f"journal: recorded {len(closed)} closed trade(s) "
          f"({sum(1 for t in closed if float(t.get('realizedPL',0))>=0)} wins)")


if __name__ == "__main__":
    main()
