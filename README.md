# AutoTrader Bot — Live Handoff

Automated trading bot for **S&P 500, Nasdaq 100, and Gold** via the **OANDA REST API**, with a
hard **±1.5% bracket on every trade**, **instrument rotation after a stop-out**, a **daily profit
goal lock-in**, and a **GitHub Actions scheduler** that runs only when the US market is open.

> ⚠️ **Start on the OANDA *practice* account** (`OANDA_ENV=practice`). Run it for at least a week
> and compare against the app's backtesting tab before switching to `live`. Trading CFDs with
> leverage can lose money quickly; no profit target is guaranteed.

## What's in here

```
bot/bot.py              # strategy engine + OANDA order placement + session loop
bot/report.py           # nightly quantstats tear sheet (Sharpe, drawdown, win rate)
bot/webhook_server.py   # optional: receives TradingView alerts → places bracketed orders
bot/requirements.txt    # includes pandas-ta (indicators) + quantstats (analytics) + alpaca-py (optional)
config.yml              # instruments, risk, goal, strategy — edit freely
.github/workflows/trading.yml   # runs the bot every market day, open → close, then uploads the tear sheet
```

### Open-source stack (wired in)
- **pandas-ta** — indicator backend; bot.py uses it automatically when installed.
- **quantstats** — after every session the workflow writes `reports/tearsheet.html` (download it from the Actions run's Artifacts).
- **alpaca-py** — in requirements for a free US-stock paper account (NVDA/TSLA/COIN legs OANDA can't trade); ask Claude Code to wire `ALPACA_KEY`/`ALPACA_SECRET` when ready.
- Watch `merovinh/best-of-algorithmic-trading` on GitHub for the weekly-ranked ecosystem list.
- For deeper research later: vectorbt (parameter sweeps), Lean/Nautilus (full engines) — reference material, not dependencies.

## Go-live checklist (do these in order)

### 1. Create the GitHub repo (2 min)
1. github.com → **New repository** → name it `trading-bot`, private.
2. On the empty repo page click **"uploading an existing file"** and drag the *contents* of this
   folder in (keep the folder structure — the `.github/workflows/` path must be preserved).
   Or with git: `git init && git add -A && git commit -m "bot" && git push`.

### 2. Add your OANDA secrets (2 min)
Repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
- `OANDA_TOKEN` — your API token (OANDA → Manage API Access → Generate)
- `OANDA_ACCOUNT` — your account ID, e.g. `101-001-1234567-001`
- `OANDA_ENV` — `practice` (switch to `live` only after a successful practice week)

Secrets are encrypted by GitHub and never appear in code or logs.

### 3. Enable the schedule (1 min)
Repo → **Actions** tab → enable workflows. The bot then starts automatically at **9:25 AM ET
every weekday** and shuts itself down at the 4:00 PM close. You can also trigger a run manually
(Actions → trading-bot → **Run workflow**) to test right now.

### 4. Optional: TradingView webhook signals
The bot trades on its own signals by default. To drive it from TradingView instead
(paid TradingView plan required for webhooks):
1. Deploy `bot/webhook_server.py` on any always-on host (Render/Fly/Railway free tiers work);
   set the same three secrets plus `WEBHOOK_SECRET` as environment variables.
2. In your TradingView alert → Notifications → **Webhook URL** → `https://<your-host>/webhook`.
3. Alert message JSON:
```json
{"secret":"YOUR_WEBHOOK_SECRET","instrument":"SPX500_USD",
 "action":"{{strategy.order.action}}","bracket_pct":1.5}
```

### 5. Agent runner ("ruflo") / Claude Code
- Open this repo in **Claude Code** and it can extend strategies, add instruments, or tune risk —
  point it at `config.yml` and `bot/bot.py`; this README is the spec.
- If your "ruflo" agent-runner connector is installed on your GitHub account, grant it access to
  this repo and it can operate/monitor the scheduled runs. (Couldn't verify a public connector by
  that name — wire-up is standard GitHub App access either way.)

## The rules the bot enforces (same as the simulator app)

- **Bracket:** every order is sent with `takeProfitOnFill` at +1.5% and `stopLossOnFill` at −1.5%.
  The stop lives **on OANDA's servers** — it triggers even if the bot crashes.
- **Rotation:** a stop-out switches to the next instrument (S&P → Nasdaq → Gold → S&P).
- **Goal lock-in:** once the day's realized P&L ≥ `daily_goal` (config, default $50), the bot stops
  opening new trades for the day.
- **Flat overnight:** any open position is closed at 3:55 PM ET.
- **Sizing honesty (important):** OANDA CFD units are whole numbers. With a $1,500 account,
  1 unit of SPX500 (~$6,300 notional) risks ~$95 at the 1.5% stop — more than 1.5% of the account.
  `max_risk_usd` in `config.yml` (default $30) makes the bot **skip** any trade whose minimum size
  risks more than that. In practice, on a $1,500 account it will mostly trade Gold. Raise
  `max_risk_usd` or the account balance to unlock the indices — that's your risk decision to make.

## Monitoring

Every run prints a full trade log (entries, exits, P&L, reasons) in the Actions run output, and the
account history is always visible in your OANDA dashboard. The design app in this project remains
your paper-trading twin: same strategies, same rules.
