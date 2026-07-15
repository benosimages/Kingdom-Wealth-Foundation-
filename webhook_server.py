"""Optional TradingView → OANDA webhook bridge.

Deploy on any always-on host (Render/Fly/Railway). Env vars:
  OANDA_TOKEN, OANDA_ACCOUNT, OANDA_ENV (practice|live), WEBHOOK_SECRET

TradingView alert message (Notifications → Webhook URL → https://<host>/webhook):
  {"secret":"YOUR_WEBHOOK_SECRET","instrument":"SPX500_USD",
   "action":"{{strategy.order.action}}","bracket_pct":1.5}
"""
import os, json
import requests
from flask import Flask, request, jsonify

TOKEN = os.environ["OANDA_TOKEN"]
ACCOUNT = os.environ["OANDA_ACCOUNT"]
ENV = os.environ.get("OANDA_ENV", "practice")
SECRET = os.environ["WEBHOOK_SECRET"]
MAX_RISK = float(os.environ.get("MAX_RISK_USD", "30"))
BASE = "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
ALLOWED = {"SPX500_USD", "NAS100_USD", "XAU_USD"}

app = Flask(__name__)


def mid_price(inst):
    r = requests.get(f"{BASE}/v3/accounts/{ACCOUNT}/pricing",
                     headers=H, params={"instruments": inst}, timeout=10)
    r.raise_for_status()
    p = r.json()["prices"][0]
    return (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2


@app.post("/webhook")
def webhook():
    d = request.get_json(force=True, silent=True) or {}
    if d.get("secret") != SECRET:
        return jsonify(error="bad secret"), 403
    inst = d.get("instrument")
    if inst not in ALLOWED:
        return jsonify(error=f"instrument must be one of {sorted(ALLOWED)}"), 400
    action = str(d.get("action", "")).lower()
    direction = 1 if action == "buy" else -1 if action == "sell" else 0
    if direction == 0:
        return jsonify(error="action must be buy or sell"), 400

    bracket = float(d.get("bracket_pct", 1.5)) / 100.0
    price = mid_price(inst)
    units = int(MAX_RISK // (price * bracket))
    if units < 1:
        return jsonify(skipped=f"1 unit risks more than ${MAX_RISK}"), 200
    prec = 3 if inst == "XAU_USD" else 1
    tp = round(price * (1 + bracket * direction), prec)
    sl = round(price * (1 - bracket * direction), prec)
    body = {"order": {"type": "MARKET", "instrument": inst,
                      "units": str(units * direction), "timeInForce": "FOK",
                      "takeProfitOnFill": {"price": f"{tp:.{prec}f}"},
                      "stopLossOnFill": {"price": f"{sl:.{prec}f}"}}}
    r = requests.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders",
                      headers=H, data=json.dumps(body), timeout=15)
    return jsonify(status=r.status_code, oanda=r.json()), 200


@app.get("/")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
