import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import os
os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8, "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "key", "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "pass", "TELEGRAM_BOT_TOKEN": "tok",
    "TELEGRAM_CHAT_ID": "123",
})

import db
db.DB_PATH = "./test_scorer.db"
db.init_db()

from config import load_config
from wallet_scorer import WalletScorer


def make_config():
    cfg = MagicMock()
    return cfg


def _ts(days_ago=0, hours_ago=0):
    """Return ISO timestamp N days/hours ago."""
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat()


def make_activity(outcome="YES", size=100, price=0.55, days_ago=1, condition_id="cond1"):
    return {
        "type": "trade",
        "outcome": outcome,
        "usdcSize": str(size),
        "price": str(price),
        "conditionId": condition_id,
        "tokenId": "tok1",
        "category": "crypto",
        "timestamp": _ts(days_ago=days_ago),
    }


def make_resolved_market(resolved_yes=True, condition_id="cond1"):
    return [{
        "id": "mkt1",
        "question": "Will BTC hit 100k?",
        "category": "crypto",
        "liquidity": 5000,
        "resolved": True,
        "resolvedYes": resolved_yes,
        "endDate": _ts(days_ago=-30),
        "conditionId": condition_id,
    }]


class TestWalletScorer(unittest.TestCase):

    def setUp(self):
        self.scorer = WalletScorer(make_config())

    def _mock_get(self, activities, market_fn=None):
        """Return a mock requests.get side_effect."""
        call_count = {"n": 0}

        def side_effect(url, params=None, timeout=None):
            r = MagicMock()
            r.raise_for_status = lambda: None
            if "activity" in url:
                # First page returns activities, second returns []
                call_count["n"] += 1
                if call_count["n"] == 1:
                    r.json.return_value = activities
                else:
                    r.json.return_value = []
            else:
                cid = (params or {}).get("id", "cond1")
                if market_fn:
                    r.json.return_value = market_fn(cid)
                else:
                    r.json.return_value = make_resolved_market(resolved_yes=True, condition_id=cid)
            return r

        return side_effect

    # Test 1: win_rate with 6 wins and 4 losses = 0.6
    def test_win_rate_6_wins_4_losses(self):
        activities = (
            [make_activity("YES", days_ago=i, condition_id=f"cond{i}") for i in range(1, 7)] +
            [make_activity("NO", days_ago=i+6, condition_id=f"cond{i+6}") for i in range(1, 5)]
        )

        def market_fn(cid):
            idx = int(cid.replace("cond", ""))
            # YES bets (cond1-6) on YES-resolving markets = win
            # NO bets (cond7-10) on YES-resolving markets = loss
            return [{"id": "mkt1", "question": "Q?", "category": "crypto",
                     "liquidity": 5000, "resolved": True,
                     "resolvedYes": True,
                     "endDate": _ts(days_ago=-30)}]

        with patch("requests.get", side_effect=self._mock_get(activities, market_fn)):
            score = self.scorer.score_wallet("0xabc")

        self.assertIsNotNone(score)
        self.assertAlmostEqual(score.win_rate, 0.6, places=5)

    # Test 2: consistency_score = 1.0 when win rate identical across all buckets
    def test_consistency_score_perfect_when_equal_buckets(self):
        # 3 wins per bucket (one per 30-day bucket), spread across 90 days
        activities = []
        for bucket in range(3):
            for i in range(4):  # 4 per bucket (3 wins + 1 loss each = 0.75 each)
                days = bucket * 30 + i + 1
                activities.append(make_activity("YES", days_ago=days, condition_id=f"cond_{bucket}_{i}"))
            # 1 loss per bucket
            activities.append(make_activity("NO", days_ago=bucket * 30 + 5, condition_id=f"condL_{bucket}"))

        def market_fn(cid):
            return [{"id": "mkt1", "question": "Q?", "category": "crypto",
                     "liquidity": 5000, "resolved": True, "resolvedYes": True,
                     "endDate": _ts(days_ago=-30)}]

        with patch("requests.get", side_effect=self._mock_get(activities, market_fn)):
            score = self.scorer.score_wallet("0xabc")

        self.assertIsNotNone(score)
        # All buckets same win rate → std = 0 → consistency = 1.0
        self.assertAlmostEqual(score.consistency_score, 1.0, places=5)

    # Test 3: hot_streak counts consecutive wins from most recent
    def test_hot_streak_counts_from_most_recent(self):
        # Most recent 4 trades = wins (days 1-4), then a loss (day 5)
        activities = [
            make_activity("YES", days_ago=1, condition_id="c1"),
            make_activity("YES", days_ago=2, condition_id="c2"),
            make_activity("YES", days_ago=3, condition_id="c3"),
            make_activity("YES", days_ago=4, condition_id="c4"),
            make_activity("NO",  days_ago=5, condition_id="c5"),
            # Add more wins after the loss for minimum trade count
            make_activity("YES", days_ago=6, condition_id="c6"),
            make_activity("YES", days_ago=7, condition_id="c7"),
            make_activity("YES", days_ago=8, condition_id="c8"),
            make_activity("YES", days_ago=9, condition_id="c9"),
            make_activity("YES", days_ago=10, condition_id="c10"),
        ]

        def market_fn(cid):
            idx = int(cid[1:])
            # cid c5 = NO bet on YES-resolving market = loss
            return [{"id": "mkt1", "question": "Q?", "category": "crypto",
                     "liquidity": 5000, "resolved": True, "resolvedYes": True,
                     "endDate": _ts(days_ago=-30)}]

        with patch("requests.get", side_effect=self._mock_get(activities, market_fn)):
            score = self.scorer.score_wallet("0xabc")

        self.assertIsNotNone(score)
        self.assertEqual(score.hot_streak, 4)

    # Test 4: composite_score between 0.0 and 1.0
    def test_composite_score_in_valid_range(self):
        activities = [
            make_activity("YES", days_ago=i, condition_id=f"c{i}") for i in range(1, 11)
        ]
        with patch("requests.get", side_effect=self._mock_get(activities)):
            score = self.scorer.score_wallet("0xabc")

        self.assertIsNotNone(score)
        self.assertGreaterEqual(score.composite_score, 0.0)
        self.assertLessEqual(score.composite_score, 1.0)

    # Test 5: returns None when fewer than 10 resolved trades
    def test_returns_none_when_insufficient_resolved_trades(self):
        activities = [
            make_activity("YES", days_ago=i, condition_id=f"c{i}") for i in range(1, 6)
        ]
        # Markets are unresolved → no resolved outcomes
        def market_fn(cid):
            return [{"id": "mkt1", "question": "Q?", "category": "crypto",
                     "liquidity": 5000, "resolved": False,
                     "endDate": _ts(days_ago=-30)}]

        with patch("requests.get", side_effect=self._mock_get(activities, market_fn)):
            score = self.scorer.score_wallet("0xabc")

        self.assertIsNone(score)

    def tearDown(self):
        self.scorer._market_cache.clear()


def tearDownModule():
    import os
    try:
        os.remove("./test_scorer.db")
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
