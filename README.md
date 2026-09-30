# Polymarket BTC 5-Minute Bot (98% rule, paper trading)

Watches every Polymarket **"Bitcoin Up or Down" 5-minute** round, 24/7, and applies one fixed rule:

| Condition | Action |
|---|---|
| UP ≥ 98% | trade UP, ₹1,100 |
| DOWN ≥ 98% | trade DOWN, ₹1,100 |
| anything else (60%, 70%, 85%, …) | no trade |
| both ≥ 98% (a data problem) | no trade, flagged as CONFLICT |

**This version is paper trading only.** It never places real orders. If you set `PAPER_MODE=False`, the bot refuses to start. Its job is to answer one question with real data: *when one side reaches 98% or more, what happens next?*

---

## Quick start (laptop)

Requires Python 3.10+.

```bash
git clone <this repo> && cd Trading-bot
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt   # or requirements.txt for just the bot (no tests/browser)
cp .env.example .env          # Windows: copy .env.example .env   (optional; defaults are fine)

python bot.py --check         # one-shot: current round, UP/DOWN %, what the rule says
python bot.py                 # run continuously (Ctrl+C to stop)
python report.py              # results so far
```

Emergency stop from another terminal:

```bash
python bot.py --stop          # creates a STOP file; the bot stops within one poll (~2s)
```

The bot will not start again until you delete the `STOP` file.

---

## How it works

```
every 2 seconds:
  find the current round  -> market slug btc-updown-5m-<start unix time>, verified (see below)
  read UP and DOWN order books from Polymarket's public CLOB API
  displayed % = midpoint of bid/ask (or last trade if spread > 10¢), the number the website shows
  rule: UP >= 98 -> UP | DOWN >= 98 -> DOWN | else NO_TRADE
  if UP/DOWN and every safety check passes -> paper-buy ₹1,100 at the real ask prices
when the round ends: record it
when Polymarket resolves it: record the result and P/L, append a row to trades.csv
```

| File | Purpose |
|---|---|
| `bot.py` | main loop, CLI |
| `strategy.py` | the 98% rule (the threshold is a constant, not a setting) |
| `market_detector.py` | finds and verifies the current BTC 5m round; reads results |
| `quotes.py` | displayed %, realistic paper fills, Polymarket taker fee |
| `risk_manager.py` | max stake, daily loss limit, duplicate protection, emergency stop |
| `logger.py` | SQLite (`trades.db`) + `trades.csv` + `bot.log` |
| `polymarket_api.py` | read-only client for Polymarket's public APIs |
| `browser.py` | optional read-only browser window (Playwright) |
| `report.py` | summary of results |
| `dashboard.py` + `web/index.html` | web dashboard and paper-trading desk |
| `paper_account.py` | paper wallet: positions, manual orders, take profit / stop loss, settlement |
| `app.py`, `Procfile`, `deploy_heroku.sh` | hosted mode: bot + dashboard in one process (Heroku) |
| `wsgi.py`, `deploy_pythonanywhere.py` | dashboard as a WSGI app + PythonAnywhere deploy (PythonAnywhere) |
| `config.py` / `.env` | settings |

### Checks before any (paper) trade

A trade is only recorded if all of these hold. Otherwise the bot waits.

1. **Correct market.** The slug matches the current 5-minute window. The question is "Bitcoin Up or Down". Outcomes are exactly `["Up", "Down"]`. The start and end times match and the round is exactly 5 minutes long.
2. **Correct round.** The current time is inside the round. The market is accepting orders. We are not in the last 3 seconds.
3. **Correct side.** The side comes from the rule. Both sides ≥ 98% gives CONFLICT, and nothing is traded.
4. **At least 98%.** Both prices are present and consistent (UP% + DOWN% ≈ 100), and the order books are fresh (under 15 seconds old).
5. **Correct stake.** A positive amount no higher than `MAX_STAKE_INR`. A config with a stake above the maximum is rejected at startup.
6. **Not already traded.** The round is not in memory or in the database. The `orders` table uses the round as its primary key, so a second order for the same round is impossible, even after a restart.
7. **Daily loss limit.** The trade is refused if losing it could push today's loss past `MAX_DAILY_LOSS_INR`. "Today" is in `TIMEZONE` (default Asia/Kolkata). With the defaults (₹1,100 stake, ₹5,000 limit), trading stops for the day after 4 losses.
8. **No emergency stop.** There is no `STOP` file.

