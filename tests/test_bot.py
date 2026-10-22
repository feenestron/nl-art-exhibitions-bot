"""Офлайн-тесты: разметка как у museum.nl, фейковые Fetcher, Claude и Telegram."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from bot import __main__ as main_mod
from bot import state as state_mod
from bot.config import Config
from bot.digest import (Collected, Candidate, collect, lines_from_selection, make_window, period_text,
                        render, selection_payload)
from bot.fetch import page_with_links, parse_period
from bot.sources import Exhibition, parse_museum_nl_detail, parse_museum_nl_listing

TZ = ZoneInfo("Europe/Amsterdam")
TODAY = date(2026, 10, 23)  # пятница


def card(href, title, when=None, where=None, last=False):
    attrs = ""
    if when is not None:
        attrs += f"""<div class="item-attribute"><svg class="icon-time-solid"></svg>
<component is="p" class="typography small-label">{when}</component></div>"""
    if where is not None:
        attrs += f"""<div class="item-attribute"><svg class="icon-location"></svg>
<component is="p" class="typography small-label">{where}</component></div>"""
    cls = "grid-item see-and-do-item" + (" see-and-do-item_last-chance" if last else "")
    return f"""<li class="{cls}"><div class="see-and-do-card"><a href="{href}">
<div class="see-and-do-card_info"><component is="h4" class="typography heading-4">{title}</component>
<component is="span" class="typography small-label">Tentoonstelling</component>{attrs}</div></a></div></li>"""


LISTING = f"""<section class="see-and-do-items"><span role="status">5 resultaten</span>
<ul class="grid four-columns">
{card("/nl/museum-more/tentoonstelling/charley-toorop-pyke-koch", "Charley Toorop &amp; Pyke Koch. Alles of niets",
      "T/m 1 november", "Museum MORE, Gorssel", last=True)}
{card("/nl/kunsthal-rotterdam/tentoonstelling/signac", "Signac. Symphony of Colours",
      "Van 10:00 tot 17:00 vanaf 24 oktober 2026 t/m 28 februari 2027", "Kunsthal  Rotterdam, Rotterdam")}
{card("/nl/museum-x/tentoonstelling/vaste-collectie", "Vaste collectie", None, "Museum X, Utrecht")}
{card("/nl/kunstmuseum-den-haag/tentoonstelling/mondriaan", "Mondriaans laatste meesterwerken",
      "Vanaf 10 september 2027 t/m 30 januari 2028", "Kunstmuseum Den Haag, Den Haag")}
{card("/nl/oudheidkamer/tentoonstelling/molens", "Molens van de streek", "T/m 31 oktober", "Oudheidkamer, Ergens")}
</ul></section>"""

DETAIL_SIGNAC = """<html><head><script type="application/ld+json">
\t\t\t{
\t\t\t\t"@context": "http://schema.org",
\t\t\t\t"@type": "ExhibitionEvent",
\t\t\t\t"name": "Signac. Symphony of Colours",
\t\t\t\t"description": "Een overzicht van Paul Signac (1863&#x2013;1935), de meester van het neo-impressionisme.",
\t\t\t\t"url" : "https://www.museum.nl/nl/kunsthal-rotterdam/tentoonstelling/signac",
\t\t\t\t"startDate" : "2026-10-24","endDate": "2027-02-28",
\t\t\t\t"offers": [{"@type": "Offer", "name": "Met Museumkaart", "price": "0.00",
\t\t\t\t\t"url": "https://www.kunsthal.nl/nl/plan-je-bezoek/tentoonstellingen/signac/"}],
\t\t\t\t"location": {"@type": "Museum", "address": {"@type": "PostalAddress",
\t\t\t\t\t"addressLocality": "Rotterdam"}, "name": "Kunsthal Rotterdam"}
\t\t\t}
</script></head><body><main><h1>Signac</h1><p>Pointillisme aan de Maas.</p></main></body></html>"""

DETAIL_TOOROP = """<html><head><script type="application/ld+json">
{"@type": "ExhibitionEvent", "name": "Charley Toorop & Pyke Koch", "startDate": "2026-06-06",
 "endDate": "2026-11-01", "description": "Twee grootheden van het Nederlandse realisme.",
 "location": {"name": "Museum MORE", "address": {"addressLocality": "Gorssel"}}}
