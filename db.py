import sqlite3
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = "./bot.db"

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    size_usdc REAL,
    price REAL,
    timestamp TEXT,
    copied BOOLEAN,
    skip_reason TEXT,
    tx_hash TEXT,
    signal_score INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_WALLET_SCORES = """
CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet_address TEXT PRIMARY KEY,
    win_rate REAL,
    total_bets INTEGER,
    avg_roi REAL,
    consistency_score REAL,
    avg_bet_size REAL,
    market_categories TEXT,
    hot_streak INTEGER,
    last_updated DATETIME
)
"""

CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    size_usdc REAL,
    entry_price REAL,
    current_price REAL,
    pnl_usdc REAL,
    opened_at DATETIME,
    closed_at DATETIME
)
"""

CREATE_SIGNAL_LOG = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    signal_name TEXT,
    signal_value REAL,
    signal_weight REAL,
    contribution REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(CREATE_TRADES)
        conn.execute(CREATE_WALLET_SCORES)
        conn.execute(CREATE_POSITIONS)
        conn.execute(CREATE_SIGNAL_LOG)
        conn.commit()


def insert_trade(trade: dict, copied: bool, skip_reason: Optional[str], tx_hash: Optional[str], signal_score: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO trades (wallet_address, market_id, token_id, side, size_usdc, price,
                                timestamp, copied, skip_reason, tx_hash, signal_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.get("wallet_address"),
                trade.get("market_id"),
                trade.get("token_id"),
                trade.get("side"),
                trade.get("size_usdc"),
                trade.get("price"),
                trade.get("timestamp"),
                copied,
                skip_reason,
                tx_hash,
                signal_score,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def update_trade_tx(trade_id: int, tx_hash: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE trades SET tx_hash = ? WHERE id = ?", (tx_hash, trade_id))
        conn.commit()


def get_open_positions() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM positions WHERE closed_at IS NULL")
        return [dict(row) for row in cursor.fetchall()]


def upsert_wallet_score(score: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO wallet_scores (wallet_address, win_rate, total_bets, avg_roi,
                consistency_score, avg_bet_size, market_categories, hot_streak, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                win_rate=excluded.win_rate,
                total_bets=excluded.total_bets,
                avg_roi=excluded.avg_roi,
                consistency_score=excluded.consistency_score,
                avg_bet_size=excluded.avg_bet_size,
                market_categories=excluded.market_categories,
                hot_streak=excluded.hot_streak,
                last_updated=excluded.last_updated
            """,
            (
                score.get("wallet_address"),
                score.get("win_rate"),
                score.get("total_bets"),
                score.get("avg_roi"),
                score.get("consistency_score"),
                score.get("avg_bet_size"),
                score.get("market_categories"),
                score.get("hot_streak"),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()


def get_wallet_score(wallet_address: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM wallet_scores WHERE wallet_address = ?", (wallet_address,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_daily_summary() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        cursor = conn.execute(
            """
            SELECT
                COUNT(*) as total_seen,
                SUM(CASE WHEN copied = 1 THEN 1 ELSE 0 END) as total_copied,
                SUM(CASE WHEN copied = 0 THEN 1 ELSE 0 END) as total_skipped,
                AVG(CASE WHEN copied = 1 THEN signal_score END) as avg_signal_score
            FROM trades WHERE created_at >= ?
            """,
            (since,),
        )
        row = cursor.fetchone()
        return dict(row) if row else {}


def log_signals(trade_id: int, signals: list[dict]):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            """
            INSERT INTO signal_log (trade_id, signal_name, signal_value, signal_weight, contribution)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    trade_id,
                    s.get("signal_name"),
                    s.get("signal_value"),
                    s.get("signal_weight"),
                    s.get("contribution"),
                )
                for s in signals
            ],
        )
        conn.commit()


def get_recent_copies(hours: int = 24) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        cursor = conn.execute(
            "SELECT * FROM trades WHERE copied = 1 AND created_at >= ? ORDER BY created_at DESC",
            (since,),
        )
        return [dict(row) for row in cursor.fetchall()]
