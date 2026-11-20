"""Append-only audit store: signals, decisions, orders, plus small key/value state (kill switch)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idem_key TEXT UNIQUE NOT NULL,
  received_at REAL NOT NULL,
  strategy_id TEXT, ticker TEXT,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received',   -- received | rejected | ordered | error | noop
  reason TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,
  signal_id INTEGER REFERENCES signals(id),
  created_at REAL NOT NULL,
  symbol TEXT, side TEXT, qty REAL, limit_price REAL,
  status TEXT NOT NULL,                     -- submitted | filled | partial | cancelled | rejected | dryrun
  broker_response TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(orders)")]
            if "kind" not in cols:  # migrate DBs created before trailing stops existed
                self._conn.execute("ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'entry'")

    # -- signals --
    def insert_signal(self, idem_key: str, strategy_id: str, ticker: str, payload: dict) -> Optional[int]:
        """Returns the new row id, or None if this idempotency key was already seen."""
        safe = {k: v for k, v in payload.items() if k != "secret"}
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO signals(idem_key, received_at, strategy_id, ticker, payload) VALUES (?,?,?,?,?)",
                    (idem_key, time.time(), strategy_id, ticker, json.dumps(safe)),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def set_signal_status(self, signal_id: int, status: str, reason: str = "") -> None:
        with self._lock:
            self._conn.execute("UPDATE signals SET status=?, reason=? WHERE id=?", (status, reason, signal_id))

    def recent_signals(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- orders --
    def insert_order(self, client_order_id: str, signal_id: Optional[int], symbol: str, side: str, qty: float,
                     limit_price: Optional[float], status: str, broker_response: Any, kind: str = "entry",
                     created_at: Optional[float] = None) -> None:
        """kind: 'entry' for signal orders (limit), 'trailing_stop' for protective stops."""
        now = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders(client_order_id, signal_id, created_at, symbol, side, qty, limit_price, "
                "status, broker_response, updated_at, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, signal_id, now, symbol, side, qty, limit_price, status,
                 json.dumps(broker_response, default=str), now, kind),
            )

    def update_order(self, client_order_id: str, status: str, broker_response: Any = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status=?, broker_response=COALESCE(?, broker_response), updated_at=? "
                "WHERE client_order_id=?",
                (status, json.dumps(broker_response, default=str) if broker_response is not None else None,
                 time.time(), client_order_id),
            )

    def open_orders(self, kind: Optional[str] = None, symbol: Optional[str] = None) -> list[dict]:
        q, args = "SELECT * FROM orders WHERE status IN ('submitted','partial')", []
        if kind:
            q += " AND kind=?"; args.append(kind)
        if symbol:
            q += " AND symbol=?"; args.append(symbol)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY created_at", args).fetchall()
        return [dict(r) for r in rows]

    def orders_since(self, ts: float) -> int:
        """Entry orders placed since ts (protective stops don't count toward the daily limit)."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE created_at>=? AND status!='rejected' AND kind='entry'",
                (ts,)).fetchone()[0]

    def recent_orders(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- kv --
    def get(self, k: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
