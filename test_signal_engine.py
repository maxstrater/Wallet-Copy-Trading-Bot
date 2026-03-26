import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from signal_engine import SignalEngine, SIGNALS_META


def make_trade(**kwargs):
    defaults = dict(
        wallet_address="0xabc",
        wallet_label="whale_1",
        market_id="market_1",
        condition_id="cond_1",
        token_id="token_1",
        question="Will X happen?",
        category="crypto",
        side="YES",
        size_usdc=100.0,
        price=0.50,
        closes_at=datetime.now(tz=timezone.utc) + timedelta(hours=48),
        liquidity_usdc=5000.0,
        detected_at=datetime.now(tz=timezone.utc),
    )
    defaults.update(kwargs)
    trade = MagicMock()
    for k, v in defaults.items():
        setattr(trade, k, v)
    return trade


def make_wallet_score(**kwargs):
    defaults = dict(
        wallet_address="0xabc",
        win_rate=0.65,
        total_bets=50,
        avg_roi=0.35,
        consistency_score=0.80,
        avg_bet_size=100.0,
        market_categories="crypto,politics",
        hot_streak=3,
        recency_weight=0.6,
        composite_score=0.72,
        last_updated=datetime.now(tz=timezone.utc),
    )
    defaults.update(kwargs)
    ws = MagicMock()
    for k, v in defaults.items():
        setattr(ws, k, v)
    return ws


def make_config():
    cfg = MagicMock()
    return cfg


class TestSignalEngine(unittest.TestCase):

    def setUp(self):
        self.engine = SignalEngine(make_config())

    def _get_signal(self, result, name):
        return next(s for s in result.signals if s.name == name)

    # Test 1: price=0.50 scores higher on price_efficiency than price=0.80
    def test_price_efficiency_near_half_beats_extreme(self):
        trade_mid = make_trade(price=0.50)
        trade_ext = make_trade(price=0.80)
        ws = make_wallet_score()

        r_mid = self.engine.compute(trade_mid, ws, [], 1000.0)
        r_ext = self.engine.compute(trade_ext, ws, [], 1000.0)

        sig_mid = self._get_signal(r_mid, "price_efficiency")
        sig_ext = self._get_signal(r_ext, "price_efficiency")
        self.assertGreater(sig_mid.value, sig_ext.value)

    # Test 2: 3x average bet size scores higher on conviction than 1x
    def test_conviction_high_size_beats_average(self):
        ws = make_wallet_score(avg_bet_size=100.0)
        trade_3x = make_trade(size_usdc=300.0)
        trade_1x = make_trade(size_usdc=100.0)

        r_3x = self.engine.compute(trade_3x, ws, [], 1000.0)
        r_1x = self.engine.compute(trade_1x, ws, [], 1000.0)

        sig_3x = self._get_signal(r_3x, "bet_size_conviction")
        sig_1x = self._get_signal(r_1x, "bet_size_conviction")
        self.assertGreater(sig_3x.value, sig_1x.value)

    # Test 3: market closing in 2h scores lower on time_value than 48h
    def test_time_value_48h_beats_2h(self):
        ws = make_wallet_score()
        trade_2h = make_trade(closes_at=datetime.now(tz=timezone.utc) + timedelta(hours=2))
        trade_48h = make_trade(closes_at=datetime.now(tz=timezone.utc) + timedelta(hours=48))

        r_2h = self.engine.compute(trade_2h, ws, [], 1000.0)
        r_48h = self.engine.compute(trade_48h, ws, [], 1000.0)

        sig_2h = self._get_signal(r_2h, "time_value")
        sig_48h = self._get_signal(r_48h, "time_value")
        self.assertGreater(sig_48h.value, sig_2h.value)

    # Test 4: existing position in same market causes portfolio_fit = 0
    def test_portfolio_fit_zero_with_existing_position(self):
        ws = make_wallet_score()
        trade = make_trade(market_id="market_1")
        open_positions = [{"market_id": "market_1", "size_usdc": 50.0}]

        result = self.engine.compute(trade, ws, open_positions, 1000.0)
        sig = self._get_signal(result, "portfolio_fit")
        self.assertEqual(sig.value, 0.0)

    # Test 5: wallet on 5-trade hot streak scores 1.0 on hot_streak signal
    def test_hot_streak_five_scores_one(self):
        ws = make_wallet_score(hot_streak=5)
        trade = make_trade()

        result = self.engine.compute(trade, ws, [], 1000.0)
        sig = self._get_signal(result, "hot_streak")
        self.assertEqual(sig.value, 1.0)

    # Test 6: total weights always sum to 100
    def test_total_weights_sum_to_100(self):
        total = sum(weight for _, weight in SIGNALS_META)
        self.assertEqual(total, 100)


if __name__ == "__main__":
    unittest.main()
