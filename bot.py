"""AutoTrader — OANDA session bot.

Runs one US-market session: polls M5 candles, trades one instrument at a time with a hard
±1.5% server-side bracket, rotates instruments on stop-out, locks in the daily goal, and
goes flat before the close. Exits on its own when the market closes.

Env: OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV (practice|live)
"""
import os, sys, time, json
from datetime import datetime, timezone, timedelta

import requests
import yaml

try:  # pandas-ta indicator backend (preferred when installed)
    import pandas as pd
    import pandas_ta as pta
    HAS_PTA = True
except ImportError:
    HAS_PTA = False

# ---------- config / env ----------
HERE = os.path.dirname(os.path.abspath(__file__))
_cfg_candidates = [os.path.join(HERE, "..", "config.yml"), os.path.join(HERE, "config.yml")]
_cfg_path = next(p for p in _cfg_candidates if os.path.exists(p))
with open(_cfg_path) as f:
    CFG = yaml.safe_load(f)

TOKEN = os.environ["OANDA_TOKEN"]
ACCOUNT = os.environ["OANDA_ACCOUNT"]
ENV = os.environ.get("OANDA_ENV", "practice")
BASE = "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

BRACKET = float(CFG.get("bracket_pct", 1.5)) / 100.0
SL = float(CFG.get("sl_pct", 3.0)) / 100.0
QUICK_TAKE = float(CFG.get("quick_take_usd", 10))
MAX_OPEN = int(CFG.get("max_open_trades", 3))
GOAL = float(CFG.get("daily_goal_usd", 50))
MAX_RISK = float(CFG.get("max_risk_usd", 30))
MAX_TRADES = int(CFG.get("max_trades_per_day", 12))
POLL = int(CFG.get("poll_seconds", 30))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