If a round can't be identified, or the API fails or returns anything unexpected, the bot does not trade that round. It never guesses.

### Paper fills are realistic, not optimistic

Being *displayed* at 98% does not mean you can buy at 98¢. The bot fills the paper order against the **real ask side of the order book**, including matching against bids on the opposite side, as the exchange does. It never pays above `MAX_ENTRY_PRICE` (99¢) and charges Polymarket's crypto taker fee: `shares × 0.07 × p × (1 − p)`. If ₹1,100 can't be filled completely, the round is recorded as `NO_FILL` with the reason (for example "no sellers"). The bot keeps retrying until the round ends. Near 99%, empty ask books are common, and `report.py` shows how often this happens.

### What 98% means for money (read this before going live)

Polymarket settles in USDC, so ₹1,100 ≈ $11.46 at `INR_PER_USD=96`.

| Entry price | Win pays (after fee) | Loss costs | Wins needed to cover one loss | Break-even win rate |
|---|---|---|---|---|
| 98¢ | ≈ +₹21 | ≈ −₹1,101 | ~52 | ~98.1% |
| 99¢ | ≈ +₹10 | ≈ −₹1,101 | ~107 | ~99.1% |

The displayed 98% is the market's price, not a guarantee. The rule only makes money if, *after* fees and real fill prices, the chosen side wins more often than the price already implies. Only a long paper record can tell you that. `report.py` prints the actual win rate next to the break-even rate.

---

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `PAPER_MODE` | `True` | must stay True; live execution isn't implemented |
| `STAKE_INR` | `1100` | fixed amount per traded round (no martingale, no increases) |
| `MAX_STAKE_INR` | `1100` | hard ceiling |
| `MAX_DAILY_LOSS_INR` | `5000` | stop trading for the day when reached |
| `STARTING_BALANCE_INR` | `100000` | paper wallet; a trade needs this much free balance |
| `MAX_MANUAL_ORDER_INR` | `100000` | largest single order from the dashboard |
| `MAX_MANUAL_DAILY_LOSS_INR` | `50000` | manual trading stops for the day at this loss (0 = no limit) |
| `DASHBOARD_KEY` | *(empty)* | if set, dashboard buttons need this key; set it for any shared URL |
| `INR_PER_USD` | `96` | conversion for stake and P/L; update occasionally |
| `MAX_ENTRY_PRICE` | `0.99` | never pay more than this per share |
| `TAKER_FEE_RATE` | `0.07` | Polymarket crypto taker fee rate |
| `POLL_SECONDS` | `2` | how often prices are checked |
| `NO_TRADE_LAST_SECONDS` | `3` | no entries in the final seconds of a round |
| `TIMEZONE` | `Asia/Kolkata` | day boundary for the loss limit |
| `BROWSER_ENABLED` | `False` | open the current round in a browser window |
| `BROWSER_CROSSCHECK` | `False` | require page values to match the API before trading |

## Web dashboard and paper trading

```bash
python bot.py                      # terminal 1: the bot
python dashboard.py                # terminal 2: open http://127.0.0.1:8000
```

The dashboard is a paper-trading desk on top of the live Polymarket BTC 5-minute market, with a **₹1,00,000 paper wallet**:

