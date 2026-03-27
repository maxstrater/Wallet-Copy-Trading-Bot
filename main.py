import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import schedule

import db
from alerts import AlertManager
from config import load_config
from decision_engine import DecisionEngine
from executor import Executor
from signal_engine import SignalEngine
from utils import log
from wallet_monitor import WalletMonitor
from wallet_scorer import WalletScorer

VERSION = "0.1.0"
start_time = datetime.now(tz=timezone.utc)

# Global state for health check
_state = {
    "mode": "dry_run",
    "wallets_watched": 0,
    "trades_copied_today": 0,
}


# ── Health check HTTP server ─────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        uptime = int((datetime.now(tz=timezone.utc) - start_time).total_seconds())
        summary = db.get_daily_summary()
        body = json.dumps({
            "status": "ok",
            "uptime_seconds": uptime,
            "mode": _state["mode"],
            "wallets_watched": _state["wallets_watched"],
            "trades_copied_today": int(summary.get("total_copied") or 0),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence default HTTP logging


def start_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("health_server_started", port=8080)


# ── Wallet loader ─────────────────────────────────────────────────────────────

def load_wallets() -> list:
    try:
        with open("wallets.json", "r") as f:
            data = json.load(f)
        return data.get("wallets", [])
    except Exception as e:
        log.error("wallets_load_failed", error=str(e))
        return []


# ── Startup banner ────────────────────────────────────────────────────────────

def print_banner(config, mode: str):
    sep = "=" * 52
    print(f"\n{sep}")
    print(f"  Polymarket Copy-Trading Bot  v{VERSION}")
    print(f"  Mode     : {mode.upper()}")
    print(f"  Wallets  : wallets.json")
    print(f"  {'-' * 43}")
    print(f"  Funder             : {config.polymarket_funder}")
    print(f"  API Key            : {config.polymarket_api_key[:8]}...")
    print(f"  Max position       : ${config.max_position_size_usdc}")
    print(f"  Max exposure       : ${config.max_portfolio_exposure_usdc}")
    print(f"  Copy ratio         : {config.copy_ratio}")
    print(f"  Min win rate       : {config.min_wallet_win_rate}")
    print(f"  Min bets           : {config.min_wallet_bets}")
    print(f"  Min signal score   : {config.min_signal_score}")
    print(f"  Poll interval      : {config.poll_interval_seconds}s")
    print(f"{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket wallet copy-trading bot")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--live", action="store_true", help="Enable live trading (real USDC)")
    mode_group.add_argument("--dry-run", action="store_true", help="Dry-run mode (default)")
    args = parser.parse_args()

    config = load_config()

    # Override DRY_RUN from CLI flags
    if args.live:
        config.dry_run = False
    else:
        config.dry_run = True

    mode = "live" if not config.dry_run else "dry_run"
    _state["mode"] = mode

    # Live mode warning
    if not config.dry_run:
        print("╔══════════════════════════════════════════════╗")
        print("║  ⚠️  LIVE MODE — real USDC will be spent.   ║")
        print("║  Press Ctrl+C within 5 seconds to cancel.   ║")
        print("╚══════════════════════════════════════════════╝")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("Cancelled.")
            sys.exit(0)

    print_banner(config, mode)

    # Initialise DB
    db.init_db()

    # Initialise components
    scorer   = WalletScorer(config)
    signals  = SignalEngine(config)
    engine   = DecisionEngine(config, signals)
    monitor  = WalletMonitor(config)
    executor = Executor(config)
    alerts   = AlertManager(config)

    # ── Startup sequence ─────────────────────────────────────────────────────

    # 1. Load wallets — exit if empty
    wallets = load_wallets()
    if not wallets:
        print("ERROR: wallets.json is empty. Add at least one wallet address and restart.")
        sys.exit(1)
    _state["wallets_watched"] = len(wallets)
    log.info("wallets_loaded", count=len(wallets))

    # 2. Refresh wallet scores
    wallet_addresses = [w["address"] for w in wallets]
    scorer.refresh_all(wallet_addresses)

    # 3. Log current balance
    balance = executor.get_balance()
    log.info("startup_balance", balance=f"${balance:.2f}")

    # 4. Send startup Telegram alert
    alerts.send_startup_alert(wallet_count=len(wallets))

    # ── Scheduling ───────────────────────────────────────────────────────────

    schedule.every(6).hours.do(
        lambda: scorer.refresh_all([w["address"] for w in load_wallets()])
    )
    schedule.every().day.at("08:00").do(alerts.send_daily_summary)
    schedule.every(5).minutes.do(
        lambda: log.info("watchdog", status="alive",
                         uptime_seconds=int((datetime.now(tz=timezone.utc) - start_time).total_seconds()))
    )

    # ── Health check server ──────────────────────────────────────────────────
    start_health_server()

    # ── Graceful shutdown ─────────────────────────────────────────────────────

    def shutdown(signum=None, frame=None):
        log.info("shutting_down")
        schedule.run_all()
        final_balance = executor.get_balance()
        alerts._send(f"🛑 Bot stopped. Final balance: ${final_balance:.2f}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)

    # ── Main loop ────────────────────────────────────────────────────────────

    consecutive_failures = 0
    poll_count = 0

    def on_new_trade(trade):
        nonlocal consecutive_failures
        try:
            wallet_score = scorer.get_score(trade.wallet_address)
            available_usdc = executor.get_balance()
            decision = engine.evaluate(trade, wallet_score, available_usdc)

            trade_dict = {
                "wallet_address": trade.wallet_address,
                "market_id": trade.market_id,
                "token_id": trade.token_id,
                "side": trade.side,
                "size_usdc": trade.size_usdc,
                "price": trade.price,
                "timestamp": trade.detected_at.isoformat(),
            }

            if decision.action == "copy":
                trade_db_id = db.insert_trade(
                    trade_dict, copied=True, skip_reason=None,
                    tx_hash=None, signal_score=decision.signal_score
                )
                db.log_signals(trade_db_id, [
                    {"signal_name": s.name, "signal_value": s.value,
                     "signal_weight": s.weight, "contribution": s.contribution}
                    for s in decision.signal_result.signals
                ])
                result = executor.execute(decision, trade_db_id)
                alerts.send_copy_alert(decision, result)
                if result.success:
                    _state["trades_copied_today"] += 1
            else:
                trade_db_id = db.insert_trade(
                    trade_dict, copied=False, skip_reason=decision.skip_reason,
                    tx_hash=None, signal_score=decision.signal_score
                )
                db.log_signals(trade_db_id, [
                    {"signal_name": s.name, "signal_value": s.value,
                     "signal_weight": s.weight, "contribution": s.contribution}
                    for s in decision.signal_result.signals
                ])
                alerts.send_skip_alert(decision)

        except Exception as e:
            log.error("on_new_trade_error", error=str(e), traceback=traceback.format_exc())
            alerts.send_error_alert("on_new_trade", str(e))

    try:
        while True:
            schedule.run_pending()
            try:
                new_trades = monitor.poll()
                consecutive_failures = 0
                for trade in new_trades:
                    on_new_trade(trade)
            except Exception as e:
                consecutive_failures += 1
                log.error("poll_failed", error=str(e), consecutive=consecutive_failures)
                if consecutive_failures >= 5:
                    alerts.send_error_alert("monitor", "5 consecutive poll failures")
                    sys.exit(1)

            poll_count += 1
            time.sleep(config.poll_interval_seconds)

    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