def notify(msg):
    """Push to your phone via ntfy.sh (free). Set NTFY_TOPIC secret + subscribe in the ntfy iOS app."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode(), timeout=5)
    except requests.RequestException:
        pass
INSTRUMENTS = CFG.get("instruments", ["SPX500_USD", "NAS100_USD", "XAU_USD"])
STRATEGY = CFG.get("strategy", "ema")
BENCH_WR = float(CFG.get("bench_wr_threshold", 0.40))
BENCH_MIN = int(CFG.get("bench_min_trades", 5))
PREFER_WINNERS = bool(CFG.get("prefer_winners", True))
BOUNCE_GREEN = bool(CFG.get("bounce_to_green", True))

# ---------- CRO governance layer ("protect capital" spec) ----------
MIN_SCORE = int(CFG.get("min_trade_score", 85))        # below this: REJECT
RISK_TIERS = {"A+": 0.005, "A": 0.004, "B": 0.0025}     # risk per trade as % of balance
MAX_PORTFOLIO_RISK = 0.02                               # max open risk across all positions
DD_LADDER = [(0.10, 0.0), (0.06, 0.0), (0.04, 0.5), (0.02, 0.75)]  # drawdown -> risk multiplier
API_FAIL_LIMIT = 5                                      # kill switch: consecutive API failures


def risk_multiplier(balance, peak):
    """Drawdown protection ladder. 6%+: paper-only (0 risk). 10%: hard stop."""
    if peak <= 0:
        return 1.0
    dd = (peak - balance) / peak
    for level, mult in DD_LADDER:
        if dd >= level:
            return mult
    return 1.0


def vol_percentile(series, ins_vol):
    rets = [abs(series[i] / series[i - 1] - 1) for i in range(max(1, len(series) - 20), len(series))]
    cur = sum(rets) / len(rets) if rets else ins_vol
    return min(99, round(50 * cur / ins_vol))


INS_VOL = {"SPX500_USD": 0.0019, "NAS100_USD": 0.0025, "US30_USD": 0.0016,
           "XAU_USD": 0.0021, "WTICO_USD": 0.0028, "NATGAS_USD": 0.0048}


def score_setup(inst, direction, series, day_series, prev, stats):
    """0-100 trade score per the CRO spec. Returns (score, grade, notes)."""
    notes = []
    c = series[-1]
    ivol = INS_VOL.get(inst, 0.0025)
    # Trend alignment 0-20 (three lookback horizons)
    bias = sum((2 if series[-1] > series[-n] else -2) for n in (48, 24, 12) if len(series) >= n)
    trend = 20 if bias * direction >= 4 else (10 if bias * direction > 0 else 0)
    notes.append(f"trend {trend}/20 (bias {bias:+d})")
    # Sweep quality 0-20 (depth of raid past prior-day level)
    hi, lo = prev
    recent = day_series[-8:]
    depth = (lo - min(recent)) / lo if direction == 1 else (max(recent) - hi) / hi
    sweep = 20 if depth > ivol * 1.4 else (12 if depth > ivol * 0.8 else 0)
    notes.append(f"sweep {sweep}/20")
    # Structure shift 0-20
    last3 = day_series[-4:-1]
    mss = 20 if (direction == 1 and c > max(last3)) or (direction == -1 and c < min(last3)) else 0
    notes.append(f"MSS {mss}/20")
    # Retracement/FVG confluence 0-10 (entry not chasing: price within 0.3% of the swept level)
    conf = 10 if abs(c - (lo if direction == 1 else hi)) / c < 0.003 else 0
    notes.append(f"retrace {conf}/10")
    # Session quality 0-10 (NY morning only — statistically the sweep window)
    ny = ny_now()
    mins = ny.hour * 60 + ny.minute
    sess = 10 if 9 * 60 + 30 <= mins <= 12 * 60 else 3
    notes.append(f"session {sess}/10")
    # Market regime 0-10 (reject chaos: vol percentile 30-80 is tradeable)
    vp = vol_percentile(series, ivol)
    regime = 10 if 30 <= vp <= 80 else (5 if vp < 30 else 0)
    notes.append(f"regime {regime}/10 (volp {vp})")
    # Historical match 0-10 (this market's real win rate from the journal)
    s = stats.get(inst)
    hist = 10 if s and s["n"] >= 5 and s["w"] / s["n"] >= 0.55 else (5 if not s or s["n"] < 5 else 0)
    notes.append(f"history {hist}/10")
    total = trend + sweep + mss + conf + sess + regime + hist
    grade = "A+" if total >= 95 else ("A" if total >= 90 else ("B" if total >= 85 else "REJECT"))
    return total, grade, " · ".join(notes)


def self_review(stats):
    """Every-session learning loop: rank markets, log the lesson. Runs without approval."""
    ranked = sorted(((s["w"] / s["n"], s["pnl"], i) for i, s in stats.items() if s["n"] >= 3), reverse=True)
    if ranked:
        best, worst = ranked[0], ranked[-1]
        log(f"SELF-REVIEW: best {best[2]} (wr {best[0]:.0%}, {best[1]:+.2f}) · "
            f"worst {worst[2]} (wr {worst[0]:.0%}, {worst[1]:+.2f}) · winners scanned first, cold markets benched")


def trade_stats():
    """Learn from real closed trades: per-instrument rolling win rate + P&L (last 100)."""
    try:
        j = api("GET", f"/v3/accounts/{ACCOUNT}/trades",
                params={"state": "CLOSED", "count": 100})
        stats = {}
        for t in j.get("trades", []):
            inst = t.get("instrument")
            pl = float(t.get("realizedPL", 0))
            s = stats.setdefault(inst, {"n": 0, "w": 0, "pnl": 0.0})
            s["n"] += 1
            s["pnl"] += pl
            if pl >= 0:
                s["w"] += 1
        return stats
    except requests.RequestException:
        return {}


def day_change(inst):
    """Percent move since today's open (last daily candle)."""
    try:
        j = api("GET", f"/v3/instruments/{inst}/candles",
                params={"granularity": "D", "count": 1, "price": "M"})
        c = j["candles"][-1]["mid"]
        o, cl = float(c["o"]), float(c["c"])
        return (cl - o) / o
    except (requests.RequestException, KeyError, IndexError):
        return 0.0


def ranked_scan_order(stats, exclude=None):
    """Winning markets first; benched (cold) markets removed; stopped-out market excluded."""
    order = []
    for inst in INSTRUMENTS:
        if inst == exclude:
            continue
        s = stats.get(inst)
        if s and s["n"] >= BENCH_MIN and s["w"] / s["n"] < BENCH_WR:
            log(f"BENCHED {inst}: win rate {s['w']}/{s['n']} below {BENCH_WR:.0%}")
            continue
        wr = (s["w"] / s["n"]) if s and s["n"] else 0.5
        order.append((wr, inst))
    if PREFER_WINNERS:
        order.sort(reverse=True)  # best rolling win rate first
    return [inst for _, inst in order]


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


