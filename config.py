import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = [
    "POLYMARKET_PK",
    "POLYMARKET_FUNDER",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


@dataclass
class Config:
    polymarket_pk: str
    polymarket_funder: str
    polymarket_api_key: str
    polymarket_api_secret: str
    polymarket_api_passphrase: str
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool
    max_position_size_usdc: float
    max_portfolio_exposure_usdc: float
    copy_ratio: float
    min_wallet_win_rate: float
    min_wallet_bets: int
    min_signal_score: int
    poll_interval_seconds: int


def load_config() -> Config:
    for var in REQUIRED_VARS:
        if not os.getenv(var):
            raise ValueError(f"Missing required environment variable: {var}")

    return Config(
        polymarket_pk=os.environ["POLYMARKET_PK"],
        polymarket_funder=os.environ["POLYMARKET_FUNDER"],
        polymarket_api_key=os.environ["POLYMARKET_API_KEY"],
        polymarket_api_secret=os.environ["POLYMARKET_API_SECRET"],
        polymarket_api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
        max_position_size_usdc=float(os.getenv("MAX_POSITION_SIZE_USDC", "50")),
        max_portfolio_exposure_usdc=float(os.getenv("MAX_PORTFOLIO_EXPOSURE_USDC", "500")),
        copy_ratio=float(os.getenv("COPY_RATIO", "0.5")),
        min_wallet_win_rate=float(os.getenv("MIN_WALLET_WIN_RATE", "0.58")),
        min_wallet_bets=int(os.getenv("MIN_WALLET_BETS", "30")),
        min_signal_score=int(os.getenv("MIN_SIGNAL_SCORE", "65")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
    )
