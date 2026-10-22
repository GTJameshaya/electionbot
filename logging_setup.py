"""Общая настройка логов для бота и админки.

* Формат — простой текст (удобно читать на хостинге).
* httpx/httpcore — только WARNING: на INFO httpx пишет полный адрес запроса, а в адресе Bot API
  есть токен бота (https://api.telegram.org/bot<TOKEN>/sendDocument).
* Плюс фильтр на всех обработчиках: вырезает из любых записей (и из трейсбеков) токен бота
  в виде «bot123:ABC…», сам токен бота и CRM_INGEST_TOKEN, а также ?token=… из адресов.
"""
from __future__ import annotations

import logging
import re

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
QUIET_LOGGERS = ("httpx", "httpcore")
REDACTED = "<скрыто>"

# bot<id>:<секрет> (в адресах Bot API) и просто <id>:<секрет> — формат токена Telegram
_BOT_TOKEN_RE = re.compile(r"(bot)?\d{5,}:[A-Za-z0-9_-]{30,}")
_QUERY_TOKEN_RE = re.compile(r"([?&](?:token|hash)=)[^&\s\"']+")

_secrets: set[str] = set()


def add_secret(value: str | None) -> None:
    """Значение, которое ни в каком виде не должно попасть в лог (токен бота, токен CRM)."""
    if value and len(value) >= 6:
        _secrets.add(value)


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        text = text.replace(s, REDACTED)
    text = _BOT_TOKEN_RE.sub(lambda m: (m.group(1) or "") + REDACTED, text)
    return _QUERY_TOKEN_RE.sub(lambda m: m.group(1) + REDACTED, text)


class RedactFilter(logging.Filter):
    """Переписывает текст записи (и трейсбек), убирая секреты. Запись не отбрасывает."""

    _fmt = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — кривые args: пусть форматирует сам logging
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._fmt.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_filter(handler: logging.Handler) -> None:
    if not any(isinstance(f, RedactFilter) for f in handler.filters):
        handler.addFilter(RedactFilter())


def setup_logging(level: int = logging.INFO, *secrets: str | None, stream=None, force: bool = False) -> None:
    """Вызывать в начале main() бота и админки. Повторный вызов безопасен."""
    kwargs = {"format": LOG_FORMAT, "level": level, "force": force}
    if stream is not None:
        kwargs["stream"] = stream
    logging.basicConfig(**kwargs)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for s in secrets:
        add_secret(s)
    for h in logging.getLogger().handlers:
        install_filter(h)
