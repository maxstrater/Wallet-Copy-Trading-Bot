from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from config import Config
from utils import log, clamp

SIGNALS_META = [
    ("wallet_quality",      25),
    ("price_efficiency",    20),
    ("bet_size_conviction", 15),
    ("time_value",          15),
    ("liquidity_depth",     10),
    ("hot_streak",          10),
    ("portfolio_fit",        5),
]


@dataclass
class SignalDetail:
    name: str
    value: float
    weight: int
    contribution: float
    description: str


@dataclass
class SignalResult:
    final_score: int
    signals: List[SignalDetail] = field(default_factory=list)
    reasoning: str = ""


class SignalEngine:
    def __init__(self, config: Config):
        self.config = config

    def _safe_signal(self, name: str, fn) -> tuple[float, str]:
        try:
            value, description = fn()
            return clamp(float(value), 0.0, 1.0), description
        except Exception as e:
            log.warning("signal_calc_error", signal=name, error=str(e))
            return 0.0, f"Error computing signal: {e}"

    def compute(self, trade, wallet_score, open_positions: list, available_usdc: float) -> SignalResult:
        def wallet_quality():
            v = wallet_score.composite_score
            return v, f"Composite wallet score {v:.3f} based on historical performance."

        def price_efficiency():
            v = 1.0 - abs(trade.price - 0.5) * 2
            return v, f"Price {trade.price:.3f} — {'near 0.5 (genuine uncertainty)' if v >= 0.7 else 'near extreme (market near resolution)'}."

        def bet_size_conviction():
            avg = wallet_score.avg_bet_size
            if avg <= 0:
                return 0.0, "No average bet size history available."
            ratio = trade.size_usdc / (avg * 2)
            v = clamp(ratio, 0.0, 1.0)
            return v, f"Bet ${trade.size_usdc:.0f} vs avg ${avg:.0f} — {trade.size_usdc/avg:.1f}x their average size."

        def time_value():
            now = datetime.now(tz=timezone.utc)
            closes_at = trade.closes_at
            if closes_at.tzinfo is None:
                closes_at = closes_at.replace(tzinfo=timezone.utc)
            hours_remaining = (closes_at - now).total_seconds() / 3600
            v = clamp((hours_remaining - 4) / (72 - 4), 0.0, 1.0)
            return v, f"{hours_remaining:.1f}h until market closes."

        def liquidity_depth():
            v = clamp(trade.liquidity_usdc / 10000, 0.0, 1.0)
            return v, f"Market liquidity ${trade.liquidity_usdc:,.0f} — {'deep' if v >= 0.8 else 'adequate' if v >= 0.4 else 'thin'}."

        def hot_streak():
            v = clamp(wallet_score.hot_streak / 5.0, 0.0, 1.0)
            return v, f"Wallet on {wallet_score.hot_streak}-trade winning streak."

        def portfolio_fit():
            exposure = sum(
                float(p.get("size_usdc", 0) or 0)
                for p in open_positions
                if p.get("market_id") == trade.market_id
            )
            v = 1.0 if exposure == 0 else 0.0
            return v, f"{'No existing position in this market.' if v == 1.0 else f'Already ${exposure:.0f} exposure in this market.'}"

        signal_fns = {
            "wallet_quality":      wallet_quality,
            "price_efficiency":    price_efficiency,
            "bet_size_conviction": bet_size_conviction,
            "time_value":          time_value,
            "liquidity_depth":     liquidity_depth,
            "hot_streak":          hot_streak,
            "portfolio_fit":       portfolio_fit,
        }

        details: List[SignalDetail] = []
        raw_score = 0.0

        for name, weight in SIGNALS_META:
            value, description = self._safe_signal(name, signal_fns[name])
            contribution = value * weight
            raw_score += contribution
            details.append(SignalDetail(
                name=name,
                value=value,
                weight=weight,
                contribution=contribution,
                description=description,
            ))

        final_score = round(raw_score)

        log.debug(
            "signal_breakdown",
            market=trade.market_id,
            score=final_score,
            signals={d.name: round(d.contribution, 2) for d in details},
        )

        result = SignalResult(final_score=final_score, signals=details)
        result.reasoning = self.generate_reasoning(result)
        return result

    def generate_reasoning(self, signal_result: SignalResult) -> str:
        signals = signal_result.signals
        sorted_by_contribution = sorted(signals, key=lambda s: s.contribution, reverse=True)
        top2 = sorted_by_contribution[:2]
        weakest = sorted_by_contribution[-1]

        def fmt(s: SignalDetail) -> str:
            return f"{s.name.replace('_', ' ')} ({s.contribution:.1f}/{s.weight}) — {s.description}"

        strong_parts = "; ".join(fmt(s) for s in top2)
        weak_part = fmt(weakest)

        return (
            f"Score {signal_result.final_score}/100. "
            f"Strong signals: {strong_parts}. "
            f"Weakest: {weak_part}"
        )
