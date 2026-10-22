"""Два вида ведомостей — «Жилые» и «Коммерция» — и поиск коммерческих абонентов в тексте.

Абонента можно назвать полным названием, его началом, отличительным словом или псевдонимом.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

RESIDENTIAL = "residential"
COMMERCE = "commerce"


@dataclass(frozen=True)
class Ledger:
    """Описание ведомости: где в шаблоне месяцы и данные, как она называется в боте и файлах."""
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
    """Готовит слово к сравнению: без регистра, «ё», пунктуации и удвоенных букв, латиница → кириллица
    («Роммашка.» = «ромашка»)."""
    w = _PUNCT.sub("", word.lower().replace("ё", "е"))
    if re.search(r"[а-я]", w) or not re.search(r"\d", w):
        w = w.translate(_LATIN_TO_CYR)
    return _DOUBLE.sub(r"\1", w)


# Слова, по которым абонента не узнать (ООО, магазин, подъезд…), — уже нормализованные («ооо» → «о»).
STOP_WORDS = {norm_word(w) for w in _STOP_RAW}


def norm_words(text: str) -> list[str]:
    """Текст → список нормализованных слов."""
    return [w for w in (norm_word(t) for t in _PUNCT.sub(" ", text).split()) if w]


def subscriber_key(name: str) -> str:
    """Короткий постоянный ключ абонента по названию: «к» + 12 hex (влезает в apartments.number)."""
    canon = " ".join(_PUNCT.sub(" ", name.lower().replace("ё", "е")).split())
    return "к" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def is_commerce_key(number: str) -> bool:
    return bool(re.fullmatch(r"к[0-9a-f]{12}", number))


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
    """Находит коммерческих абонентов в словах сообщения: по названию или его началу,
    по отличительному слову («аптека», «продукты») и по псевдонимам."""

    def __init__(self, subscribers: list[tuple[str, str]], aliases: dict[str, str] | None = None,
                 extra_names: dict[str, list[str]] | None = None):
        self.titles = dict(subscribers)                       # ключ -> название
        self.order = [k for k, _ in subscribers]
        self.words = {k: norm_words(t) for k, t in subscribers}
        self.phrases: dict[tuple[str, ...], list[str]] = {}
        # все известные названия: текущее + старые и новые из истории переименований
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
        # Псевдоним: фраза из названия (str) или (фраза, [ключи]). Ключи переживают переименования,
        # а фразу ищем во всех названиях — текущих и прошлых.
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
        """Объединяет списки ключей, сохраняя порядок абонентов."""
        return [k for k in self.order if k in set(a) | set(b)]

    def _containing(self, target: list[str]) -> list[str]:
        """Абоненты, у которых фраза target есть в текущем или прошлом названии."""
        n = len(target)
        if not n:
            return []
        return [k for k in self.order
                if any(w[i:i + n] == target for w in self.names_words[k] for i in range(len(w) - n + 1))]

    def __bool__(self):
        return bool(self.titles)

    def lookup(self, words: list[str]) -> list[str]:
        """Кого может означать фраза (слова уже нормализованы). Пустой список — никого."""
        if not words or all(w in STOP_WORDS or w.isdigit() for w in words):
            return []
        t = tuple(words)
        if t in self.phrases:
            return self.phrases[t]
        # начало полного названия: «ооо ромашка» → все её подъезды, «аптека на» → «Аптека на углу»
        prefix = [k for k in self.order if any(w[:len(words)] == list(words) for w in self.names_words[k])]
        if prefix:
            return prefix
        if len(words) == 1 and len(words[0]) >= 4:  # недописанное слово: «прод» → «продукты»
            return self._merge([], [k for p, ks in self.phrases.items()
                                    if len(p) == 1 and p[0].startswith(words[0]) for k in ks])
        return []

    def match_at(self, tokens: list[str], i: int) -> Match | None:
        """Пробует узнать абонента с позиции i; сначала пробуем самые длинные фразы."""
        if not re.search(r"[a-zа-яё]", tokens[i], re.IGNORECASE):
            return None
        for n in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            chunk = tokens[i:i + n]
            # цифры внутри фразы допустимы, только если это начало настоящего названия
            if n > 1 and any(not re.search(r"[a-zа-яё]", t, re.IGNORECASE) for t in chunk[1:]) \
                    and not self._is_full_title_with_digits(chunk):
                continue
            cands = self.lookup([norm_word(t) for t in chunk])
            if cands:
                return Match(" ".join(chunk), cands, n)
        m = _ATTACHED_RE.match(tokens[i])  # «ромашка1», «ромашка1п»
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
        """Совпадает ли фраза с началом какого-то названия (даже если в ней есть цифры)."""
        words = [norm_word(t) for t in chunk]
        return any(w[:len(words)] == words for k in self.order for w in self.names_words[k])

    def pick_entrance(self, candidates: list[str], n: int) -> str | None:
        """Из кандидатов выбирает того, у кого в названии «N подъезд». None — не нашли однозначно."""
        found = [k for k in candidates
                 if any(int(x) == n for x in _ENTRANCE_IN_TITLE.findall(self.titles[k].lower()))]
        return found[0] if len(found) == 1 else None

    def entrance_token(self, candidates: list[str], tokens: list[str], i: int) -> tuple[str | None, int]:
        """Номер подъезда после неоднозначного названия: «1», «1п», «1 подъезд» → (ключ, сколько слов занято)."""
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
