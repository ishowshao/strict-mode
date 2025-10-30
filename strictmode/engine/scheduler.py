from __future__ import annotations

from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class DailyScheduler:
    def __init__(self, timezone: str = "America/New_York") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def add_daily_job(self, func: Callable[[], None], hour: int = 16, minute: int = 15) -> None:
        trigger = CronTrigger(hour=hour, minute=minute)
        self.scheduler.add_job(func, trigger=trigger, misfire_grace_time=300)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        self.scheduler.shutdown()
