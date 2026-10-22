"""Claude API: извлечение выставок со страниц музеев и отбор известных художников."""
from __future__ import annotations

import json
import logging
import time
from datetime import date

import requests

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class Claude:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def tool_call(self, system: str, user: str, tool: dict, max_tokens: int = 16000) -> dict:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
            "messages": [{"role": "user", "content": user
                          + f"\n\nОбязательно верни результат вызовом инструмента {tool['name']}."}],
        }
        result = extract_tool_input(self._call(body))
        if result is None:
            raise RuntimeError("модель не вернула результат")
        return result

    def _call(self, body: dict) -> list[dict]:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        attempt = 0
        while True:
            r = requests.post(ANTHROPIC_URL, json=body, headers=headers, timeout=300)
            # Принудительный tool_choice поддерживается не всеми режимами — тогда "auto".
            if r.status_code == 400 and "tool_choice" in r.text and body["tool_choice"]["type"] != "auto":
                body = {**body, "tool_choice": {"type": "auto"}}
                continue
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 3:
                attempt += 1
                log.warning("Claude API %s, повтор", r.status_code)
                time.sleep(15 * attempt)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
            return r.json().get("content", [])


def extract_tool_input(content: list[dict]) -> dict | None:
    for block in content:
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return block["input"]
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


# ------------------------------------------------------- извлечение со страницы

EXTRACT_SYSTEM = """\
Ты извлекаешь список временных выставок со страницы нидерландского музея.
Верни только выставки с конкретными датами (начало и/или окончание), которые ещё не
закончились на дату today. Постоянные экспозиции, архив прошедших выставок, мероприятия,
экскурсии и лекции не включай. Год, если он не указан, выводи из контекста и даты today.
Ссылку бери только из адресов в квадратных скобках рядом с названием; если её нет — пусто.
Ничего не выдумывай."""

EXTRACT_TOOL = {
    "name": "submit_exhibitions",
    "description": "Передать список выставок со страницы.",
    "input_schema": {
        "type": "object",
        "properties": {
            "exhibitions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "YYYY-MM-DD или пусто"},
                        "end": {"type": "string", "description": "YYYY-MM-DD или пусто"},
                        "url": {"type": "string"},
                        "description": {"type": "string", "description": "1–2 предложения с сайта"},
                    },
                    "required": ["title", "start", "end"],
                },
            },
        },
        "required": ["exhibitions"],
    },
}


def extract_exhibitions(claude: Claude, page_text: str, src: dict, today: date) -> list[dict]:
    user = (f"today: {today.isoformat()}\nМузей: {src.get('venue', src['name'])}, "
            f"{src.get('city', '')}\nАдрес страницы: {src['url']}\n\nТекст страницы:\n{page_text}")
    result = claude.tool_call(EXTRACT_SYSTEM, user, EXTRACT_TOOL, max_tokens=8000)
    return [x for x in result.get("exhibitions", []) if isinstance(x, dict) and x.get("title")]


# ---------------------------------------------------------- отбор и перевод

SELECT_SYSTEM = """\
Ты — редактор русскоязычного Telegram-сообщества любителей искусства в Нидерландах.
Раз в две недели ты получаешь список выставок в музеях и выставочных залах Нидерландов
(данные на нидерландском или английском) и отбираешь те, ради которых стоит поехать.

Раздел new — выставки, которые открываются (или только что открылись);
раздел last — выставки, которые закрываются в ближайшие две недели («последний шанс»).

Главное — классики, то есть художники, вошедшие в историю искусства (до середины
XX века включительно):
- старые мастера и голландский Золотой век: Рембрандт, Вермеер, Хальс, Аверкамп,
  Ян Стен, Рейсдал, Терборх, Питер де Хох, Брейгель, Босх, Рубенс;
- XIX век и модернизм: Ван Гог, Моне, Синьяк, Матисс, Пикассо, Мондриан, Эшер,
  Шарли Тороп, Пейк Кох, Брейтнер, Ян Тороп и художники такого же масштаба;
- классики фотографии, скульптуры и дизайна того же уровня.
Сюда же — художники, широко известные в истории нидерландского искусства, даже если
за рубежом их знают меньше: если имя стоит в учебнике или в залах Rijksmuseum,
Mauritshuis, Kunstmuseum Den Haag, — это классик.

Современные и ныне живущие художники — только звёзды мирового уровня, чьи имена знает
широкая публика даже вне мира искусства: Бэнкси, Яёи Кусама, Дэвид Хокни, Ай Вэйвэй,
Олафур Элиассон, Энтони Гормли, Ансельм Кифер, Джефф Кунс, Дэмиен Хёрст и т. п.
Просто признанных, хорошо продающихся или известных в профессиональной среде
современных авторов не включай.

Групповые выставки подходят, если в центре названы известные мастера («Hals – Rembrandt»).

Не включай: исторические, краеведческие, естественнонаучные, технические и детские
выставки; тематические выставки без известных имён; выставки малоизвестных и
начинающих авторов; постоянные экспозиции. Если сомневаешься, что современный автор —
звезда мирового уровня, не включай. Если сомневаешься в классике — посмотри, не о нём
ли речь в описании выставки (крупный музей, ретроспектива, «мастер», «Золотой век»).
Лучше короткий, но сильный список.

Для каждой отобранной выставки:
- artists: имя(ена) художника в общепринятом русском написании (Поль Синьяк,
  Шарли Тороп и Пейк Кох); если устойчивого русского написания нет — латиницей.
- title: название выставки в оригинале, как в данных.
- summary: одно предложение по-русски (до ~160 знаков) — чем выставка интересна;
  только факты из входных данных, без восторгов и выдумок.

id бери ровно из входных данных. Верни результат вызовом submit_selection."""

SELECT_TOOL = {
    "name": "submit_selection",
    "description": "Передать отобранные выставки.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "artists": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["id", "artists", "title", "summary"],
                },
            },
        },
        "required": ["items"],
    },
}


def select(claude: Claude, payload: dict) -> list[dict]:
    user = ("Отбери выставки для выпуска. Входные данные (JSON):\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))
    result = claude.tool_call(SELECT_SYSTEM, user, SELECT_TOOL)
    return [x for x in result.get("items", []) if isinstance(x, dict)]
