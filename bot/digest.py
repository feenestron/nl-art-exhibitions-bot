"""Сбор кандидатов, отбор и оформление выпуска."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape

from .fetch import Fetcher
from .sources import Exhibition, SourceResult, Window, museum_nl, norm_title, venue_page

log = logging.getLogger(__name__)

TG_LIMIT = 4096
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


@dataclass
class Candidate:
    id: str
    section: str          # "new" | "last"
    ex: Exhibition


@dataclass
class Collected:
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stats: dict[str, str] = field(default_factory=dict)


@dataclass
class Line:
    section: str
    artists: str
    title: str
    summary: str
    ex: Exhibition


# --------------------------------------------------------------------- окно

def make_window(today: date, period_days: int, last_post: date | None, interval_days: int) -> Window:
    """Новые выставки берём с прошлого выпуска (чтобы не пропустить открывшиеся между
    выпусками и объявленные поздно), но не дальше, чем на интервал назад."""
    since = today - timedelta(days=7)
    if last_post and last_post < today:
        since = max(last_post, today - timedelta(days=interval_days))
    return Window(today, today + timedelta(days=period_days - 1), since)


def classify(ex: Exhibition, w: Window) -> str | None:
    if ex.start and w.opened_since <= ex.start <= w.period_end:
        if ex.start >= w.today or not ex.end or ex.end >= w.today:
            return "new"
    if ex.end and w.today <= ex.end <= w.period_end and (not ex.start or ex.start < w.today):
        return "last"
    return None


# --------------------------------------------------------------------- сбор

def collect(sources: list[dict], fetcher: Fetcher, w: Window, announced: dict,
            max_fetch: int, extract=None) -> Collected:
    """extract(text, src) — извлечение выставок со страницы музея (Claude); None — пропустить."""
    out = Collected()
    items: list[Exhibition] = []
    for src in sources:
        name = src["name"]
        try:
            if src["type"] == "museum_nl":
                res = museum_nl(src, fetcher, w, max_fetch)
            elif src["type"] == "venue_page":
                if extract is None:
                    out.errors.append(f"{name}: пропущен — нет ANTHROPIC_API_KEY")
                    continue
                res = venue_page(src, fetcher, extract)
            else:
                out.errors.append(f"{name}: неизвестный тип {src['type']}")
                continue
        except Exception as exc:
            log.exception("Источник %s упал", name)
            out.errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:150]}")
            continue
        out.errors.extend(res.errors)
        out.stats[name] = ", ".join(f"{k} {v}" for k, v in res.stats.items())
        items.extend(res.items)

    seen: set[tuple[str, str]] = set()
    for ex in items:
        section = classify(ex, w)
        if not section or f"{section}:{ex.url}" in announced:
            continue
        key = (norm_title(ex.title), norm_title(ex.venue)[:12])
        if key in seen:  # одна и та же выставка из двух источников
            continue
        seen.add(key)
        out.candidates.append(Candidate(f"x{len(out.candidates)}", section, ex))
    return out


def selection_payload(c: Collected, w: Window, max_new: int, max_last: int) -> dict:
    return {
        "today": w.today.isoformat(),
        "period": f"{w.today.isoformat()} .. {w.period_end.isoformat()}",
        "limits": {"new_max": max_new, "last_max": max_last},
        "exhibitions": [
            {"id": cand.id, "section": cand.section, "title": cand.ex.title,
             "venue": cand.ex.venue, "city": cand.ex.city,
             "start": cand.ex.start.isoformat() if cand.ex.start else None,
             "end": cand.ex.end.isoformat() if cand.ex.end else None,
             "text": cand.ex.text[:700]}
            for cand in c.candidates
        ],
    }


def lines_from_selection(selection: list[dict], c: Collected, max_new: int, max_last: int) -> list[Line]:
    """Ответ модели → строки выпуска; ссылки, даты и места берутся только из наших данных."""
    by_id = {cand.id: cand for cand in c.candidates}
    lines, used = [], set()
    for item in selection:
        cand = by_id.get(str(item.get("id")))
        if not cand or cand.id in used:
            continue
        used.add(cand.id)
        lines.append(Line(cand.section, (item.get("artists") or "").strip(),
                          (item.get("title") or "").strip() or cand.ex.title,
                          (item.get("summary") or "").strip(), cand.ex))
    far = date.max
    new = sorted((x for x in lines if x.section == "new"), key=lambda x: (x.ex.start or far, x.title))
    last = sorted((x for x in lines if x.section == "last"), key=lambda x: (x.ex.end or far, x.title))
    return new[:max_new] + last[:max_last]


# ---------------------------------------------------------------- оформление

def ru_date(d: date, today: date, with_year: bool | None = None) -> str:
    text = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    if with_year or (with_year is None and d.year != today.year):
        text += f" {d.year}"
    return text


def period_text(ex: Exhibition, section: str, today: date) -> str:
    if section == "last" and ex.end:
        return f"до {ru_date(ex.end, today)}"
    if ex.start and ex.end:
        if ex.start == ex.end:
            return ru_date(ex.start, today)
        return f"с {ru_date(ex.start, today)} по {ru_date(ex.end, today)}"
    if ex.start:
        return f"с {ru_date(ex.start, today)}"
    if ex.end:
        return f"до {ru_date(ex.end, today)}"
    return ""


def render_line(line: Line, today: date) -> str:
    ex = line.ex
    head = f"<b>{escape(line.artists)}</b> — " if line.artists else ""
    head += f'<a href="{escape(ex.link, quote=True)}">{escape(line.title)}</a>'
    # «Kunsthal Rotterdam, Rotterdam» → «Kunsthal Rotterdam»
    if ex.city and ex.city.lower() not in ex.venue.lower():
        place = ", ".join(x for x in (ex.venue, ex.city) if x)
    else:
        place = ex.venue or ex.city
    meta = " · ".join(x for x in (escape(place), period_text(ex, line.section, today)) if x)
    parts = [f"• {head}", meta]
    if line.summary:
        parts.append(f"<i>{escape(line.summary)}</i>")
    return "\n".join(p for p in parts if p)


def render(lines: list[Line], w: Window) -> list[str]:
    """Выпуск в HTML для Telegram; длинный — несколько сообщений по границам пунктов."""
    header = (f"🎨 <b>Выставки в Нидерландах</b>\n"
              f"{ru_date(w.today, w.today)} — {ru_date(w.period_end, w.today)}")
    blocks: list[str] = [header]
    for section, title in (("new", "🆕 <b>Открываются</b>"), ("last", "⏳ <b>Последний шанс</b>")):
        part = [x for x in lines if x.section == section]
        if part:
            blocks.append(title)
            blocks.extend(render_line(x, w.today) for x in part)
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > TG_LIMIT and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
