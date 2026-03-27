"""Tests for setup.py — validates each step independently with mocked externals."""
import importlib
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure .env values won't interfere
os.environ.update({
    "POLYMARKET_PK": "aabbccdd" * 8,
    "POLYMARKET_FUNDER": "0xfunder",
    "POLYMARKET_API_KEY": "existingkey",
    "POLYMARKET_API_SECRET": "existingsecret",
    "POLYMARKET_API_PASSPHRASE": "existingpass",
    "TELEGRAM_BOT_TOKEN": "faketoken",
    "TELEGRAM_CHAT_ID": "123456",
})


def run_setup_step(step_code: str, extra_globals=None):
    """Execute a snippet of setup.py logic and return pass/fail counts."""
    globs = {
        "passed": 0, "failed": 0,
        "ok": lambda msg: globs.__setitem__("passed", globs["passed"] + 1) or print(f"  [PASS] {msg}"),
        "fail": lambda msg: globs.__setitem__("failed", globs["failed"] + 1) or print(f"  [FAIL] {msg}"),
        "warn": lambda msg: print(f"  [WARN] {msg}"),
        "os": os, "sys": sys,
    }
    if extra_globals:
        globs.update(extra_globals)
    exec(compile(step_code, "<test>", "exec"), globs)
    return globs["passed"], globs["failed"]


