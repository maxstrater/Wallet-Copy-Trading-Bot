import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

import db
from config import Config
from utils import log

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"


@dataclass
class ClosedPosition:
    position_id: int
    market_id: str
    condition_id: str
    question: str
    side: str
    size_usdc: float
    entry_price: float
    pnl_usdc: float
    won: bool
    closed_at: datetime


class PnlTracker:
    def __init__(self, config: Config):
        self.config = config

    def _fetch_market(self, condition_id: str) -> Optional[dict]:
        try:
            resp = requests.get(
                GAMMA_API_URL,
                params={"condition_ids": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
            return None
        except Exception as e:
            log.warning("pnl_tracker_api_error", condition_id=condition_id[:12] + "...", error=str(e))
            return None

    def _parse_outcome_prices(self, market: dict) -> Optional[tuple[float, float]]:
        """Returns (yes_price, no_price) or None if unparseable."""
        raw = market.get("outcomePrices")
        if not raw:
            return None
        try:
            if isinstance(raw, str):
                prices = json.loads(raw)
            else:
                prices = raw
            if len(prices) < 2:
                return None
            return float(prices[0]), float(prices[1])
        except Exception:
            return None

    def _calculate_pnl(self, won: bool, size_usdc: float, entry_price: float) -> float:
        if won:
            return round(size_usdc * (1.0 - entry_price) / entry_price, 2)
        return round(-size_usdc, 2)

    def check_resolutions(self) -> List[ClosedPosition]:
        positions = db.get_open_simulated_positions()
        if not positions:
            return []

        log.info("pnl_tracker_check", open_positions=len(positions))
        closed: List[ClosedPosition] = []
        market_cache: dict = {}

        for pos in positions:
            condition_id = pos.get("condition_id") or ""
            if not condition_id:
                log.warning("pnl_tracker_no_condition_id", position_id=pos["id"])
                continue

            if condition_id not in market_cache:
                market = self._fetch_market(condition_id)
                market_cache[condition_id] = market
                time.sleep(0.5)
            else:
                market = market_cache[condition_id]

            if market is None:
                continue

            # Only settle when market is fully closed and resolved
            if not market.get("closed", False):
                continue

            prices = self._parse_outcome_prices(market)
            if prices is None:
                log.warning("pnl_tracker_no_prices", condition_id=condition_id[:12] + "...")
                continue

            yes_price, no_price = prices

            # Need a definitive winner (price at 1.0 or very close)
            resolved_yes = yes_price >= 0.99
            resolved_no = no_price >= 0.99
            if not resolved_yes and not resolved_no:
                log.debug("pnl_tracker_ambiguous", condition_id=condition_id[:12] + "...",
                          yes=yes_price, no=no_price)
                continue

            side = pos.get("side", "")
            won = (side == "YES" and resolved_yes) or (side == "NO" and resolved_no)
            final_price = 1.0 if won else 0.0

            size_usdc = float(pos.get("size_usdc") or 0)
            entry_price = float(pos.get("entry_price") or 0)
            pnl = self._calculate_pnl(won, size_usdc, entry_price)

            closed_at = datetime.now(tz=timezone.utc)
            db.close_position(pos["id"], pnl, closed_at.isoformat(), final_price)

            result = ClosedPosition(
                position_id=pos["id"],
                market_id=pos.get("market_id", ""),
                condition_id=condition_id,
                question=pos.get("question") or pos.get("market_id", "Unknown market"),
                side=side,
                size_usdc=size_usdc,
                entry_price=entry_price,
                pnl_usdc=pnl,
                won=won,
                closed_at=closed_at,
            )
            closed.append(result)

            log.info(
                "position_settled",
                position_id=pos["id"],
                side=side,
                pnl=f"${pnl:+.2f}",
                won=won,
                question=result.question[:55],
            )

        if closed:
            log.info("pnl_tracker_settled", count=len(closed),
                     total_pnl=f"${sum(p.pnl_usdc for p in closed):+.2f}")

        return closed