</script></head><body><main>Toorop en Koch</main></body></html>"""

DETAIL_MOLENS = """<html><head><script type="application/ld+json">
{"@type": "ExhibitionEvent", "name": "Molens", "startDate": "2026-05-01", "endDate": "2026-10-31"}
</script></head><body><main>Molens</main></body></html>"""


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeFetcher:
    def __init__(self, pages: dict[str, str], listing: str = LISTING):
        self.pages = pages
        self.listing = listing
        self.got: list[str] = []

    def post(self, url, **kwargs):
        assert "zoeken" in url
        assert kwargs["json"]["EventTypes"] == ["Exhibition"]
        return FakeResponse(self.listing)

    def get(self, url, **kwargs):
        self.got.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return FakeResponse(self.pages[url])


PAGES = {
    "https://www.museum.nl/nl/kunsthal-rotterdam/tentoonstelling/signac": DETAIL_SIGNAC,
    "https://www.museum.nl/nl/museum-more/tentoonstelling/charley-toorop-pyke-koch": DETAIL_TOOROP,
    "https://www.museum.nl/nl/oudheidkamer/tentoonstelling/molens": DETAIL_MOLENS,
}


MNL = {"name": "museum.nl", "type": "museum_nl", "min_cards": 1}


class DatesTest(unittest.TestCase):
    def test_parse_period(self):
        cases = {
            "T/m 25 oktober van 09 tot 18 uur": (None, date(2026, 10, 25)),
            "Vanaf 24 oktober 2026 t/m 28 februari 2027": (date(2026, 10, 24), date(2027, 2, 28)),
            "Vanaf 26 oktober t/m 7 november": (date(2026, 10, 26), date(2026, 11, 7)),
            "Van 11 tot 17 uur vanaf 21 november 2026 t/m 17 mei 2027": (date(2026, 11, 21), date(2027, 5, 17)),
            "20 november 2026 t/m 17 januari 2027, meerdere opties": (date(2026, 11, 20), date(2027, 1, 17)),
            ", vanaf 28 november": (date(2026, 11, 28), None),
            "Zaterdag 7 november van 10 tot 16 uur": (date(2026, 11, 7), date(2026, 11, 7)),
            "T/m 21 maart 2027": (None, date(2027, 3, 21)),
            "Vanaf 20 december t/m 5 januari 2027": (date(2026, 12, 20), date(2027, 1, 5)),
            "Museum MORE, Gorssel": (None, None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_period(text, TODAY), expected)


class MuseumNlTest(unittest.TestCase):
    def test_listing(self):
        items = parse_museum_nl_listing(LISTING)
        self.assertEqual(len(items), 5)
        first = items[0]
        self.assertEqual(first["url"], "https://www.museum.nl/nl/museum-more/tentoonstelling/charley-toorop-pyke-koch")
        self.assertEqual(first["title"], "Charley Toorop & Pyke Koch. Alles of niets")
        self.assertEqual((first["venue"], first["city"], first["when"]), ("Museum MORE", "Gorssel", "T/m 1 november"))
        self.assertTrue(first["last_chance"])
        self.assertEqual(items[2]["when"], "")  # постоянная экспозиция без дат
        self.assertEqual(items[1]["venue"], "Kunsthal Rotterdam")

    def test_detail(self):
        d = parse_museum_nl_detail(DETAIL_SIGNAC)
        self.assertEqual((d["start"], d["end"]), (date(2026, 10, 24), date(2027, 2, 28)))
        self.assertEqual(d["city"], "Rotterdam")
        self.assertIn("Paul Signac (1863–1935)", d["description"])
        self.assertEqual(d["official_url"], "https://www.kunsthal.nl/nl/plan-je-bezoek/tentoonstellingen/signac/")


class CollectTest(unittest.TestCase):
    def window(self, last_post=None):
        return make_window(TODAY, 14, last_post, 14)

    def test_window(self):
        w = self.window()
        self.assertEqual((w.today, w.period_end, w.opened_since), (TODAY, date(2026, 11, 5), date(2026, 10, 16)))
        w = self.window(date(2026, 10, 9))
        self.assertEqual(w.opened_since, date(2026, 10, 9))

    def test_collect_and_classify(self):
        f = FakeFetcher(PAGES)
        c = collect([MNL], f, self.window(), {}, 50)
        got = {cand.ex.title: cand.section for cand in c.candidates}
        self.assertEqual(got, {
            "Signac. Symphony of Colours": "new",
            "Charley Toorop & Pyke Koch. Alles of niets": "last",
            "Molens van de streek": "last",
        })
        # постоянная экспозиция и выставка 2027 года страницы не открывали
        self.assertEqual(len(f.got), 3)
        signac = next(x.ex for x in c.candidates if x.section == "new")
        self.assertEqual(signac.link, "https://www.kunsthal.nl/nl/plan-je-bezoek/tentoonstellingen/signac/")
        self.assertEqual(signac.city, "Rotterdam")
        self.assertEqual(c.errors, [])

    def test_announced_skipped_per_section(self):
        url = "https://www.museum.nl/nl/museum-more/tentoonstelling/charley-toorop-pyke-koch"
        c = collect([MNL], FakeFetcher(PAGES), self.window(),
                    {f"new:{url}": "2026-06-05"}, 50)
        self.assertIn(url, [x.ex.url for x in c.candidates])  # объявлена как новая — но не как «последний шанс»
        c = collect([MNL], FakeFetcher(PAGES), self.window(),
                    {f"last:{url}": "2026-10-09"}, 50)
        self.assertNotIn(url, [x.ex.url for x in c.candidates])

    def test_budget_and_broken_page(self):
        pages = dict(PAGES)
        del pages["https://www.museum.nl/nl/oudheidkamer/tentoonstelling/molens"]
        c = collect([MNL], FakeFetcher(pages), self.window(), {}, 2)
        self.assertTrue(any("MAX_FETCH" in e for e in c.errors))
        c = collect([MNL], FakeFetcher(pages), self.window(), {}, 50)
        self.assertTrue(any("не открылись 1" in e for e in c.errors))
        # дата из списка осталась — выставка всё равно в кандидатах
        self.assertIn("Molens van de streek", [x.ex.title for x in c.candidates])

    def test_venue_page_and_dedupe(self):
        venue_html = """<html><body><nav>menu</nav><main>
