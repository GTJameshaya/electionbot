"""Связь с CRM: после отправки ведомости в группу создаём (или дописываем) тикет.

API CRM: POST {CRM_INGEST_URL}, заголовок «Authorization: Bearer {CRM_INGEST_TOKEN}», multipart/form-data.
Поля: text (подпись, обязательно), chat_id, source_message_id, author, meta (JSON), reply_to_ticket
(дописать в существующий тикет), files (xlsx). Ответ: {"ticket_number", "ticket_id", "created", "warnings"?},
ошибка — {"detail": "..."}.

Если схема запроса поменяется — правится только build_request() (и, при необходимости, parse_response()).
Ошибки CRM никогда не роняют бота и админку: всё возвращается как CrmResult и пишется в лог.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("crm")

TIMEOUT_SECONDS = 20.0
OVERSIZE_NOTE = "файл не приложен: слишком большой"
# CRM принимает файлы до 20 МиБ; берём с запасом: прикладываем, только если len(data) < 20 000 000.
MAX_FILE_BYTES = 20_000_000
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Понятные сообщения для админки по кодам ответа CRM.
ERROR_TEXTS = {
    401: "CRM не приняла токен — проверьте CRM_INGEST_TOKEN",
    404: "приём тикетов в CRM выключен или неверный CRM_INGEST_URL",
    409: "чат отключён от CRM",
    413: "файл слишком большой для CRM (больше 20 МБ)",
    422: "CRM отклонила данные",
    503: "в CRM не настроен Telegram-бот",
}


@dataclass(frozen=True)
class CrmConfig:
    url: str = ""
    token: str = ""
    timeout: float = TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)


def load_crm_config() -> CrmConfig:
    """CRM_INGEST_URL и CRM_INGEST_TOKEN из окружения. Если хоть одна пустая — интеграция выключена."""
    return CrmConfig(url=(os.getenv("CRM_INGEST_URL") or "").strip(),
                     token=(os.getenv("CRM_INGEST_TOKEN") or "").strip())


def auth_headers(cfg: CrmConfig) -> dict[str, str]:
    """Заголовок авторизации — единственное место, где он собирается."""
    return {"Authorization": f"Bearer {cfg.token}"}


def describe_config(cfg: CrmConfig) -> str:
    """Для лога при запуске (без токена)."""
    if not cfg.enabled:
        missing = [n for n, v in (("CRM_INGEST_URL", cfg.url), ("CRM_INGEST_TOKEN", cfg.token)) if not v]
        return f"CRM-интеграция выключена (не задано: {', '.join(missing)})"
    return f"CRM-интеграция включена: {httpx.URL(cfg.url).host}"


@dataclass
class CrmResult:
    ok: bool
    ticket_number: int | None = None
    ticket_id: int | None = None
    created: bool | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    status_code: int | None = None
    detail: str = ""                   # detail из ответа CRM (как есть)
    fallback: bool = False             # тикет для дописывания не нашёлся — создали новый

    def summary(self) -> str:
        """Одна строка для админки и журнала."""
        if not self.ok:
            return f"ошибка: {self.error}"
        text = f"тикет #{self.ticket_number} " + ("создан" if self.created else "дополнен")
        if self.fallback:
            text += " (старый тикет не найден — создан новый)"
        if self.warnings:
            text += "; предупреждения: " + "; ".join(self.warnings)
        return text


@dataclass
class TicketRequest:
    """Что отправляем в CRM (не зависит от формата запроса)."""
    text: str
    chat_id: int
    source_message_id: int | None
    author: str
    meta: dict
    file_name: str
    data: bytes
    reply_to_ticket: int | None = None


def file_fits(data: bytes) -> bool:
    """Прикладываем файл, только если он заведомо меньше лимита CRM."""
    return len(data) < MAX_FILE_BYTES


def build_request(cfg: CrmConfig, req: TicketRequest) -> dict:
    """Адаптер: TicketRequest → аргументы httpx.post (url, headers, data, files)."""
    form = {"text": req.text, "chat_id": str(req.chat_id), "author": req.author,
            "meta": json.dumps(req.meta, ensure_ascii=False)}
    if req.source_message_id is not None:
        form["source_message_id"] = str(req.source_message_id)
    if req.reply_to_ticket is not None:
        form["reply_to_ticket"] = str(req.reply_to_ticket)
    files = []
    if file_fits(req.data):
        files.append(("files", (req.file_name, req.data, XLSX_MIME)))
    else:
        form["text"] = f"{req.text}\n{OVERSIZE_NOTE}"
        logger.warning("CRM: файл %s слишком большой (%d байт) — отправляю тикет без файла",
                       req.file_name, len(req.data))
    return {"url": cfg.url, "headers": auth_headers(cfg), "data": form, "files": files or None}


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, (list, dict)):
        detail = json.dumps(detail, ensure_ascii=False)
    return str(detail or resp.text or "").strip()[:300]


def parse_response(resp: httpx.Response) -> CrmResult:
    if resp.status_code == 200:
        try:
            body = resp.json()
            warnings = [str(w) for w in (body.get("warnings") or [])]
            return CrmResult(ok=True, ticket_number=int(body["ticket_number"]),
                             ticket_id=body.get("ticket_id"), created=bool(body.get("created")),
                             warnings=warnings, status_code=200)
        except (ValueError, KeyError, TypeError, AttributeError):
            return CrmResult(ok=False, status_code=200, error=f"непонятный ответ CRM: {resp.text[:200]}")
    detail = _detail(resp)
    if resp.status_code == 404 and is_ticket_missing(detail):
        base = "тикет для дописывания не найден"
    else:
        base = ERROR_TEXTS.get(resp.status_code, f"CRM ответила кодом {resp.status_code}")
    return CrmResult(ok=False, status_code=resp.status_code, detail=detail,
                     error=f"{base} (HTTP {resp.status_code}{': ' + detail if detail else ''})")


def is_ticket_missing(detail: str) -> bool:
    """404 бывает двух видов. CRM отвечает {"detail": "Тикет #N не найден"}, если нет тикета из
    reply_to_ticket, и {"detail": "Not Found"}, если приём тикетов выключен (или адрес неверный)."""
    d = (detail or "").strip().lower()
    return "тикет" in d and "не найден" in d


def make_client(cfg: CrmConfig) -> httpx.AsyncClient:
    """Отдельной функцией — чтобы в тестах подменить транспорт (без сети)."""
    return httpx.AsyncClient(timeout=cfg.timeout)


async def push_ticket(cfg: CrmConfig, req: TicketRequest) -> CrmResult:
    """Один запрос в CRM. Никогда не бросает исключений."""
    try:
        async with make_client(cfg) as client:
            resp = await client.post(**build_request(cfg, req))
        result = parse_response(resp)
    except httpx.TimeoutException:
        result = CrmResult(ok=False, error=f"CRM не ответила за {cfg.timeout:g} с")
    except httpx.HTTPError as e:
        result = CrmResult(ok=False, error=f"не удалось связаться с CRM: {e.__class__.__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — CRM не должна ломать отправку
        logger.exception("CRM: неожиданная ошибка")
        result = CrmResult(ok=False, error=f"неожиданная ошибка: {e}")
    return result


async def push_with_fallback(cfg: CrmConfig, req: TicketRequest) -> CrmResult:
    """Как push_ticket, но если тикет для дописывания не найден (404) — создаёт новый."""
    result = await push_ticket(cfg, req)
    if req.reply_to_ticket is not None and result.status_code == 404 and is_ticket_missing(result.detail):
        logger.warning("CRM: тикет #%s не найден (%s) — создаю новый", req.reply_to_ticket, result.error)
        req = TicketRequest(**{**req.__dict__, "reply_to_ticket": None})
        result = await push_ticket(cfg, req)
        result.fallback = result.ok
    return result


async def push_ledger_ticket(cfg: CrmConfig, db, *, house_id: int, house_name: str, period: str, kind: str,
                             source: str, user_id: int | None, author: str, text: str, chat_id: int,
                             message_id: int | None, file_name: str, data: bytes,
                             reply_to_ticket: int | None) -> CrmResult:
    """Тикет по отправленной ведомости + запоминаем его номер в БД (для следующего «дописать»).

    Вызывать строго после удачного send_document. Результат — в лог; исключений не бросает."""
    meta = {"house": house_name, "house_id": house_id, "period": period, "kind": kind,
            "source": source, "user_id": user_id}
    req = TicketRequest(text=text, chat_id=chat_id, source_message_id=message_id, author=author,
                        meta=meta, file_name=file_name, data=data, reply_to_ticket=reply_to_ticket)
    result = await push_with_fallback(cfg, req)
    where = f"дом {house_id}, {period}, {kind}, {source}"
    if result.ok:
        logger.info("CRM: %s (%s)", result.summary(), where)
        for w in result.warnings:
            logger.warning("CRM: предупреждение по тикету #%s: %s", result.ticket_number, w)
        try:
            db.save_crm_ticket(house_id, period, kind, result.ticket_number, result.ticket_id, source)
        except Exception:  # noqa: BLE001
            logger.exception("CRM: тикет создан, но номер не удалось сохранить в БД")
    else:
        logger.warning("CRM: %s (%s)", result.error, where)
    return result
