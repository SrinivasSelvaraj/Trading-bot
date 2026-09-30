"""Web dashboard for the paper bot: live view plus paper-trading actions.

    python dashboard.py                 # http://127.0.0.1:8000
    python dashboard.py --port 8080 --host 0.0.0.0

Reads: the bot's status.json heartbeat and trades.db.
Actions (buy, sell, edit take profit / stop loss, auto-trader settings) are never
executed here. They are queued in the database and the bot runs them on its next poll,
against the live order books, with the same risk checks as its own trades. If
DASHBOARD_KEY is set, every action must carry that key.
"""
from __future__ import annotations

import argparse
import hmac
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import BASE_DIR, Config, load_config
from logger import TradeStore, utc_iso
from paper_account import SIDES, wallet_summary
from report import load_rounds, summarize
from strategy import THRESHOLD_PCT

INDEX_HTML = BASE_DIR / "web" / "index.html"
RECENT_ROUNDS = 60
RECENT_TRADES = 40
RECENT_COMMANDS = 15
MAX_BODY_BYTES = 2048
ROUND_COLUMNS = (
    "slug", "question", "round_start", "round_end", "up_percentage", "down_percentage",
    "peak_up", "peak_down", "decision", "fill_status", "reason", "seconds_left_at_signal",
    "entry_price", "result", "profit_loss_inr",
)
# What each action must carry. Prices are in cents (1-99) as typed in the page, blank = none.
COMMAND_FIELDS = {
    "BUY": {"side", "amount_inr", "take_profit", "stop_loss"},
    "SELL": {"position_id"},
    "SET_EXITS": {"position_id", "take_profit", "stop_loss"},
    "BOT_SETTINGS": {"enabled", "take_profit", "stop_loss"},
}


def _query(db_path: Path, sql: str, args: tuple = ()) -> list[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:  # table not created yet by an older/new bot
        return []
    finally:
        conn.close()


def read_status(cfg: Config, now: float) -> dict:
    try:
        status = json.loads(cfg.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
    if cfg.stop_file.exists():
        state = "stopped"
    elif status is None:
        state = "offline"
    else:
        age = now - float(status.get("updated_at", 0))
        state = "running" if age <= max(15.0, 5 * float(status.get("poll_seconds", 2))) else "offline"
    return {"state": state, "status": status}


def build_state(cfg: Config) -> dict:
    now = time.time()
    now_iso = utc_iso(datetime.fromtimestamp(now, tz=timezone.utc))
    rows = load_rounds(cfg.db_path)
    positions = _query(cfg.db_path, "SELECT * FROM positions ORDER BY id")
    closed = sorted((p for p in positions if p["pnl_inr"] is not None), key=lambda p: (p["closed_at"] or "", p["id"]))

    series, total = [], 0.0
    for p in closed:
        total += p["pnl_inr"]
        series.append({
            "t": p["closed_at"], "id": p["id"], "source": p["source"], "side": p["side"],
            "reason": p["close_reason"], "result": p["result"], "pnl": p["pnl_inr"],
            "equity": round(cfg.starting_balance_inr + total, 2),
        })
    history = [
        {k: p[k] for k in ("id", "source", "slug", "question", "side", "opened_at", "stake_inr", "shares",
                           "entry_price", "exit_price", "close_reason", "closed_at", "result", "pnl_inr")}
        for p in reversed(closed[-RECENT_TRADES:])
    ]
    commands = _query(cfg.db_path, "SELECT id, created_at, kind, status, message, processed_at FROM commands "
                                   "ORDER BY id DESC LIMIT ?", (RECENT_COMMANDS,))
    return {
        "now": now,
        "bot": read_status(cfg, now),
        "wallet": wallet_summary(positions, cfg.starting_balance_inr, now_iso),
        "rules": {
            "threshold_pct": THRESHOLD_PCT,
            "stake_inr": cfg.stake_inr,
            "max_daily_loss_inr": cfg.max_daily_loss_inr,
            "max_entry_price": cfg.max_entry_price,
            "max_manual_order_inr": cfg.max_manual_order_inr,
            "inr_per_usd": cfg.inr_per_usd,
            "taker_fee_rate": cfg.taker_fee_rate,
            "paper_mode": cfg.paper_mode,
            "timezone": cfg.timezone,
        },
        "actions": {"key_required": bool(cfg.dashboard_key)},
        "summary": summarize(rows),
        "pnl_series": series,
        "history": history,
        "commands": commands,
        "recent": [{k: r.get(k) for k in ROUND_COLUMNS} for r in reversed(rows[-RECENT_ROUNDS:])],
    }


def submit_command(cfg: Config, body: bytes, key: str) -> tuple[int, dict]:
    """Validate a dashboard action and queue it for the bot. Returns (HTTP status, JSON body)."""
    if cfg.dashboard_key and not hmac.compare_digest(key.encode(), cfg.dashboard_key.encode()):
        return 401, {"error": "Wrong or missing dashboard key"}
    if len(body) > MAX_BODY_BYTES:
        return 413, {"error": "Request too large"}
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "Request is not valid JSON"}
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind not in COMMAND_FIELDS:
        return 400, {"error": "Unknown action"}
    payload = {k: data.get(k) for k in COMMAND_FIELDS[kind]}
    for k, v in payload.items():
        if v is not None and not isinstance(v, (str, int, float, bool)):
            return 400, {"error": f"Bad value for {k}"}
    if kind == "BUY" and payload["side"] not in SIDES:
        return 400, {"error": "Side must be UP or DOWN"}
    store = TradeStore(cfg.db_path, cfg.csv_path)
    try:
        cmd_id = store.enqueue_command(kind, payload, time.time())
    finally:
        store.close()
    return 202, {"id": cmd_id, "status": "PENDING"}


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperBotDashboard/2.0"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(HTTPStatus.OK, json.dumps(build_state(cfg)).encode("utf-8"), "application/json")
            elif path == "/healthz":
                self._send(HTTPStatus.OK, b"ok", "text/plain")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/command":
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY_BYTES + 1))
            code, out = submit_command(cfg, body, self.headers.get("X-Dashboard-Key", ""))
            self._send(code, json.dumps(out).encode("utf-8"), "application/json")

        def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
            pass

    return Handler


def render_snapshot(cfg: Config) -> str:
    """The dashboard page with the current data built in, so it works as a single offline file."""
    data = json.dumps(build_state(cfg)).replace("</", "<\\/")  # keep the JSON from closing the <script>
    html = INDEX_HTML.read_text(encoding="utf-8")
    marker = "<script>\n(() => {"
    if marker not in html:
        raise RuntimeError("web/index.html layout changed; cannot embed snapshot data")
    return html.replace(marker, f"<script>window.__SNAPSHOT__ = {data};</script>\n{marker}", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard for the paper bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--snapshot", metavar="FILE", help="save a static copy of the dashboard and exit")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.snapshot:
        Path(args.snapshot).write_text(render_snapshot(cfg), encoding="utf-8")
        print(f"Snapshot written to {args.snapshot}")
        return 0
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cfg))
    print(f"Dashboard on http://{args.host}:{args.port}  (reading {Path(cfg.db_path)})")
    if args.host not in ("127.0.0.1", "localhost") and not cfg.dashboard_key:
        print("Warning: the dashboard is reachable from other machines and DASHBOARD_KEY is not set,")
        print("         so anyone who can open it can place paper trades.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
