"""Hosted entry point (Heroku and similar): the paper bot and the dashboard in one process.

    PORT=8000 python app.py

The bot runs in a background thread; the read-only dashboard serves on $PORT.
They share status.json and trades.db, which is why they live in one process
(dynos don't share a filesystem). If the bot loop crashes it is restarted after
a pause; if it stops because of the emergency STOP file, it stays stopped and
the dashboard says so.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from bot import Bot
from config import Config, load_config
from dashboard import make_handler
from logger import TradeStore, setup_logging
from market_detector import MarketDetector
from polymarket_api import PolymarketClient
from risk_manager import RiskManager

log = logging.getLogger("bot")
RESTART_DELAY_SECONDS = 30


def run_bot_forever(cfg: Config) -> None:
    while True:
        store = TradeStore(cfg.db_path, cfg.csv_path)  # sqlite connections belong to this thread
        try:
            client = PolymarketClient()
            code = Bot(cfg, client, MarketDetector(client), store, RiskManager(cfg, store)).run()
        except Exception:  # noqa: BLE001 - keep the dashboard up and retry
            log.exception("Bot loop crashed; restarting in %ss", RESTART_DELAY_SECONDS)
            code = None
        finally:
            store.close()
        if code is not None and (cfg.stop_file.exists() or code == 2):
            log.warning("Bot stopped (exit code %s). Dashboard stays up.", code)
            return
        time.sleep(RESTART_DELAY_SECONDS)


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.log_path)
    threading.Thread(target=run_bot_forever, args=(cfg,), name="paper-bot", daemon=True).start()

    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg))
    log.info("Dashboard listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
