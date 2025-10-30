from __future__ import annotations

from typing import Any

import httpx


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, session: httpx.Client | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session = session or httpx.Client(timeout=10.0)

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, text: str, **kwargs: Any) -> None:
        payload = {"chat_id": self.chat_id, "text": text, **kwargs}
        try:
            response = self._session.post(f"{self.base_url}/sendMessage", json=payload)
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network disabled
            raise RuntimeError(f"Failed to send Telegram message: {exc}") from exc

    def close(self) -> None:
        self._session.close()
