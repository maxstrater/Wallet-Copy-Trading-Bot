"""Unit tests for position_monitor.py"""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import db
from config import Config
from position_monitor import ExitResult, PositionMonitor


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_config(**overrides):
    defaults = dict(
        polymarket_pk="pk",
        polymarket_funder="0xfunder",
        polymarket_api_key="",
        polymarket_api_secret="",
        polymarket_api_passphrase="",
        telegram_bot_token="tok",
        telegram_chat_id="cid",
        dry_run=True,
        max_position_size_usdc=50.0,
        max_portfolio_exposure_usdc=500.0,
        copy_ratio=0.5,
        min_wallet_win_rate=0.58,
        min_wallet_bets=30,
        min_signal_score=65,
        poll_interval_seconds=30,
        take_profit_pct=0.40,
        stop_loss_threshold=0.20,
        position_check_interval=300,
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_position(entry_price=0.50, size_usdc=25.0, side="YES",
                  closes_at=None, market_id="mkt1", token_id="tok1",
                  condition_id="cond1", pos_id=1, question="Will it rain?"):
    if closes_at is None:
        closes_at = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
    return {
        "id": pos_id,
        "market_id": market_id,
        "token_id": token_id,
        "side": side,
        "size_usdc": size_usdc,
        "entry_price": entry_price,
        "current_price": entry_price,
        "pnl_usdc": 0.0,
        "opened_at": datetime.now(tz=timezone.utc).isoformat(),
        "closed_at": None,
        "condition_id": condition_id,
        "question": question,
        "is_simulated": 1,
        "closes_at": closes_at,
    }


def make_monitor(dry_run=True, clob_client=None):
    config = make_config(dry_run=dry_run)
    alerts = MagicMock()
    return PositionMonitor(config, clob_client, alerts)


# ── _evaluate_exit tests ──────────────────────────────────────────────────────

class TestEvaluateExit:
    def test_take_profit_strong(self):
        mon = make_monitor()
        pos = make_position(entry_price=0.40)
        # current = entry + 0.25 = 0.65
        reason = mon._evaluate_exit(pos, 0.65)
        assert reason == "take_profit_strong"

    def test_take_profit_pct(self):
        mon = make_monitor()
        pos = make_position(entry_price=0.50)
        # 40%+ profit — use 0.71 to avoid float precision edge at exactly 0.70
        reason = mon._evaluate_exit(pos, 0.71)
        assert reason == "take_profit_pct"

    def test_stop_loss(self):
        mon = make_monitor()
        pos = make_position(entry_price=0.50)
        # stop_loss_threshold=0.20: current <= 0.50 - 0.20 = 0.30
        reason = mon._evaluate_exit(pos, 0.30)
        assert reason == "stop_loss"

    def test_time_decay(self):
        mon = make_monitor()
        # Market closing in 4 hours, we're up 0.10
        closes_at = (datetime.now(tz=timezone.utc) + timedelta(hours=4)).isoformat()
        pos = make_position(entry_price=0.50, closes_at=closes_at)
        reason = mon._evaluate_exit(pos, 0.61)  # up 0.11 from entry
        assert reason == "time_decay"

    def test_no_exit_condition(self):
        mon = make_monitor()
        pos = make_position(entry_price=0.50)
        # Price barely moved — no exit
        reason = mon._evaluate_exit(pos, 0.55)
        assert reason is None

    def test_take_profit_strong_takes_priority(self):
        """take_profit_strong is checked before take_profit_pct."""
        mon = make_monitor()
        pos = make_position(entry_price=0.40)
        # This satisfies both strong (+0.25) and pct (+40%)
        reason = mon._evaluate_exit(pos, 0.70)
        assert reason == "take_profit_strong"


# ── _execute_exit dry-run test ────────────────────────────────────────────────

class TestExecuteExitDryRun:
    def test_dry_run_returns_true(self):
        mon = make_monitor(dry_run=True)
        pos = make_position(entry_price=0.50, size_usdc=25.0)
        result = mon._execute_exit(pos, 0.75, "take_profit_strong")
        assert result is True


# ── check_all_positions integration test ─────────────────────────────────────

class TestCheckAllPositions:
    def test_exit_triggered_and_db_updated(self, tmp_path):
        """When a price hits take-profit, position is closed and ExitResult returned."""
        db_path = str(tmp_path / "test_pm.db")
        original_db_path = db.DB_PATH
        db.DB_PATH = db_path

        try:
            db.init_db()
            # Insert a position directly
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO positions
                       (market_id, token_id, side, size_usdc, entry_price, current_price,
                        pnl_usdc, opened_at, condition_id, question, is_simulated, closes_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?)""",
                    ("mkt1", "tok1", "YES", 25.0, 0.50, 0.50,
                     datetime.now(tz=timezone.utc).isoformat(),
                     "cond1", "Will it happen?",
                     (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()),
                )
                conn.commit()

            mon = make_monitor(dry_run=True)

            # Patch get_open_positions to return our position
            with patch("position_monitor.db.get_open_positions") as mock_get, \
                 patch("position_monitor.db.close_position") as mock_close:
                pos = make_position(entry_price=0.50, size_usdc=25.0)
                mock_get.return_value = [pos]

                # Patch price fetch to return take-profit price
                with patch.object(mon, "_get_current_price", return_value=0.80):
                    results = mon.check_all_positions()

            assert len(results) == 1
            r = results[0]
            assert r.exit_reason == "take_profit_strong"
            assert r.success is True
            assert r.pnl_usdc > 0
            mock_close.assert_called_once()

        finally:
            db.DB_PATH = original_db_path
