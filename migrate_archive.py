"""Однократный импорт старых архивных ведомостей (archive/*.xlsx) в базу данных.

Использование:
    python migrate_archive.py                  # archive/ рядом с ботом, DATABASE_URL из .env
    python migrate_archive.py --archive /path/to/archive --dry-run
    python migrate_archive.py --no-template    # не брать стартовые показания из шаблонов

Скрипт идемпотентен: повторный запуск ничего не дублирует.
  * Имя файла: YYYYMMDD_HHMM_house{N}_*.xlsx, показания берутся из колонки E.
  * Период (месяц) определяется по ячейке D2 файла (дата начала сессии), иначе по дате в имени.
  * Если за один месяц несколько файлов — побеждает самый поздний.
  * Показания, введённые через бота (source=bot), НЕ перезаписываются.
  * Из шаблонов (если не указан --no-template) добавляются показания, которые в них записаны
    (колонки D/E с месяцами из строки 4), только там, где в БД ещё ничего нет.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from config import BASE_DIR, HOUSES, load_settings, resolve_database_url
from ledgers import COMMERCE, LEDGERS, RESIDENTIAL
from db import Apartment, Database, Reading, Submission, period_of, utcnow
from excel_report import read_sheet_values

logger = logging.getLogger("migrate_archive")

# YYYYMMDD_HHMM(SS)_house{N}_[commerce_]Название.xlsx
FILE_RE = re.compile(r"^(\d{8})_(\d{4}|\d{6})_house(\d+)_(commerce_)?.*\.xlsx$", re.IGNORECASE)


def _period_from_file(path: Path, stamp: datetime, sheet: dict) -> str:
    d2 = sheet.get("date")
    if d2 is not None:
        return period_of(d2)
    cur_month = sheet.get("cur_month")
    if cur_month:
        year = stamp.year - 1 if cur_month > stamp.month else stamp.year
        return f"{year:04d}-{cur_month:02d}"
    return period_of(stamp)


def _upsert(s, house_id: int, number: str, period: str, value: float, source: str,
            stats: dict, overwrite_sources: tuple[str, ...], kind: str = RESIDENTIAL) -> None:
    apt = s.scalar(select(Apartment).where(Apartment.house_id == house_id, Apartment.kind == kind,
                                           Apartment.number == number))
    if apt is None:
        stats["unknown_apartments"] += 1
        logger.debug("Дом %s: квартира %s отсутствует в шаблоне — пропуск", house_id, number)
        return
    r = s.scalar(select(Reading).where(Reading.apartment_id == apt.id, Reading.period == period))
    if r is None:
        s.add(Reading(apartment_id=apt.id, period=period, value=value, entered_by=None, source=source))
        stats["inserted"] += 1
    elif r.source in overwrite_sources and r.value != value:
        r.value, r.source, r.updated_at = value, source, utcnow()
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1


def collect_archive(archive_dir: Path) -> list[tuple[Path, int, datetime, str]]:
    files = []
    for path in sorted(archive_dir.glob("*.xlsx")):
        m = FILE_RE.match(path.name)
        if not m:
            logger.warning("Пропуск (имя не по шаблону): %s", path.name)
            continue
        house_id = int(m.group(3))
        if house_id not in HOUSES:
            logger.warning("Пропуск (неизвестный дом %s): %s", house_id, path.name)
            continue
        fmt = "%Y%m%d%H%M%S" if len(m.group(2)) == 6 else "%Y%m%d%H%M"
        stamp = datetime.strptime(m.group(1) + m.group(2), fmt)
        kind = COMMERCE if m.group(4) else RESIDENTIAL
        files.append((path, house_id, stamp, kind))
    files.sort(key=lambda t: (t[2], t[0].name))
    return files


def migrate(db: Database, archive_dir: Path, use_templates: bool = True) -> dict:
    stats = {"files": 0, "files_failed": 0, "inserted": 0, "updated": 0, "unchanged": 0,
             "unknown_apartments": 0, "submissions_added": 0, "template_inserted": 0}

    # 1. Архивные файлы: для каждого (дом, месяц) берём самый поздний файл.
    latest: dict[tuple[int, str, str], dict[str, float]] = {}
    file_info = []
    files = collect_archive(archive_dir) if archive_dir.exists() else []
    if not archive_dir.exists():
        logger.warning("Папка архива %s не найдена", archive_dir)
    for path, house_id, stamp, kind in files:
        try:
            sheet = read_sheet_values(path, kind)
        except Exception as e:
            stats["files_failed"] += 1
            logger.error("Не удалось прочитать %s: %s", path.name, e)
            continue
        period = _period_from_file(path, stamp, sheet)
        stats["files"] += 1
        merged = latest.setdefault((house_id, period, kind), {})
        merged.update(sheet["cur"])  # более поздний файл перекрывает более ранний
        file_info.append((path.name, house_id, period, stamp, kind))
        logger.info("%s -> дом %s (%s), период %s, показаний: %d", path.name, house_id,
                    LEDGERS[kind].title, period, len(sheet["cur"]))

    with db.Session.begin() as s:
        for (house_id, period, kind), values in latest.items():
            for number, value in values.items():
                _upsert(s, house_id, number, period, value, "archive", stats,
                        overwrite_sources=("archive", "template"), kind=kind)
        for name, house_id, period, stamp, kind in file_info:
            exists = s.scalar(select(Submission.id).where(Submission.file_name == name,
                                                          Submission.source == "archive"))
            if exists is None:
                s.add(Submission(house_id=house_id, period=period, submitted_at=stamp, kind=kind,
                                 submitted_by=None, file_name=name, source="archive"))
                stats["submissions_added"] += 1

    # 2. Стартовые значения из шаблонов (только если в БД пусто для квартиры+месяца).
    if use_templates:
        with db.Session.begin() as s:
            for house_id, kind in ((h, k) for h in HOUSES for k in LEDGERS):
                tpl = db.template_path(house_id, kind)
                if tpl is None or not tpl.exists():
                    continue
                sheet = read_sheet_values(tpl, kind)
                d2 = sheet["date"]
                if d2 is None or not sheet["cur_month"]:
                    logger.warning("Шаблон %s: не удалось определить месяц, пропуск", tpl.name)
                    continue
                cur_year, cur_month = d2.year, sheet["cur_month"]
                prev_month = sheet["prev_month"] or (12 if cur_month == 1 else cur_month - 1)
                prev_year = cur_year - 1 if prev_month > cur_month else cur_year
                for key, period in (("prev", f"{prev_year:04d}-{prev_month:02d}"),
                                    ("cur", f"{cur_year:04d}-{cur_month:02d}")):
                    before = stats["inserted"]
                    for number, value in sheet[key].items():
                        _upsert(s, house_id, number, period, value, "template", stats,
                                overwrite_sources=(), kind=kind)
                    added = stats["inserted"] - before
                    stats["template_inserted"] += added
                    stats["inserted"] = before
                    if added:
                        logger.info("Шаблон %s: добавлено %d показаний за %s", tpl.name, added, period)
    return stats


def main(argv=None) -> int:
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(description="Импорт archive/*.xlsx в БД")
    ap.add_argument("--archive", type=Path, default=BASE_DIR / "archive")
    ap.add_argument("--database-url", default=None, help="по умолчанию DATABASE_URL из .env")
    ap.add_argument("--no-template", action="store_true", help="не импортировать значения из шаблонов")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет импортировано")
    args = ap.parse_args(argv)

    url = resolve_database_url(args.database_url) if args.database_url else \
        load_settings(require_telegram=False).database_url
    db = Database(url)
    db.init()
    if args.dry_run:
        for path, house_id, stamp, kind in collect_archive(args.archive):
            sheet = read_sheet_values(path, kind)
            print(f"{path.name}: дом {house_id} ({LEDGERS[kind].title}), период {_period_from_file(path, stamp, sheet)}, "
                  f"показаний {len(sheet['cur'])}")
        db.dispose()
        return 0
    stats = migrate(db, args.archive, use_templates=not args.no_template)
    db.dispose()
    print("Готово:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    return 1 if stats["files_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
