"""Настройки из переменных окружения (секреты и переменные GitHub Actions)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


@dataclass
class Config:
    bot_token: str
    chat_id: str                 # группа (или канал), куда публикуется выпуск
    admin_chat_id: str           # необязательно: куда слать сообщения о сбоях
    anthropic_key: str
    model: str
    tz: ZoneInfo
    post_weekday: int            # 0 = понедельник … 4 = пятница
    post_hour: int               # с какого часа (местного) можно публиковать
    interval_days: int           # раз во сколько дней выходит выпуск
    period_days: int             # на сколько дней вперёд смотрим
    max_new: int
    max_last: int
    max_fetch: int               # сколько страниц выставок открывать за запуск
    state_path: str
    sources_path: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bot_token=_str("TELEGRAM_BOT_TOKEN"),
            chat_id=_str("CHAT_ID"),
            admin_chat_id=_str("ADMIN_CHAT_ID"),
            anthropic_key=_str("ANTHROPIC_API_KEY"),
            model=_str("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            tz=ZoneInfo(_str("TIMEZONE", "Europe/Amsterdam")),
            post_weekday=_int("POST_WEEKDAY", 4),
            post_hour=_int("POST_HOUR", 9),
            interval_days=_int("INTERVAL_DAYS", 14),
            period_days=_int("PERIOD_DAYS", 14),
            max_new=_int("MAX_NEW", 15),
            max_last=_int("MAX_LAST", 10),
            max_fetch=_int("MAX_FETCH", 160),
            state_path=_str("STATE_PATH", "state/state.json"),
            sources_path=_str("SOURCES_PATH", "sources.yaml"),
        )
