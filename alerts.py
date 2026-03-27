from datetime import datetime, timezone

from telegram import Bot

import db
from config import Config
from utils import log, format_usdc, time_until, pct


class AlertManager:
    def __init__(self, config: Config):
        self.config = config
        self.bot = Bot(token=config.telegram_bot_token)
        self.chat_id = config.telegram_chat_id

    def _send(self, text: str) -> None:
        try:
            import asyncio
            asyncio.run(self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            ))
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
        pnl = sum(float(p.get("pnl_usdc", 0) or 0) for p in open_positions)

        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        text = (
            f"📊 <b>Daily Summary — {date_str}</b>\n"
            f"\n"
            f"Trades copied: {copies}\n"
            f"Trades skipped: {skips}\n"
            f"Estimated P&L today: {format_usdc(pnl)}\n"
            f"Open positions: {len(open_positions)}\n"
            f"Portfolio exposure: {format_usdc(total_exposure)}\n"
            f"Bot status: Running ✓"
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
