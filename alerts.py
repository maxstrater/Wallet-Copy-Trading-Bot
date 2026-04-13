from datetime import datetime, timezone

from telegram import Bot

import db
from config import Config
from utils import log, format_usdc, time_until, pct


class AlertManager:
    def __init__(self, config: Config):
        self.config = config
        self.chat_id = config.telegram_chat_id

    def _send(self, text: str) -> None:
        try:
            import asyncio
            async def _do_send():
                async with Bot(token=self.config.telegram_bot_token) as bot:
                    await bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode="HTML",
                    )
            asyncio.run(_do_send())
        except Exception as e:
            log.warning("telegram_send_failed", error=str(e))

    def send_copy_alert(self, decision, result) -> None:
        prefix = "[DRY RUN] " if self.config.dry_run else ""
        ws = decision.wallet_score
        trade = decision.trade

        win_rate_pct = pct(ws.win_rate) if ws else "N/A"
        total_bets = ws.total_bets if ws else 0

        closes_at_str = time_until(trade.closes_at.isoformat()) if hasattr(trade.closes_at, 'isoformat') else str(trade.closes_at)

        status_line = "✓ FILLED" if result.success else "✗ FAILED"
        detail_line = result.tx_hash if result.success else f"Error: {result.error}"

        text = (
            f"{prefix}✅ <b>COPIED</b> — Score: {decision.signal_score}/100 ({decision.confidence_label})\n"
            f"\n"
            f"📋 <b>{trade.question[:80]}</b>\n"
            f"👤 Copying: {trade.wallet_label} ({win_rate_pct} win rate, {total_bets} bets)\n"
            f"📊 Side: <b>{decision.side}</b> @ ${trade.price:.2f} | Size: ${decision.size_usdc:.2f} USDC\n"
            f"⏰ Closes: {closes_at_str}\n"
            f"\n"
            f"💡 {decision.reasoning}\n"
            f"\n"
            f"Status: {status_line}\n"
            f"{detail_line}"
        )
        self._send(text)

    def send_skip_alert(self, decision) -> None:
        if decision.signal_score < 50:
            return

        trade = decision.trade
        text = (
            f"⏭ <b>SKIPPED</b> — Score: {decision.signal_score}/100\n"
            f"\n"
            f"📋 {trade.question[:80]}\n"
            f"👤 Wallet: {trade.wallet_label}\n"
            f"❌ Reason: {decision.skip_reason}\n"
            f"💡 {decision.reasoning}"
        )
        self._send(text)

    def send_daily_summary(self) -> None:
        summary = db.get_daily_summary()
        copies = summary.get("total_copied") or 0
        skips = summary.get("total_skipped") or 0

        open_positions = db.get_open_positions()
        total_exposure = sum(float(p.get("size_usdc", 0) or 0) for p in open_positions)
        unrealised_pnl = sum(float(p.get("pnl_usdc", 0) or 0) for p in open_positions)

        settled = db.get_settled_pnl_summary(hours=24)
        settled_count = settled.get("settled_count") or 0
        realised_pnl = float(settled.get("total_pnl") or 0)
        winners = settled.get("winners") or 0
        losers = settled.get("losers") or 0

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        text = (
            f"📊 <b>Daily Summary — {date_str}</b>\n"
            f"\n"
            f"Trades copied: {copies}\n"
            f"Trades skipped: {skips}\n"
            f"\n"
            f"Open positions: {len(open_positions)}\n"
            f"Portfolio exposure: {format_usdc(total_exposure)}\n"
            f"Unrealised P&L: {format_usdc(unrealised_pnl)}\n"
        )
        if settled_count > 0:
            text += (
                f"\n"
                f"Settled today: {settled_count} ({winners}W / {losers}L)\n"
                f"Realised P&L: <b>{'+' if realised_pnl >= 0 else ''}{format_usdc(realised_pnl)}</b>\n"
            )
        text += f"\nBot status: Running ✓"
        self._send(text)

    def send_position_closed_alert(self, pos) -> None:
        won_emoji = "✅" if pos.won else "❌"
        result_label = "WIN" if pos.won else "LOSS"
        pnl_str = f"{'+' if pos.pnl_usdc >= 0 else ''}{format_usdc(pos.pnl_usdc)}"
        text = (
            f"[DRY RUN] 🏁 <b>Position Settled</b> — {won_emoji}\n"
            f"\n"
            f"📋 <b>{pos.question[:80]}</b>\n"
            f"📊 Side: <b>{pos.side}</b> | Entry: ${pos.entry_price:.2f} | Size: {format_usdc(pos.size_usdc)}\n"
            f"💰 P&amp;L: <b>{pnl_str}</b>  {result_label} {won_emoji}"
        )
        self._send(text)

    def send_startup_alert(self, wallet_count: int) -> None:
        mode = "DRY RUN 🔶" if self.config.dry_run else "LIVE 🔴"
        text = (
            f"🤖 <b>Bot started</b>\n"
            f"Watching: {wallet_count} wallets\n"
            f"Mode: {mode}\n"
            f"Min signal score: {self.config.min_signal_score}/100\n"
            f"Min win rate: {pct(self.config.min_wallet_win_rate)}\n"
            f"Poll interval: every {self.config.poll_interval_seconds}s"
        )
        self._send(text)

    def send_error_alert(self, context: str, error: str) -> None:
        text = (
            f"🚨 <b>Bot Error</b>\n"
            f"Context: {context}\n"
            f"Error: {error}"
        )
        self._send(text)

    def send_api_warning(self, wallet_label: str, consecutive_failures: int, error: str = "") -> None:
        """Alert when a wallet's API calls have been failing repeatedly."""
        text = (
            f"⚠️ <b>API Warning</b>\n"
            f"Wallet <b>{wallet_label}</b> has failed {consecutive_failures} polls in a row.\n"
            f"The bot is still running but skipping this wallet.\n"
        )
        if error:
            text += f"Last error: {error}"
        self._send(text)
