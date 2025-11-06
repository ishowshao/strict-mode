from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(".env"), override=False)


@dataclass
class TelegramSettings:
    bot_token: str
    chat_id: str


@dataclass
class IBSettings:
    """Socket API (TWS/Gateway) settings and mode selector.

    mode: "socket" (default) uses ib_insync/TWS. "webapi" uses Client Portal Web API.
    """
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    mode: str = "socket"  # "socket" or "webapi"


@dataclass
class WebAPISettings:
    base_url: str = "https://127.0.0.1:5000/v1/api"
    verify_tls: bool = False  # local gateway uses self-signed cert by default
    heartbeat_sec: int = 45
    account_hint: str | None = None  # e.g. "DU" or "U" or exact account id


@dataclass
class DataSettings:
    api_key: str | None = None  # 仅 Alpha Vantage 需要，启用时需显式配置
    source: str = "yfinance"  # 默认启用 yfinance，可通过环境变量切换


@dataclass
class StrategySettings:
    atr_n: int = 22
    atr_k: float = 3.0
    auto_liquidate: bool = False
    rth_only: bool = True
    drawdown_pct: float | None = None
    initial_stop_pct: float = 0.05


@dataclass
class AppSettings:
    tz_local: str = "Asia/Singapore"
    tz_market: str = "America/New_York"
    tz_market2: str | None = None
    database_url: str = field(default_factory=lambda: f"sqlite:///{Path('strictmode.db').absolute()}")
    ib: IBSettings = field(default_factory=IBSettings)
    ib_webapi: WebAPISettings = field(default_factory=WebAPISettings)
    telegram: Optional[TelegramSettings] = None
    data: DataSettings = field(default_factory=DataSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)


def _env(key: str, default: str | None = None) -> str | None:
    return os.getenv(f"STRICTMODE_{key}", default)


def _env_bool(key: str, default: bool) -> bool:
    value = _env(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y"}


def load_settings() -> AppSettings:
    settings = AppSettings()
    settings.tz_local = _env("TZ_LOCAL", settings.tz_local) or settings.tz_local
    settings.tz_market = _env("TZ_MARKET", settings.tz_market) or settings.tz_market
    settings.tz_market2 = _env("TZ_MARKET2", settings.tz_market2) or settings.tz_market2
    settings.database_url = _env("DATABASE_URL", settings.database_url) or settings.database_url

    settings.ib = IBSettings(
        host=_env("IB_HOST", settings.ib.host) or settings.ib.host,
        port=int(_env("IB_PORT", str(settings.ib.port)) or settings.ib.port),
        client_id=int(_env("IB_CLIENT_ID", str(settings.ib.client_id)) or settings.ib.client_id),
        mode=_env("IB_MODE", settings.ib.mode) or settings.ib.mode,
    )

    telegram_token = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat = _env("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat:
        settings.telegram = TelegramSettings(bot_token=telegram_token, chat_id=telegram_chat)

    settings.data = DataSettings(
        api_key=_env("DATA_API_KEY"),  # 可选，仅 Alpha Vantage 需要
        source=_env("DATA_SOURCE", settings.data.source) or settings.data.source,
    )

    # 注意：_env() 会自动添加 STRICTMODE_ 前缀，所以这里实际查找的是 STRICTMODE_DRAWDOWN_PCT
    drawdown = _env("DRAWDOWN_PCT")
    settings.strategy = StrategySettings(
        atr_n=int(_env("ATR_N", str(settings.strategy.atr_n)) or settings.strategy.atr_n),
        atr_k=float(_env("ATR_K", str(settings.strategy.atr_k)) or settings.strategy.atr_k),
        auto_liquidate=_env_bool("AUTO_LIQUIDATE", settings.strategy.auto_liquidate),
        rth_only=_env_bool("RTH_ONLY", settings.strategy.rth_only),
        drawdown_pct=float(drawdown) if drawdown is not None else settings.strategy.drawdown_pct,
        initial_stop_pct=float(_env("INITIAL_STOP_PCT", str(settings.strategy.initial_stop_pct)) or settings.strategy.initial_stop_pct),
    )

    # Web API specific settings
    settings.ib_webapi = WebAPISettings(
        base_url=_env("IB_WEBAPI_BASE_URL", settings.ib_webapi.base_url) or settings.ib_webapi.base_url,
        verify_tls=_env_bool("IB_WEBAPI_VERIFY_TLS", settings.ib_webapi.verify_tls),
        heartbeat_sec=int(_env("IB_WEBAPI_HEARTBEAT_SEC", str(settings.ib_webapi.heartbeat_sec)) or settings.ib_webapi.heartbeat_sec),
        account_hint=_env("IB_WEBAPI_ACCOUNT_HINT", settings.ib_webapi.account_hint) or settings.ib_webapi.account_hint,
    )
    return settings


settings = load_settings()
