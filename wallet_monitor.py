import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional

import requests

from config import Config
from utils import log, retry_with_backoff

DATA_API_URL = "https://data-api.polymarket.com/activity"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

MARKET_CACHE_TTL_SECONDS = 300  # 5 minutes

MIN_TRADE_SIZE_USDC = 10.0
MIN_LIQUIDITY_USDC = 500.0
MIN_HOURS_TO_CLOSE = 3
MAX_DAYS_TO_CLOSE = 120


@dataclass
class NewTrade:
    wallet_address: str
    wallet_label: str
    market_id: str
    condition_id: str
    token_id: str
    question: str
    category: str
    side: str
    size_usdc: float
    price: float
    closes_at: datetime
    liquidity_usdc: float
    detected_at: datetime


class WalletMonitor:
    def __init__(self, config: Config):
        self.config = config
        self._last_seen: Dict[str, str] = {}  # wallet_address -> ISO timestamp
        self._market_cache: Dict[str, dict] = {}  # condition_id -> market data
        self._market_cache_ts: Dict[str, datetime] = {}  # condition_id -> cached at
        self._poll_count = 0

    def _load_wallets(self) -> List[dict]:
        try:
            with open("wallets.json", "r") as f:
                data = json.load(f)
            wallets = data.get("wallets", [])
            if not wallets:
                log.warning("no_wallets_configured", file="wallets.json")
            return wallets
        except FileNotFoundError:
            log.warning("wallets_file_missing", file="wallets.json")
            return []
        except json.JSONDecodeError as e:
            log.warning("wallets_file_invalid", error=str(e))
            return []

    @retry_with_backoff(max_retries=3, base_delay=2)
    def _fetch_activity(self, wallet_address: str) -> List[dict]:
        resp = requests.get(
            DATA_API_URL,
            params={"user": wallet_address, "limit": 20},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    @retry_with_backoff(max_retries=3, base_delay=2)
    def _fetch_market_raw(self, condition_id: str) -> Optional[dict]:
        resp = requests.get(
            GAMMA_API_URL,
            params={"id": condition_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None

    def _get_market(self, condition_id: str) -> Optional[dict]:
        now = datetime.now(tz=timezone.utc)
        cached_at = self._market_cache_ts.get(condition_id)
        if cached_at and (now - cached_at).total_seconds() < MARKET_CACHE_TTL_SECONDS:
            log.debug("market_cache_hit", condition_id=condition_id)
            return self._market_cache.get(condition_id)

        market = self._fetch_market_raw(condition_id)
        if market:
            self._market_cache[condition_id] = market
            self._market_cache_ts[condition_id] = now
        return market

    def _parse_closes_at(self, market: dict) -> Optional[datetime]:
        for field in ("endDate", "end_date", "endDateIso", "end_date_iso"):
            raw = market.get(field)
            if raw:
                try:
                    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    return dt.astimezone(timezone.utc)
                except ValueError:
                    pass
        return None

    def _is_market_valid(self, market: dict) -> tuple[bool, str]:
        if market.get("resolved", False):
            return False, "market_resolved"

        closes_at = self._parse_closes_at(market)
        if closes_at is None:
            return False, "no_end_date"

        now = datetime.now(tz=timezone.utc)
        hours_remaining = (closes_at - now).total_seconds() / 3600
        days_remaining = hours_remaining / 24

        if hours_remaining <= MIN_HOURS_TO_CLOSE:
            return False, f"closes_too_soon ({hours_remaining:.1f}h)"
        if days_remaining > MAX_DAYS_TO_CLOSE:
            return False, f"closes_too_far ({days_remaining:.0f}d)"

        liquidity = float(market.get("liquidity", market.get("liquidityNum", 0)) or 0)
        if liquidity < MIN_LIQUIDITY_USDC:
            return False, f"low_liquidity (${liquidity:.0f})"

        return True, ""

    def _process_activity(self, activity: dict, wallet: dict, market: dict) -> Optional[NewTrade]:
        closes_at = self._parse_closes_at(market)
        if closes_at is None:
            return None

        side = activity.get("outcome", "").upper()
        if side not in ("YES", "NO"):
            return None

        size_usdc = float(activity.get("usdcSize", 0) or 0)
        price = float(activity.get("price", 0) or 0)
        condition_id = activity.get("conditionId", "")
        token_id = activity.get("tokenId", activity.get("asset", ""))
        market_id = market.get("id", market.get("marketMakerAddress", condition_id))
        question = market.get("question", "Unknown")
        category = market.get("category", market.get("groupItemTagged", "unknown"))
        liquidity = float(market.get("liquidity", market.get("liquidityNum", 0)) or 0)

        return NewTrade(
            wallet_address=wallet["address"],
            wallet_label=wallet.get("label", wallet["address"][:8]),
            market_id=market_id,
            condition_id=condition_id,
            token_id=token_id,
            question=question,
            category=category,
            side=side,
            size_usdc=size_usdc,
            price=price,
            closes_at=closes_at,
            liquidity_usdc=liquidity,
            detected_at=datetime.now(tz=timezone.utc),
        )

    def _poll_wallet(self, wallet: dict) -> List[NewTrade]:
        address = wallet["address"]
        label = wallet.get("label", address[:8])
        new_trades: List[NewTrade] = []

        try:
            activities = self._fetch_activity(address)
        except requests.exceptions.RequestException as e:
            log.warning("wallet_fetch_failed", wallet=address, error=str(e))
            return []

        last_seen = self._last_seen.get(address)
        newest_ts: Optional[str] = last_seen

        for activity in activities:
            ts = activity.get("timestamp") or activity.get("createdAt") or ""

            if last_seen and ts and ts <= last_seen:
                continue

            if newest_ts is None or (ts and ts > newest_ts):
                newest_ts = ts

            if activity.get("type") != "trade":
                continue
            if activity.get("outcome", "").upper() not in ("YES", "NO"):
                continue
            size_usdc = float(activity.get("usdcSize", 0) or 0)
            if size_usdc < MIN_TRADE_SIZE_USDC:
                continue

            condition_id = activity.get("conditionId", "")
            if not condition_id:
                continue

            try:
                market = self._get_market(condition_id)
            except requests.exceptions.RequestException as e:
                log.warning("market_fetch_failed", condition_id=condition_id, error=str(e))
                continue

            if market is None:
                continue

            valid, reason = self._is_market_valid(market)
            if not valid:
                log.debug("trade_filtered", wallet=label, reason=reason)
                continue

            trade = self._process_activity(activity, wallet, market)
            if trade:
                log.info(
                    "new_trade_detected",
                    wallet=label,
                    question=trade.question[:50],
                    side=trade.side,
                    size=f"${trade.size_usdc:.2f}",
                    price=f"{trade.price:.3f}",
                )
                new_trades.append(trade)

        if newest_ts:
            self._last_seen[address] = newest_ts

        return new_trades

    def poll(self) -> List[NewTrade]:
        self._poll_count += 1
        wallets = self._load_wallets()
        log.info("poll_starting", poll=self._poll_count, wallets=len(wallets))

        all_new_trades: List[NewTrade] = []

        for i, wallet in enumerate(wallets):
            trades = self._poll_wallet(wallet)
            all_new_trades.extend(trades)
            if i < len(wallets) - 1:
                time.sleep(1.0)

        log.info("poll_complete", poll=self._poll_count, new_trades=len(all_new_trades))
        return all_new_trades

    def run_forever(self, callback: Callable[[NewTrade], None]) -> None:
        while True:
            trades = self.poll()
            for trade in trades:
                try:
                    callback(trade)
                except Exception as e:
                    log.warning("callback_error", error=str(e), trade=trade.market_id)
            time.sleep(self.config.poll_interval_seconds)
