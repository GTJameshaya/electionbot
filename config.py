"""Загрузка и проверка настроек из .env / переменных окружения."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_URL = "sqlite:///data/electionbot.db"

# Справочник домов. Ключ = номер дома в боте (как в старом коде: house1/house2).
# template — жилые квартиры, commerce_template — коммерческие абоненты (необязательно).
HOUSES = {
    1: {"name": "Волкова д.9 к.1", "hashtag": "#Волкова", "template": "template_1.xlsx",
        "commerce_template": "template_1_commerce.xlsx"},
    2: {"name": "Химиков 4", "hashtag": "#Химиков", "template": "template_2.xlsx",
        "commerce_template": "template_2_commerce.xlsx"},
}

# Файл с псевдонимами коммерческих абонентов (см. README). Если файла нет — встроенные значения.
ALIASES_FILE = BASE_DIR / "aliases.json"

_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


class ConfigError(Exception):
    """Ошибка конфигурации (бот не должен стартовать)."""


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
    """Относительный путь SQLite считаем от папки бота и создаём каталог."""
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
    load_dotenv(BASE_DIR / ".env")
    load_dotenv()  # .env в текущем каталоге (если запуск не из папки бота)

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
    """Настройки админ-панели. Нужны BOT_TOKEN (проверка подписи Telegram) и ADMIN_SECRET_KEY."""
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
