from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import db
from config import Config
from signal_engine import SignalEngine, SignalResult
from utils import log


def _confidence_label(score: int) -> str:
    if score >= 85:
        return "very high"
    if score >= 70:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


@dataclass
class Decision:
    action: str                        # "copy" or "skip"
    side: Optional[str]                # "YES" or "NO" (None if skip)
    size_usdc: Optional[float]         # None if skip
    signal_score: int
    confidence_label: str
    skip_reason: Optional[str]
    reasoning: str
    trade: object
    wallet_score: object
    signal_result: SignalResult


class DecisionEngine:
    def __init__(self, config: Config, signal_engine: SignalEngine):
        self.config = config
        self.signal_engine = signal_engine

    def _skip(self, reason: str, trade, wallet_score, signal_result: SignalResult) -> Decision:
        log.info(
            "decision",
            action="skip",
            wallet=getattr(trade, "wallet_label", "?"),
            score=signal_result.final_score,
            reason=reason,
            market=trade.question[:55],
        )
        return Decision(
            action="skip",
            side=None,
            size_usdc=None,
            signal_score=signal_result.final_score,
            confidence_label=_confidence_label(signal_result.final_score),
            skip_reason=reason,
            reasoning=signal_result.reasoning,
            trade=trade,
            wallet_score=wallet_score,
            signal_result=signal_result,
        )

    def evaluate(self, trade, wallet_score, available_usdc: float) -> Decision:
        open_positions = db.get_open_positions()

        # Compute signals first (needed for all decision paths)
        signal_result = self.signal_engine.compute(
            trade, wallet_score, open_positions, available_usdc
        ) if wallet_score is not None else SignalResult(
            final_score=0, signals=[], reasoning="No wallet data available."
        )

        # GATE 1 — Wallet data available
        if wallet_score is None:
            return self._skip("no_wallet_data", trade, wallet_score, signal_result)

        # GATE 2 — Wallet minimum quality
        if (wallet_score.win_rate < self.config.min_wallet_win_rate or
                wallet_score.total_bets < self.config.min_wallet_bets):
            return self._skip("wallet_below_threshold", trade, wallet_score, signal_result)

        # GATE 3 — Minimum signal score
        if signal_result.final_score < self.config.min_signal_score:
            return self._skip("signal_score_too_low", trade, wallet_score, signal_result)

        # GATE 4 — Market timing
        now = datetime.now(tz=timezone.utc)
        closes_at = trade.closes_at
        if closes_at.tzinfo is None:
            closes_at = closes_at.replace(tzinfo=timezone.utc)
        hours_remaining = (closes_at - now).total_seconds() / 3600
        days_remaining = hours_remaining / 24
        if hours_remaining < 3 or days_remaining > 90:
            return self._skip("market_timing", trade, wallet_score, signal_result)

        # GATE 5 — Price sanity
        if trade.price < 0.04 or trade.price > 0.96:
            return self._skip("price_out_of_range", trade, wallet_score, signal_result)

        # GATE 6 — Duplicate position
        for pos in open_positions:
            if pos.get("market_id") == trade.market_id and pos.get("side") == trade.side:
                return self._skip("duplicate_position", trade, wallet_score, signal_result)

        # GATE 7 — Portfolio exposure cap
        total_exposure = sum(float(p.get("size_usdc", 0) or 0) for p in open_positions)
        if total_exposure >= self.config.max_portfolio_exposure_usdc:
            return self._skip("max_exposure_reached", trade, wallet_score, signal_result)

        # GATE 8 — Minimum capital
        if available_usdc < 25.0:
            return self._skip("insufficient_capital", trade, wallet_score, signal_result)

        # POSITION SIZING
        base_size = trade.size_usdc * self.config.copy_ratio
        cap_by_max = self.config.max_position_size_usdc
        cap_by_pct = available_usdc * 0.12
        cap_by_room = self.config.max_portfolio_exposure_usdc - total_exposure
        final_size = round(min(base_size, cap_by_max, cap_by_pct, cap_by_room), 2)

        if final_size < 10.0:
            return self._skip("position_too_small", trade, wallet_score, signal_result)

        log.info(
            "decision",
            action="copy",
            wallet=getattr(trade, "wallet_label", "?"),
            score=signal_result.final_score,
            size=f"${final_size}",
            market=trade.question[:55],
        )

        return Decision(
            action="copy",
            side=trade.side,
            size_usdc=final_size,
            signal_score=signal_result.final_score,
            confidence_label=_confidence_label(signal_result.final_score),
            skip_reason=None,
            reasoning=signal_result.reasoning,
            trade=trade,
            wallet_score=wallet_score,
            signal_result=signal_result,
        )
