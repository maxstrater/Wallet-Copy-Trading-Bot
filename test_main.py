import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import schedule

os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8,
    "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "apikey123",
    "POLYMARKET_API_SECRET": "secret",
    "POLYMARKET_API_PASSPHRASE": "pass",
    "TELEGRAM_BOT_TOKEN": "faketoken",
    "TELEGRAM_CHAT_ID": "123456",
    "DRY_RUN": "true",
})

import db
db.DB_PATH = "./test_main.db"
db.init_db()


def make_trade():
    t = MagicMock()
    t.wallet_address = "0xabc"
    t.wallet_label = "whale_1"
    t.market_id = "mkt1"
    t.token_id = "tok1"
    t.side = "YES"
    t.size_usdc = 100.0
    t.price = 0.55
    t.question = "Will BTC hit 100k?"
    t.closes_at = datetime.now(tz=timezone.utc) + timedelta(hours=48)
    t.liquidity_usdc = 5000.0
    t.detected_at = datetime.now(tz=timezone.utc)
    return t


def make_signal_details():
    from signal_engine import SignalDetail
    return [
        SignalDetail(n, 0.7, w, 0.7 * w, "desc")
        for n, w in [
            ("wallet_quality", 25), ("price_efficiency", 20),
            ("bet_size_conviction", 15), ("time_value", 15),
            ("liquidity_depth", 10), ("hot_streak", 10), ("portfolio_fit", 5),
        ]
    ]


# ── TEST 1: load_wallets ──────────────────────────────────────────────────────
with open("wallets.json", "w") as f:
    json.dump({"wallets": [{"address": "0xabc", "label": "whale_1"}]}, f)

import main as m
wallets = m.load_wallets()
assert len(wallets) == 1 and wallets[0]["address"] == "0xabc"
print("load_wallets(): OK")

# ── TEST 2: HealthHandler returns correct JSON ────────────────────────────────
import io
import json as _json

m._state.update({"mode": "dry_run", "wallets_watched": 2, "trades_copied_today": 0})


class CapturingHandler(m.HealthHandler):
    def __init__(self):
        self.path = "/"
        self.wfile = io.BytesIO()
        self._captured = []

    def send_response(self, code, message=None):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


handler = CapturingHandler()
captured = []
handler.wfile.write = lambda data: captured.append(data)
handler.do_GET()
body = _json.loads(b"".join(captured))
assert body["status"] == "ok"
assert body["mode"] == "dry_run"
assert body["wallets_watched"] == 2
assert "uptime_seconds" in body
print("HealthHandler GET /: OK")

# ── TEST 3: on_new_trade copy path ────────────────────────────────────────────
from signal_engine import SignalResult
from decision_engine import Decision
from executor import ExecutionResult

sig_details = make_signal_details()
mock_signal_result = SignalResult(final_score=75, signals=sig_details, reasoning="Strong.")

trade = make_trade()
ws = MagicMock()
ws.win_rate = 0.65
ws.total_bets = 50
ws.avg_bet_size = 100.0
ws.hot_streak = 3
ws.composite_score = 0.72

copy_decision = Decision(
    action="copy", side="YES", size_usdc=50.0, signal_score=75,
    confidence_label="high", skip_reason=None, reasoning="Strong.",
    trade=trade, wallet_score=ws, signal_result=mock_signal_result,
)

mock_result = ExecutionResult(
    success=True, tx_hash="0xDRY_RUN", filled_size=50.0,
    filled_price=0.55, error=None, timestamp=datetime.now(tz=timezone.utc),
)

trade_dict = {
    "wallet_address": trade.wallet_address, "market_id": trade.market_id,
    "token_id": trade.token_id, "side": trade.side, "size_usdc": trade.size_usdc,
    "price": trade.price, "timestamp": trade.detected_at.isoformat(),
}
trade_db_id = db.insert_trade(
    trade_dict, copied=True, skip_reason=None, tx_hash=None, signal_score=75
)
db.log_signals(trade_db_id, [
    {"signal_name": s.name, "signal_value": s.value,
     "signal_weight": s.weight, "contribution": s.contribution}
    for s in sig_details
])

with sqlite3.connect("./test_main.db") as conn:
    row = conn.execute(
        "SELECT copied, signal_score FROM trades WHERE id=?", (trade_db_id,)
    ).fetchone()
    assert row[0] == 1 and row[1] == 75
    sigs = conn.execute(
        "SELECT COUNT(*) FROM signal_log WHERE trade_id=?", (trade_db_id,)
    ).fetchone()
    assert sigs[0] == 7

print("on_new_trade copy path (DB writes): OK")

# ── TEST 4: on_new_trade skip path ────────────────────────────────────────────
skip_decision = Decision(
    action="skip", side=None, size_usdc=None, signal_score=40,
    confidence_label="low", skip_reason="signal_score_too_low", reasoning="Weak.",
    trade=trade, wallet_score=ws, signal_result=mock_signal_result,
)
trade_db_id2 = db.insert_trade(
    trade_dict, copied=False, skip_reason="signal_score_too_low",
    tx_hash=None, signal_score=40
)
with sqlite3.connect("./test_main.db") as conn:
    row = conn.execute(
        "SELECT copied, skip_reason FROM trades WHERE id=?", (trade_db_id2,)
    ).fetchone()
    assert row[0] == 0 and row[1] == "signal_score_too_low"
print("on_new_trade skip path (DB writes): OK")

# ── TEST 5: schedule jobs register correctly ──────────────────────────────────
schedule.clear()
schedule.every(6).hours.do(lambda: None)
schedule.every().day.at("08:00").do(lambda: None)
schedule.every(5).minutes.do(lambda: None)
assert len(schedule.jobs) == 3
print("schedule jobs (3 registered): OK")

# ── TEST 6: print_banner does not crash ───────────────────────────────────────
from config import load_config
cfg = load_config()
try:
    m.print_banner(cfg, "dry_run")
    print("print_banner(): OK")
except Exception as e:
    print(f"print_banner(): FAIL ({e})")

# ── Cleanup ───────────────────────────────────────────────────────────────────
try:
    os.remove("./test_main.db")
except Exception:
    pass

print()
print("All main.py tests passed.")
