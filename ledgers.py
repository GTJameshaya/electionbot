"""Ведомости (жилые / коммерция) и сопоставление коммерческих абонентов по названию и псевдонимам."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RESIDENTIAL = "residential"
COMMERCE = "commerce"


@dataclass(frozen=True)
class Ledger:
    kind: str
    title: str                 # подпись кнопки / в сообщениях
    template_key: str          # ключ в config.HOUSES
    month_row: int             # строка с названиями месяцев (D/E)
    first_row: int             # первая строка данных
    key_mode: str              # "apartment" — номер квартиры в A, "name" — название абонента в A
    write_diff: bool           # True — писать расход в F числом; False — оставить формулу шаблона
    hashtag: str | None        # дополнительный хэштег в подписи
    file_tag: str              # вставка в имя файла
    noun: tuple[str, str, str]  # склонения: 1 / 2-4 / 5+


LEDGERS = {
    RESIDENTIAL: Ledger(RESIDENTIAL, "Жилые", "template", month_row=4, first_row=5, key_mode="apartment",
                        write_diff=True, hashtag=None, file_tag="",
                        noun=("квартира", "квартиры", "квартир")),
    COMMERCE: Ledger(COMMERCE, "Коммерция", "commerce_template", month_row=5, first_row=6, key_mode="name",
                     write_diff=False, hashtag="#Коммерция", file_tag="commerce_",
                     noun=("абонент", "абонента", "абонентов")),
}

# ---------------------------------------------------------------- нормализация названий
_LATIN_TO_CYR = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
                               "k": "к", "m": "м", "h": "н", "b": "в", "t": "т"})
_PUNCT = re.compile(r"[«»\"'“”„`.,;:!?()\[\]{}]+")
_DOUBLE = re.compile(r"([а-я])\1+")
_STOP_RAW = {"ооо", "оао", "зао", "пао", "ип", "ао", "магазин", "лтд", "подъезд", "подъезда", "под", "п"}


def norm_word(word: str) -> str:
    """Слово для сравнения: регистр, ё, латиница-двойники, пунктуация, удвоенные буквы («леккс» = «лекс»)."""
    w = _PUNCT.sub("", word.lower().replace("ё", "е"))
    if re.search(r"[а-я]", w) or not re.search(r"\d", w):
        w = w.translate(_LATIN_TO_CYR)
    return _DOUBLE.sub(r"\1", w)


# Стоп-слова в нормализованном виде («ооо» -> «о» после схлопывания удвоенных букв).
STOP_WORDS = {norm_word(w) for w in _STOP_RAW}


def norm_words(text: str) -> list[str]:
    return [w for w in (norm_word(t) for t in _PUNCT.sub(" ", text).split()) if w]


def subscriber_key(name: str) -> str:
    """Стабильный короткий ключ абонента (влезает в apartments.number VARCHAR(20))."""
    canon = " ".join(_PUNCT.sub(" ", name.lower().replace("ё", "е")).split())
    return "к" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def is_commerce_key(number: str) -> bool:
    return bool(re.fullmatch(r"к[0-9a-f]{12}", number))


def load_aliases(path: Path | None) -> dict[str, dict[str, str]]:
    """{"*": {псевдоним: фраза}, "1": {...}}; при ошибке/отсутствии — встроенные значения."""
    default = {"*": {"лекс": "борис", "пост": "пост лтд"}}
    if path is None or not Path(path).exists():
        return default
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {k: {str(a): str(t) for a, t in v.items()} for k, v in data.items()
                if isinstance(v, dict) and not k.startswith("_")}
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось прочитать %s: %s — использую встроенные псевдонимы", path, e)
        return default


# ---------------------------------------------------------------- сопоставление
_ENTRANCE_RE = re.compile(r"^(\d{1,2})(?:п|пд|под|подъезд|подьезд|-?й)?$")
_ATTACHED_RE = re.compile(r"^([a-zа-яё.]+?)(\d{1,2})(п|пд|под|подъезд)?$", re.IGNORECASE)
_ENTRANCE_IN_TITLE = re.compile(r"(\d+)\s*подъезд")
MAX_PHRASE = 5


@dataclass
class Match:
    raw: str                 # как написал пользователь
    candidates: list[str]    # ключи абонентов
    consumed: int            # сколько токенов занято


class CommerceMatcher:
    """Ищет коммерческих абонентов в токенах сообщения: полное название или его начало,
    отличительное слово названия («озон», «овощной», «лавка»), псевдонимы из aliases.json."""

    def __init__(self, subscribers: list[tuple[str, str]], aliases: dict[str, str] | None = None,
                 extra_names: dict[str, list[str]] | None = None):
        self.titles = dict(subscribers)                       # ключ -> название
        self.order = [k for k, _ in subscribers]
        self.words = {k: norm_words(t) for k, t in subscribers}
        self.phrases: dict[tuple[str, ...], list[str]] = {}
        # все известные названия: текущее + старые/новые из истории переименований
        self.names_words: dict[str, list[list[str]]] = {k: [w] for k, w in self.words.items()}
        for key, names in (extra_names or {}).items():
            if key in self.titles:
                for n in names:
                    nw = norm_words(n)
                    if nw and nw not in self.names_words[key]:
                        self.names_words[key].append(nw)
        for key, variants in self.names_words.items():
            for nw in variants[1:]:  # старое/другое название целиком тоже узнаём
                self.phrases[tuple(nw)] = self._merge(self.phrases.get(tuple(nw), []), [key])
        # псевдонимы: значение — фраза из названия (str) или (фраза, [ключи абонентов]).
        # Привязка к ключам устойчива к переименованиям; фраза ищется во всех названиях (текущих и прошлых).
        for alias, spec in (aliases or {}).items():
            target, keys = (spec, []) if isinstance(spec, str) else (spec[0], list(spec[1]))
            cands = self._merge([k for k in keys if k in self.titles], self._containing(norm_words(target or "")))
            aw = tuple(norm_words(alias))
            if cands and aw:
                self.phrases[aw] = self._merge(self.phrases.get(aw, []), cands)
        for key, variants in self.names_words.items():
            for words in variants:
                for w in words:  # автоматические псевдонимы: отличительные слова
                    if w not in STOP_WORDS and not w.isdigit() and len(w) >= 3:
                        self.phrases[(w,)] = self._merge(self.phrases.get((w,), []), [key])

    def _merge(self, a, b):
        return [k for k in self.order if k in set(a) | set(b)]

    def _containing(self, target: list[str]) -> list[str]:
        """Абоненты, в одном из названий которых (текущем или прошлом) есть фраза target."""
        n = len(target)
        if not n:
            return []
        return [k for k in self.order
                if any(w[i:i + n] == target for w in self.names_words[k] for i in range(len(w) - n + 1))]

    def __bool__(self):
        return bool(self.titles)

    def lookup(self, words: list[str]) -> list[str]:
        """Кандидаты для фразы (уже нормализованной)."""
        if not words or all(w in STOP_WORDS or w.isdigit() for w in words):
            return []
        t = tuple(words)
        if t in self.phrases:
            return self.phrases[t]
        # начало полного названия: «ооо борис» -> оба подъезда, «щедрая лавка» -> Щедрая Лавка
        prefix = [k for k in self.order if any(w[:len(words)] == list(words) for w in self.names_words[k])]
        if prefix:
            return prefix
        if len(words) == 1 and len(words[0]) >= 4:  # недописанное слово: «овощ» -> «овощной»
            return self._merge([], [k for p, ks in self.phrases.items()
                                    if len(p) == 1 and p[0].startswith(words[0]) for k in ks])
        return []

    def match_at(self, tokens: list[str], i: int) -> Match | None:
        """Пробует распознать абонента, начиная с tokens[i] (самая длинная фраза)."""
        if not re.search(r"[a-zа-яё]", tokens[i], re.IGNORECASE):
            return None
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            chunk = tokens[i:i + n]
            if n > 1 and any(not re.search(r"[a-zа-яё]", t, re.IGNORECASE) for t in chunk[1:]) \
                    and not self._is_full_title_with_digits(chunk):
                continue
            cands = self.lookup([norm_word(t) for t in chunk])
            if cands:
                return Match(" ".join(chunk), cands, n)
        m = _ATTACHED_RE.match(tokens[i])  # «лекс1», «лекс1п»
        if m:
            cands = self.lookup([norm_word(m.group(1))])
            if len(cands) == 1:
                return Match(tokens[i], cands, 1)
            if len(cands) > 1:
                picked = self.pick_entrance(cands, int(m.group(2)))
                if picked:
                    return Match(tokens[i], [picked], 1)
        return None

    def _is_full_title_with_digits(self, chunk: list[str]) -> bool:
        words = [norm_word(t) for t in chunk]
        return any(w[:len(words)] == words for k in self.order for w in self.names_words[k])

    def pick_entrance(self, candidates: list[str], n: int) -> str | None:
        found = [k for k in candidates
                 if any(int(x) == n for x in _ENTRANCE_IN_TITLE.findall(self.titles[k].lower()))]
        return found[0] if len(found) == 1 else None

    def entrance_token(self, candidates: list[str], tokens: list[str], i: int) -> tuple[str | None, int]:
        """После неоднозначного названия: «1», «1п», «1 подъезд» -> (ключ, сколько токенов)."""
        if i >= len(tokens):
            return None, 0
        m = _ENTRANCE_RE.match(norm_word(tokens[i]) if not tokens[i].isdigit() else tokens[i])
        if not m:
            return None, 0
        used = 1
        if i + 1 < len(tokens) and norm_word(tokens[i + 1]) in ("подъезд", "под", "п", "подьезд"):
            used = 2
        key = self.pick_entrance(candidates, int(m.group(1)))
        return (key, used) if key else (None, 0)