# ---------- market hours (America/New_York, DST-aware) ----------
def ny_now():
    # US Eastern offset: EDT (UTC-4) roughly Mar–Nov, EST (UTC-5) otherwise.
    utc = datetime.now(timezone.utc)
    year = utc.year
    # 2nd Sunday of March, 1st Sunday of November, 2 AM local
    def nth_sunday(month, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        days = (6 - d.weekday()) % 7 + (n - 1) * 7
        return d + timedelta(days=days)
    edt = nth_sunday(3, 2) + timedelta(hours=7) <= utc < nth_sunday(11, 1) + timedelta(hours=6)
    return utc + timedelta(hours=-4 if edt else -5)


def market_phase():
    now = ny_now()
    if now.weekday() >= 5:
        return "closed"
    mins = now.hour * 60 + now.minute
    if mins < 9 * 60 + 30 or mins >= 16 * 60:
        return "closed"
    if mins >= 15 * 60 + 55:
        return "closing"   # flatten, no new trades
    return "open"


# ---------- OANDA helpers ----------
def api(method, path, **kw):
    r = requests.request(method, BASE + path, headers=H, timeout=15, **kw)
    if r.status_code >= 400:
        log(f"OANDA {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()


def candles(inst, count=120):
    j = api("GET", f"/v3/instruments/{inst}/candles",
            params={"granularity": "M5", "count": count, "price": "M"})
    return [float(c["mid"]["c"]) for c in j["candles"] if c["complete"]]


def account_summary():
    j = api("GET", f"/v3/accounts/{ACCOUNT}/summary")["account"]
    return float(j["balance"]), float(j["pl"]), int(j["openTradeCount"])


def open_trades():
    return api("GET", f"/v3/accounts/{ACCOUNT}/openTrades")["trades"]


def price_precision(inst):
    return {"SPX500_USD": 1, "NAS100_USD": 1, "XAU_USD": 3,
            "BTC_USD": 1, "WTICO_USD": 3}.get(inst, 2)


def place_bracketed(inst, direction, price, risk_usd=None):
    """Market order with server-side TP/SL. Sized by governed risk budget when provided."""
    risk_per_unit = price * SL
    budget = risk_usd if risk_usd is not None else MAX_RISK
    units = int(budget // risk_per_unit)
    if units < 1:
        log(f"SKIP {inst}: 1 unit would risk ${risk_per_unit:.2f} > budget ${budget:.2f}")
        return False
    p = price_precision(inst)
    tp = round(price * (1 + BRACKET * direction), p)
    sl = round(price * (1 - SL * direction), p)
    body = {"order": {
        "type": "MARKET", "instrument": inst, "units": str(units * direction),
        "timeInForce": "FOK", "positionFill": "DEFAULT",
        "takeProfitOnFill": {"price": f"{tp:.{p}f}"},
        "stopLossOnFill": {"price": f"{sl:.{p}f}"},
    }}
    j = api("POST", f"/v3/accounts/{ACCOUNT}/orders", data=json.dumps(body))
    fill = j.get("orderFillTransaction")
    if fill:
        log(f"ENTER {inst} {'LONG' if direction==1 else 'SHORT'} {units}u @ {fill['price']} "
            f"TP {tp} SL {sl} (risk ~${units*risk_per_unit:.2f})")
        notify(f"📈 ENTER {inst} {'LONG' if direction==1 else 'SHORT'} @ {fill['price']} (SL {sl})")
        return True
    log(f"Order not filled: {list(j.keys())}")
    return False


def close_all():
    for t in open_trades():
        j = api("PUT", f"/v3/accounts/{ACCOUNT}/trades/{t['id']}/close")
        pl = j.get("orderFillTransaction", {}).get("pl", "?")
        log(f"EXIT {t['instrument']} trade {t['id']} P&L {pl} (session close)")


# ---------- strategies (same rules as the simulator app) ----------
def ema(arr, n):
    k, out = 2 / (n + 1), [arr[0]]
    for v in arr[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    if HAS_PTA and len(closes) > n:
        v = pta.rsi(pd.Series(closes), length=n)
        if v is not None and len(v) and not pd.isna(v.iloc[-1]):
            return float(v.iloc[-1])
    if len(closes) < n + 1:
        return 50.0
    g = l = 0.0
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = (g + d, l) if d > 0 else (g, l - d)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def prev_day_levels(inst):
    j = api("GET", f"/v3/instruments/{inst}/candles",
            params={"granularity": "D", "count": 3, "price": "M"})
    done = [c for c in j["candles"] if c["complete"]]
    c = done[-1]["mid"]
    return float(c["h"]), float(c["l"])


def signal(strat, series, day_series, prev=None):
    """Return +1 (long), -1 (short) or 0. Mirrors the app's engine."""
    if len(series) < 30:
        return 0
    c = series[-1]
    if strat == "ema":
        e9, e21 = ema(series[-60:], 9), ema(series[-60:], 21)
        roc = (c - series[-6]) / series[-6]
        if e9[-1] > e21[-1] and e9[-2] <= e21[-2] and roc > 0:
            return 1
        if e9[-1] < e21[-1] and e9[-2] >= e21[-2] and roc < 0:
            return -1
    elif strat == "orb":
        if len(day_series) < 7:
            return 0
        hi, lo = max(day_series[:6]), min(day_series[:6])
        buf = (hi - lo) * 0.1
        if c > hi + buf:
            return 1
        if c < lo - buf:
            return -1
    elif strat == "tjr":
        # TJR playbook: sweep of prior-day high/low -> rejection -> break of structure.
        if not prev or len(day_series) < 5:
            return 0
        hi, lo = prev
        recent, last3 = day_series[-8:], day_series[-4:-1]
        if max(recent) > hi and c < hi and c < min(last3):
            return -1
        if min(recent) < lo and c > lo and c > max(last3):
            return 1
    elif strat == "smc":
        # A+ Liquidity Model: HTF bias score + prior-day sweep + structure shift + VWAP side.
        if not prev or len(series) < 60 or len(day_series) < 6:
            return 0
        hi, lo = prev
        score = 0
        for n in (48, 24, 12):
            seg = series[-n:]
            score += 2 if seg[-1] > seg[0] else -2
        vwap_mean = sum(day_series) / len(day_series)
        recent, last3 = day_series[-8:], day_series[-4:-1]
        if score >= 4 and min(recent) < lo and c > lo and c > max(last3) and c > vwap_mean * 0.999:
            return 1
        if score <= -4 and max(recent) > hi and c < hi and c < min(last3) and c < vwap_mean * 1.001:
            return -1
    elif strat == "rsi2":
        # Best-evidenced retail edge: deep RSI(2) pullback traded WITH the trend.
        if len(series) < 60:
            return 0
        r2 = rsi(series, 2)
        trend_ma = sum(series[-50:]) / min(50, len(series))
        if r2 < 10 and c > trend_ma:
            return 1
        if r2 > 90 and c < trend_ma:
            return -1
    elif strat == "box":
        # Darvas-style candle box: 8-bar tight consolidation -> trade the break.
        if len(series) < 12:
            return 0
        box = series[-9:-1]
        hi, lo = max(box), min(box)
        mid = (hi + lo) / 2
        if (hi - lo) / mid <= 0.004:
            if c > hi:
                return 1
            if c < lo:
                return -1
    elif strat == "vwap":
        if len(day_series) < 10:
            return 0
        mean = sum(day_series) / len(day_series)
        dev, r = (c - mean) / mean, rsi(series)
        if dev < -0.0018 and r < 38:
            return 1
        if dev > 0.0018 and r > 62:
            return -1
    return 0


# ---------- session loop ----------
def main():
    log(f"AutoTrader start — env={ENV} strategy={STRATEGY} bracket=±{BRACKET*100:.1f}% "
        f"goal=${GOAL} max_risk=${MAX_RISK}")
    start_balance, _, _ = account_summary()
    peak_balance = start_balance
    api_fails = 0
    last_review_n = 0
    trades_today = 0
    goal_locked = False
    session_open = ny_now().replace(hour=9, minute=30)

    while True:
        phase = market_phase()
        if phase == "closed":
            close_all()
            log("Market closed — bot exiting. See you at the next open.")
            return
        if phase == "closing":
            close_all()
            log("3:55 PM ET — flat for the close.")
            time.sleep(POLL)
            continue

        try:
            balance, _, _ = account_summary()
            peak_balance = max(peak_balance, balance)
            rm = risk_multiplier(balance, peak_balance)
            if rm == 0.0:
                log(f"DRAWDOWN PROTECTION: {(peak_balance-balance)/peak_balance:.1%} down — no new trades (paper-only mode). Stops remain live.")
            day_pl = balance - start_balance
            api_fails = 0

            if not goal_locked and day_pl >= GOAL:
                goal_locked = True
                log(f"DAILY GOAL HIT (+${day_pl:.2f} ≥ ${GOAL}) — no new trades today.")
                notify(f"🎯 DAILY GOAL HIT +${day_pl:.2f} — done for the day")

            open_list = open_trades()
            open_count = len(open_list)

            # quick-take on every open trade, every cycle (stop stays server-side)
            for t in open_list:
                upl = float(t.get("unrealizedPL", 0))
                if QUICK_TAKE > 0 and upl >= QUICK_TAKE:
                    j = api("PUT", f"/v3/accounts/{ACCOUNT}/trades/{t['id']}/close")
                    pl = j.get("orderFillTransaction", {}).get("pl", "?")
                    log(f"QUICK TAKE {t['instrument']} trade {t['id']} P&L {pl} (>= ${QUICK_TAKE})")
                    notify(f"💰 CASH OUT {t['instrument']} +${pl}")
                    open_count -= 1

            if open_count < MAX_OPEN:
                # detect stop-out → bounce to a market that's green today
                just_stopped = None
                closed = api("GET", f"/v3/accounts/{ACCOUNT}/trades",
                             params={"state": "CLOSED", "count": 1}).get("trades", [])
                if closed and float(closed[0].get("realizedPL", 0)) < 0:
                    just_stopped = closed[0]["instrument"]
                    notify(f"🛑 STOP {just_stopped} {float(closed[0]['realizedPL']):+.2f} — bouncing to a green market")

                if not goal_locked and trades_today < MAX_TRADES and rm > 0.0:
                    # learn from real trade history: winners first, cold markets benched,
                    # stopped-out market excluded; after a loss, green-today markets lead
                    stats = trade_stats()
                    n_closed = sum(s["n"] for s in stats.values())
                    if n_closed >= last_review_n + 20:
                        self_review(stats)
                        last_review_n = n_closed
                    held = {t["instrument"] for t in open_list}
                    scan = ranked_scan_order(stats, exclude=just_stopped)
                    if BOUNCE_GREEN and just_stopped:
                        scan.sort(key=lambda i: day_change(i) <= 0)  # green-day markets first
                        log(f"Stop-out on {just_stopped} → bouncing to: {scan[:3]}")
                    for inst in scan:
                        if inst in held:
                            continue  # one position per instrument when stacking
                        try:
                            series = candles(inst)
                        except requests.RequestException:
                            continue
                        bars_today = min(len(series), max(2, int(
                            (ny_now() - session_open).total_seconds() // 300)))
                        prev = prev_day_levels(inst) if STRATEGY in ("tjr", "smc") else None
                        sig = signal(STRATEGY, series, series[-bars_today:], prev)
                        if sig != 0 and prev:
                            score, grade, why = score_setup(inst, sig, series, series[-bars_today:], prev, stats)
                            if score < MIN_SCORE:
                                log(f"REJECT {inst} score {score}/100 — {why}")
                                continue
                            open_risk = len(open_list) * RISK_TIERS["B"]  # conservative estimate
                            tier = min(RISK_TIERS[grade], max(0.0, MAX_PORTFOLIO_RISK - open_risk))
                            risk_usd = balance * tier * rm
                            log(f"TAKE {inst} grade {grade} ({score}/100) risk ${risk_usd:.2f} — {why}")
                            if place_bracketed(inst, sig, series[-1], risk_usd=risk_usd):
                                notify(f"✅ {grade} setup: {inst} {'LONG' if sig==1 else 'SHORT'} ({score}/100)")
                                trades_today += 1
                                break
                        elif sig != 0 and place_bracketed(inst, sig, series[-1]):
                            trades_today += 1
                            break
            else:
                log(f"Holding {open_count} position(s) · day P&L {day_pl:+.2f} · balance ${balance:.2f}")
        except requests.RequestException as e:
            api_fails += 1
            log(f"API error {api_fails}/{API_FAIL_LIMIT} (will retry): {e}")
            if api_fails >= API_FAIL_LIMIT:
                close_all()
                log("KILL SWITCH: repeated data failures — flat and exiting. Safety over opportunity.")
                notify("🛑 KILL SWITCH: data failures — bot flat and stopped")
                return

        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
