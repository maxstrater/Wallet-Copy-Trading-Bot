import time
import functools
import logging
from datetime import datetime, timezone

import requests
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
)

_file_handler = logging.FileHandler("./bot.log")
_file_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

log = structlog.get_logger()


def retry_with_backoff(max_retries: int = 3, base_delay: float = 2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    log.warning("retrying", func=func.__name__, attempt=attempt + 1, delay=delay, error=str(e))
                    time.sleep(delay)
        return wrapper
    return decorator


def format_usdc(amount: float) -> str:
    return f"${amount:.2f}"


def time_until(timestamp: str) -> str:
    try:
        target = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
    except ValueError:
        target = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    delta = target - now
    if delta.total_seconds() <= 0:
        return "expired"
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m"


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"
