from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
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
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS symbols (
                symbol TEXT PRIMARY KEY
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                adj_close REAL,
                PRIMARY KEY(symbol, date)
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

    def get_all_positions(self) -> list[Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return [
            Position(
                symbol=row["symbol"],
                qty=row["qty"],
                avg_price=row["avg_price"],
                opened_at=datetime.fromisoformat(row["opened_at"]),
                paper=bool(row["paper"]),
            )
            for row in rows
        ]

    def cache_price_data(
        self, symbol: str, price_date: date, open_price: float, high: float, low: float, close: float, adj_close: float
    ) -> None:
        self.conn.execute(
            """
            REPLACE INTO price_cache(symbol, date, open, high, low, close, adj_close)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, price_date.isoformat(), open_price, high, low, close, adj_close),
        )
        self.conn.commit()

    def get_cached_price(self, symbol: str, price_date: date) -> dict[str, float] | None:
        row = self.conn.execute(
            "SELECT * FROM price_cache WHERE symbol = ? AND date = ?", (symbol, price_date.isoformat())
        ).fetchone()
        if row is None:
            return None
        return {
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "adj_close": row["adj_close"],
        }

    def get_latest_cached_date(self, symbol: str) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(date) as latest_date FROM price_cache WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None or row["latest_date"] is None:
            return None
        return date.fromisoformat(row["latest_date"])

    def list_cached_prices(
        self,
        symbol: str,
        limit: int | None = None,
        start: date | None = None,
        end: date | None = None,
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT date, open, high, low, close, adj_close FROM price_cache WHERE symbol = ?"
        params: list[Any] = [symbol]
        if start is not None:
            query += " AND date >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND date <= ?"
            params.append(end.isoformat())
        order = "ASC" if ascending else "DESC"
        query += f" ORDER BY date {order}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [
            {
                "date": date.fromisoformat(row["date"]),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "adj_close": row["adj_close"],
            }
            for row in rows
        ]

    def upsert_symbol(self, symbol: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO symbols(symbol) VALUES (?)", (symbol,))
        self.conn.commit()

    def clear_price_cache(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM price_cache WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
