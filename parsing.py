"""Разбор сообщений с показаниями вида «кв значение кв значение …», например «12 345 2ц 100».

Понимает квартиры из нескольких слов («2 ц 345», «цоколь 2 345», «2 цоколь 345»), а если передан
matcher — ещё и коммерческих абонентов вперемешку с квартирами.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from apartments import is_affix_token, normalize_apartment
from ledgers import CommerceMatcher

_VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_DIGITS_RE = re.compile(r"^\d+$")


@dataclass
class ParseResult:
    entries: list[tuple[str, float]] = field(default_factory=list)  # (кв, значение)
    errors: list[str] = field(default_factory=list)                  # тексты для пользователя
    commerce: list[tuple[str, float, str]] = field(default_factory=list)  # (ключ абонента, значение, как написано)
    ambiguous: list[dict] = field(default_factory=list)  # {"raw", "value", "candidates": [ключи]}


def parse_value(raw: str) -> float | None:
    """«345», «345,5», «345.5» → число; всё остальное → None."""
    raw = raw.strip()
    if not _VALUE_RE.match(raw):
        return None
    value = float(raw.replace(",", "."))
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _take_apartment(parts: list[str], i: int) -> tuple[str, int]:
    """Берёт номер квартиры с позиции i, склеивая «2 ц» и «цоколь 2».
    Возвращает (как написано, индекс следующего слова)."""
    tok = parts[i]
    nxt = parts[i + 1] if i + 1 < len(parts) else None
    if nxt is not None:
        # «цоколь 2 345» / «ц 2 345»: слово-префикс + число
        if is_affix_token(tok) and _DIGITS_RE.match(nxt):
            return f"{tok} {nxt}", i + 2
        # «2 ц 345» / «2 цоколь 345»: число + слово-суффикс
        if _DIGITS_RE.match(tok) and is_affix_token(nxt):
            return f"{tok} {nxt}", i + 2
    return tok, i + 1


def _take_commerce(parts: list[str], i: int, matcher: CommerceMatcher, result: ParseResult) -> int | None:
    """Пробует прочитать абонента с позиции i: «ромашка 67», «ромашка 1 67», «ромашка1п 67», «аптека на углу 100».
    Возвращает индекс следующего слова или None, если это не абонент."""
    m = matcher.match_at(parts, i)
    if m is None:
        return None
    j = i + m.consumed
    candidates = m.candidates
    raw = m.raw
    if len(candidates) > 1:
        key, used = matcher.entrance_token(candidates, parts, j)
        # число после названия — номер подъезда, только если за ним идёт ещё и показание
        if key and j + used < len(parts) and parse_value(parts[j + used]) is not None:
            raw = " ".join(parts[i:j + used])
            candidates, j = [key], j + used
    if j >= len(parts):
        result.errors.append(f"❌ Для «{raw}» не указано показание")
        return j
    value = parse_value(parts[j])
    if value is None:
        result.errors.append(f"❌ Для «{raw}» введите число (получено «{parts[j]}»)")
        return j + 1
    if len(candidates) == 1:
        result.commerce.append((candidates[0], value, raw))
    else:
        result.ambiguous.append({"raw": raw, "value": value, "candidates": candidates})
    return j + 1


def parse_readings(text: str, matcher: CommerceMatcher | None = None) -> ParseResult:
    """Разбирает сообщение целиком. Хорошие пары идут в результат, ошибки — в errors
    и остальным не мешают.

    С matcher вперемешку с квартирами узнаются и абоненты коммерции: «12 345 ромашка 67 альфа 420 5ц 10».
    """
    result = ParseResult()
    parts = text.replace("\n", " ").replace("\t", " ").replace(";", " ").split()
    i = 0
    while i < len(parts):
        if matcher:
            nxt = _take_commerce(parts, i, matcher, result)
            if nxt is not None:
                i = nxt
                continue
        apt_raw, i = _take_apartment(parts, i)
        if i >= len(parts):
            result.errors.append(f"❌ Для квартиры {apt_raw} не указано показание")
            break
        value_raw = parts[i]
        i += 1
        apt = normalize_apartment(apt_raw)
        if apt is None:
            if matcher and not any(ch.isdigit() for ch in apt_raw):
                result.errors.append(f"❌ «{apt_raw}» — нет такой квартиры или абонента")
            else:
                result.errors.append(f"❌ «{apt_raw}» не похоже на номер квартиры")
            continue
        value = parse_value(value_raw)
        if value is None:
            result.errors.append(f"❌ Для квартиры {apt_raw} введите число (получено «{value_raw}»)")
            continue
        result.entries.append((apt, value))
    return result


def fmt_value(value: float | None) -> str:
    """Число для сообщений: 12.0 → «12», 12.5 → «12,5», None → «—»."""
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")
