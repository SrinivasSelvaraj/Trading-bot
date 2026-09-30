"""Read-only web dashboard for the paper bot.

    python dashboard.py                 # http://127.0.0.1:8000
    python dashboard.py --port 8080 --host 0.0.0.0

Shows the live round (from the bot's status.json heartbeat), results and recent
rounds (from trades.db). It has no buttons and no write endpoints: nothing on
the page can start, stop or change the bot, so it is safe to share temporarily.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import BASE_DIR, Config, load_config
from report import load_rounds, summarize
from strategy import THRESHOLD_PCT

INDEX_HTML = BASE_DIR / "web" / "index.html"
RECENT_ROUNDS = 60
ROUND_COLUMNS = (
    "slug", "question", "round_start", "round_end", "up_percentage", "down_percentage",
    "peak_up", "peak_down", "decision", "fill_status", "reason", "seconds_left_at_signal",
    "entry_price", "result", "profit_loss_inr",
)


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
    rows = load_rounds(cfg.db_path)
    pnl_series, total = [], 0.0
    for r in rows:
        if r["fill_status"] == "FILLED" and r["profit_loss_inr"] is not None:
            total += r["profit_loss_inr"]
            pnl_series.append({
                "t": r["round_end"], "slug": r["slug"], "side": r["decision"], "result": r["result"],
                "pnl": r["profit_loss_inr"], "cum": round(total, 2),
            })
    recent = [{k: r.get(k) for k in ROUND_COLUMNS} for r in reversed(rows[-RECENT_ROUNDS:])]
    return {
        "now": now,
        "bot": read_status(cfg, now),
        "rules": {
            "threshold_pct": THRESHOLD_PCT,
            "stake_inr": cfg.stake_inr,
            "max_daily_loss_inr": cfg.max_daily_loss_inr,
            "max_entry_price": cfg.max_entry_price,
            "paper_mode": cfg.paper_mode,
            "timezone": cfg.timezone,
        },
        "summary": summarize(rows),
        "pnl_series": pnl_series,
        "recent": recent,
    }


def make_handler(cfg: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperBotDashboard/1.0"

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
                body = json.dumps(build_state(cfg)).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
            elif path == "/healthz":
                self._send(HTTPStatus.OK, b"ok", "text/plain")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        do_HEAD = do_GET

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
    parser = argparse.ArgumentParser(description="Read-only dashboard for the paper bot")
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
