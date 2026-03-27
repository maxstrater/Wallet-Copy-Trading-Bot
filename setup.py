"""
Run this script once before starting the bot.
It validates credentials, generates API keys, and confirms all services are reachable.
"""
import asyncio
import os
import sys

# Load .env early so os.getenv picks up values
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # caught in step 2

passed = 0
failed = 0


def ok(msg: str):
    global passed
    passed += 1
    print(f"  [PASS] {msg}")


def fail(msg: str):
    global failed
    failed += 1
    print(f"  [FAIL] {msg}")


def warn(msg: str):
    print(f"  [WARN] {msg}")


def header(step: int, title: str):
    print(f"\nStep {step}: {title}")
    print("-" * 50)


# ── Step 1 — Python version ───────────────────────────────────────────────────
header(1, "Python version check")
version = sys.version_info
version_str = f"{version.major}.{version.minor}.{version.micro}"
if version >= (3, 11):
    ok(f"Python {version_str}")
else:
    fail(f"Python {version_str} — requires 3.11+")
    print("       Install from: https://www.python.org/downloads/")

# ── Step 2 — Dependency check ─────────────────────────────────────────────────
header(2, "Dependency check")
PACKAGES = [
    ("py_clob_client", "py-clob-client"),
    ("web3",           "web3"),
    ("dotenv",         "python-dotenv"),
    ("requests",       "requests"),
    ("telegram",       "python-telegram-bot"),
    ("schedule",       "schedule"),
    ("structlog",      "structlog"),
    ("numpy",          "numpy"),
]
any_missing = False
for import_name, package_name in PACKAGES:
    try:
        __import__(import_name)
        ok(import_name)
    except ImportError:
        fail(f"{import_name} not installed")
        any_missing = True
if any_missing:
    print("\n       Run: pip install -r requirements.txt")

# ── Step 3 — .env validation ──────────────────────────────────────────────────
header(3, ".env file validation")
if not os.path.exists(".env"):
    fail(".env file not found — copy .env.example to .env and fill in values")
else:
    ok(".env file exists")
    REQUIRED_VARS = [
        "POLYMARKET_PK",
        "POLYMARKET_FUNDER",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    for var in REQUIRED_VARS:
        val = os.getenv(var, "")
        if val:
            ok(f"{var} is set")
        else:
            fail(f"{var} is missing or empty")

# ── Step 4 — Generate Polymarket API credentials ──────────────────────────────
header(4, "Polymarket API credentials")
client = None
try:
    from py_clob_client.client import ClobClient
    pk = os.getenv("POLYMARKET_PK", "")
    funder = os.getenv("POLYMARKET_FUNDER", "")

    if not pk or not funder:
        fail("Cannot generate credentials — POLYMARKET_PK or POLYMARKET_FUNDER missing")
    elif os.getenv("POLYMARKET_API_KEY", ""):
        ok("API credentials already configured")
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=1,
            funder=funder,
        )
        from py_clob_client.clob_types import ApiCreds
        client.set_api_creds(ApiCreds(
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        ))
    else:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=1,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        api_key = creds.get("apiKey") or creds.get("api_key", "")
        api_secret = creds.get("secret") or creds.get("api_secret", "")
        api_passphrase = creds.get("passphrase") or creds.get("api_passphrase", "")

        # Write back to .env
        with open(".env", "r") as f:
            content = f.read()

        def set_env_line(content, key, value):
            import re
            pattern = rf"^{key}=.*$"
            replacement = f"{key}={value}"
            if re.search(pattern, content, flags=re.MULTILINE):
                return re.sub(pattern, replacement, content, flags=re.MULTILINE)
            return content + f"\n{key}={value}"

        content = set_env_line(content, "POLYMARKET_API_KEY", api_key)
        content = set_env_line(content, "POLYMARKET_API_SECRET", api_secret)
        content = set_env_line(content, "POLYMARKET_API_PASSPHRASE", api_passphrase)

        with open(".env", "w") as f:
            f.write(content)

        ok("API credentials generated and written to .env")
        print(f"       API_KEY: {api_key[:8]}...")
        client.set_api_creds(
            __import__("py_clob_client.clob_types", fromlist=["ApiCreds"]).ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
        )
except Exception as e:
    fail(f"Credential setup failed: {e}")

# ── Step 5 — Polymarket connectivity ─────────────────────────────────────────
header(5, "Polymarket API connectivity")
try:
    if client is None:
        fail("Skipped — client not initialised (fix Step 4 first)")
    else:
        result = client.get_ok()
        if result:
            ok("Polymarket API reachable")
        else:
            fail(f"Polymarket API unreachable: unexpected response {result}")
except Exception as e:
    fail(f"Polymarket API unreachable: {e}")

# ── Step 6 — Wallet balance ───────────────────────────────────────────────────
header(6, "Wallet balance")
try:
    if client is None:
        fail("Skipped — client not initialised (fix Step 4 first)")
    else:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = float(result.get("balance", 0))
        ok(f"Wallet balance: ${balance:.2f} USDC")
        if balance == 0:
            warn("Balance is $0. Fund your wallet before going live.")
            print(f"       Deposit USDC (Polygon network) to: {os.getenv('POLYMARKET_FUNDER', '')}")
except Exception as e:
    fail(f"Could not fetch balance: {e}")

# ── Step 7 — Telegram connectivity ───────────────────────────────────────────
header(7, "Telegram connectivity")
try:
    from telegram import Bot
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    asyncio.run(bot.send_message(
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        text="Polymarket Copy Bot -- setup test message. If you see this, Telegram is working.",
    ))
    ok("Telegram message sent")
except Exception as e:
    fail(f"Telegram failed: {e}")
    print("       Check your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

# ── Step 8 — wallets.json ─────────────────────────────────────────────────────
header(8, "wallets.json validation")
try:
    import json
    with open("wallets.json", "r") as f:
        data = json.load(f)
    wallets = data.get("wallets", [])
    count = len(wallets)
    if count == 0:
        warn("wallets.json is empty. Add wallets before starting.")
        print("       Find top wallets at: https://polytrackhq.app")
        print("       Look for: win rate > 60%, total bets > 50, active recently")
        passed += 1  # not a hard failure
    else:
        ok(f"{count} wallet(s) configured")
        for w in wallets:
            addr = w.get("address", "")
            label = w.get("label", addr[:10])
            print(f"       {label}: {addr[:10]}...")
except FileNotFoundError:
    fail("wallets.json not found")
except json.JSONDecodeError as e:
    fail(f"wallets.json is invalid JSON: {e}")
except Exception as e:
    fail(f"wallets.json error: {e}")

# ── Step 9 — Database initialisation ─────────────────────────────────────────
header(9, "Database initialisation")
try:
    import db
    db.init_db()
    ok("Database initialised at ./bot.db")
except Exception as e:
    fail(f"Database initialisation failed: {e}")

# ── Step 10 — Final summary ───────────────────────────────────────────────────
print("\n" + "=" * 50)
print(f"  Setup Summary: {passed} passed, {failed} failed")
print("=" * 50)
if failed == 0:
    print("\n  Setup complete. Start the bot with:")
    print("    python main.py --dry-run    (watch logs for 24h first)")
    print("    python main.py --live       (when ready to trade real money)")
else:
    print("\n  Fix the issues above before starting the bot.")
print()
