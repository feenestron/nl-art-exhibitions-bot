"""Источники выставок: агрегатор museum.nl и отдельные страницы музеев."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import yaml
from bs4 import BeautifulSoup

from .fetch import Fetcher, clean_text, iso_date, iter_jsonld, main_text, page_with_links, parse_period

log = logging.getLogger(__name__)

MUSEUM_NL = "https://www.museum.nl"
MUSEUM_NL_SEARCH = MUSEUM_NL + "/nl/zien-en-doen/evenementen/zoeken"


@dataclass
class Exhibition:
    source: str
    title: str
    url: str
    venue: str = ""
    city: str = ""
    start: date | None = None
    end: date | None = None
    text: str = ""
    official_url: str = ""
    period_text: str = ""

    @property
    def link(self) -> str:
        return self.official_url or self.url


@dataclass
class Window:
    """Окно выпуска: [today, period_end]; новые выставки берём с opened_since."""
    today: date
    period_end: date
    opened_since: date

    def wants(self, start: date | None, end: date | None) -> bool:
        """Грубый предварительный фильтр по данным списка (до загрузки страницы)."""
        if start and self.opened_since <= start <= self.period_end:
            return True
        return bool(end and self.today <= end <= self.period_end)


@dataclass
class SourceResult:
    items: list[Exhibition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def load_sources(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


# ------------------------------------------------------------------ museum.nl

def parse_museum_nl_listing(html: str) -> list[dict]:
    """Карточки выставок: [{"url", "title", "when", "venue", "city", "last_chance"}]."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.select("li.see-and-do-item"):
        a = li.find("a", href=True)
        if not a:
            continue
        title_tag = li.select_one(".heading-4") or li.find(["h3", "h4"])
        when = where = ""
        for attr in li.select(".item-attribute"):
            icon = attr.find("svg")
            classes = " ".join(icon.get("class", [])) if icon else ""
            text = clean_text(attr.get_text(" "))
            if "icon-time" in classes:
                when = text
            elif "icon-location" in classes:
                where = text
        venue, _, city = where.rpartition(",")
        if not venue:
            venue, city = where, ""
        href = a["href"]
        items.append({
            "url": href if href.startswith("http") else MUSEUM_NL + href,
            "title": clean_text(title_tag.get_text(" ") if title_tag else a.get_text(" ")),
            "when": when,
            "venue": clean_text(venue),
            "city": clean_text(city),
            "last_chance": "last-chance" in " ".join(li.get("class", [])),
        })
    return items


