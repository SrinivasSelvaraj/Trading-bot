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
pip install -r requirements.txt
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
| `INR_PER_USD` | `96` | conversion for stake and P/L; update occasionally |
| `MAX_ENTRY_PRICE` | `0.99` | never pay more than this per share |
| `TAKER_FEE_RATE` | `0.07` | Polymarket crypto taker fee rate |
| `POLL_SECONDS` | `2` | how often prices are checked |
| `NO_TRADE_LAST_SECONDS` | `3` | no entries in the final seconds of a round |
| `TIMEZONE` | `Asia/Kolkata` | day boundary for the loss limit |
| `BROWSER_ENABLED` | `False` | open the current round in a browser window |
| `BROWSER_CROSSCHECK` | `False` | require page values to match the API before trading |

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

## Tests

```bash
python -m pytest
```

The tests are offline and cover the rule table, market verification, fills and fees, duplicate protection, the daily loss limit, emergency stop, CONFLICT, stale data and restart safety.
