import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

import db
from config import Config
from utils import log

DB_PATH = db.DB_PATH


@dataclass
class ExecutionResult:
    success: bool
    tx_hash: Optional[str]
    filled_size: Optional[float]
    filled_price: Optional[float]
    error: Optional[str]
    timestamp: datetime


class Executor:
    def __init__(self, config: Config):
        self.config = config
        self.cloudflare_block_count = 0

        self._client = ClobClient(
            host="https://clob.polymarket.com",
            key=config.polymarket_pk,
            chain_id=137,
            signature_type=1,
            funder=config.polymarket_funder,
        )
        self._client.set_api_creds(ApiCreds(
            api_key=config.polymarket_api_key,
            api_secret=config.polymarket_api_secret,
            api_passphrase=config.polymarket_api_passphrase,
        ))
        log.debug("executor_initialized")

    def get_balance(self) -> float:
        log.debug("get_balance_enter")
        try:
            result = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance = float(result.get("balance", 0))
            log.debug("get_balance_exit", balance=balance)
            return balance
        except Exception as e:
            log.warning("get_balance_failed", error=str(e))
            return 0.0

    def execute(self, decision, trade_db_id: int) -> ExecutionResult:
        log.debug("execute_enter", trade_db_id=trade_db_id, side=decision.side, size=decision.size_usdc)

        # DRY RUN — must be checked first, no API calls
        if self.config.dry_run:
            log.info(
                "dry_run_order",
                side=decision.side,
                size=f"${decision.size_usdc}",
                question=decision.trade.question,
            )
            db.update_trade_tx(trade_db_id, "DRY_RUN")
            self._record_position(decision, filled_size=decision.size_usdc, filled_price=decision.trade.price)
            result = ExecutionResult(
                success=True,
                tx_hash="DRY_RUN",
                filled_size=decision.size_usdc,
                filled_price=decision.trade.price,
                error=None,
                timestamp=datetime.now(tz=timezone.utc),
            )
            log.debug("execute_exit", success=True, dry_run=True)
            return result

        # SAFETY INVARIANT
        try:
            assert decision.size_usdc <= self.config.max_position_size_usdc * 1.05, (
                f"Order size {decision.size_usdc} exceeds maximum. This is a bug."
            )
        except AssertionError as e:
            log.critical("safety_invariant_violated", error=str(e))
            return ExecutionResult(
                success=False, tx_hash=None, filled_size=None,
                filled_price=None, error="safety_invariant_violated",
                timestamp=datetime.now(tz=timezone.utc),
            )

        # BALANCE VERIFICATION
        balance = self.get_balance()
        if balance < decision.size_usdc:
            log.error(
                "insufficient_balance",
                have=f"${balance:.2f}",
                need=f"${decision.size_usdc:.2f}",
            )
            return ExecutionResult(
                success=False, tx_hash=None, filled_size=None,
                filled_price=None, error="insufficient_balance",
                timestamp=datetime.now(tz=timezone.utc),
            )
        log.debug("balance_check_ok", balance=balance, order_size=decision.size_usdc)

        # ORDER PLACEMENT
        side = BUY if decision.side == "YES" else SELL
        order_args = MarketOrderArgs(
            token_id=decision.trade.token_id,
            amount=decision.size_usdc,
            side=side,
        )

        try:
            signed_order = self._client.create_market_order(order_args)
        except Exception as e:
            log.error("create_order_failed", error=str(e), market=decision.trade.market_id)
            return ExecutionResult(
                success=False, tx_hash=None, filled_size=None,
                filled_price=None, error=f"create_order_failed: {e}",
                timestamp=datetime.now(tz=timezone.utc),
            )

        # POST ORDER with Cloudflare retry
        response = self._post_with_cf_retry(signed_order)
        if response is None:
            return ExecutionResult(
                success=False, tx_hash=None, filled_size=None,
                filled_price=None, error="cloudflare_blocked",
                timestamp=datetime.now(tz=timezone.utc),
            )

        log.debug("post_order_response", response=str(response))

        # Parse response
        tx_hash = response.get("orderID") or response.get("transactionHash") or response.get("id")
        filled_size = float(response.get("size_matched", decision.size_usdc) or decision.size_usdc)
        filled_price = float(response.get("price", decision.trade.price) or decision.trade.price)
        success = response.get("status") not in ("unmatched", "cancelled", None) or bool(tx_hash)

        if success:
            db.update_trade_tx(trade_db_id, tx_hash)
            self._record_position(decision, filled_size=filled_size, filled_price=filled_price)
            log.info(
                "order_filled",
                side=decision.side,
                size=f"${filled_size:.2f}",
                question=decision.trade.question,
                price=filled_price,
                tx_hash=tx_hash,
            )
        else:
            log.error(
                "order_failed",
                response=str(response),
                market=decision.trade.market_id,
                side=decision.side,
                size=decision.size_usdc,
            )

        result = ExecutionResult(
            success=success,
            tx_hash=tx_hash,
            filled_size=filled_size if success else None,
            filled_price=filled_price if success else None,
            error=None if success else f"unmatched_or_cancelled: {response}",
            timestamp=datetime.now(tz=timezone.utc),
        )
        log.debug("execute_exit", success=success)
        return result

    def _post_with_cf_retry(self, signed_order):
        for attempt in range(2):
            try:
                return self._client.post_order(signed_order, OrderType.FOK)
            except Exception as e:
                error_str = str(e)
                if "403" in error_str or "Cloudflare" in error_str or "cloudflare" in error_str:
                    log.warning("cloudflare_403", attempt=attempt + 1)
                    if attempt == 0:
                        time.sleep(5)
                        continue
                    # Second 403 — increment block counter
                    self.cloudflare_block_count += 1
                    if self.cloudflare_block_count >= 3:
                        log.error(
                            "persistent_cloudflare_blocking",
                            message="Persistent Cloudflare blocking. Consider a residential proxy. See README.md for instructions.",
                        )
                    return None
                else:
                    log.error("post_order_failed", attempt=attempt + 1, error=error_str)
                    return None
        return None

    def _record_position(self, decision, filled_size: float, filled_price: float):
        now = datetime.now(tz=timezone.utc).isoformat()
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    """
                    INSERT INTO positions (market_id, token_id, side, size_usdc,
                        entry_price, current_price, pnl_usdc, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0.0, ?)
                    """,
                    (
                        decision.trade.market_id,
                        decision.trade.token_id,
                        decision.side,
                        filled_size,
                        filled_price,
                        filled_price,
                        now,
                    ),
                )
                conn.commit()
        except Exception as e:
            log.warning("record_position_failed", error=str(e))
