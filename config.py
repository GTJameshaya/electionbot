"""Настройки бота и админки: читаем .env, проверяем и складываем в удобные объекты.

Список домов (HOUSES) и стартовые псевдонимы абонентов берутся из локального файла houses.json
(путь можно поменять через HOUSES_FILE). Образец — houses.example.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_URL = "sqlite:///data/electionbot.db"

DEFAULT_HOUSES_FILE = "houses.json"

_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


class ConfigError(Exception):
    """С такими настройками запускаться нельзя."""


# ---------------------------------------------------------------- дома (houses.json)
def houses_file_path() -> Path:
    """Где лежит файл с домами: HOUSES_FILE из окружения или .env, иначе houses.json в папке бота."""
    raw = (os.getenv("HOUSES_FILE") or dotenv_values(BASE_DIR / ".env").get("HOUSES_FILE") or "").strip()
    path = Path(raw or DEFAULT_HOUSES_FILE)
    return path if path.is_absolute() else BASE_DIR / path


def parse_aliases(raw, house_ids, source: str = "houses.json") -> dict[str, dict[str, str]]:
    """Раздел «aliases»: {"*": {псевдоним: фраза}, "1": {...}}. Ключи на «_» (комментарии) пропускаем."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: «aliases» должен быть объектом (см. houses.example.json)")
    out: dict[str, dict[str, str]] = {}
    for scope, mapping in raw.items():
        if scope.startswith("_"):
            continue
        if scope != "*" and not (scope.isdigit() and int(scope) in house_ids):
            raise ConfigError(f"{source}: в «aliases» неизвестный дом «{scope}» (нужно «*» или номер дома)")
        if not isinstance(mapping, dict):
            raise ConfigError(f"{source}: aliases[{scope!r}] должен быть объектом {{псевдоним: фраза}}")
        out[scope] = {str(a): str(t) for a, t in mapping.items() if not str(a).startswith("_")}
    return out