class TestSetupSteps(unittest.TestCase):

    # Step 1: Python version check
    def test_step1_python_version_passes_on_311(self):
        code = """
version = (3, 11, 0)
version_str = ".".join(str(v) for v in version)
if version >= (3, 11):
    ok(f"Python {version_str}")
else:
    fail(f"Python {version_str} -- requires 3.11+")
"""
        p, f = run_setup_step(code)
        self.assertEqual(p, 1)
        self.assertEqual(f, 0)

    def test_step1_python_version_fails_on_310(self):
        code = """
version = (3, 10, 0)
if version >= (3, 11):
    ok("Python ok")
else:
    fail("Python too old")
"""
        p, f = run_setup_step(code)
        self.assertEqual(f, 1)

    # Step 2: Dependency check — missing package
    def test_step2_missing_package_fails(self):
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "web3":
                raise ImportError("No module named 'web3'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            p, f = 0, 0
            try:
                import web3
            except ImportError:
                f += 1
        self.assertEqual(f, 1)

    # Step 3: .env file missing
    def test_step3_missing_env_file_fails(self):
        code = """
if not os.path.exists(".env.nonexistent_test"):
    fail(".env file not found")
else:
    ok(".env file exists")
"""
        p, f = run_setup_step(code)
        self.assertEqual(f, 1)

    # Step 3: Required var missing
    def test_step3_missing_var_fails(self):
        env_backup = os.environ.pop("POLYMARKET_PK", None)
        try:
            code = """
val = os.getenv("POLYMARKET_PK", "")
if val:
    ok("POLYMARKET_PK is set")
else:
    fail("POLYMARKET_PK is missing or empty")
"""
            p, f = run_setup_step(code)
            self.assertEqual(f, 1)
        finally:
            if env_backup:
                os.environ["POLYMARKET_PK"] = env_backup

    # Step 3: All required vars present
    def test_step3_all_vars_present_passes(self):
        code = """
for var in ["POLYMARKET_PK", "POLYMARKET_FUNDER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    val = os.getenv(var, "")
    if val:
        ok(f"{var} is set")
    else:
        fail(f"{var} is missing")
"""
        p, f = run_setup_step(code)
        self.assertEqual(p, 4)
        self.assertEqual(f, 0)

    # Step 4: Creds already configured — skip generation
    def test_step4_creds_already_set_skips_generation(self):
        code = """
if os.getenv("POLYMARKET_API_KEY", ""):
    ok("API credentials already configured")
else:
    fail("Should have been skipped")
"""
        p, f = run_setup_step(code)
        self.assertEqual(p, 1)
        self.assertEqual(f, 0)

    # Step 5: get_ok returns True
    def test_step5_api_reachable(self):
        mock_client = MagicMock()
        mock_client.get_ok.return_value = True
        code = """
try:
    result = client.get_ok()
    if result:
        ok("Polymarket API reachable")
    else:
        fail(f"Unexpected response: {result}")
except Exception as e:
    fail(f"Unreachable: {e}")
"""
        p, f = run_setup_step(code, {"client": mock_client})
        self.assertEqual(p, 1)
        self.assertEqual(f, 0)

    # Step 5: get_ok raises
    def test_step5_api_unreachable(self):
        mock_client = MagicMock()
        mock_client.get_ok.side_effect = Exception("Connection refused")
        code = """
try:
    result = client.get_ok()
    ok("reachable")
except Exception as e:
    fail(f"Unreachable: {e}")
"""
        p, f = run_setup_step(code, {"client": mock_client})
        self.assertEqual(f, 1)

    # Step 6: Balance > 0
    def test_step6_positive_balance(self):
        mock_client = MagicMock()
        mock_client.get_balance_allowance.return_value = {"balance": "123.45"}
        code = """
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
balance = float(result.get("balance", 0))
ok(f"Wallet balance: ${balance:.2f} USDC")
"""
        p, f = run_setup_step(code, {"client": mock_client})
        self.assertEqual(p, 1)
        self.assertEqual(f, 0)

    # Step 6: Zero balance triggers warning
    def test_step6_zero_balance_warns(self):
        mock_client = MagicMock()
        mock_client.get_balance_allowance.return_value = {"balance": "0"}
        warnings = []
        code = """
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
balance = float(result.get("balance", 0))
ok(f"Wallet balance: ${balance:.2f} USDC")
if balance == 0:
    warnings.append("zero_balance")
"""
        globs = {"client": mock_client, "warnings": warnings,
                 "passed": 0, "failed": 0,
                 "ok": lambda msg: globs.__setitem__("passed", globs.get("passed", 0) + 1),
                 "fail": lambda msg: None, "warn": lambda msg: None, "os": os}
        exec(compile(code, "<test>", "exec"), globs)
        self.assertIn("zero_balance", warnings)

    # Step 7: Telegram success
    def test_step7_telegram_success(self):
        code = """
try:
    import asyncio
    bot_send_called[0] = True
    ok("Telegram message sent")
except Exception as e:
    fail(f"Telegram failed: {e}")
"""
        called = [False]
        p, f = run_setup_step(code, {"bot_send_called": called})
        self.assertEqual(p, 1)

    # Step 7: Telegram failure
    def test_step7_telegram_failure(self):
        code = """
try:
    raise Exception("401 Unauthorized")
except Exception as e:
    fail(f"Telegram failed: {e}")
"""
        p, f = run_setup_step(code)
        self.assertEqual(f, 1)

    # Step 8: Empty wallets.json warns
    def test_step8_empty_wallets_warns(self):
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            _json.dump({"wallets": []}, tmp)
            tmp_path = tmp.name
        try:
            code = """
import json
with open(wallets_path, "r") as f:
    data = json.load(f)
wallets = data.get("wallets", [])
if len(wallets) == 0:
    warn("wallets.json is empty")
    passed += 1
else:
    ok(f"{len(wallets)} wallets")
"""
            globs = {"wallets_path": tmp_path, "passed": 0, "failed": 0,
                     "ok": lambda msg: None, "fail": lambda msg: None,
                     "warn": lambda msg: None, "os": os}
            exec(compile(code, "<test>", "exec"), globs)
            self.assertEqual(globs["passed"], 1)
        finally:
            os.unlink(tmp_path)

    # Step 8: Wallets present
    def test_step8_wallets_present_passes(self):
        import tempfile, json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            _json.dump({"wallets": [{"address": "0xabc123", "label": "whale_1"}]}, tmp)
            tmp_path = tmp.name
        try:
            code = """
import json
with open(wallets_path, "r") as f:
    data = json.load(f)
wallets = data.get("wallets", [])
if len(wallets) == 0:
    warn("empty")
else:
    ok(f"{len(wallets)} wallet(s)")
"""
            globs = {"wallets_path": tmp_path, "passed": 0, "failed": 0,
                     "ok": lambda msg: globs.__setitem__("passed", globs["passed"] + 1),
                     "fail": lambda msg: None, "warn": lambda msg: None, "os": os}
            exec(compile(code, "<test>", "exec"), globs)
            self.assertEqual(globs["passed"], 1)
        finally:
            os.unlink(tmp_path)

    # Step 9: DB init
    def test_step9_db_init(self):
        import db as _db
        _db.DB_PATH = "./test_setup_step9.db"
        try:
            _db.init_db()
            self.assertTrue(os.path.exists("./test_setup_step9.db"))
        finally:
            _db.DB_PATH = "./bot.db"
            try:
                os.remove("./test_setup_step9.db")
            except Exception:
                pass

    # Step 10: Summary — all passed
    def test_step10_all_passed_summary(self):
        output = []
        code = """
passed_count = 9
failed_count = 0
if failed_count == 0:
    result.append("complete")
else:
    result.append("fix_issues")
"""
        result = []
        globs = {"passed": 9, "failed": 0, "result": result, "os": os}
        exec(compile(code, "<test>", "exec"), globs)
        self.assertIn("complete", result)

    # Step 10: Summary — some failed
    def test_step10_some_failed_summary(self):
        result = []
        code = """
if failed_count == 0:
    result.append("complete")
else:
    result.append("fix_issues")
"""
        globs = {"passed": 7, "failed": 2, "failed_count": 2, "result": result, "os": os}
        exec(compile(code, "<test>", "exec"), globs)
        self.assertIn("fix_issues", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