<h2>Nu</h2><a href="/tentoonstellingen/icons">Celebrating the Icons of Art</a> 12 september 2026 - 28 februari 2027
<h2>Verwacht</h2><a href="/tentoonstellingen/eliasson">Olafur Eliasson</a> 20 maart - 5 september 2027
<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.
Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.</p></main></body></html>"""
        pages = dict(PAGES, **{"https://www.voorlinden.nl/tentoonstellingen/": venue_html})
        seen_text = []

        def extract(text, src):
            seen_text.append(text)
            return [{"title": "Olafur Eliasson", "start": "2026-10-30", "end": "2027-09-05",
                     "url": "https://www.voorlinden.nl/tentoonstellingen/eliasson"},
                    {"title": "Signac. Symphony of Colours", "start": "2026-10-24", "end": "2027-02-28"}]

        sources = [MNL,
                   {"name": "Voorlinden", "type": "venue_page", "url": "https://www.voorlinden.nl/tentoonstellingen/",
                    "venue": "Kunsthal Rotterdam", "city": "Wassenaar"}]
        c = collect(sources, FakeFetcher(pages), self.window(), {}, 50, extract)
        self.assertIn("[https://www.voorlinden.nl/tentoonstellingen/eliasson]", seen_text[0])
        self.assertNotIn("menu", seen_text[0])
        titles = [x.ex.title for x in c.candidates]
        self.assertEqual(titles.count("Signac. Symphony of Colours"), 1)
        self.assertIn("Olafur Eliasson", titles)

    def test_health_checks(self):
        # Обычный порог «мало карточек» срабатывает на маленьком списке
        c = collect([{"name": "museum.nl", "type": "museum_nl"}], FakeFetcher(PAGES), self.window(), {}, 50)
        self.assertTrue(any("всего 5 выставок" in e for e in c.errors))
        # Страницы выставок без JSON-LD: даты из списка, но с предупреждением
        many = "".join(card(f"/nl/m/tentoonstelling/x{i}", f"X{i}", "T/m 1 november", "M, C") for i in range(12))
        pages = {f"https://www.museum.nl/nl/m/tentoonstelling/x{i}": "<html><main>x</main></html>" for i in range(12)}
        c = collect([MNL], FakeFetcher(pages, f"<ul>{many}</ul>"), self.window(), {}, 50)
        self.assertTrue(any("нет точных дат" in e for e in c.errors))
        self.assertEqual(len(c.candidates), 12)  # выставки при этом не потеряны
        # Пустая страница музея
        empty = "<html><main>" + "Er zijn op dit moment geen tentoonstellingen. " * 10 + "</main></html>"
        c = collect([{"name": "V", "type": "venue_page", "url": "https://v.nl/t"}],
                    FakeFetcher({"https://v.nl/t": empty}), self.window(), {}, 50, lambda t, s: [])
        self.assertTrue(any("ни одной выставки" in e for e in c.errors))

    def test_api_error_hints(self):
        from bot.llm import explain_api_error
        self.assertIn("баланс", explain_api_error(400, '{"error":{"message":"Your credit balance is too low"}}', "m"))
        self.assertIn("ANTHROPIC_MODEL", explain_api_error(404, '{"error":{"type":"not_found_error","message":"model: x"}}', "x"))
        self.assertIn("недействителен", explain_api_error(401, '{"error":{"type":"authentication_error"}}', "m"))

    def test_venue_page_without_key(self):
        sources = [{"name": "Voorlinden", "type": "venue_page", "url": "https://x.nl/"}]
        c = collect(sources, FakeFetcher({}), self.window(), {}, 50, None)
        self.assertTrue(c.errors and "ANTHROPIC_API_KEY" in c.errors[0])


def sample_collected() -> Collected:
    c = Collected()
    c.candidates = [
        Candidate("x0", "new", Exhibition("museum.nl", "Signac. Symphony of Colours",
                                          "https://www.museum.nl/signac", "Kunsthal Rotterdam", "Rotterdam",
                                          date(2026, 10, 24), date(2027, 2, 28),
                                          official_url="https://www.kunsthal.nl/signac?a=1&b=2")),
        Candidate("x1", "last", Exhibition("museum.nl", "Charley Toorop & Pyke Koch", "https://www.museum.nl/toorop",
                                           "Museum MORE", "Gorssel", date(2026, 6, 6), date(2026, 11, 1))),
        Candidate("x2", "new", Exhibition("museum.nl", "Hals – Rembrandt", "https://www.museum.nl/hals",
                                          "Frans Hals Museum", "Haarlem", date(2026, 11, 6), date(2027, 2, 28))),
        Candidate("x3", "last", Exhibition("museum.nl", "Molens", "https://www.museum.nl/molens",
                                           "Oudheidkamer", "Ergens", None, date(2026, 10, 31))),
    ]
    return c


class RenderTest(unittest.TestCase):
    def test_selection_and_render(self):
        c = sample_collected()
        w = make_window(TODAY, 14, None, 14)
        payload = selection_payload(c, w, 15, 10)
        self.assertEqual(len(payload["exhibitions"]), 4)
        selection = [
            {"id": "x2", "artists": "Франс Хальс, Рембрандт", "title": "Hals – Rembrandt", "summary": "Два гения."},
            {"id": "x1", "artists": "Шарли Тороп и Пейк Кох", "title": "Charley Toorop & Pyke Koch", "summary": ""},
            {"id": "x0", "artists": "Поль Синьяк", "title": "Signac. Symphony of Colours",
             "summary": "Большой обзор <пуантилизма>."},
            {"id": "x0", "artists": "дубль", "title": "", "summary": ""},
            {"id": "x99", "artists": "выдумка", "title": "", "summary": ""},
        ]
        lines = lines_from_selection(selection, c, 15, 10)
        self.assertEqual([x.ex.title for x in lines],
                         ["Signac. Symphony of Colours", "Hals – Rembrandt", "Charley Toorop & Pyke Koch"])
        messages = render(lines, w)
        self.assertEqual(len(messages), 1)
        text = messages[0]
        self.assertIn("23 октября — 5 ноября", text)
        self.assertIn('<b>Поль Синьяк</b> — <a href="https://www.kunsthal.nl/signac?a=1&amp;b=2">', text)
        self.assertIn("Kunsthal Rotterdam · с 24 октября по 28 февраля 2027", text)
        self.assertIn("Museum MORE, Gorssel · до 1 ноября", text)
        self.assertIn("&lt;пуантилизма&gt;", text)
        self.assertLess(text.index("Открываются"), text.index("Последний шанс"))
        self.assertLess(text.index("Синьяк"), text.index("Хальс"))

    def test_split_long(self):
        c = sample_collected()
        w = make_window(TODAY, 14, None, 14)
        big = [{"id": "x0", "artists": "А", "title": "T", "summary": "ы" * 3000},
               {"id": "x2", "artists": "Б", "title": "T", "summary": "ы" * 3000}]
        messages = render(lines_from_selection(big, c, 15, 10), w)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(len(m) <= 4096 for m in messages))

    def test_period_text(self):
        ex = Exhibition("s", "t", "u", start=date(2026, 11, 7), end=date(2026, 11, 7))
        self.assertEqual(period_text(ex, "new", TODAY), "7 ноября")
        ex = Exhibition("s", "t", "u", start=date(2026, 11, 28))
        self.assertEqual(period_text(ex, "new", TODAY), "с 28 ноября")


def make_cfg(tmp: str, **kw) -> Config:
    base = dict(bot_token="T", chat_id="-100500", admin_chat_id="", anthropic_key="K", model="m", tz=TZ,
                post_weekday=4, post_hour=9, interval_days=14, period_days=14, max_new=15, max_last=10,
                max_fetch=50, state_path=os.path.join(tmp, "state.json"), sources_path="sources.yaml")
    base.update(kw)
    return Config(**base)


class ScheduleTest(unittest.TestCase):
    def test_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            fri = datetime(2026, 10, 23, 9, 17, tzinfo=TZ)
            st = state_mod.load(cfg.state_path)
            self.assertTrue(main_mod.due(cfg, st, fri)[0])
            self.assertFalse(main_mod.due(cfg, st, fri.replace(hour=7))[0])
            self.assertFalse(main_mod.due(cfg, st, datetime(2026, 10, 22, 9, 17, tzinfo=TZ))[0])
            st["last_post_date"] = "2026-10-23"
            self.assertFalse(main_mod.due(cfg, st, fri)[0])  # уже было сегодня
            self.assertFalse(main_mod.due(cfg, st, datetime(2026, 10, 30, 9, 17, tzinfo=TZ))[0])  # через неделю
            self.assertTrue(main_mod.due(cfg, st, datetime(2026, 11, 6, 9, 17, tzinfo=TZ))[0])   # через две
            self.assertTrue(main_mod.due(cfg, st, datetime(2026, 11, 13, 10, 0, tzinfo=TZ))[0])  # пропущенный


class FakeTelegram:
    sent: list = []

    def __init__(self, token):
        pass

    def send_html(self, chat_id, text, preview=False):
        FakeTelegram.sent.append((chat_id, text))
        return {}

    def send_text(self, chat_id, text):
        FakeTelegram.sent.append((chat_id, text))
        return {}


class PostFlowTest(unittest.TestCase):
    def test_post_marks_announced(self):
        FakeTelegram.sent = []
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, admin_chat_id="42")
            c = sample_collected()
            c.errors = ["H'ART: упал"]
            selection = [{"id": "x0", "artists": "Поль Синьяк", "title": "Signac", "summary": "s"},
                         {"id": "x1", "artists": "Тороп", "title": "Toorop", "summary": ""}]
            fixed_now = datetime(2026, 10, 23, 9, 20, tzinfo=TZ)

            class FixedDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed_now

            with mock.patch.object(main_mod, "datetime", FixedDT), \
                    mock.patch.object(main_mod, "collect", return_value=c), \
                    mock.patch.object(main_mod, "load_sources", return_value=[]), \
                    mock.patch.object(main_mod, "select", return_value=selection), \
                    mock.patch.object(main_mod, "Telegram", FakeTelegram), \
                    mock.patch.object(main_mod, "write_summary"):
                main_mod.cmd_post(cfg, mock.Mock(force=False, dry_run=False))
                st = state_mod.load(cfg.state_path)
                self.assertEqual(st["last_post_date"], "2026-10-23")
                self.assertEqual(set(st["announced"]), {"new:https://www.museum.nl/signac",
                                                        "last:https://www.museum.nl/toorop"})
                self.assertEqual(FakeTelegram.sent[0][0], "-100500")
                self.assertIn("Поль Синьяк", FakeTelegram.sent[0][1])
                self.assertEqual(FakeTelegram.sent[1][0], "42")  # админу — о проблеме с источником
                self.assertIn("H'ART", FakeTelegram.sent[1][1])

                # повторный запуск в тот же день ничего не шлёт
                n = len(FakeTelegram.sent)
                main_mod.cmd_post(cfg, mock.Mock(force=False, dry_run=False))
                self.assertEqual(len(FakeTelegram.sent), n)


class HelpersTest(unittest.TestCase):
    def test_page_with_links(self):
        html = '<html><body><header>x</header><main><a href="/a">A</a> <a href="https://other.nl/b">B</a></main></body></html>'
        text = page_with_links(html, "https://m.nl/list")
        self.assertIn("A [https://m.nl/a]", text)
        self.assertNotIn("other.nl", text)

    def test_sources_yaml(self):
        from bot.sources import load_sources
        names = [s["name"] for s in load_sources("sources.yaml")]
        self.assertEqual(names[0], "museum.nl")
        self.assertNotIn("Moco Museum", names)


if __name__ == "__main__":
    unittest.main()
