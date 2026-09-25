#!/usr/bin/env python3
"""order-radar: монитор свежих заказов FL.ru -> Telegram.

Использование:
    python3 radar.py once        # один проход (для cron/launchd)
    python3 radar.py loop 300    # бесконечный цикл с паузой 300 сек

Настройка (переменные окружения):
    TG_BOT_TOKEN   — токен бота от @BotFather; если пусто — печать в консоль
    TG_CHAT_ID     — id чата для уведомлений
    RSS_URL        — лента (по умолчанию https://www.fl.ru/rss/all.xml)
    KEYWORDS_FILE  — файл ключевых слов (по умолчанию keywords.txt рядом)

Зависимостей нет: только стандартная библиотека Python 3.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "radar.db"
KEYWORDS_FILE = Path(os.getenv("KEYWORDS_FILE", BASE / "keywords.txt"))
RSS_URL = os.getenv("RSS_URL", "https://www.fl.ru/rss/all.xml")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
MAX_SEND_PER_RUN = 10  # защита от флуда

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def load_keywords() -> list[tuple[str, "re.Pattern[str] | None"]]:
    """Строка = подстрока (регистр не важен); 're:...' = регулярное выражение."""
    out: list[tuple[str, "re.Pattern[str] | None"]] = []
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append((line, re.compile(line[3:], re.IGNORECASE)))
        else:
            out.append((line, None))
    return out


def match_keyword(text: str, keywords) -> str | None:
    low = text.lower()
    for label, rx in keywords:
        if rx is not None:
            if rx.search(text):
                return label
        elif label.lower() in low:
            return label
    return None


def fetch_items() -> list[dict]:
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # FL.ru за ddos-guard: вместо XML иногда прилетает HTML-челлендж или
        # заглушка. Даём понятную ошибку вместо сырого ParseError в логах cron.
        head = raw[:200].decode("utf-8", "replace").strip()
        raise RuntimeError(
            "Лента вернула не XML (вероятно, защита от ботов или временная "
            f"недоступность). Начало ответа: {head!r}"
        )
    items = []
    for it in root.iter("item"):
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "description": (it.findtext("description") or "").strip(),
        })
    return items


def db(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY, first_seen TEXT DEFAULT (datetime('now')))")


def is_new(conn: sqlite3.Connection, link: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE link=?", (link,)).fetchone() is None


def mark_seen(conn: sqlite3.Connection, link: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen (link) VALUES (?)", (link,))
    conn.commit()


def notify(text: str) -> None:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        print(text + "\n" + "-" * 60)
        return
    import json
    body = json.dumps({
        "chat_id": TG_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def fmt_message(item: dict, kw: str) -> str:
    free = "(для всех)" in item["title"]
    title = item["title"].replace("(для всех)", "").strip()
    tag = "🟢 бесплатный отклик" if free else "💰 платный отклик"
    snippet = re.sub(r"\s+", " ", item["description"])[:220]
    return (
        f"{tag} · ключ: <b>{kw}</b>\n\n"
        f"<b>{title}</b>\n"
        f"{snippet}…\n\n"
        f"{item['link']}"
    )


def run_once() -> None:
    keywords = load_keywords()
    conn = sqlite3.connect(DB_PATH)
    db(conn)
    fresh_seen = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0

    items = fetch_items()
    matched = []
    for item in items:
        if not item["link"] or not is_new(conn, item["link"]):
            continue
        mark_seen(conn, item["link"])
        kw = match_keyword(item["title"] + "\n" + item["description"], keywords)
        if kw:
            matched.append((item, kw))

    if fresh_seen:
        print(f"Первый запуск: база наполнена ({len(items)} заказов), "
              f"уведомления — со следующего прохода. Совпадений уже есть: {len(matched)}")
        for item, kw in matched[:3]:  # покажем в консоли пару свежих совпадений
            print(fmt_message(item, kw))
        return

    for item, kw in matched[:MAX_SEND_PER_RUN]:
        try:
            notify(fmt_message(item, kw))
        except Exception as exc:
            print(f"[!] ошибка отправки: {exc}")
    print(f"Проход: {len(items)} заказов в ленте, новых по ключам: {len(matched)}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        run_once()
    elif mode == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300
        print(f"Радар запущен, интервал {interval} сек. Ctrl+C — стоп.")
        while True:
            try:
                run_once()
            except Exception as exc:
                print(f"[!] ошибка прохода: {exc}")
            time.sleep(interval)
    else:
        print(__doc__)
