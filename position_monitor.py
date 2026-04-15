import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

import db
from config import Config
from utils import log, format_usdc

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"


@dataclass
class ExitResult:
    position_id: int
    market_id: str
    question: str
    exit_reason: str   # "take_profit_strong" / "take_profit_pct" / "time_decay" / "stop_loss"
    entry_price: float
    exit_price: float
    pnl_usdc: float
    pnl_pct: float
    success: bool
    dry_run: bool


class PositionMonitor:
    def __init__(self, config: Config, clob_client, alerts):
        self.config = config
        self._client = clob_client  # may be None in dry-run
        self.alerts = alerts

    def _get_current_price(self, token_id: str, condition_id: str, side: str) -> Optional[float]:
        """Fetch current price. Uses CLOB client if available, falls back to Gamma API."""
        if self._client is not None:
            try:
                price = self._client.get_price(token_id, side="BUY")
                return float(price)
            except Exception as e:
                log.warning("position_monitor_clob_price_failed", token_id=token_id[:12] + "...", error=str(e))

        # Fallback: Gamma API outcomePrices
        if not condition_id:
            return None
        try:
            resp = requests.get(GAMMA_API_URL, params={"condition_ids": condition_id}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            market = data[0] if isinstance(data, list) and data else None
            if not market:
                return None
            import json as _json
            raw = market.get("outcomePrices", "")
            prices = _json.loads(raw) if isinstance(raw, str) else raw
            if len(prices) < 2:
                return None
            # YES = index 0, NO = index 1
            idx = 0 if side == "YES" else 1
            return float(prices[idx])
        except Exception as e:
            log.warning("position_monitor_gamma_price_failed", condition_id=(condition_id or "")[:12] + "...", error=str(e))
            return None

    def _evaluate_exit(self, pos: dict, current_price: float) -> Optional[str]:
        """Returns exit reason string if a condition is met, else None."""
        entry = float(pos.get("entry_price") or 0)
        closes_at_raw = pos.get("closes_at")
        hours_remaining = None
        if closes_at_raw:
            try:
                closes_at = datetime.fromisoformat(str(closes_at_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                hours_remaining = (closes_at - datetime.now(tz=timezone.utc)).total_seconds() / 3600
            except Exception:
                pass

        # Condition 1 — Strong take-profit: moved 25 cents in our favour
        if current_price >= entry + 0.25:
            return "take_profit_strong"

        # Condition 2 — Scaled take-profit: up TAKE_PROFIT_PCT% from entry
        if entry > 0:
            profit_pct = (current_price - entry) / entry
            if profit_pct >= self.config.take_profit_pct:
                return "take_profit_pct"

        # Condition 3 — Time-decay: market closes soon and we're in profit
        if hours_remaining is not None and hours_remaining <= 6 and current_price >= entry + 0.10:
            return "time_decay"

        # Condition 4 — Stop-loss: down STOP_LOSS_THRESHOLD cents from entry
        if current_price <= entry - self.config.stop_loss_threshold:
            return "stop_loss"

        return None

    def _execute_exit(self, pos: dict, current_price: float, exit_reason: str) -> bool:
        """Place a market sell order. Returns True on success (or dry-run)."""
        if self.config.dry_run:
            size_usdc = float(pos.get("size_usdc") or 0)
            entry = float(pos.get("entry_price") or 0)
            tokens_held = size_usdc / entry if entry > 0 else 0
            proceeds = tokens_held * current_price
            pnl = proceeds - size_usdc
            log.info(
                "dry_run_exit",
                question=(pos.get("question") or "")[:55],
                entry=f"{entry:.2f}",
                current=f"{current_price:.2f}",
                reason=exit_reason,
                pnl=f"${pnl:+.2f}",
            )
            return True

        if self._client is None:
            log.warning("position_monitor_no_client")
            return False

        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        size_usdc = float(pos.get("size_usdc") or 0)
        entry = float(pos.get("entry_price") or 0)
        token_id = pos.get("token_id", "")
        tokens_held = size_usdc / entry if entry > 0 else 0

        try:
            order = MarketOrderArgs(token_id=token_id, amount=tokens_held, side=SELL)
            signed = self._client.create_market_order(order)
            response = self._client.post_order(signed, OrderType.FOK)
            log.debug("exit_order_response", response=str(response))
            return True
        except Exception as e:
            log.error("position_monitor_sell_failed", token_id=token_id[:12] + "...", error=str(e))
            return False

    def check_all_positions(self) -> List[ExitResult]:
        positions = db.get_open_positions()
        if not positions:
            return []

        log.debug("position_monitor_check", open_positions=len(positions))
        results: List[ExitResult] = []

        for pos in positions:
            token_id = pos.get("token_id", "")
            condition_id = pos.get("condition_id", "")
            side = pos.get("side", "YES")
            entry = float(pos.get("entry_price") or 0)
            size_usdc = float(pos.get("size_usdc") or 0)
            question = pos.get("question") or pos.get("market_id", "Unknown")

            current_price = self._get_current_price(token_id, condition_id, side)
            if current_price is None:
                log.warning("position_monitor_no_price", position_id=pos["id"], token_id=token_id[:12] + "...")
                time.sleep(0.5)
                continue

            log.debug("position_monitor_price", position_id=pos["id"],
                      entry=entry, current=current_price, question=question[:40])

            exit_reason = self._evaluate_exit(pos, current_price)
            if exit_reason is None:
                time.sleep(0.5)
                continue

            # Calculate P&L
            tokens_held = size_usdc / entry if entry > 0 else 0
            proceeds = tokens_held * current_price
            pnl = round(proceeds - size_usdc, 2)
            pnl_pct = round((pnl / size_usdc) * 100, 1) if size_usdc > 0 else 0.0

            success = self._execute_exit(pos, current_price, exit_reason)

            if success:
                db.close_position(pos["id"], pnl, datetime.now(tz=timezone.utc).isoformat(), current_price)
                log.info(
                    "position_exited",
                    position_id=pos["id"],
                    reason=exit_reason,
                    entry=f"{entry:.2f}",
                    exit=f"{current_price:.2f}",
                    pnl=f"${pnl:+.2f}",
                    pnl_pct=f"{pnl_pct:+.1f}%",
                    question=question[:55],
                )

            result = ExitResult(
                position_id=pos["id"],
                market_id=pos.get("market_id", ""),
                question=question,
                exit_reason=exit_reason,
                entry_price=entry,
                exit_price=current_price,
                pnl_usdc=pnl,
                pnl_pct=pnl_pct,
                success=success,
                dry_run=self.config.dry_run,
            )
            results.append(result)
            self.alerts.send_exit_alert(result)
            time.sleep(0.5)

        return results