def parse_museum_nl_detail(html: str) -> dict:
    """Точные даты, описание, город и официальная ссылка со страницы выставки."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"start": None, "end": None, "description": "", "official_url": "", "city": "",
           "venue": "", "text": ""}
    for node in iter_jsonld(soup):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if not any(t and "Event" in str(t) for t in types):
            continue
        out["start"] = iso_date(node.get("startDate"))
        out["end"] = iso_date(node.get("endDate"))
        out["description"] = clean_text(node.get("description"), 600)
        loc = node.get("location")
        if isinstance(loc, dict):
            out["venue"] = clean_text(loc.get("name"))
            addr = loc.get("address")
            if isinstance(addr, dict):
                out["city"] = clean_text(addr.get("addressLocality"))
        offers = node.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        for offer in offers:
            url = (offer or {}).get("url") or ""
            if url.startswith("http") and "museum.nl" not in url:
                out["official_url"] = url.strip()
                break
        break
    out["text"] = clean_text(main_text(soup), 1200)
    return out


def museum_nl(src: dict, fetcher: Fetcher, window: Window, budget: int) -> SourceResult:
    res = SourceResult()
    html = fetcher.post(MUSEUM_NL_SEARCH, json={"EventTypes": ["Exhibition"], "PageIndex": 0,
                                                "PageSize": src.get("page_size", 2000)},
                        headers={"Accept": "text/html"}).text
    cards = parse_museum_nl_listing(html)
    if not cards:
        raise ValueError("в ответе museum.nl нет ни одной карточки — сайт изменил вёрстку?")
    res.stats["в списке"] = len(cards)
    wanted = []
    for c in cards:
        start, end = parse_period(c["when"], window.today)
        if not start and not end:
            continue  # постоянная экспозиция или нет дат
        if window.wants(start, end):
            wanted.append((c, start, end))
    res.stats["подходят по датам"] = len(wanted)
    # Признаки того, что сайт поменялся и бот работает «вслепую», хотя и не падает.
    if len(cards) < src.get("min_cards", 150):
        res.errors.append(f"museum.nl: в списке всего {len(cards)} выставок (обычно 500+) — "
                          f"проверьте, не изменился ли сайт")
    dated = sum(1 for c in cards if c["when"])
    if cards and dated < len(cards) * 0.2:
        res.errors.append(f"museum.nl: даты распознаны только у {dated} из {len(cards)} карточек — "
                          f"похоже, изменилась вёрстка списка")
    failed = no_jsonld = opened = 0
    for i, (c, start, end) in enumerate(wanted):
        ex = Exhibition("museum.nl", c["title"], c["url"], c["venue"], c["city"], start, end,
                        period_text=c["when"])
        if i < budget:
            try:
                opened += 1
                d = parse_museum_nl_detail(fetcher.get(c["url"]).text)
                if not d["start"] and not d["end"]:
                    no_jsonld += 1
                ex.start = d["start"] or ex.start
                ex.end = d["end"] or ex.end
                ex.city = d["city"] or ex.city
                ex.official_url = d["official_url"]
                ex.text = d["description"]
                if d["text"] and d["text"][:80] not in ex.text:
                    ex.text = clean_text(f"{ex.text} {d['text']}", 1200)
            except Exception as exc:  # одна битая страница не должна ломать источник
                failed += 1
                log.warning("%s: %s", c["url"], exc)
        res.items.append(ex)
    if failed:
        res.errors.append(f"museum.nl: не открылись {failed} страниц выставок")
    if opened >= 10 and no_jsonld > (opened - failed) * 0.5:
        res.errors.append(f"museum.nl: на {no_jsonld} страницах выставок нет точных дат — "
                          f"используются даты из списка; похоже, изменилась вёрстка страниц")
    if len(wanted) > budget:
        res.errors.append(f"museum.nl: {len(wanted)} кандидатов, страниц открыто только {budget} "
                          f"(увеличьте MAX_FETCH)")
    return res


# ------------------------------------------------- страница выставок музея

def venue_page(src: dict, fetcher: Fetcher, extract) -> SourceResult:
    """Страница «Tentoonstellingen» отдельного музея; выставки и даты извлекает модель.

    extract(text, src) -> [{"title", "start", "end", "url", "description"}]"""
    res = SourceResult()
    html = fetcher.get(src["url"]).text
    text = page_with_links(html, src["url"], src.get("scope_selector"))
    if len(text) < 200:
        raise ValueError("страница почти пустая — возможно, она собирается скриптами")
    found = extract(text, src)
    for item in found:
        start, end = iso_date(item.get("start")), iso_date(item.get("end"))
        url = item.get("url") or src["url"]
        if not url.startswith("http"):
            url = src["url"]
        res.items.append(Exhibition(src["name"], clean_text(item.get("title")), url,
                                    src.get("venue", src["name"]), src.get("city", ""),
                                    start, end, clean_text(item.get("description"), 600)))
    res.stats["найдено"] = len(res.items)
    if not res.items:
        # У музея почти всегда идёт хоть одна выставка; пусто — скорее всего, сменился адрес.
        res.errors.append(f"{src['name']}: на странице не найдено ни одной выставки — "
                          f"проверьте адрес {src['url']}")
    return res


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", title.lower()).strip()
