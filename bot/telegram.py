"""Минимальная обёртка над Telegram Bot API."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, description: str, code: int | None = None, parameters: dict | None = None):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code
        self.description = description
        self.parameters = parameters or {}


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session or requests.Session()

    def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        timeout = payload.get("timeout", 0) + 30
        for attempt in range(5):
            try:
                r = self.session.post(self.base + method, json=payload, timeout=timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise
                log.warning("Telegram %s: %s, повтор", method, exc)
                time.sleep(2 + attempt * 3)
                continue
            if data.get("ok"):
                return data["result"]
            parameters = data.get("parameters") or {}
            retry = parameters.get("retry_after")
            if data.get("error_code") == 429 and retry and attempt < 4:
                time.sleep(int(retry) + 1)
                continue
            raise TelegramError(data.get("description", "unknown error"), data.get("error_code"),
                                parameters)
        raise TelegramError("слишком много повторов")

    def send_html(self, chat_id, text: str, preview: bool = False) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         link_preview_options={"is_disabled": not preview})

    def send_text(self, chat_id, text: str) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         link_preview_options={"is_disabled": True})

    def get_updates(self, offset: int) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=0,
                         allowed_updates=["message", "my_chat_member"])
