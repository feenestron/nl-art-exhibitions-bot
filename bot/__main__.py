"""Точка входа.

    python -m bot post [--force] [--dry-run]
        Собрать выставки, отобрать известных художников и опубликовать выпуск в группу.
        Без --force публикует только в день и час по расписанию и не чаще INTERVAL_DAYS.
        --dry-run печатает выпуск и кандидатов, ничего не отправляет и не меняет состояние.

    python -m bot chats
        Показать группы, в которые добавлен бот, и их chat_id (для секрета CHAT_ID).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

from . import state as state_mod
from .config import Config
from .digest import Collected, collect, lines_from_selection, make_window, render, selection_payload
from .fetch import Fetcher
from .llm import Claude, extract_exhibitions, select
from .sources import load_sources
from .telegram import Telegram, TelegramError

log = logging.getLogger("bot")


def write_summary(text: str) -> None:
    log.info("\n%s", text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")


def due(cfg: Config, st: dict, now: datetime) -> tuple[bool, str]:
    today = now.date()
    last = date.fromisoformat(st["last_post_date"]) if st.get("last_post_date") else None
    if last == today:
        return False, "сегодня выпуск уже был"
    if now.weekday() != cfg.post_weekday:
        return False, "не день выпуска"
    if not (cfg.post_hour <= now.hour < cfg.post_hour + 4):
        return False, f"сейчас {now:%H:%M}, выпуск с {cfg.post_hour}:00"
    # «-1»: запуск по cron может прийти на день раньше по UTC; неделя «через раз» —
    # следующая пятница после выпуска даёт 7 дней и пропускается.
    if last and (today - last).days < cfg.interval_days - 1:
        return False, f"прошлый выпуск {last}, следующий не раньше чем через {cfg.interval_days} дней"
    return True, ""


def report(c: Collected, messages: list[str], now: datetime) -> str:
    lines = [f"# Выпуск {now:%Y-%m-%d %H:%M}", "", "## Источники", ""]
    lines += [f"- {k}: {v}" for k, v in c.stats.items()]
    if c.errors:
        lines += ["", "## Проблемы", ""] + [f"- {e}" for e in c.errors]
    for i, m in enumerate(messages, 1):
        lines += ["", f"## Сообщение {i} (HTML для Telegram)", "", "```html", m, "```"]
    if not messages:
        lines += ["", "_Публиковать нечего._"]
    lines += ["", f"## Кандидаты ({len(c.candidates)})", ""]
    for cand in c.candidates:
        ex = cand.ex
        when = f"{ex.start or '?'} .. {ex.end or '?'}"
        lines.append(f"- [{cand.section}] {when} — [{ex.title}]({ex.link}) — {ex.venue}, {ex.city}")
    return "\n".join(lines) + "\n"


def cmd_post(cfg: Config, args) -> int:
    now = datetime.now(cfg.tz)
    today = now.date()
    st = state_mod.load(cfg.state_path)
    if not args.force and not args.dry_run:
        ok, why = due(cfg, st, now)
        if not ok:
            log.info("Пропускаю: %s", why)
            return 0
    if not args.dry_run:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", cfg.bot_token), ("CHAT_ID", cfg.chat_id),
                                  ("ANTHROPIC_API_KEY", cfg.anthropic_key)) if not v]
        if missing:
            raise SystemExit(f"Не заданы секреты: {', '.join(missing)}")

    last = date.fromisoformat(st["last_post_date"]) if st.get("last_post_date") else None
    w = make_window(today, cfg.period_days, last, cfg.interval_days)
    claude = Claude(cfg.anthropic_key, cfg.model) if cfg.anthropic_key else None
    extract = (lambda text, src: extract_exhibitions(claude, text, src, today)) if claude else None

    c = collect(load_sources(cfg.sources_path), Fetcher(), w, st["announced"], cfg.max_fetch, extract)
    log.info("Кандидатов: %d (новых %d, последний шанс %d), ошибок %d", len(c.candidates),
             sum(x.section == "new" for x in c.candidates),
             sum(x.section == "last" for x in c.candidates), len(c.errors))

    lines = []
    if c.candidates and claude:
        selection = select(claude, selection_payload(c, w, cfg.max_new, cfg.max_last))
        lines = lines_from_selection(selection, c, cfg.max_new, cfg.max_last)
    elif c.candidates:
        c.errors.append("отбор не выполнен — нет ANTHROPIC_API_KEY (в пробном прогоне это нормально)")
    messages = render(lines, w) if lines else []

    text = report(c, messages, now)
    write_summary(text)
    if args.dry_run:
        os.makedirs("preview", exist_ok=True)
        with open("preview/last_dry_run.md", "w", encoding="utf-8") as f:
            f.write(text)
        return 0

    tg = Telegram(cfg.bot_token)
    chat_id = cfg.chat_id
    for m in messages:
        try:
            tg.send_html(chat_id, m)
        except TelegramError as exc:
            new_id = exc.parameters.get("migrate_to_chat_id")
            if not new_id:
                raise
            # группа стала супергруппой — у неё новый id
            chat_id = str(new_id)
            tg.send_html(chat_id, m)
            c.errors.append(f"группа сменила id на {new_id} — обновите секрет CHAT_ID")

    for line in lines:
        st["announced"][f"{line.section}:{line.ex.url}"] = today.isoformat()
    st["last_post_date"] = today.isoformat()
    st["history"].append({"date": today.isoformat(), "new": sum(x.section == "new" for x in lines),
                          "last": sum(x.section == "last" for x in lines)})
    state_mod.prune(st, today)
    state_mod.save(cfg.state_path, st)

    if cfg.admin_chat_id and (c.errors or not messages):
        note = [f"🎨 Выпуск {today:%d.%m}: " + (f"опубликовано {len(lines)} выставок."
                                               if messages else "подходящих выставок не нашлось, "
                                               "в группу ничего не отправлено.")]
        if c.errors:
            note.append("\n⚠️ Проблемы:\n" + "\n".join(f"• {e}" for e in c.errors))
        try:
            tg.send_text(cfg.admin_chat_id, "\n".join(note))
        except TelegramError as exc:
            log.warning("Не удалось написать админу: %s", exc)
    return 0


def cmd_chats(cfg: Config, args) -> int:
    if not cfg.bot_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    st = state_mod.load(cfg.state_path)
    tg = Telegram(cfg.bot_token)
    me = tg.call("getMe")
    updates = tg.get_updates(st["tg_offset"])
    for u in updates:
        st["tg_offset"] = max(st["tg_offset"], u["update_id"] + 1)
        chat = (u.get("my_chat_member") or u.get("message") or {}).get("chat") or {}
        if chat.get("id") and chat.get("type") != "private":
            st["known_chats"][str(chat["id"])] = f"{chat.get('title', '')} ({chat.get('type')})"
        member = u.get("my_chat_member", {}).get("new_chat_member", {})
        if member.get("status") in ("left", "kicked"):
            st["known_chats"].pop(str(chat.get("id")), None)
    state_mod.save(cfg.state_path, st)
    lines = [f"## Бот @{me.get('username')}", ""]
    if st["known_chats"]:
        lines += ["Группы, куда добавлен бот (значение для секрета `CHAT_ID`):", ""]
        lines += [f"- `{cid}` — {title}" for cid, title in st["known_chats"].items()]
    else:
        lines.append("Бот пока не видит ни одной группы. Добавьте его в группу (или удалите и "
                     "добавьте снова, если он уже там) и запустите этот workflow ещё раз.")
    if cfg.chat_id:
        lines += ["", f"Сейчас в `CHAT_ID`: `{cfg.chat_id}`"]
    write_summary("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    post = sub.add_parser("post")
    post.add_argument("--force", action="store_true", help="игнорировать расписание")
    post.add_argument("--dry-run", action="store_true", help="только напечатать, ничего не отправлять")
    sub.add_parser("chats")
    args = p.parse_args(argv)
    cfg = Config.from_env()
    if args.cmd == "chats":
        return cmd_chats(cfg, args)
    try:
        return cmd_post(cfg, args)
    except Exception as exc:
        # Причину — сразу админу; workflow всё равно упадёт и пришлёт ссылку на лог.
        if cfg.bot_token and cfg.admin_chat_id and not args.dry_run:
            try:
                Telegram(cfg.bot_token).send_text(
                    cfg.admin_chat_id, f"⚠️ Выпуск выставок не опубликован: {str(exc)[:600]}")
            except Exception:
                log.exception("Не удалось написать админу")
        raise


if __name__ == "__main__":
    sys.exit(main())
