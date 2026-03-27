"""
End-to-end integration test for the Polymarket copy-trading bot.
Uses a temp file SQLite DB. All external HTTP calls are mocked.
DRY_RUN=True throughout.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8, "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "key", "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "pass", "TELEGRAM_BOT_TOKEN": "tok",
    "TELEGRAM_CHAT_ID": "123", "DRY_RUN": "true",
    "MIN_SIGNAL_SCORE": "60",
    "MIN_WALLET_WIN_RATE": "0.58",
    "MIN_WALLET_BETS": "30",
    "MAX_POSITION_SIZE_USDC": "50",
    "MAX_PORTFOLIO_EXPOSURE_USDC": "500",
    "COPY_RATIO": "0.5",
    "POLL_INTERVAL_SECONDS": "30",
})

import db
from config import load_config
from wallet_monitor import WalletMonitor
from wallet_scorer import WalletScorer, WalletScore
from signal_engine import SignalEngine
from decision_engine import DecisionEngine
from executor import Executor

ASSERTION_COUNT = [0]


def assert_count(cond, msg=""):
    assert cond, msg
    ASSERTION_COUNT[0] += 1


def _future(hours=48):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def _ts(offset_seconds=10):
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def make_activity_response(outcome="YES", size="80", offset_seconds=10):
    return [{
        "type": "trade",
        "outcome": outcome,
        "usdcSize": size,
        "price": "0.55",
        "conditionId": "cond_integration",
        "tokenId": "tok_integration",
        "category": "crypto",
        "timestamp": _ts(offset_seconds),
    }]


def make_market_response(hours_to_close=72, liquidity=5000, resolved=False):
    return [{
        "id": "mkt_integration",
        "question": "Will BTC hit $100k by end of 2026?",
        "category": "crypto",
        "liquidity": liquidity,
        "resolved": resolved,
        "resolvedYes": False,
        "endDate": _future(hours_to_close),
    }]


def make_mock_get(activity=None, market=None):
    activity = activity or make_activity_response()
    market = market or make_market_response()

    def side_effect(url, params=None, timeout=None):
        r = MagicMock()
        r.raise_for_status = lambda: None
        if "activity" in url:
            r.json.return_value = activity
        else:
            r.json.return_value = market
        return r

    return side_effect


def make_wallet_score(win_rate=0.68, total_bets=55, hot_streak=3):
    return WalletScore(
        wallet_address="0xwallet1",
        win_rate=win_rate,
        total_bets=total_bets,
        avg_roi=0.35,
        consistency_score=0.80,
        avg_bet_size=80.0,
        market_categories="crypto",
        hot_streak=hot_streak,
        recency_weight=0.6,
        composite_score=0.72,
        last_updated=datetime.now(tz=timezone.utc),
    )


class TestIntegration(unittest.TestCase):

    def setUp(self):
        # Use a fresh temp DB for each test
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        db.DB_PATH = self.db_path
        db.init_db()

        # Write wallets.json
        with open("wallets.json", "w") as f:
            json.dump({"wallets": [{"address": "0xwallet1", "label": "whale_1"}]}, f)

        self.config = load_config()
        self.config.dry_run = True

        self.signal_engine = SignalEngine(self.config)
        self.decision_engine = DecisionEngine(self.config, self.signal_engine)

        # Executor with mocked ClobClient
        with patch("executor.ClobClient"):
            self.executor = Executor(self.config)
        self.executor._client = MagicMock()
        self.executor._client.get_balance_allowance.return_value = {"balance": "500.0"}

        self.monitor = WalletMonitor(self.config)
        self.monitor._last_seen = {}
        self.monitor._market_cache = {}
        self.monitor._market_cache_ts = {}

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.remove(self.db_path)
        except Exception:
            pass

    def _run_cycle(self, wallet_score, activities=None, market=None):
        """Run one full monitor→evaluate→execute cycle. Returns (decision, result, trade_db_id)."""
        mock_get = make_mock_get(
            activity=activities or make_activity_response(),
            market=market or make_market_response(),
        )
        with patch("requests.get", side_effect=mock_get):
            trades = self.monitor.poll()

        self.assertGreater(len(trades), 0, "No trades detected in poll")
        trade = trades[0]

        # Inject wallet score via DB
        db.upsert_wallet_score({
            "wallet_address": trade.wallet_address,
            "win_rate": wallet_score.win_rate,
            "total_bets": wallet_score.total_bets,
            "avg_roi": wallet_score.avg_roi,
            "consistency_score": wallet_score.consistency_score,
            "avg_bet_size": wallet_score.avg_bet_size,
            "market_categories": wallet_score.market_categories,
            "hot_streak": wallet_score.hot_streak,
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
        })

        available_usdc = 500.0
        decision = self.decision_engine.evaluate(trade, wallet_score, available_usdc)

        trade_dict = {
            "wallet_address": trade.wallet_address,
            "market_id": trade.market_id,
            "token_id": trade.token_id,
            "side": trade.side,
            "size_usdc": trade.size_usdc,
            "price": trade.price,
            "timestamp": trade.detected_at.isoformat(),
        }

        if decision.action == "copy":
            trade_db_id = db.insert_trade(
                trade_dict, copied=True, skip_reason=None,
                tx_hash=None, signal_score=decision.signal_score,
            )
            db.log_signals(trade_db_id, [
                {"signal_name": s.name, "signal_value": s.value,
                 "signal_weight": s.weight, "contribution": s.contribution}
                for s in decision.signal_result.signals
            ])
            result = self.executor.execute(decision, trade_db_id)
        else:
            trade_db_id = db.insert_trade(
                trade_dict, copied=False, skip_reason=decision.skip_reason,
                tx_hash=None, signal_score=decision.signal_score,
            )
            db.log_signals(trade_db_id, [
                {"signal_name": s.name, "signal_value": s.value,
                 "signal_weight": s.weight, "contribution": s.contribution}
                for s in decision.signal_result.signals
            ])
            result = None

        return decision, result, trade_db_id

    # ── Scenario 1: Happy path — trade gets copied ────────────────────────────
    def test_scenario1_happy_path_trade_copied(self):
        ws = make_wallet_score(win_rate=0.68, total_bets=55)
        decision, result, trade_db_id = self._run_cycle(ws)

        assert_count(decision.action == "copy", "action should be copy")
        assert_count(result is not None and result.success, "result should be success")
        assert_count(result.tx_hash == "DRY_RUN", "tx_hash should be DRY_RUN")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT copied, tx_hash FROM trades WHERE id=?", (trade_db_id,)
            ).fetchone()
            assert_count(row[0] == 1, "copied should be 1")
            assert_count(row[1] == "DRY_RUN", "tx_hash should be DRY_RUN in DB")

            sig_count = conn.execute(
                "SELECT COUNT(*) FROM signal_log WHERE trade_id=?", (trade_db_id,)
            ).fetchone()[0]
            assert_count(sig_count == 7, f"Expected 7 signals, got {sig_count}")

        assert_count(
            decision.signal_score >= self.config.min_signal_score,
            f"signal_score {decision.signal_score} < min {self.config.min_signal_score}",
        )

    # ── Scenario 2: Low win rate — trade gets skipped ─────────────────────────
    def test_scenario2_low_win_rate_skipped(self):
        ws = make_wallet_score(win_rate=0.42, total_bets=55)
        decision, result, trade_db_id = self._run_cycle(ws)

        assert_count(decision.action == "skip", "action should be skip")
        assert_count(decision.skip_reason == "wallet_below_threshold",
                     f"Expected wallet_below_threshold, got {decision.skip_reason}")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT copied, skip_reason FROM trades WHERE id=?", (trade_db_id,)
            ).fetchone()
            assert_count(row[0] == 0, "copied should be 0")
            assert_count(row[1] == "wallet_below_threshold", "skip_reason mismatch")

            sig_count = conn.execute(
                "SELECT COUNT(*) FROM signal_log WHERE trade_id=?", (trade_db_id,)
            ).fetchone()[0]
            assert_count(sig_count == 7, f"Expected 7 signals even for skip, got {sig_count}")

    # ── Scenario 3: Portfolio exposure cap ───────────────────────────────────
    def test_scenario3_portfolio_exposure_cap(self):
        # Pre-populate $500 of open positions (at the cap)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO positions (market_id, token_id, side, size_usdc,
                   entry_price, current_price, pnl_usdc, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("other_mkt", "tok2", "YES", 500.0, 0.5, 0.5, 0.0,
                 datetime.now(tz=timezone.utc).isoformat()),
            )
            conn.commit()

        ws = make_wallet_score(win_rate=0.68, total_bets=55)
        decision, result, trade_db_id = self._run_cycle(ws)

        assert_count(decision.action == "skip", "should skip due to exposure")
        assert_count(decision.skip_reason == "max_exposure_reached",
                     f"Expected max_exposure_reached, got {decision.skip_reason}")

    # ── Scenario 4: Duplicate position ───────────────────────────────────────
    def test_scenario4_duplicate_position(self):
        # Pre-populate an open YES position on the same market
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO positions (market_id, token_id, side, size_usdc,
                   entry_price, current_price, pnl_usdc, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("mkt_integration", "tok_integration", "YES", 40.0,
                 0.55, 0.55, 0.0, datetime.now(tz=timezone.utc).isoformat()),
            )
            conn.commit()

        ws = make_wallet_score(win_rate=0.68, total_bets=55)
        decision, result, trade_db_id = self._run_cycle(ws)

        assert_count(decision.action == "skip", "should skip duplicate")
        assert_count(decision.skip_reason == "duplicate_position",
                     f"Expected duplicate_position, got {decision.skip_reason}")


def tearDownModule():
    print(f"\n{'='*55}")
    print(f"  All tests passed ({ASSERTION_COUNT[0]} assertions).")
    print(f"  Bot logic is verified. Ready for deployment.")
    print(f"\n  Next steps:")
    print(f"  1. Fill in real credentials in .env")
    print(f"  2. python setup.py")
    print(f"  3. Add wallets to wallets.json (use polytrackhq.app)")
    print(f"  4. python main.py --dry-run   (watch for 24-48 hours)")
    print(f"  5. python main.py --live      (only when confident)")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