- **Paper wallet** at the top: equity valued at live prices, free cash, realized P/L, live P/L on open trades.
- **Paper trade** ticket: Buy UP or Buy DOWN any amount (up to `MAX_MANUAL_ORDER_INR`), with an optional **take profit** and **stop loss** in cents.
- **Auto-trader** panel: pause or resume the 98% rule bot, and set a take profit / stop loss for its trades. Its stake stays fixed at ₹1,100.
- **Open positions**: every bot and manual trade, marked to the live price every 2 seconds, with **Sell now** and **Exits** (edit take profit / stop loss).
- **Trade history**, an equity chart, and the rounds table showing what the 98% rule saw.

How the simulation matches a real account:
- Buys fill against the real ask side of the order book (plus matching bids on the other side, as Polymarket's exchange does), never above 99¢, and pay Polymarket's taker fee.
- Sells (Sell now, take profit, stop loss) fill against the real bids and pay the taker fee.
- **Take profit** fires when the best bid reaches your price. **Stop loss** fires when the displayed price falls to your price and then sells at the bids, so it can fill below the stop in a fast market, like a real stop order.
- Anything still open when a round ends is paid out at resolution: ₹ equivalent of 1 USDC per share if its side won, 0 if it lost.
- A trade needs free balance; the daily loss limit and emergency stop apply to manual orders too.

**Maintenance** (bottom of the page):
- **System check** checks the bot's heartbeat, market data, stuck actions, unpaid rounds, the auto-trader's records, the wallet, both daily loss limits and Polymarket access, and says what to do about anything wrong. Same check from a terminal: `python health.py` (or `python health.py --url https://your-app.herokuapp.com` for a deployed app).
- **Clear stuck orders** cancels actions the bot hasn't picked up. **Restart bot** restarts the trading loop (needs the bot to be responding; otherwise restart the app).
- **Reset paper account** (type RESET) wipes all trades, rounds and settings and starts again with the starting balance; the old trades.csv is kept under a new name.

If an order won't go through, the Paper trade box says why (bot offline, round closing, no order book, over the limit, not enough cash for amount + fee, exit price that would fire immediately). The auto-trader and manual trading have separate daily loss limits (`MAX_DAILY_LOSS_INR`, `MAX_MANUAL_DAILY_LOSS_INR`), so a big manual trade never blocks the auto-trader.

Buttons never execute in the browser or the web server. They queue a command that the bot runs on its next poll (within ~2 s) against the live book, with the same checks as its own trades; commands older than 30 s are refused rather than run late. If `DASHBOARD_KEY` is set, the page asks for it before the first action (and remembers it in that browser).

To show it to someone else temporarily, run a tunnel next to it on your laptop, for example:

```bash
cloudflared tunnel --url http://127.0.0.1:8000      # prints a https://…trycloudflare.com link
```

Stop the tunnel (Ctrl+C) and the link dies. To share a frozen copy instead, save one file with the data built in:

```bash
python dashboard.py --snapshot dashboard-snapshot.html
```

## Deploy to Heroku

`app.py` runs the bot in a background thread and serves the dashboard on `$PORT`, in one web dyno so both share the same files.

```bash
export HEROKU_API_KEY=...                # heroku authorizations:create, or Account Settings → API Key
./deploy_heroku.sh my-btc-paper-bot      # creates the app if needed, pushes, starts 1 web dyno
```

Or with the Heroku CLI: `heroku create my-btc-paper-bot && git push heroku HEAD:main && heroku ps:scale web=1`.

Limits to know:
- **Data resets on every restart.** Heroku's filesystem is temporary, and dynos restart at least once a day and on every deploy, so `trades.db` starts empty each time. Use Heroku for a demo; keep the long paper record on your own machine (or add a Heroku Postgres database).
- **Eco dynos sleep** after 30 minutes without web traffic, which pauses the bot. Use a Basic dyno for 24/7 monitoring.
- The app only reads Polymarket's public data and stays in paper mode. Set `DASHBOARD_KEY` (the deploy script does) so only you can use the trading buttons.

## Deploy to PythonAnywhere

On PythonAnywhere the dashboard runs as the web app (`wsgi.py`) and the bot runs as an always-on task (`bot.py`). They share the same folder, and PythonAnywhere keeps files, so the paper record **survives restarts**. The sites the bot needs (`gamma-api.polymarket.com`, `clob.polymarket.com`, `api.coinbase.com`) are on the free-plan allowlist. No extra packages are needed: `requests` is preinstalled and the timezone data comes from the system.

```bash
export PYTHONANYWHERE_USERNAME=yourname
export PYTHONANYWHERE_API_TOKEN=...                 # Account → API token
export PYTHONANYWHERE_HOST=www.pythonanywhere.com   # eu.pythonanywhere.com for EU accounts
python deploy_pythonanywhere.py --dry-run           # see the steps
python deploy_pythonanywhere.py                     # upload, create/reload web app, start the bot task
```

The script uploads the files committed to git (not your `.env` or data files), sets up `https://<yourname>.pythonanywhere.com/`, and creates the always-on task. Re-run it to deploy new code, then restart the task from the Tasks tab.

- **Free plan:** the dashboard works, but always-on tasks need a paid plan (Hacker). On free you can start `python3.11 ~/Trading-bot/bot.py` from a Bash console, but consoles don't run 24/7.
- **Emergency stop:** open a Bash console and run `cd ~/Trading-bot && python3.11 bot.py --stop`, or disable the task in the Tasks tab.

## Optional browser window

```bash
python -m playwright install chromium    # once
python bot.py --browser-check            # opens the live round and prints the UP/DOWN it reads
```

Then set `BROWSER_ENABLED=True` in `.env`. The bot opens each new round's page so you can watch along. The browser is **read-only**: it never clicks Buy, types an amount or submits anything. The page is read by its visible labels ("Up 98¢", "Down 2¢"), not by screen coordinates.

The data the bot acts on comes from Polymarket's own API, which is the same source the website displays. If you also turn on `BROWSER_CROSSCHECK=True`, a round is skipped whenever the page can't be read or disagrees with the API by more than 2 points. Run `--browser-check` on your own machine first. The page labels have not been verified from a region where the trading panel is shown.

## Output

`trades.csv` gets one row per round once it is resolved, including no-trade rounds:
`timestamp, market_id, slug, round_start, round_end, btc_price, up_percentage, down_percentage, peak_up, peak_down, decision, fill_status, reason, seconds_left_at_signal, stake, stake_usd, entry_price, shares, fee_usd, result, profit_loss, profit_loss_usd, mode`.

- `decision`: `UP` / `DOWN` (98% reached), `NO_TRADE`, or `CONFLICT`
- `fill_status`: `FILLED`, `NO_FILL` (no sellers at ≤ 99¢), `BLOCKED` (a safety check said no; see `reason`), or `NONE`
- `btc_price` is Coinbase spot, for reference only. Polymarket resolves on the Chainlink BTC/USD stream.

## Before any real money

- Run in paper mode for a meaningful sample (weeks, not hours) and review `report.py`.
- Check that Polymarket is available to you. Trading is geo-restricted in some countries, and you must follow Polymarket's terms and your local laws.
- Live execution would need Polymarket's order API (`py-clob-client`), a funded wallet and API keys. It should reuse exactly the same checks above. It is deliberately not part of this version.

## Tests and routine checks

```bash
python -m pytest          # offline test suite
python health.py          # system check of a running setup
```

GitHub Actions (`.github/workflows/checks.yml`) runs the test suite on every push, and every day at 08:00 IST also reads the live Polymarket market (`python bot.py --check`) and runs the system check of the deployed app. For the last one, set the repository variable `APP_URL` (Settings → Secrets and variables → Actions → Variables) to the app's address.

The tests are offline and cover the rule table, market verification, fills and fees, duplicate protection, the daily loss limit, emergency stop, CONFLICT, stale data and restart safety.
