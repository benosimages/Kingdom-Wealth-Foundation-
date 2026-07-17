"""Diagnostic backtest — finds WHY zero trades fire and which settings produce them.

Replays 30 days of real OANDA M5 candles through bot.signal()/score_setup(),
counting every rejection reason, then simulates a grid of (min_score, min_rr)
combos with full P&L. Writes diag_report.md.

Usage: python diagtest.py          Env: OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV
"""
import csv
from datetime import datetime, timedelta, timezone

import bot

BALANCE = 100_000.0
DAYS = 30
INSTRUMENTS = bot.INSTRUMENTS
NY_OFFSET = timedelta(hours=-4)
GRID = [(80, 2.0), (65, 2.0), (65, 1.5), (55, 1.5), (50, 1.2), (0, 1.0)]


def parse_t(s):
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def fetch_m5(inst):
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


def simulate(merged, levels, min_score, min_rr, diag=None):
    series = {i: [] for i in INSTRUMENTS}
    day_series = {i: [] for i in INSTRUMENTS}
    trades, open_pos = [], {}
    cur_day, day_pl, trades_today, consec, day_stopped = None, 0.0, 0, 0, False
    day_results = {}

    def bump(k):
        if diag is not None:
            diag[k] = diag.get(k, 0) + 1

    for (t, inst, o, h, l, c) in merged:
        ny = t + NY_OFFSET
        d = ny.date()
        if d != cur_day:
            for i2, p in list(open_pos.items()):
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

        p = open_pos.get(inst)
        if p:
            hit_sl = l <= p["sl"] if p["dir"] == 1 else h >= p["sl"]
            hit_tp = h >= p["tp"] if p["dir"] == 1 else l <= p["tp"]
            if hit_sl:
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
            else:
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
        bump("bars_checked")
        sig = bot.signal("tjr", series[inst], day_series[inst], prev)
        if sig == 0:
            bump("no_signal")
            continue
        bump("signal_fired")
        bot.ny_now = lambda ny=ny: ny
        score, grade, why = bot.score_setup(inst, sig, series[inst], day_series[inst], prev, {})
        if diag is not None:
            diag.setdefault("scores", []).append(score)
        if score < min_score:
            bump("score_too_low")
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
        if rr < min_rr:
            bump("rr_too_low")
            continue
        bump("trade_taken")
        risk_usd = BALANCE * bot.RISK_TIERS.get(grade, min(bot.RISK_TIERS.values()))
        units = risk_usd / max(abs(c - sl), 1e-9)
        open_pos[inst] = {"date": str(d), "time": ny.strftime("%H:%M"), "inst": inst,
                          "dir": sig, "grade": grade, "score": score, "rr": round(rr, 2),
                          "entry": c, "sl": sl, "sl0": sl, "tp": tp, "units": units}
        trades_today += 1
    if cur_day is not None:
        day_results[cur_day] = day_pl
    return trades, day_results


def main():
    data, levels = {}, {}
    for inst in INSTRUMENTS:
        bot.log(f"fetching {inst}...")
        data[inst] = fetch_m5(inst)
        levels[inst] = daily_levels(inst)
    merged = sorted(((t, inst, o, h, l, c) for inst in INSTRUMENTS
                     for (t, o, h, l, c) in data[inst]), key=lambda x: x[0])

    diag = {}
    lines = [f"# Diagnostic backtest — last {DAYS} days · {', '.join(INSTRUMENTS)}",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z", "",
             "## Settings grid — trades / win rate / net P&L", "",
             "| min score | min RR | trades | win rate | net P&L | green days | worst day |",
             "|---|---|---|---|---|---|---|"]
    best_trades = None
    for (ms, mr) in GRID:
        d_ = diag if (ms, mr) == (0, 1.0) else None
        trades, days = simulate(merged, levels, ms, mr, d_)
        wins = sum(1 for t in trades if t["pl"] >= 0)
        net = sum(t["pl"] for t in trades)
        wr = f"{wins/len(trades):.0%}" if trades else "—"
        worst = min(days.values()) if days else 0.0
        green = sum(1 for v in days.values() if v > 0)
        lines.append(f"| {ms} | {mr} | {len(trades)} | {wr} | ${net:+,.2f} | {green}/{len(days)} | ${worst:+,.2f} |")
        if (ms, mr) == (0, 1.0):
            best_trades = trades
        bot.log(f"grid {ms}/{mr}: {len(trades)} trades net {net:+.2f}")

    lines += ["", "## Why setups were rejected (fully loosened pass)", ""]
    scores = diag.pop("scores", [])
    for k in ("bars_checked", "no_signal", "signal_fired", "score_too_low", "rr_too_low", "trade_taken"):
        lines.append(f"- {k}: {diag.get(k, 0)}")
    if scores:
        scores.sort()
        mid = scores[len(scores)//2]
        lines.append(f"- score distribution: min {scores[0]}, median {mid}, max {scores[-1]} (n={len(scores)})")
    lines += ["", "_Same conservative fill model as backtest.py. Use the grid row with the best net P&L"
              " AND an acceptable worst day to choose live settings._"]

    if best_trades:
        with open("diag_trades.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "time", "inst", "dir", "grade", "score", "rr", "entry", "exit", "pl", "how"])
            for t in best_trades:
                w.writerow([t["date"], t["time"], t["inst"], "LONG" if t["dir"] == 1 else "SHORT",
                            t["grade"], t["score"], t["rr"], f"{t['entry']:.5f}", f"{t['exit']:.5f}",
                            f"{t['pl']:.2f}", t["how"]])

    with open("diag_report.md", "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
