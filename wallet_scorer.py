import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import numpy as np
import requests

import db
from config import Config
from utils import log, retry_with_backoff, clamp

DATA_API_URL = "https://data-api.polymarket.com/activity"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

SCORE_TTL_HOURS = 6
LOOKBACK_DAYS = 90
MIN_RESOLVED_TRADES = 10
MAX_TRADES_TO_FETCH = 500
PAGE_SIZE = 100


@dataclass
class WalletScore:
    wallet_address: str
    win_rate: float
    total_bets: int
    avg_roi: float
    consistency_score: float
    avg_bet_size: float
    market_categories: str
    hot_streak: int
    recency_weight: float
    composite_score: float
    last_updated: datetime


class WalletScorer:
    def __init__(self, config: Config):
        self.config = config
        self._market_cache: dict = {}

    @retry_with_backoff(max_retries=3, base_delay=2)
    def _fetch_activity_page(self, address: str, offset: int) -> List[dict]:
        resp = requests.get(
            DATA_API_URL,
            params={"user": address, "limit": PAGE_SIZE, "offset": offset},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_all_activity(self, address: str) -> List[dict]:
        all_activities = []
        offset = 0
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)

        while len(all_activities) < MAX_TRADES_TO_FETCH:
            page = self._fetch_activity_page(address, offset)
            if not page:
                break
            all_activities.extend(page)
            if len(page) < PAGE_SIZE:
                break
            # Stop paginating if oldest item on this page is beyond the 90-day window
            oldest_ts = page[-1].get("timestamp") or page[-1].get("createdAt") or ""
            if oldest_ts:
                try:
                    oldest_dt = datetime.fromisoformat(
                        oldest_ts.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    if oldest_dt < cutoff:
                        break
                except ValueError:
                    pass
            offset += PAGE_SIZE
            time.sleep(0.5)

        return all_activities

    @retry_with_backoff(max_retries=3, base_delay=2)
    def _fetch_market(self, condition_id: str) -> Optional[dict]:
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]
        resp = requests.get(
            GAMMA_API_URL,
            params={"id": condition_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        market = None
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict):
            market = data
        if market:
            self._market_cache[condition_id] = market
        return market

    def _parse_ts(self, ts: str) -> Optional[datetime]:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    def _filter_to_window(self, activities: List[dict]) -> List[dict]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        result = []
        for a in activities:
            ts_raw = a.get("timestamp") or a.get("createdAt") or ""
            dt = self._parse_ts(ts_raw)
            if dt and dt >= cutoff:
                result.append(a)
        return result

    def _determine_outcome(self, activity: dict, market: dict) -> Optional[bool]:
        """Returns True if the bet won, False if lost, None if unresolvable."""
        if not market.get("resolved", False):
            return None
        wallet_side = activity.get("outcome", "").upper()
        if wallet_side not in ("YES", "NO"):
            return None
        resolved_yes = market.get("resolvedYes", market.get("resolved_yes"))
        if resolved_yes is None:
            return None
        winning_side = "YES" if resolved_yes else "NO"
        return wallet_side == winning_side

    def score_wallet(self, wallet_address: str) -> Optional[WalletScore]:
        try:
            raw_activities = self._fetch_all_activity(wallet_address)
        except requests.exceptions.RequestException as e:
            log.warning("scorer_fetch_failed", wallet=wallet_address, error=str(e))
            return None

        trades = [
            a for a in self._filter_to_window(raw_activities)
            if a.get("type") == "trade"
        ]

        if not trades:
            log.warning("scorer_no_trades", wallet=wallet_address)
            return None

        total_bets = len(trades)
        now = datetime.now(tz=timezone.utc)
        cutoff_30 = now - timedelta(days=30)

        # --- Per-trade resolution data ---
        resolved_outcomes: List[tuple] = []  # (won: bool, roi: float, dt: datetime, category: str)
        bet_sizes: List[float] = []
        categories: List[str] = []

        for trade in trades:
            ts_raw = trade.get("timestamp") or trade.get("createdAt") or ""
            dt = self._parse_ts(ts_raw)
            size = float(trade.get("usdcSize", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            condition_id = trade.get("conditionId", "")
            category = trade.get("category", "unknown")

            if size > 0:
                bet_sizes.append(size)
            if category:
                categories.append(category)

            if not condition_id:
                continue

            try:
                market = self._fetch_market(condition_id)
                time.sleep(0.5)
            except requests.exceptions.RequestException as e:
                log.warning("scorer_market_fetch_failed", condition_id=condition_id, error=str(e))
                continue

            if market is None:
                continue

            # Pull category from market if not on trade activity
            if category == "unknown":
                category = market.get("category", market.get("groupItemTagged", "unknown"))
                categories.append(category)

            outcome = self._determine_outcome(trade, market)
            if outcome is None:
                continue

            roi = (1.0 - price) / price if outcome and price > 0 else -1.0
            resolved_outcomes.append((outcome, roi, dt, category))

        # --- Win rate ---
        if len(resolved_outcomes) < MIN_RESOLVED_TRADES:
            log.warning(
                "scorer_insufficient_data",
                wallet=wallet_address,
                resolved=len(resolved_outcomes),
                required=MIN_RESOLVED_TRADES,
            )
            return None

        wins = sum(1 for won, *_ in resolved_outcomes if won)
        win_rate = wins / len(resolved_outcomes)

        # --- avg_roi ---
        avg_roi = float(np.mean([roi for _, roi, *_ in resolved_outcomes]))

        # --- avg_bet_size ---
        avg_bet_size = float(np.mean(bet_sizes)) if bet_sizes else 0.0

        # --- market_categories ---
        cat_counter = Counter(categories)
        top_categories = ",".join(cat for cat, _ in cat_counter.most_common(3))

        # --- consistency_score ---
        bucket_win_rates = []
        for bucket_idx in range(3):
            bucket_start = now - timedelta(days=30 * (bucket_idx + 1))
            bucket_end = now - timedelta(days=30 * bucket_idx)
            bucket = [
                won for won, _, dt, _ in resolved_outcomes
                if bucket_start <= dt < bucket_end
            ]
            if len(bucket) >= 3:
                bucket_win_rates.append(sum(bucket) / len(bucket))

        if len(bucket_win_rates) >= 2:
            consistency_score = float(clamp(1.0 - float(np.std(bucket_win_rates)), 0.0, 1.0))
        elif len(bucket_win_rates) == 1:
            consistency_score = bucket_win_rates[0]
        else:
            consistency_score = 0.5

        # --- hot_streak ---
        sorted_resolved = sorted(resolved_outcomes, key=lambda x: x[2], reverse=True)
        hot_streak = 0
        for won, _, _, _ in sorted_resolved:
            if won:
                hot_streak += 1
            else:
                break

        # --- recency_weight ---
        bets_last_30 = sum(
            1 for t in trades
            if (self._parse_ts(t.get("timestamp") or t.get("createdAt") or "") or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff_30
        )
        recency_weight = bets_last_30 / max(total_bets, 1)

        # --- composite_score ---
        roi_factor = clamp(avg_roi / 0.6, 0.0, 1.0)
        streak_factor = clamp(hot_streak / 5.0, 0.0, 1.0)
        composite_score = (
            win_rate          * 0.35 +
            consistency_score * 0.25 +
            roi_factor        * 0.20 +
            recency_weight    * 0.10 +
            streak_factor     * 0.10
        )

        score = WalletScore(
            wallet_address=wallet_address,
            win_rate=win_rate,
            total_bets=total_bets,
            avg_roi=avg_roi,
            consistency_score=consistency_score,
            avg_bet_size=avg_bet_size,
            market_categories=top_categories,
            hot_streak=hot_streak,
            recency_weight=recency_weight,
            composite_score=composite_score,
            last_updated=now,
        )

        db.upsert_wallet_score({
            "wallet_address": wallet_address,
            "win_rate": win_rate,
            "total_bets": total_bets,
            "avg_roi": avg_roi,
            "consistency_score": consistency_score,
            "avg_bet_size": avg_bet_size,
            "market_categories": top_categories,
            "hot_streak": hot_streak,
            "last_updated": now.isoformat(),
        })

        log.info(
            "wallet_scored",
            wallet=wallet_address,
            composite=f"{composite_score:.3f}",
            win_rate=f"{win_rate:.2%}",
            bets=total_bets,
            streak=hot_streak,
        )
        return score

    def get_score(self, wallet_address: str) -> Optional[WalletScore]:
        row = db.get_wallet_score(wallet_address)
        if row is None:
            return None
        last_updated = datetime.fromisoformat(row["last_updated"]).replace(tzinfo=timezone.utc) \
            if row.get("last_updated") else datetime.min.replace(tzinfo=timezone.utc)
        return WalletScore(
            wallet_address=row["wallet_address"],
            win_rate=row["win_rate"] or 0.0,
            total_bets=row["total_bets"] or 0,
            avg_roi=row["avg_roi"] or 0.0,
            consistency_score=row["consistency_score"] or 0.0,
            avg_bet_size=row["avg_bet_size"] or 0.0,
            market_categories=row["market_categories"] or "",
            hot_streak=row["hot_streak"] or 0,
            recency_weight=0.0,   # not persisted; recalculated on next score_wallet call
            composite_score=0.0,  # not persisted; recalculated on next score_wallet call
            last_updated=last_updated,
        )

    def _is_stale(self, wallet_address: str) -> bool:
        row = db.get_wallet_score(wallet_address)
        if row is None or not row.get("last_updated"):
            return True
        try:
            last_updated = datetime.fromisoformat(row["last_updated"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return (datetime.now(tz=timezone.utc) - last_updated).total_seconds() > SCORE_TTL_HOURS * 3600

    def refresh_all(self, wallet_addresses: List[str]) -> None:
        results = []
        for address in wallet_addresses:
            if not self._is_stale(address):
                log.info("scorer_skip_fresh", wallet=address)
                continue
            try:
                score = self.score_wallet(address)
                if score:
                    results.append(score)
            except Exception as e:
                log.warning("scorer_wallet_error", wallet=address, error=str(e))

        if results:
            log.info("scorer_refresh_summary", total=len(results))
            for s in sorted(results, key=lambda x: x.composite_score, reverse=True):
                log.info(
                    "scorer_summary_row",
                    wallet=s.wallet_address,
                    composite=f"{s.composite_score:.3f}",
                    win_rate=f"{s.win_rate:.2%}",
                    streak=s.hot_streak,
                    bets=s.total_bets,
                )
