import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8, "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "key", "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "pass", "TELEGRAM_BOT_TOKEN": "tok",
    "TELEGRAM_CHAT_ID": "123",
})

from config import load_config
from wallet_monitor import WalletMonitor


def _ts(offset_seconds=1):
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _future(hours=48):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def make_valid_activity(outcome="YES", size="100", condition_id="cond1", offset_seconds=1):
    return {
        "type": "trade",
        "outcome": outcome,
        "usdcSize": size,
        "price": "0.55",
        "conditionId": condition_id,
        "tokenId": "tok1",
        "timestamp": _ts(offset_seconds),
    }


def make_market(resolved=False, hours_to_close=48, liquidity=5000):
    return [{
        "id": "mkt1",
        "question": "Will BTC hit 100k?",
        "category": "crypto",
        "liquidity": liquidity,
        "resolved": resolved,
        "resolvedYes": False,
        "endDate": _future(hours_to_close),
    }]


def make_config():
    cfg = MagicMock()
    cfg.poll_interval_seconds = 30
    return cfg


def make_wallets_json(wallets=None):
    data = {"wallets": wallets or [{"address": "0xabc", "label": "whale_1"}]}
    with open("wallets.json", "w") as f:
        json.dump(data, f)


class TestWalletMonitor(unittest.TestCase):

    def setUp(self):
        make_wallets_json()
        self.monitor = WalletMonitor(make_config())
        self.monitor._last_seen = {}
        self.monitor._market_cache = {}
        self.monitor._market_cache_ts = {}
        self.monitor._poll_count = 0

    def _mock_get(self, activities, market_data=None):
        if market_data is None:
            market_data = make_market()

        def side_effect(url, params=None, timeout=None):
            r = MagicMock()
            r.raise_for_status = lambda: None
            if "activity" in url:
                r.json.return_value = activities
            else:
                r.json.return_value = market_data
            return r

        return side_effect

    # Test 1: poll() returns only valid trades (redemption filtered out)
    def test_poll_filters_redemption(self):
        activities = [
            make_valid_activity("YES", offset_seconds=3),
            make_valid_activity("NO",  offset_seconds=2),
            {"type": "redeem", "outcome": "YES", "usdcSize": "50",
             "conditionId": "cond1", "tokenId": "tok1",
             "timestamp": _ts(1)},
        ]
        with patch("requests.get", side_effect=self._mock_get(activities)):
            trades = self.monitor.poll()

        self.assertEqual(len(trades), 2)
        self.assertTrue(all(t.side in ("YES", "NO") for t in trades))

    # Test 2: second poll returns 0 trades (deduplication)
    def test_poll_deduplication_on_second_call(self):
        activities = [make_valid_activity("YES", offset_seconds=5)]

        with patch("requests.get", side_effect=self._mock_get(activities)):
            first = self.monitor.poll()

        self.assertEqual(len(first), 1)

        with patch("requests.get", side_effect=self._mock_get(activities)):
            second = self.monitor.poll()

        self.assertEqual(len(second), 0)

    # Test 3: trade below minimum size filtered out
    def test_poll_filters_small_trade(self):
        activities = [make_valid_activity("YES", size="5.0")]  # < 10 USDC
        with patch("requests.get", side_effect=self._mock_get(activities)):
            trades = self.monitor.poll()

        self.assertEqual(len(trades), 0)

    # Test 4: market closing in < 3 hours filtered out
    def test_poll_filters_market_closing_too_soon(self):
        activities = [make_valid_activity("YES", offset_seconds=10)]
        soon_market = make_market(hours_to_close=1)  # 1h — below 3h threshold
        with patch("requests.get", side_effect=self._mock_get(activities, soon_market)):
            trades = self.monitor.poll()

        self.assertEqual(len(trades), 0)

    # Test 5: 429 response triggers retry_with_backoff
    def test_poll_retries_on_429(self):
        import requests as req

        call_count = {"n": 0}

        def side_effect_429(url, params=None, timeout=None):
            call_count["n"] += 1
            if "activity" in url and call_count["n"] == 1:
                r = MagicMock()
                r.raise_for_status.side_effect = req.exceptions.RequestException("429 Too Many Requests")
                return r
            r = MagicMock()
            r.raise_for_status = lambda: None
            if "activity" in url:
                r.json.return_value = [make_valid_activity("YES", offset_seconds=99)]
            else:
                r.json.return_value = make_market()
            return r

        with patch("time.sleep"):  # suppress actual sleep
            with patch("requests.get", side_effect=side_effect_429):
                trades = self.monitor.poll()

        # After retry: should succeed on 2nd attempt
        self.assertGreaterEqual(call_count["n"], 2)

    # Test 6: resolved market is filtered out
    def test_poll_filters_resolved_market(self):
        activities = [make_valid_activity("YES", offset_seconds=10)]
        resolved_market = make_market(resolved=True)
        with patch("requests.get", side_effect=self._mock_get(activities, resolved_market)):
            trades = self.monitor.poll()

        self.assertEqual(len(trades), 0)

    # Test 7: low liquidity market filtered out
    def test_poll_filters_low_liquidity(self):
        activities = [make_valid_activity("YES", offset_seconds=10)]
        thin_market = make_market(liquidity=100)  # < 500 threshold
        with patch("requests.get", side_effect=self._mock_get(activities, thin_market)):
            trades = self.monitor.poll()

        self.assertEqual(len(trades), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
