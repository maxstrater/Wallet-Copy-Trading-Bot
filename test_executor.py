import os
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8, "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "key", "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "pass", "TELEGRAM_BOT_TOKEN": "tok",
    "TELEGRAM_CHAT_ID": "123", "DRY_RUN": "true",
})

import db
db.DB_PATH = "./test_executor.db"
db.init_db()

from executor import Executor, ExecutionResult


def make_trade():
    t = MagicMock()
    t.market_id = "mkt1"
    t.token_id = "tok1"
    t.side = "YES"
    t.size_usdc = 50.0
    t.price = 0.55
    t.question = "Will BTC hit 100k?"
    return t


def make_decision(size=50.0, side="YES"):
    d = MagicMock()
    d.side = side
    d.size_usdc = size
    d.trade = make_trade()
    return d


def make_executor(dry_run=True, max_pos=100.0, balance=500.0):
    cfg = MagicMock()
    cfg.dry_run = dry_run
    cfg.max_position_size_usdc = max_pos
    cfg.polymarket_pk = "aabbccdd" * 8
    cfg.polymarket_funder = "0xfunder"
    cfg.polymarket_api_key = "key"
    cfg.polymarket_api_secret = "secret"
    cfg.polymarket_api_passphrase = "pass"

    ex = Executor.__new__(Executor)
    ex.config = cfg
    ex.cloudflare_block_count = 0
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": str(balance)}
    mock_client.create_market_order.return_value = MagicMock()
    mock_client.post_order.return_value = {
        "orderID": "0xTX123",
        "status": "matched",
        "size_matched": "50.0",
        "price": "0.55",
    }
    ex._client = mock_client
    return ex, mock_client


def insert_test_trade():
    return db.insert_trade(
        {"wallet_address": "0xabc", "market_id": "mkt1", "token_id": "tok1",
         "side": "YES", "size_usdc": 50, "price": 0.55, "timestamp": "2026-01-01"},
        copied=True, skip_reason=None, tx_hash=None, signal_score=75,
    )


class TestExecutor(unittest.TestCase):

    # Test 1: DRY_RUN=True returns success without any ClobClient calls
    def test_dry_run_no_clob_calls(self):
        ex, mock_client = make_executor(dry_run=True)
        trade_id = insert_test_trade()
        result = ex.execute(make_decision(), trade_id)

        self.assertTrue(result.success)
        self.assertEqual(result.tx_hash, "DRY_RUN")
        mock_client.post_order.assert_not_called()
        mock_client.create_market_order.assert_not_called()
        mock_client.get_balance_allowance.assert_not_called()

    # Test 2: DRY_RUN=True writes tx_hash="DRY_RUN" to DB
    def test_dry_run_writes_dry_run_to_db(self):
        ex, _ = make_executor(dry_run=True)
        trade_id = insert_test_trade()
        ex.execute(make_decision(), trade_id)

        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute(
                "SELECT tx_hash FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
        self.assertEqual(row[0], "DRY_RUN")

    # Test 3: DRY_RUN=False calls post_order once
    def test_live_calls_post_order_once(self):
        ex, mock_client = make_executor(dry_run=False)
        trade_id = insert_test_trade()
        result = ex.execute(make_decision(), trade_id)

        mock_client.post_order.assert_called_once()
        self.assertTrue(result.success)

    # Test 4: 403 triggers one retry then returns cloudflare_blocked
    def test_cloudflare_403_retry_then_blocked(self):
        ex, mock_client = make_executor(dry_run=False)
        mock_client.post_order.side_effect = Exception("403 Cloudflare blocked")
        trade_id = insert_test_trade()

        with patch("executor.time"):
            result = ex.execute(make_decision(), trade_id)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "cloudflare_blocked")
        self.assertEqual(mock_client.post_order.call_count, 2)  # original + 1 retry

    # Test 5: safety invariant (size > MAX * 1.05) returns failure
    def test_safety_invariant_blocks_oversized_order(self):
        ex, mock_client = make_executor(dry_run=False, max_pos=10.0)
        # size=50 >> max_pos=10 * 1.05 = 10.5
        trade_id = insert_test_trade()
        result = ex.execute(make_decision(size=50.0), trade_id)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "safety_invariant_violated")
        mock_client.post_order.assert_not_called()

    # Test 6: get_balance() returns 0.0 on API error
    def test_get_balance_returns_zero_on_error(self):
        ex, mock_client = make_executor()
        mock_client.get_balance_allowance.side_effect = Exception("network error")
        balance = ex.get_balance()

        self.assertEqual(balance, 0.0)

    # Test 7: insufficient balance returns failure
    def test_insufficient_balance_returns_failure(self):
        ex, mock_client = make_executor(dry_run=False, balance=5.0)
        trade_id = insert_test_trade()
        result = ex.execute(make_decision(size=50.0), trade_id)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "insufficient_balance")
        mock_client.post_order.assert_not_called()

    # Test 8: successful fill records position in DB
    def test_successful_fill_records_position(self):
        ex, _ = make_executor(dry_run=False)
        trade_id = insert_test_trade()
        result = ex.execute(make_decision(), trade_id)

        self.assertTrue(result.success)
        positions = db.get_open_positions()
        self.assertTrue(any(p["market_id"] == "mkt1" for p in positions))


def tearDownModule():
    try:
        os.remove("./test_executor.db")
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
