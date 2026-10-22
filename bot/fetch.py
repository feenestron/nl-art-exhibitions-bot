"""HTTP-загрузка с уважением к robots.txt, разбор HTML и нидерландских дат."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = ("NLArtExhibitionsBot/1.0 (+https://github.com/feenestron/nl-art-exhibitions-bot; "
              "exhibition digest, one visit every two weeks)")


class Fetcher:
    def __init__(self, timeout: int = 30, per_host_delay: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "nl,en;q=0.8",
        })
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(host + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[host] = rp
        return rp.can_fetch(USER_AGENT, url)

    def _wait(self, url: str) -> None:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt запрещает {url}")
        host = urlparse(url).netloc
        wait = self.per_host_delay - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        self._wait(url)
        r = self.session.get(url, timeout=self.timeout, **kwargs)
        r.raise_for_status()
        return r

    def post(self, url: str, **kwargs) -> requests.Response:
        self._wait(url)
        r = self.session.post(url, timeout=self.timeout, **kwargs)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------- HTML helpers

def clean_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw, strict=False)  # в строках бывают сырые переводы строк
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def iso_date(value) -> date | None:
    if not value:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def main_text(soup: BeautifulSoup, scope_selector: str | None = None) -> str:
    soup = BeautifulSoup(str(soup), "html.parser")  # копия: ниже удаляем теги
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form", "svg"]):
        tag.decompose()
    scope = (soup.select_one(scope_selector) if scope_selector else None) \
        or soup.select_one("main") or soup.select_one("article") or soup.body or soup
    return clean_text(scope.get_text(" "))


def page_with_links(html: str, base_url: str, scope_selector: str | None = None,
                    limit: int = 15000) -> str:
    """Текст страницы, где у ссылок в скобках указан адрес — для извлечения моделью."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form", "svg"]):
        tag.decompose()
    scope = (soup.select_one(scope_selector) if scope_selector else None) \
        or soup.select_one("main") or soup.body or soup
    host = urlparse(base_url).netloc
    for a in scope.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"].strip()))[0]
        if urlparse(url).netloc == host and url.rstrip("/") != base_url.rstrip("/"):
            a.append(f" [{url}]")
    return clean_text(scope.get_text(" "), limit)


# ------------------------------------------------------ нидерландские даты

MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maart": 3, "mrt": 3,
    "april": 4, "apr": 4, "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "augustus": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_RX = re.compile(rf"\b(\d{{1,2}})\s+({_MON})\b\.?(?:\s+(\d{{4}}))?", re.IGNORECASE)


def _mk(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        return date(year or today.year, month, day)
    except ValueError:
        return None


def parse_period(text: str, today: date) -> tuple[date | None, date | None]:
    """Период выставки из строк museum.nl: «Vanaf 24 oktober 2026 t/m 28 februari 2027»,
    «T/m 25 oktober», «Zaterdag 1 november van 10 tot 16 uur», «, vanaf 28 november».

    Год, если не указан, — текущий (museum.nl пишет год только для других лет);
    если начало без года оказывается позже конца — значит, это прошлый год."""
    if not text:
        return None, None
    found = [(int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3)) if m.group(3) else None)
             for m in _DATE_RX.finditer(text)]
    if not found:
        return None, None
    has_until = re.search(r"\bt/?m\b|tot en met", text, re.IGNORECASE) is not None
    has_from = re.search(r"\bvanaf\b", text, re.IGNORECASE) is not None
    start = end = None
    if has_until:
        d, mth, y = found[-1]
        end = _mk(d, mth, y, today)
        if len(found) > 1 or has_from:
            d, mth, y = found[0]
            start = _mk(d, mth, y or (end.year if end else None), today)
            if start and end and start > end and y is None:
                start = _mk(d, mth, start.year - 1, today)
    else:
        d, mth, y = found[0]
        start = _mk(d, mth, y, today)
        if not has_from:  # однодневная выставка / мероприятие
            end = start
    return start, end
