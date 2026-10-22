"""Состояние бота в JSON-файле, который workflow коммитит обратно в репозиторий."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import date, timedelta

DEFAULT_STATE: dict = {
    "version": 1,
    # "new:<url>" / "last:<url>" -> дата выпуска, в котором выставка уже была.
    # Раздельно, чтобы выставка, объявленная при открытии, могла попасть
    # в «последний шанс» перед закрытием.
    "announced": {},
    "last_post_date": None,
    "tg_offset": 0,         # offset getUpdates для команды chats
    "known_chats": {},      # chat_id -> название; группы, куда добавлен бот
    "history": [],
}


def load(path: str) -> dict:
    state = copy.deepcopy(DEFAULT_STATE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def prune(state: dict, today: date) -> None:
    keep_from = (today - timedelta(days=400)).isoformat()
    state["announced"] = {k: v for k, v in state["announced"].items() if str(v) >= keep_from}
    state["history"] = state["history"][-50:]
