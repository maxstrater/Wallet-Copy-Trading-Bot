import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from decision_engine import DecisionEngine, _confidence_label
from signal_engine import SignalEngine, SignalResult, SignalDetail


def make_config(**kwargs):
    cfg = MagicMock()
    cfg.min_wallet_win_rate = 0.58
    cfg.min_wallet_bets = 30
    cfg.min_signal_score = 65
    cfg.max_position_size_usdc = 50.0
    cfg.max_portfolio_exposure_usdc = 500.0
    cfg.copy_ratio = 0.5
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def make_trade(**kwargs):
    t = MagicMock()
    t.wallet_address = "0xabc"
    t.wallet_label = "whale_1"
    t.market_id = "mkt1"
    t.side = "YES"
    t.size_usdc = 100.0
    t.price = 0.55
    t.question = "Will BTC hit 100k?"
    t.closes_at = datetime.now(tz=timezone.utc) + timedelta(hours=48)
    t.liquidity_usdc = 5000.0
    for k, v in kwargs.items():
        setattr(t, k, v)
    return t


def make_wallet_score(**kwargs):
    ws = MagicMock()
    ws.win_rate = 0.65
    ws.total_bets = 50
    ws.avg_roi = 0.35
    ws.consistency_score = 0.80
    ws.avg_bet_size = 100.0
    ws.hot_streak = 3
    ws.recency_weight = 0.6
    ws.composite_score = 0.72
    for k, v in kwargs.items():
        setattr(ws, k, v)
    return ws


def make_signal_result(score=75):
    details = [
        SignalDetail("wallet_quality", 0.72, 25, 18.0, "desc"),
        SignalDetail("price_efficiency", 0.90, 20, 18.0, "desc"),
        SignalDetail("bet_size_conviction", 0.50, 15, 7.5, "desc"),
        SignalDetail("time_value", 0.80, 15, 12.0, "desc"),
        SignalDetail("liquidity_depth", 0.50, 10, 5.0, "desc"),
        SignalDetail("hot_streak", 0.60, 10, 6.0, "desc"),
        SignalDetail("portfolio_fit", 1.0, 5, 5.0, "desc"),
    ]
    return SignalResult(final_score=score, signals=details, reasoning="Test reasoning.")


def make_engine(config=None):
    cfg = config or make_config()
    signal_engine = MagicMock(spec=SignalEngine)
    signal_engine.compute.return_value = make_signal_result(75)
    engine = DecisionEngine(cfg, signal_engine)
    return engine, signal_engine


class TestDecisionEngine(unittest.TestCase):

    # Test 1: wallet_score=None → skip with "no_wallet_data"
    def test_no_wallet_data(self):
        engine, _ = make_engine()
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(), None, 500.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "no_wallet_data")

    # Test 2: win_rate below threshold → skip with "wallet_below_threshold"
    def test_win_rate_below_threshold(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score(win_rate=0.40)
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(), ws, 500.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "wallet_below_threshold")

    # Test 3: signal_score below MIN_SIGNAL_SCORE → skip with "signal_score_too_low"
    def test_signal_score_too_low(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(40)
        ws = make_wallet_score()
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(), ws, 500.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "signal_score_too_low")

    # Test 4: duplicate position same market+side → skip with "duplicate_position"
    def test_duplicate_position(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score()
        open_pos = [{"market_id": "mkt1", "side": "YES", "size_usdc": 30.0}]
        with patch("decision_engine.db.get_open_positions", return_value=open_pos):
            d = engine.evaluate(make_trade(market_id="mkt1", side="YES"), ws, 500.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "duplicate_position")

    # Test 5: available_usdc=15 → skip with "insufficient_capital"
    def test_insufficient_capital(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score()
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(), ws, 15.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "insufficient_capital")

    # Test 6: portfolio at max exposure → skip with "max_exposure_reached"
    def test_max_exposure_reached(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score()
        open_pos = [{"market_id": "mkt2", "side": "YES", "size_usdc": 500.0}]
        with patch("decision_engine.db.get_open_positions", return_value=open_pos):
            d = engine.evaluate(make_trade(), ws, 500.0)
        self.assertEqual(d.action, "skip")
        self.assertEqual(d.skip_reason, "max_exposure_reached")

    # Test 7: valid trade → action="copy", size respects all caps
    def test_valid_trade_copy(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score()
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(size_usdc=200.0), ws, 500.0)
        self.assertEqual(d.action, "copy")
        self.assertIsNotNone(d.size_usdc)
        # base=100, cap_by_max=50, cap_by_pct=60, cap_by_room=500 → min=50
        self.assertEqual(d.size_usdc, 50.0)
        self.assertIsNone(d.skip_reason)

    # Test 8: final_size rounds to 2 decimal places
    def test_final_size_rounds_to_2dp(self):
        engine, se = make_engine()
        se.compute.return_value = make_signal_result(75)
        ws = make_wallet_score()
        # available=333 → cap_by_pct = 333 * 0.12 = 39.96, base = 55*0.5 = 27.5 → min = 27.5
        with patch("decision_engine.db.get_open_positions", return_value=[]):
            d = engine.evaluate(make_trade(size_usdc=55.0), ws, 333.0)
        self.assertEqual(d.action, "copy")
        # Verify it's rounded to 2dp
        self.assertEqual(d.size_usdc, round(d.size_usdc, 2))

    # Test 9: confidence_label maps correctly at 85, 70, 55 boundaries
    def test_confidence_label_boundaries(self):
        self.assertEqual(_confidence_label(85), "very high")
        self.assertEqual(_confidence_label(84), "high")
        self.assertEqual(_confidence_label(70), "high")
        self.assertEqual(_confidence_label(69), "medium")
        self.assertEqual(_confidence_label(55), "medium")
        self.assertEqual(_confidence_label(54), "low")


if __name__ == "__main__":
    unittest.main()
