"""System check: finds the usual reasons orders fail or numbers look wrong.

    python health.py              # all checks, including a live call to Polymarket
    python health.py --offline    # skip the network check

Also runs from the dashboard's "System check" button. Each check reports ok, warn or fail
with a plain explanation of what to do. Exit code 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import Config, load_config
from logger import utc_iso
from market_detector import MarketDetector, MarketVerificationError
from paper_account import signed_inr, wallet_summary
from polymarket_api import ApiError, PolymarketClient
from quotes import parse_book
from risk_manager import BOT, MANUAL, day_bounds_utc

OK, WARN, FAIL = "ok", "warn", "fail"
HEARTBEAT_MAX_AGE = 15
SETTLE_GRACE_SECONDS = 15 * 60


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def run_checks(cfg: Config, live: bool = True, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    now_iso = utc_iso(datetime.fromtimestamp(now, tz=timezone.utc))
    out: list[dict] = []

    out.append(_check("Mode", OK if cfg.paper_mode else FAIL,
                      "Paper trading: no real orders are ever placed." if cfg.paper_mode
                      else "PAPER_MODE is off. This version refuses to run live."))

    # --- bot heartbeat -----------------------------------------------------------------
    status = None
    try:
        status = json.loads(cfg.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if cfg.stop_file.exists():
        out.append(_check("Emergency stop", FAIL, f"{cfg.stop_file.name} exists, so the bot won't trade. "
                                                  "Delete that file and start the bot again."))
    if status is None:
        out.append(_check("Bot running", FAIL, "No heartbeat from the bot, so orders can't be placed. Start it "
                                                "with `python bot.py`, or restart the app (Heroku: More → Restart "
                                                "all dynos)."))
    else:
        age = now - float(status.get("updated_at", 0))
        out.append(_check("Bot running", OK if age <= HEARTBEAT_MAX_AGE else FAIL,
                          f"Last heartbeat {age:.0f}s ago." if age <= HEARTBEAT_MAX_AGE else
                          f"Last heartbeat {age:.0f}s ago: the bot has stopped. Restart the app (Heroku: More → "
                          "Restart all dynos; on your computer: stop and start bot.py). The Restart bot button "
                          "only works while the bot is still responding."))
        rnd = status.get("round") or {}
        if status.get("warning"):
            out.append(_check("Market data", WARN, f"The bot reports: {status['warning']}"))
        elif not rnd:
            out.append(_check("Market data", WARN, "The bot has no live round right now (between rounds or "
                                                   "Polymarket hasn't listed the next one yet)."))
        elif not rnd.get("quote"):
            out.append(_check("Market data", WARN, "No usable order book this moment; manual orders will wait."))
        else:
            out.append(_check("Market data", OK, f"Live round {rnd.get('slug')} with fresh order books."))
        s = status.get("bot_settings") or {}
        if s and not s.get("enabled", True):
            out.append(_check("Auto-trader", WARN, "Paused from the dashboard. Press Resume to let it trade."))

    # --- database ----------------------------------------------------------------------
    db = Path(cfg.db_path)
    if not db.exists():
        out.append(_check("Database", WARN, "No trades.db yet; it's created when the bot first runs."))
    else:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            out.append(_check("Database", OK if integrity == "ok" else FAIL,
                              "trades.db is readable and intact." if integrity == "ok"
                              else f"trades.db is damaged ({integrity}). Reset the paper account."))
            positions = _rows(conn, "SELECT * FROM positions ORDER BY id")
            out.extend(_ledger_checks(conn, cfg, positions, now, now_iso))
        except sqlite3.OperationalError as exc:
            out.append(_check("Database", FAIL, f"trades.db can't be read ({exc}). Restart the bot so it can "
                                                "upgrade it, or reset the paper account."))
        finally:
            conn.close()

    # --- Polymarket ----------------------------------------------------------------------
    if live:
        out.append(_live_market_check())
    return out


def _ledger_checks(conn, cfg: Config, positions: list[dict], now: float, now_iso: str) -> list[dict]:
    out = []
    stuck = _rows(conn, "SELECT id, kind FROM commands WHERE status = 'PENDING' AND created_at < ?",
                  (now - cfg.command_max_age_seconds,))
    out.append(_check("Pending orders", WARN if stuck else OK,
                      f"{len(stuck)} action(s) have waited over {cfg.command_max_age_seconds:.0f}s: the bot isn't "
                      "picking them up. Use Clear stuck orders, and check that the bot is running." if stuck
                      else "No actions are waiting on the bot."))

    late = [p for p in positions if p["status"] == "OPEN" and p["round_end"]
            and datetime.fromisoformat(p["round_end"]).timestamp() < now - SETTLE_GRACE_SECONDS]
    out.append(_check("Settlement", WARN if late else OK,
                      f"{len(late)} position(s) from rounds that ended over 15 minutes ago are still open "
                      f"(#{', #'.join(str(p['id']) for p in late)}). Polymarket may be slow to resolve; "
                      "if it persists, restart the bot." if late else "Every finished round has been paid out."))

    orders = {r["slug"] for r in _rows(conn, "SELECT slug FROM orders")}
    bot_slugs = [p["slug"] for p in positions if p["source"] == BOT]
    dupes = sorted({s for s in bot_slugs if bot_slugs.count(s) > 1})
    missing = sorted(orders - set(bot_slugs))
    extra = sorted(set(bot_slugs) - orders)
    problems = []
    if dupes:
        problems.append(f"more than one auto-trader trade in {', '.join(dupes)}")
    if missing:
        problems.append(f"orders without a position: {', '.join(missing)}")
    if extra:
        problems.append(f"positions without an order: {', '.join(extra)}")
    out.append(_check("Auto-trader records", FAIL if problems else OK,
                      "; ".join(problems) + ". Reset the paper account to start clean." if problems
                      else f"{len(orders)} auto-trader trade(s), one per round, all accounted for."))

    w = wallet_summary(positions, cfg.starting_balance_inr, now_iso)
    out.append(_check("Wallet", FAIL if w["cash_inr"] < 0 else OK,
                      f"Free cash is negative (₹{w['cash_inr']:,.2f}); the books don't balance. Reset the account."
                      if w["cash_inr"] < 0 else
                      f"Equity ₹{w['equity_inr']:,.2f}, free cash ₹{w['cash_inr']:,.2f}, "
                      f"{len(w['positions'])} open position(s)."))

    day_start, day_end = day_bounds_utc(cfg.timezone, now)
    for source, limit, label in ((BOT, cfg.max_daily_loss_inr, "Auto-trader daily loss"),
                                 (MANUAL, cfg.max_manual_daily_loss_inr, "Manual daily loss")):
        today = conn.execute(
            "SELECT COALESCE(SUM(pnl_inr), 0) FROM positions WHERE pnl_inr IS NOT NULL AND source = ? "
            "AND round_start >= ? AND round_start < ?", (source, day_start, day_end)).fetchone()[0]
        if not limit:
            out.append(_check(label, OK, f"{signed_inr(today)} today; no limit set."))
        elif today <= -limit:
            out.append(_check(label, WARN, f"Limit reached ({signed_inr(today)} of −₹{limit:,.0f}), so these orders are "
                                           f"refused until midnight ({cfg.timezone}) or a reset."))
        else:
            out.append(_check(label, OK, f"{signed_inr(today)} today (limit −₹{limit:,.0f})."))
    return out


def _live_market_check() -> dict:
    client = PolymarketClient(timeout=8)
    try:
        rnd = MarketDetector(client).current_round(time.time())
        book = parse_book(client.get_book(rnd.up_token))
    except (ApiError, MarketVerificationError, KeyError, ValueError) as exc:
        return _check("Polymarket", FAIL, f"Couldn't read the live BTC 5-minute market: {exc}")
    return _check("Polymarket", OK, f"Reached {rnd.slug}; UP book has {len(book.bids)} bid and "
                                    f"{len(book.asks)} ask levels.")


def fetch_remote(url: str) -> list[dict]:
    """Run the check on a deployed dashboard (e.g. the Heroku app) through its /api/health."""
    import requests

    try:
        resp = requests.get(url.rstrip("/") + "/api/health", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        return [_check("Deployed app", FAIL, f"{url} did not answer the health check: {exc}")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the bot, its data and Polymarket access")
    parser.add_argument("--offline", action="store_true", help="skip the live Polymarket check")
    parser.add_argument("--url", help="check a deployed dashboard instead, e.g. https://my-app.herokuapp.com")
    args = parser.parse_args()
    results = fetch_remote(args.url) if args.url else run_checks(load_config(), live=not args.offline)
    marks = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    for r in results:
        print(f"[{marks[r['status']]}] {r['name']}: {r['detail']}")
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
