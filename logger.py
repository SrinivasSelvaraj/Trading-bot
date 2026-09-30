"""Persistence: every round goes into SQLite (source of truth) and, once settled, trades.csv.

The `orders` table has the round slug as PRIMARY KEY. Recording a fill inserts into it,
so the database itself refuses a second order for the same round, even across restarts.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROUND_COLUMNS = [
    "slug", "market_id", "question", "round_start", "round_end", "mode",
    "up_percentage", "down_percentage", "peak_up", "peak_down",
    "btc_price_start", "btc_price_signal",
    "decision", "fill_status", "reason", "signal_at", "seconds_left_at_signal",
    "stake_inr", "stake_usd", "entry_price", "shares", "fee_usd",
    "result", "profit_loss_usd", "profit_loss_inr", "settled_at", "updated_at",
    # live valuation of an open paper position (price the website shows, and best bid)
    "mark_price", "mark_bid", "mark_at",
]

CSV_COLUMNS = [
    "timestamp", "market_id", "slug", "round_start", "round_end", "btc_price",
    "up_percentage", "down_percentage", "peak_up", "peak_down",
    "decision", "fill_status", "reason", "seconds_left_at_signal",
    "stake", "stake_usd", "entry_price", "shares", "fee_usd",
    "result", "profit_loss", "profit_loss_usd", "mode",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS rounds (
    slug TEXT PRIMARY KEY,
    {", ".join(ROUND_COLUMNS[1:])}
);
CREATE TABLE IF NOT EXISTS orders (
    slug TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    stake_inr REAL NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class TradeStore:
    def __init__(self, db_path: Path, csv_path: Path):
        self.db_path = Path(db_path)
        self.csv_path = Path(csv_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        # Databases created by older versions lack newer columns; add them in place.
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(rounds)")}
        for col in ROUND_COLUMNS:
            if col not in have:
                self.conn.execute(f"ALTER TABLE rounds ADD COLUMN {col}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- rounds ----------------------------------------------------------
    def upsert_round(self, row: dict) -> None:
        row = {k: v for k, v in row.items() if k in ROUND_COLUMNS}
        row["updated_at"] = utc_iso()
        cols = list(row)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
        self.conn.execute(
            f"INSERT INTO rounds ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(slug) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )
        self.conn.commit()

    def get_round(self, slug: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM rounds WHERE slug = ?", (slug,))
        r = cur.fetchone()
        return dict(r) if r else None

    # --- orders / duplicate protection ------------------------------------
    def has_order(self, slug: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM orders WHERE slug = ?", (slug,))
        return cur.fetchone() is not None

    def record_order(self, slug: str, side: str, stake_inr: float, mode: str, round_fields: dict) -> bool:
        """Atomically claim the round and store the fill. False if the round already has one."""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO orders (slug, side, stake_inr, mode, created_at) VALUES (?, ?, ?, ?, ?)",
                    (slug, side, stake_inr, mode, utc_iso()),
                )
        except sqlite3.IntegrityError:
            return False
        self.upsert_round({"slug": slug, **round_fields})
        return True

    # --- settlement --------------------------------------------------------
    def unsettled_rounds(self, before_iso: str, limit: int = 20) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM rounds WHERE result IS NULL AND round_end <= ? ORDER BY round_end LIMIT ?",
            (before_iso, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def settle(self, slug: str, result: str, pnl_usd: float | None, pnl_inr: float | None) -> None:
        self.upsert_round({
            "slug": slug, "result": result, "profit_loss_usd": pnl_usd,
            "profit_loss_inr": pnl_inr, "settled_at": utc_iso(),
        })
        row = self.get_round(slug)
        if row:
            self._append_csv(row)

    # --- risk queries -------------------------------------------------------
    def realized_pnl_inr(self, start_iso: str, end_iso: str) -> float:
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(profit_loss_inr), 0) FROM rounds "
            "WHERE profit_loss_inr IS NOT NULL AND round_start >= ? AND round_start < ?",
            (start_iso, end_iso),
        )
        return float(cur.fetchone()[0])

    def total_realized_inr(self) -> float:
        cur = self.conn.execute("SELECT COALESCE(SUM(profit_loss_inr), 0) FROM rounds")
        return float(cur.fetchone()[0])

    def open_exposure_inr(self) -> float:
        """Money tied up in unsettled paper trades: stakes plus their taker fees, in ₹."""
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(stake_inr + COALESCE(fee_usd, 0) * stake_inr / stake_usd), 0) "
            "FROM rounds WHERE fill_status = 'FILLED' AND result IS NULL AND stake_usd > 0"
        )
        return float(cur.fetchone()[0])

    # --- csv -----------------------------------------------------------------
    def _append_csv(self, row: dict) -> None:
        new_file = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        out = {
            "timestamp": row.get("settled_at"),
            "market_id": row.get("market_id"),
            "slug": row.get("slug"),
            "round_start": row.get("round_start"),
            "round_end": row.get("round_end"),
            "btc_price": row.get("btc_price_signal") or row.get("btc_price_start"),
            "up_percentage": row.get("up_percentage"),
            "down_percentage": row.get("down_percentage"),
            "peak_up": row.get("peak_up"),
            "peak_down": row.get("peak_down"),
            "decision": row.get("decision"),
            "fill_status": row.get("fill_status"),
            "reason": row.get("reason"),
            "seconds_left_at_signal": row.get("seconds_left_at_signal"),
            "stake": row.get("stake_inr"),
            "stake_usd": row.get("stake_usd"),
            "entry_price": row.get("entry_price"),
            "shares": row.get("shares"),
            "fee_usd": row.get("fee_usd"),
            "result": row.get("result"),
            "profit_loss": row.get("profit_loss_inr"),
            "profit_loss_usd": row.get("profit_loss_usd"),
            "mode": row.get("mode"),
        }
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(out)


def setup_logging(log_path: Path, level: int = logging.INFO) -> logging.Logger:
    log = logging.getLogger("bot")
    if log.handlers:
        return log
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log
