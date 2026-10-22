"""Номера квартир: приводим ввод к одному виду, называем по-человечески и подсказываем похожие.

Нестандартные квартиры — с буквами в номере (например «2ц» = цоколь 2). Список берётся
из шаблонов, так что всё работает для любого дома.
"""
from __future__ import annotations

import re

# Буква-суффикс → понятное название («2ц» → «Цоколь 2»). Остальные показываем как «Кв. 15а».
SUFFIX_NAMES = {"ц": "Цоколь"}

# Слова, которые можно написать вместо буквы-суффикса.
_WORD_ALIASES = [
    (re.compile(r"цокол[а-яё]*"), "ц"),
    (re.compile(r"цок"), "ц"),
]

# Латинские буквы, которые выглядят как русские.
_LATIN_TO_CYR = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
    "k": "к", "m": "м", "h": "н", "b": "в", "t": "т",
})
# Латинские c / u сразу после цифр почти всегда означают «ц».
_LATIN_SUFFIX_TS = re.compile(r"(?<=\d)[cu](?![a-zа-я])")

_STRIP_CHARS = re.compile(r"[\s\-/.№#]+")
_PREFIX_KV = re.compile(r"^(?:кв(?:артира)?)(?=\d)")
_SUFFIX_FORM = re.compile(r"^(\d+)([а-я]{0,2})$")
_PREFIX_FORM = re.compile(r"^([а-я]{1,2})(\d+)$")
_AFFIX_TOKEN = re.compile(r"^[а-я]{1,2}$")


def _letters(text: str) -> str:
    """Нижний регистр, латиница → кириллица, «цоколь» → «ц»."""
    text = text.lower().replace("ё", "е")
    text = _LATIN_SUFFIX_TS.sub("ц", text)
    text = text.translate(_LATIN_TO_CYR)
    for pattern, repl in _WORD_ALIASES:
        text = pattern.sub(repl, text)
    return text


def normalize_apartment(value) -> str | None:
    """Приводит номер квартиры (из шаблона или сообщения) к одному виду.

    «2ц», «2 Ц», «2-ц», «2c», «2u», «ц2», «цоколь 2», «2цоколь» → «2ц»; 12 / 12.0 / «12» → «12».
    None — если это вообще не номер (сноска, итог и т.п.).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    text = _STRIP_CHARS.sub("", str(value))
    if not text:
        return None
    text = _letters(text)
    text = _PREFIX_KV.sub("", text)
    m = _SUFFIX_FORM.match(text)
    if m:
        return str(int(m.group(1))) + m.group(2)
    m = _PREFIX_FORM.match(text)  # "ц2" -> "2ц"
    if m and m.group(1) != "кв":
        return str(int(m.group(2))) + m.group(1)
    return None


def is_affix_token(token: str) -> bool:
    """Это отдельное слово-приставка к номеру без цифр? «ц», «Ц», «цоколь», «c» и т.п."""
    if any(ch.isdigit() for ch in token):
        return False
    if token.strip().lower() in ("c", "u"):  # латинские c/u как отдельное слово: "2 c 345"
        return True
    text = _letters(_STRIP_CHARS.sub("", token))
    return bool(_AFFIX_TOKEN.match(text)) and text != "кв"


def is_nonstandard(number: str) -> bool:
    """В номере есть не только цифры (например «2ц»)."""
    return not number.isdigit()


def natural_key(number: str) -> tuple[int, str]:
    """Ключ для сортировки «по-человечески»: «2ц» < «3ц» < «10ц»."""
    digits, suffix = split_number(number)
    return (int(digits) if digits.isdigit() else 10**9, suffix)


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """1 квартира, 2–4 квартиры, 5–20 квартир, 21 квартира, 11–14 квартир."""
    n10, n100 = abs(n) % 10, abs(n) % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def apartments_count(n: int) -> str:
    return f"{n} {plural_ru(n, 'квартира', 'квартиры', 'квартир')}"


def split_number(number: str) -> tuple[str, str]:
    """«15а» → («15», «а»)."""
    m = re.match(r"^(\d+)(.*)$", number)
    return (m.group(1), m.group(2)) if m else (number, "")


def apartment_label(number: str) -> str:
    """Название для кнопок и сообщений: «2ц» → «Цоколь 2», «15а» → «Кв. 15а», «5» → «Кв. 5»."""
    digits, suffix = split_number(number)
    if suffix in SUFFIX_NAMES:
        return f"{SUFFIX_NAMES[suffix]} {digits}"
    return f"Кв. {number}"


def apartment_ref(number: str) -> str:
    """Как назвать квартиру в подсказке: «4ц (Цоколь 4)» или просто «5»."""
    digits, suffix = split_number(number)
    if suffix in SUFFIX_NAMES:
        return f"{number} ({SUFFIX_NAMES[suffix]} {digits})"
    return number


def input_example(number: str, value: str = "345") -> str:
    """Пример ввода для подсказки: «2ц 345» или «цоколь 2 345»."""
    digits, suffix = split_number(number)
    if suffix in SUFFIX_NAMES:
        return f"«{number} {value}» или «{SUFFIX_NAMES[suffix].lower()} {digits} {value}»"
    return f"«{number} {value}»"


def suggest_apartments(number: str, existing: list[str], limit: int = 3) -> list[str]:
    """Похожие квартиры, если ввели несуществующую: тот же номер с другой буквой или соседний номер."""
    existing_set = set(existing)
    digits, suffix = split_number(number)
    out: list[str] = []

    def add(candidate: str):
        if candidate in existing_set and candidate != number and candidate not in out:
            out.append(candidate)

    if digits.isdigit():
        n = int(digits)
        if suffix:
            for delta in (-1, 1):  # тот же суффикс, номер ±1
                if n + delta >= 0:
                    add(f"{n + delta}{suffix}")
        for cand in existing:       # те же цифры, другой суффикс (или без него)
            if split_number(cand)[0] == digits:
                add(cand)
        if not suffix:
            for delta in (-1, 1):
                add(f"{n + delta}")
    return out[:limit]


def format_suggestions(candidates: list[str]) -> str:
    """«Может, 4ц (Цоколь 4) или 5?» — или пустая строка, если предложить нечего."""
    refs = [apartment_ref(c) for c in candidates]
    if not refs:
        return ""
    if len(refs) == 1:
        return f"Может, {refs[0]}?"
    return f"Может, {', '.join(refs[:-1])} или {refs[-1]}?"
