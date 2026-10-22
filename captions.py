"""Подпись к файлу и имя файла ведомости — общие для бота и админки (повторная отправка в чат)."""
from __future__ import annotations

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


def build_caption(tag: str, house_id: int, period: str, kind: str = RESIDENTIAL) -> str:
    """Подпись к файлу в группе. tag — TARGET_BOT_TAG; пустой tag — подпись без тега."""
    tags = f"#{month_tag(period)} {HOUSE_HASHTAG[house_id]}"
    if LEDGERS[kind].hashtag:
        tags += f" {LEDGERS[kind].hashtag}"
    prefix = f"{tag} " if tag else ""
    return (f"{prefix}Привет! Показания за текущий {period_label(period)} — {HOUSE_NAMES[house_id]}"
            f"{ledger_suffix(kind)}\n{tags}")
