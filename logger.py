"""Persistence: SQLite is the source of truth; settled rounds are also appended to trades.csv.

- `rounds`    one row per 5-minute round the bot watched (what the 98% rule saw and decided)
- `orders`    the bot's one-order-per-round guard: the round slug is the PRIMARY KEY, so the
              database itself refuses a second bot order for the same round, even across restarts
- `positions` every paper position (bot or manual) and its money: entry, exits, result, P/L
- `settings`  auto-trader settings changed from the dashboard
- `commands`  actions queued by the dashboard (buy, sell, edit exits); the bot runs them
"""
from __future__ import annotations

import csv
import json
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
]

POSITION_COLUMNS = [
    "source", "slug", "question", "round_start", "round_end", "side", "opened_at", "mode",
    "stake_inr", "stake_usd", "shares", "entry_price", "fee_usd",
    "take_profit", "stop_loss", "mark_price", "mark_bid", "mark_at",
    "status", "close_reason", "exit_price", "exit_value_usd", "exit_fee_usd", "closed_at",
    "result", "pnl_usd", "pnl_inr",
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
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(POSITION_COLUMNS)}
);
CREATE INDEX IF NOT EXISTS positions_by_status ON positions (status, slug);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    message TEXT,
    processed_at REAL
);
"""


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class TradeStore:
    def __init__(self, db_path: Path, csv_path: Path, migrate: bool = True):
        """migrate=False skips schema upgrades and backfill; for short-lived writers like the
        dashboard, so only the bot ever changes the schema."""
        self.db_path = Path(db_path)
        self.csv_path = Path(csv_path)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        if not migrate:
            return
        self.conn.executescript(_SCHEMA)
        # Databases created by older versions lack newer columns; add them in place.
        for table, columns in (("rounds", ROUND_COLUMNS), ("positions", POSITION_COLUMNS)):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in columns:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        self.conn.commit()
        self._backfill_positions()

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

    # --- money queries (all from positions) ------------------------------------
    def realized_pnl_inr(self, start_iso: str, end_iso: str, source: str | None = None) -> float:
        """Realized P/L of positions whose round started in [start, end), optionally BOT or MANUAL only."""
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_inr), 0) FROM positions "
            "WHERE pnl_inr IS NOT NULL AND round_start >= ? AND round_start < ? AND (? IS NULL OR source = ?)",
            (start_iso, end_iso, source, source),
        )
        return float(cur.fetchone()[0])

    def total_realized_inr(self) -> float:
        cur = self.conn.execute("SELECT COALESCE(SUM(pnl_inr), 0) FROM positions")
        return float(cur.fetchone()[0])

    def open_exposure_inr(self, source: str | None = None) -> float:
        """Money tied up in open positions (stakes plus buy fees, in ₹), optionally BOT or MANUAL only."""
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(stake_inr + fee_usd * stake_inr / stake_usd), 0) "
            "FROM positions WHERE status = 'OPEN' AND (? IS NULL OR source = ?)",
            (source, source),
        )
        return float(cur.fetchone()[0])

    def reset_all(self, keep_command_id: int | None = None) -> str | None:
        """Wipe every trade, round, setting and old command: a fresh paper account.

        trades.csv is kept under a new name rather than deleted. Returns that name, if any."""
        with self.conn:
            for table in ("positions", "orders", "rounds", "settings"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("DELETE FROM commands WHERE id IS NOT ?", (keep_command_id,))
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name = 'positions'")
        if self.csv_path.exists() and self.csv_path.stat().st_size:
            archived = self.csv_path.with_name(
                f"{self.csv_path.stem}-before-reset-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}{self.csv_path.suffix}")
            self.csv_path.rename(archived)
            return archived.name
        return None

    def clear_pending_commands(self, now: float) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE commands SET status = 'FAILED', message = 'Cleared from the dashboard; nothing was done', "
                "processed_at = ? WHERE status = 'PENDING'", (now,))
        return cur.rowcount

    # --- positions -----------------------------------------------------------
    def insert_position(self, fields: dict) -> int:
        row = {k: v for k, v in fields.items() if k in POSITION_COLUMNS}
        row.setdefault("status", "OPEN")
        cols = list(row)
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO positions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [row[c] for c in cols],
            )
        return int(cur.lastrowid)

    def update_position(self, pos_id: int, fields: dict) -> None:
        row = {k: v for k, v in fields.items() if k in POSITION_COLUMNS}
        if not row:
            return
        with self.conn:
            self.conn.execute(
                f"UPDATE positions SET {', '.join(f'{c} = ?' for c in row)} WHERE id = ?",
                [*row.values(), pos_id],
            )

    def get_position(self, pos_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM positions WHERE id = ?", (pos_id,)).fetchone()
        return dict(r) if r else None

    def open_positions(self, slug: str | None = None) -> list[dict]:
        if slug is None:
            cur = self.conn.execute("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id")
        else:
            cur = self.conn.execute("SELECT * FROM positions WHERE status = 'OPEN' AND slug = ? ORDER BY id", (slug,))
        return [dict(r) for r in cur.fetchall()]

    def ended_open_position_slugs(self, before_iso: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT slug FROM positions WHERE status = 'OPEN' AND round_end <= ? ORDER BY round_end",
            (before_iso,),
        )
        return [r[0] for r in cur.fetchall()]

    def bot_position(self, slug: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM positions WHERE source = 'BOT' AND slug = ? ORDER BY id LIMIT 1", (slug,)
        ).fetchone()
        return dict(r) if r else None

    def _backfill_positions(self) -> None:
        """Databases from before the positions table: copy the bot's trades into it once."""
        if self.conn.execute("SELECT 1 FROM positions LIMIT 1").fetchone():
            return
        rows = self.conn.execute(
            "SELECT * FROM rounds WHERE fill_status = 'FILLED' AND stake_usd > 0 ORDER BY round_start"
        ).fetchall()
        for r in map(dict, rows):
            settled = r.get("profit_loss_inr") is not None
            self.insert_position({
                "source": "BOT", "slug": r["slug"], "question": r["question"],
                "round_start": r["round_start"], "round_end": r["round_end"], "side": r["decision"],
                "opened_at": r.get("signal_at") or r["round_start"], "mode": r.get("mode"),
                "stake_inr": r["stake_inr"], "stake_usd": r["stake_usd"], "shares": r["shares"],
                "entry_price": r["entry_price"], "fee_usd": r.get("fee_usd") or 0.0,
                "status": "SETTLED" if settled else "OPEN",
                "close_reason": "RESOLVED" if settled else None,
                "closed_at": r.get("settled_at") if settled else None,
                "result": r.get("result"), "pnl_usd": r.get("profit_loss_usd"), "pnl_inr": r.get("profit_loss_inr"),
            })

    # --- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- dashboard commands --------------------------------------------------
    def enqueue_command(self, kind: str, payload: dict, now: float) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO commands (created_at, kind, payload) VALUES (?, ?, ?)",
                (now, kind, json.dumps(payload)),
            )
        return int(cur.lastrowid)

    def pending_commands(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM commands WHERE status = 'PENDING' ORDER BY id")
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in cur.fetchall()]

    def claim_command(self, cmd_id: int) -> bool:
        """Mark a command as taken. False if someone else (e.g. a second bot process) already did."""
        with self.conn:
            cur = self.conn.execute("UPDATE commands SET status = 'RUNNING' WHERE id = ? AND status = 'PENDING'", (cmd_id,))
        return cur.rowcount == 1

    def finish_command(self, cmd_id: int, ok: bool, message: str, now: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE commands SET status = ?, message = ?, processed_at = ? WHERE id = ?",
                ("DONE" if ok else "FAILED", message, now, cmd_id),
            )

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
