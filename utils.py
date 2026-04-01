import json
import logging
import sys
import time
import functools
from datetime import datetime, timezone

import requests
import structlog

# ── File log (JSON, line-buffered) ───────────────────────────────────────────

_log_file = open("./bot.log", "a", encoding="utf-8", buffering=1)


def _tee_json_to_file(logger, method, event_dict: dict) -> dict:
    """Side-effect processor: write a JSON copy of every log line to bot.log."""
    try:
        safe = {}
        for k, v in event_dict.items():
            safe[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
        _log_file.write(json.dumps(safe) + "\n")
    except Exception:
        pass
    return event_dict


# ── Structlog configuration ───────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=True),
        structlog.processors.add_log_level,
        _tee_json_to_file,
        structlog.dev.ConsoleRenderer(
            colors=False,
            sort_keys=False,
            pad_event=32,
        ),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
)

log = structlog.get_logger()


# ── Retry decorator ───────────────────────────────────────────────────────────

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
                    log.warning(
                        "api_retry",
                        func=func.__name__,
                        attempt=attempt + 1,
                        delay_s=delay,
                        error=str(e),
                    )
                    time.sleep(delay)
        return wrapper
    return decorator


# ── Formatters ────────────────────────────────────────────────────────────────

def format_usdc(amount: float) -> str:
    return f"${amount:.2f}"


def time_until(timestamp) -> str:
    try:
        if isinstance(timestamp, datetime):
            target = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        else:
            target = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "unknown"
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
