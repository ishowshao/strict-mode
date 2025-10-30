from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


class HTTPError(RuntimeError):
    pass


@dataclass
class Response:
    status_code: int = 200
    _json_data: Dict[str, Any] | None = None

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise HTTPError(f"HTTP error {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return dict(self._json_data or {})


class Client:
    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout

    def get(self, url: str, params: Dict[str, Any] | None = None) -> Response:
        raise HTTPError("HTTP requests not supported in offline mode")

    def post(self, url: str, json: Dict[str, Any] | None = None) -> Response:
        raise HTTPError("HTTP requests not supported in offline mode")

    def close(self) -> None:
        return None
