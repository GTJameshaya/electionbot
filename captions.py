"""Подпись к файлу и имена файла ведомости — общие для бота и админки (повторная отправка в чат).

Имён у отправленного файла два:
* archive_file_name() — служебное, уникальное, с отметкой времени отправки
  («20261005_101500_house1_…xlsx»). Пишется в БД (submissions, chat_resends), по нему же
  migrate_archive.py разбирает старые архивные файлы. Повторные отправки за месяц не совпадают.
* display_file_name() — то, что видят люди в чате и в CRM: «Дом_1_Сентябрь_2026.xlsx»,
  «Дом_1_Коммерция_Сентябрь_2026.xlsx». Месяц и год — отчётного периода, а не даты отправки.
"""
from __future__ import annotations

import re
from datetime import datetime

from config import HOUSES
from db import parse_period
from excel_report import MONTHS_RU
from ledgers import LEDGERS, RESIDENTIAL

HOUSE_NAMES = {house_id: info["name"] for house_id, info in HOUSES.items()}
HOUSE_HASHTAG = {house_id: info["hashtag"] for house_id, info in HOUSES.items()}


def period_label(period: str) -> str:
    year, month = parse_period(period)
    return f"{month:02d}.{year}"


def period_title(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS_RU[month - 1]} {year}"


def month_tag(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS_RU[month - 1]}_{year}"


def ledger_suffix(kind: str) -> str:
    return "" if kind == RESIDENTIAL else f" — {LEDGERS[kind].title}"


def archive_file_name(house_id: int, when: datetime | None = None, kind: str = RESIDENTIAL) -> str:
    when = when or datetime.now()
    return (f"{when.strftime('%Y%m%d_%H%M%S')}_house{house_id}_{LEDGERS[kind].file_tag}"
            f"{HOUSE_NAMES[house_id].replace(' ', '_')}.xlsx")


# Запрещённые в именах файлов символы (Windows/macOS/Linux), управляющие символы и кавычки-ёлочки.
_FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|«»\x00-\x1f\x7f]')


def safe_file_part(text: str) -> str:
    """Кусок имени файла: запрещённое → «_», пробелы → «_», «__» → «_», без «_» и точек по краям.
    Кириллица остаётся как есть."""
    text = _FORBIDDEN_CHARS.sub("_", text or "")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_.")


def pretty_file_name(house_name: str, period: str, kind: str = RESIDENTIAL) -> str:
    """«<Адрес>[_Коммерция]_<Месяц>_<год>.xlsx» за отчётный период period ('YYYY-MM')."""
    year, month = parse_period(period)
    parts = [house_name, LEDGERS[kind].title if kind != RESIDENTIAL else "", MONTHS_RU[month - 1], str(year)]
    base = "_".join(s for s in (safe_file_part(p) for p in parts) if s)
    return f"{base or 'ведомость'}.xlsx"


def display_file_name(house_id: int, period: str, kind: str = RESIDENTIAL) -> str:
    """Имя файла для чата и CRM — по названию дома из houses.json."""
    return pretty_file_name(HOUSE_NAMES[house_id], period, kind)


_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))


def ascii_file_name(name: str) -> str:
    """Запасное ASCII-имя для старых браузеров (filename= в Content-Disposition):
    кириллица транслитом, всё прочее не из [A-Za-z0-9._-] → «_». «Дом_1_Май_2024.xlsx» → «Dom_1_May_2024.xlsx»."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    out = []
    for ch in stem:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch != low else t)
        else:
            out.append(ch)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", "".join(out))
    stem = re.sub(r"_+", "_", stem).strip("_.") or "report"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)
    return f"{stem}.{ext}" if ext else stem


def content_disposition(name: str) -> str:
    """Заголовок для скачивания: ASCII-запаска filename="…" и полное имя filename*=UTF-8''… (RFC 5987/6266)."""
    from urllib.parse import quote
    return f"attachment; filename=\"{ascii_file_name(name)}\"; filename*=UTF-8''{quote(name, safe='')}"


def build_caption(tag: str, house_id: int, period: str, kind: str = RESIDENTIAL) -> str:
    """Подпись к файлу в группе. tag — TARGET_BOT_TAG; пустой tag — подпись без тега."""
    tags = f"#{month_tag(period)} {HOUSE_HASHTAG[house_id]}"
    if LEDGERS[kind].hashtag:
        tags += f" {LEDGERS[kind].hashtag}"
    prefix = f"{tag} " if tag else ""
    return (f"{prefix}Привет! Показания за текущий {period_label(period)} — {HOUSE_NAMES[house_id]}"
            f"{ledger_suffix(kind)}\n{tags}")
