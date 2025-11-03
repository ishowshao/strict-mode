from __future__ import annotations

import signal
import sys

from .cli import DependencyContainer, build_container
from .config import settings
from .engine.daily_task import daily_update_task, daily_update_task_for_timezone
from .engine.scheduler import DailyScheduler


def main() -> None:
    """启动每日任务调度器（支持双市场计划任务）"""
    container = build_container()
    primary_tz = settings.tz_market
    secondary_tz = getattr(settings, "tz_market2", None)

    scheduler = DailyScheduler(timezone=primary_tz)
    scheduler.add_daily_job(lambda: daily_update_task_for_timezone(container, primary_tz), hour=16, minute=15)

    scheduler2 = None
    if secondary_tz and secondary_tz.strip() and secondary_tz != primary_tz:
        scheduler2 = DailyScheduler(timezone=secondary_tz)
        scheduler2.add_daily_job(lambda: daily_update_task_for_timezone(container, secondary_tz), hour=16, minute=15)

    def signal_handler(sig, frame):  # type: ignore
        print("\nShutting down scheduler...")
        try:
            scheduler.shutdown()
        finally:
            if scheduler2:
                scheduler2.shutdown()
            container.journal.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"Starting StrictMode scheduler (primary tz: {primary_tz})")
    print("Primary daily task at 16:15 local to primary tz")
    if secondary_tz and secondary_tz != primary_tz:
        print(f"Secondary scheduler enabled (tz: {secondary_tz}) at 16:15")
    print("Press Ctrl+C to stop")

    scheduler.start()
    if scheduler2:
        scheduler2.start()

    # 保持运行
    try:
        while True:
            signal.pause()
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()
