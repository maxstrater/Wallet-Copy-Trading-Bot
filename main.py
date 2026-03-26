import schedule
import time

from config import load_config
from db import init_db
from utils import log


def main():
    config = load_config()
    init_db()
    log.info("bot_started", dry_run=config.dry_run, poll_interval=config.poll_interval_seconds)

    # Trading loop — logic added in later prompts
    def poll():
        log.info("poll_tick")

    schedule.every(config.poll_interval_seconds).seconds.do(poll)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
