"""Offline backtest — role priority #2.

Replays the last ~30 days of real OANDA M5 candles through the EXACT live code:
bot.signal('tjr', ...) for entries and bot.score_setup() for grading, plus the
same structural SL/TP, 2R filter, breakeven move, per-day caps and loss stops
as bot.py's main loop. Writes backtest_report.md + backtest_trades.csv.

Fills are simulated conservatively: if a bar touches both stop and target, the
stop wins. Results are an estimate — no spread/slippage model.

Usage: python backtest.py          Env: OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV
"""
import csv
from datetime import datetime, timedelta, timezone

import bot  # the live strategy code — same signal(), score_setup(), constants

BALANCE = 100_000.0
DAYS = 30
INSTRUMENTS = bot.INSTRUMENTS
MIN_SCORE = bot.MIN_SCORE
NY_OFFSET = timedelta(hours=-4)  # EDT all summer (backtest window is Jun-Jul)


def parse_t(s):
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def fetch_m5(inst):
    """Paginated M5 OHLC for the window."""
    start = datetime.now(timezone.utc) - timedelta(days=DAYS + 5)
    out, frm = [], start
    while True:
        j = bot.api("GET", f"/v3/instruments/{inst}/candles",
                    params={"granularity": "M5", "from": frm.isoformat(), "count": 5000, "price": "M"})
        cs = [c for c in j.get("candles", []) if c["complete"]]
        for c in cs:
            m = c["mid"]
            out.append((parse_t(c["time"]), float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"])))
        if len(cs) < 4999:
            return out
        frm = out[-1][0] + timedelta(minutes=5)


def daily_levels(inst):
    j = bot.api("GET", f"/v3/instruments/{inst}/candles",
                params={"granularity": "D", "count": DAYS + 10, "price": "M"})
    lv = {}
    for c in j["candles"]:
        if c["complete"]:
            d = (parse_t(c["time"]) + NY_OFFSET).date()
            lv[d] = (float(c["mid"]["h"]), float(c["mid"]["l"]))
    return lv


def main():
    data, levels = {}, {}
    for inst in INSTRUMENTS:
        bot.log(f"fetching {inst}...")
        data[inst] = fetch_m5(inst)
        levels[inst] = daily_levels(inst)

    # merge bars chronologically, tagged by instrument
    merged = sorted(((t, inst, o, h, l, c) for inst in INSTRUMENTS
                     for (t, o, h, l, c) in data[inst]), key=lambda x: x[0])

    series = {i: [] for i in INSTRUMENTS}      # full close history per inst
    day_series = {i: [] for i in INSTRUMENTS}  # today's closes per inst
    trades = []
    open_pos = {}                              # inst -> position dict
    cur_day, day_pl, trades_today, consec, day_stopped = None, 0.0, 0, 0, False
    day_results = {}

    for (t, inst, o, h, l, c) in merged:
        ny = t + NY_OFFSET
        d = ny.date()
        if d != cur_day:
            for i2, p in list(open_pos.items()):   # flatten at day roll (safety)
                pl = (series[i2][-1] - p["entry"]) * p["units"] * p["dir"]
                trades.append({**p, "exit": series[i2][-1], "pl": pl, "how": "eod"})
                day_pl += pl
            open_pos.clear()
            if cur_day is not None:
                day_results[cur_day] = day_pl
            cur_day, day_pl, trades_today, consec, day_stopped = d, 0.0, 0, 0, False
            for i2 in INSTRUMENTS:
                day_series[i2] = []
        if ny.weekday() >= 5:
            continue
        mins = ny.hour * 60 + ny.minute
        series[inst].append(c)
        if mins < 9 * 60 + 30 or mins >= 16 * 60:
            continue
        day_series[inst].append(c)

        # manage open position on this instrument
        p = open_pos.get(inst)
        if p:
            hit_sl = l <= p["sl"] if p["dir"] == 1 else h >= p["sl"]
            hit_tp = h >= p["tp"] if p["dir"] == 1 else l <= p["tp"]
            if hit_sl:  # conservative: stop first
                pl = (p["sl"] - p["entry"]) * p["units"] * p["dir"]
                trades.append({**p, "exit": p["sl"], "pl": pl, "how": "stop" if p["sl"] != p["entry"] else "breakeven"})
                day_pl += pl
                consec = consec + 1 if pl < 0 else 0
                del open_pos[inst]
            elif hit_tp:
                pl = (p["tp"] - p["entry"]) * p["units"] * p["dir"]
                trades.append({**p, "exit": p["tp"], "pl": pl, "how": "target"})
                day_pl += pl
                consec = 0
                del open_pos[inst]
            else:  # breakeven move at ~1R (same as move_stop_to_breakeven)
                risk = abs(p["entry"] - p["sl0"])
                up = (h - p["entry"]) if p["dir"] == 1 else (p["entry"] - l)
                if up >= risk and p["sl"] != p["entry"]:
                    p["sl"] = p["entry"]
            if mins >= 15 * 60 + 55 and inst in open_pos:
                pl = (c - p["entry"]) * p["units"] * p["dir"]
                trades.append({**p, "exit": c, "pl": pl, "how": "close"})
                day_pl += pl
                del open_pos[inst]
            continue

        # day-level guards (same rules as main loop)
        if day_stopped or trades_today >= bot.MAX_TRADES or len(open_pos) >= bot.MAX_OPEN:
            continue
        if day_pl <= -BALANCE * bot.DAILY_LOSS_CAP or consec >= bot.MAX_CONSEC_LOSSES:
            day_stopped = True
            continue
        if mins >= 15 * 60 + 30 or len(series[inst]) < 60 or len(day_series[inst]) < 6:
            continue
        prev = levels[inst].get(d - timedelta(days=1)) or levels[inst].get(d - timedelta(days=3))
        if not prev:
            continue
        sig = bot.signal("tjr", series[inst], day_series[inst], prev)
        if sig == 0:
            continue
        bot.ny_now = lambda ny=ny: ny  # score session window at the BAR's time, not now
        score, grade, why = bot.score_setup(inst, sig, series[inst], day_series[inst], prev, {})
        if score < MIN_SCORE:
            continue
        hi, lo = prev
        recent8 = day_series[inst][-8:]
        if sig == 1:
            sl = max(min(recent8) * 0.9995, c * (1 - bot.SL))
            tp = hi
        else:
            sl = min(max(recent8) * 1.0005, c * (1 + bot.SL))
            tp = lo
        rr = abs(tp - c) / max(abs(c - sl), 1e-9)
        if rr < 2.0:
            continue
        risk_usd = BALANCE * bot.risk_usd = BALANCE * bot.RISK_TIERS.get(grade, min(bot.RISK_TIERS.values()))
        units = risk_usd / max(abs(c - sl), 1e-9)
        open_pos[inst] = {"date": str(d), "time": ny.strftime("%H:%M"), "inst": inst,
                          "dir": sig, "grade": grade, "score": score, "rr": round(rr, 2),
                          "entry": c, "sl": sl, "sl0": sl, "tp": tp, "units": units}
        trades_today += 1
    if cur_day is not None:
        day_results[cur_day] = day_pl

    # ---- report ----
    wins = [t for t in trades if t["pl"] >= 0]
    net = sum(t["pl"] for t in trades)
    by_grade, by_inst = {}, {}
    for t in trades:
        for key, bucket in ((t["grade"], by_grade), (t["inst"], by_inst)):
            b = bucket.setdefault(key, {"n": 0, "w": 0, "pl": 0.0})
            b["n"] += 1
            b["pl"] += t["pl"]
            if t["pl"] >= 0:
                b["w"] += 1
    worst_day = min(day_results.values()) if day_results else 0.0
    green_days = sum(1 for v in day_results.values() if v > 0)

    with open("backtest_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "inst", "dir", "grade", "score", "rr", "entry", "exit", "pl", "how"])
        for t in trades:
            w.writerow([t["date"], t["time"], t["inst"], "LONG" if t["dir"] == 1 else "SHORT",
                        t["grade"], t["score"], t["rr"], f"{t['entry']:.5f}", f"{t['exit']:.5f}",
                        f"{t['pl']:.2f}", t["how"]])

    def pct(w_, n_):
        return f"{w_/n_:.0%}" if n_ else "—"
    lines = [
        f"# Backtest — TJR + governance, last {DAYS} days, ${BALANCE:,.0f} nominal",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z · instruments: {', '.join(INSTRUMENTS)}",
        "", f"**Trades:** {len(trades)} · **Win rate:** {pct(len(wins), len(trades))} · "
        f"**Net P&L:** ${net:+,.2f} · **Green days:** {green_days}/{len(day_results)} · "
        f"**Worst day:** ${worst_day:+,.2f}", "",
        "| Grade | trades | win rate | P&L |", "|---|---|---|---|",
    ]
    for g in ("A+", "A", "B"):
        b = by_grade.get(g)
        if b:
            lines.append(f"| {g} | {b['n']} | {pct(b['w'], b['n'])} | ${b['pl']:+,.2f} |")
    lines += ["", "| Market | trades | win rate | P&L |", "|---|---|---|---|"]
    for i, b in sorted(by_inst.items(), key=lambda x: -x[1]["pl"]):
        lines.append(f"| {i} | {b['n']} | {pct(b['w'], b['n'])} | ${b['pl']:+,.2f} |")
    lines += ["", "_Conservative fills (stop wins ties); no spread/slippage model. "
              "This estimates the edge — live results will differ._"]
    with open("backtest_report.md", "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
