from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    opened_at: datetime
    paper: bool = True


@dataclass
class Stop:
    symbol: str
    stop_price: float
    method: str
    atr_n: int
    atr_k: float
    updated_at: datetime


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    qty: float
    type: str
    limit_price: float | None
    stop_price: float | None
    tif: str
    status: str
    placed_at: datetime


class Journal:
    def __init__(self, database_url: str) -> None:
        self.database_path = self._resolve_path(database_url)
        self.conn = sqlite3.connect(self.database_path, detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _resolve_path(self, url: str) -> str:
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "", 1) or ":memory:"
        if url.startswith("sqlite://"):
            return ":memory:"
        return url

    def _ensure_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL,
                avg_price REAL,
                opened_at TEXT,
                paper INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS stops (
                symbol TEXT PRIMARY KEY,
                stop_price REAL,
                method TEXT,
                atr_n INTEGER,
                atr_k REAL,
                updated_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty REAL,
                type TEXT,
                limit_price REAL,
                stop_price REAL,
                tif TEXT,
                status TEXT,
                placed_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                level TEXT,
                msg TEXT,
                ctx TEXT
            )
            """
        )
        self.conn.commit()

    def get_position(self, symbol: str) -> Position | None:
        row = self.conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            return None
        return Position(
            symbol=row["symbol"],
            qty=row["qty"],
            avg_price=row["avg_price"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            paper=bool(row["paper"]),
        )

    def upsert_position(self, position: Position) -> None:
        self.conn.execute(
            "REPLACE INTO positions(symbol, qty, avg_price, opened_at, paper) VALUES (?, ?, ?, ?, ?)",
            (
                position.symbol,
                position.qty,
                position.avg_price,
                position.opened_at.isoformat(),
                int(position.paper),
            ),
        )
        self.conn.commit()

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def get_stop(self, symbol: str) -> Stop | None:
        row = self.conn.execute("SELECT * FROM stops WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            return None
        return Stop(
            symbol=row["symbol"],
            stop_price=row["stop_price"],
            method=row["method"],
            atr_n=row["atr_n"],
            atr_k=row["atr_k"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def upsert_stop(self, stop: Stop) -> None:
        self.conn.execute(
            "REPLACE INTO stops(symbol, stop_price, method, atr_n, atr_k, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                stop.symbol,
                stop.stop_price,
                stop.method,
                stop.atr_n,
                stop.atr_k,
                stop.updated_at.isoformat(),
            ),
        )
        self.conn.commit()

    def delete_stop(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM stops WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def record_order(self, order: Order) -> None:
        self.conn.execute(
            """
            REPLACE INTO orders(id, symbol, side, qty, type, limit_price, stop_price, tif, status, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.id,
                order.symbol,
                order.side,
                order.qty,
                order.type,
                order.limit_price,
                order.stop_price,
                order.tif,
                order.status,
                order.placed_at.isoformat(),
            ),
        )
        self.conn.commit()

    def log(self, level: str, message: str, ctx: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, level, msg, ctx) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), level, message, ctx),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
