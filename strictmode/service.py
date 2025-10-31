from __future__ import annotations

import signal
import sys

from .cli import DependencyContainer, build_container
from .config import settings
from .engine.daily_task import daily_update_task
from .engine.scheduler import DailyScheduler


def main() -> None:
    """启动每日任务调度器"""
    container = build_container()
    scheduler = DailyScheduler(timezone=settings.tz_market)

    # 注册每日任务（美东时间16:15，收盘后15分钟）
    scheduler.add_daily_job(
        lambda: daily_update_task(container),
        hour=16,
        minute=15,
    )

    def signal_handler(sig, frame):  # type: ignore
        print("\nShutting down scheduler...")
        scheduler.shutdown()
        container.journal.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"Starting StrictMode scheduler (timezone: {settings.tz_market})")
    print("Daily task scheduled at 16:15 market time")
    print("Press Ctrl+C to stop")

    scheduler.start()

    # 保持运行
    try:
        while True:
            signal.pause()
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()