def load_houses(path: Path) -> tuple[dict[int, dict[str, str]], dict[str, dict[str, str]]]:
    """Читает houses.json и возвращает (дома, стартовые псевдонимы).

    Дома — {номер: {"name", "hashtag", "template", ["commerce_template"]}}. Номер дома — это id
    в базе и house1/house2 в именах файлов, поэтому у работающего бота его менять нельзя.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"не найден файл с домами {path}. Скопируйте houses.example.json в houses.json "
                          f"и впишите свои дома (или укажите другой путь в HOUSES_FILE)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"не удалось прочитать {path}: {exc}") from exc
    raw_houses = data.get("houses") if isinstance(data, dict) else None
    if not isinstance(raw_houses, dict) or not any(not k.startswith("_") for k in raw_houses):
        raise ConfigError(f"{path.name}: нет раздела «houses» со списком домов (см. houses.example.json)")
    houses: dict[int, dict[str, str]] = {}
    for key, info in raw_houses.items():
        if key.startswith("_"):
            continue
        if not key.isdigit():
            raise ConfigError(f"{path.name}: номер дома «{key}» должен быть числом (1, 2, …)")
        if not isinstance(info, dict) or not all(isinstance(info.get(f), str) and info[f].strip()
                                                 for f in ("name", "hashtag", "template")):
            raise ConfigError(f"{path.name}: у дома {key} должны быть заполнены name, hashtag и template")
        house = {f: info[f] for f in ("name", "hashtag", "template")}
        if info.get("commerce_template"):
            house["commerce_template"] = str(info["commerce_template"])
        houses[int(key)] = house
    names = [h["name"] for h in houses.values()]
    if len(set(names)) != len(names):
        raise ConfigError(f"{path.name}: названия домов должны быть разными")
    houses = dict(sorted(houses.items()))
    return houses, parse_aliases(data.get("aliases"), set(houses), path.name)


HOUSES_FILE = houses_file_path()
try:
    # Дома: ключ — номер дома (id в БД, house1/house2 в именах файлов).
    # DEFAULT_ALIASES — псевдонимы, которые один раз переносятся в БД при первом запуске.
    HOUSES, DEFAULT_ALIASES = load_houses(HOUSES_FILE)
except ConfigError as _exc:
    # Без списка домов ни бот, ни админка работать не могут — выходим с понятным сообщением.
    raise SystemExit(f"Ошибка конфигурации: {_exc}") from None


@dataclass
class Settings:
    bot_token: str
    group_chat_id: int
    target_bot_tag: str = "@supp0rt_dom"
    database_url: str = DEFAULT_DATABASE_URL
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)   # роль user (только бот)
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)     # роль admin (бот + админ-панель)
    admin_public_url: str = ""                                            # https://… адрес админ-панели


def resolve_database_url(url: str | None, base_dir: Path = BASE_DIR) -> str:
    """Относительный путь к SQLite считаем от папки бота и заодно создаём каталог для файла."""
    url = (url or "").strip() or DEFAULT_DATABASE_URL
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != "sqlite:///:memory:":
        path_part = url[len(prefix):]
        path = Path(path_part)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = prefix + str(path)
    return url


def parse_allowed_user_ids(raw: str | None, var: str = "ALLOWED_USER_IDS") -> frozenset[int]:
    """«111, 222; 333» → {111, 222, 333}. Не число — ConfigError."""
    ids = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{var}: '{chunk}' не является числом") from exc
    return frozenset(ids)


def load_settings(require_telegram: bool = True) -> Settings:
    """Настройки бота. require_telegram=False — для админки и миграции,
    где BOT_TOKEN и GROUP_CHAT_ID не обязательны."""
    load_dotenv(BASE_DIR / ".env")
    load_dotenv()  # плюс .env из текущей папки — на случай запуска не из папки бота

    token = (os.getenv("BOT_TOKEN") or "").strip()
    raw_chat = (os.getenv("GROUP_CHAT_ID") or "").strip()

    errors = []
    if require_telegram:
        if not token:
            errors.append("BOT_TOKEN не задан")
        elif not _TOKEN_RE.match(token):
            errors.append("BOT_TOKEN имеет неверный формат (ожидается '123456:ABC...')")
    group_chat_id = 0
    if raw_chat:
        try:
            group_chat_id = int(raw_chat)
        except ValueError:
            errors.append(f"GROUP_CHAT_ID должен быть целым числом, получено '{raw_chat}'")
    if require_telegram and group_chat_id == 0 and not any("GROUP_CHAT_ID" in e for e in errors):
        errors.append("GROUP_CHAT_ID не задан (например -1001234567890)")
    if errors:
        raise ConfigError("; ".join(errors))

    allowed = parse_allowed_user_ids(os.getenv("ALLOWED_USER_IDS"))
    admins = parse_allowed_user_ids(os.getenv("ADMIN_USER_IDS"), "ADMIN_USER_IDS")
    return Settings(
        bot_token=token,
        group_chat_id=group_chat_id,
        target_bot_tag=(os.getenv("TARGET_BOT_TAG") or "@supp0rt_dom").strip(),
        database_url=resolve_database_url(os.getenv("DATABASE_URL")),
        allowed_user_ids=allowed,
        admin_user_ids=admins,
        admin_public_url=(os.getenv("ADMIN_PUBLIC_URL") or "").strip().rstrip("/"),
    )


@dataclass
class AdminSettings:
    secret_key: str
    bot_token: str
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""
    bot_username: str = ""          # для Telegram Login Widget (необязательно)
    session_hours: int = 12
    database_url: str = DEFAULT_DATABASE_URL

    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")


def load_admin_settings() -> AdminSettings:
    """Настройки админки. Обязательны BOT_TOKEN (им проверяем подпись Telegram) и ADMIN_SECRET_KEY."""
    load_dotenv(BASE_DIR / ".env")
    load_dotenv()
    errors = []
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        errors.append("BOT_TOKEN не задан (нужен для проверки входа через Telegram)")
    secret = (os.getenv("ADMIN_SECRET_KEY") or "").strip()
    if len(secret) < 32:
        errors.append("ADMIN_SECRET_KEY не задан или короче 32 символов "
                      "(сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
    try:
        port = int(os.getenv("ADMIN_PORT") or 8080)
    except ValueError:
        errors.append("ADMIN_PORT должен быть числом")
        port = 8080
    if errors:
        raise ConfigError("; ".join(errors))
    return AdminSettings(
        secret_key=secret, bot_token=token,
        host=(os.getenv("ADMIN_HOST") or "127.0.0.1").strip(), port=port,
        public_url=(os.getenv("ADMIN_PUBLIC_URL") or "").strip().rstrip("/"),
        bot_username=(os.getenv("ADMIN_BOT_USERNAME") or "").strip().lstrip("@"),
        database_url=resolve_database_url(os.getenv("DATABASE_URL")),
    )
