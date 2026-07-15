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
with open(os.path.join(HERE, "..", "config.yml")) as f:
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
INSTRUMENTS = CFG.get("instruments", ["SPX500_USD", "NAS100_USD", "XAU_USD"])
STRATEGY = CFG.get("strategy", "ema")


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


def place_bracketed(inst, direction, price):
    """Market order with server-side TP/SL at ±bracket. Returns True if placed."""
    risk_per_unit = price * SL
    units = int(MAX_RISK // risk_per_unit)
    if units < 1:
        log(f"SKIP {inst}: 1 unit would risk ${risk_per_unit:.2f} > max_risk ${MAX_RISK:.2f}")
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
    active = 0
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
            day_pl = balance - start_balance

            if not goal_locked and day_pl >= GOAL:
                goal_locked = True
                log(f"DAILY GOAL HIT (+${day_pl:.2f} ≥ ${GOAL}) — no new trades today.")

            open_list = open_trades()
            open_count = len(open_list)

            # quick-take on every open trade, every cycle (stop stays server-side)
            for t in open_list:
                upl = float(t.get("unrealizedPL", 0))
                if upl >= QUICK_TAKE:
                    j = api("PUT", f"/v3/accounts/{ACCOUNT}/trades/{t['id']}/close")
                    pl = j.get("orderFillTransaction", {}).get("pl", "?")
                    log(f"QUICK TAKE {t['instrument']} trade {t['id']} P&L {pl} (>= ${QUICK_TAKE})")
                    open_count -= 1

            if open_count < MAX_OPEN:
                # detect stop-out → rotate instrument
                closed = api("GET", f"/v3/accounts/{ACCOUNT}/trades",
                             params={"state": "CLOSED", "count": 1}).get("trades", [])
                if closed:
                    t = closed[0]
                    if float(t.get("realizedPL", 0)) < 0 and t["instrument"] == INSTRUMENTS[active]:
                        active = (active + 1) % len(INSTRUMENTS)
                        log(f"Stop-out on {t['instrument']} → rotating to {INSTRUMENTS[active]}")

                if not goal_locked and trades_today < MAX_TRADES:
                    # scan ALL instruments every cycle — active first, then the rest
                    held = {t["instrument"] for t in open_list}
                    order = [active] + [i for i in range(len(INSTRUMENTS)) if i != active]
                    for i in order:
                        inst = INSTRUMENTS[i]
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
                        if sig != 0 and place_bracketed(inst, sig, series[-1]):
                            active = i
                            trades_today += 1
                            break
            else:
                log(f"Holding {open_count} position(s) · day P&L {day_pl:+.2f} · balance ${balance:.2f}")
        except requests.RequestException as e:
            log(f"API error (will retry): {e}")

        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
