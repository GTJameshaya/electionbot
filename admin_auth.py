"""Проверка входа через Telegram для админ-панели.

* Mini App (WebApp): https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash       = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
* Login Widget: https://core.telegram.org/widgets/login#checking-authorization
    secret_key = SHA256(bot_token)
    hash       = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
data_check_string — все поля, кроме hash (signature у Mini App ВКЛЮЧАЕТСЯ), «key=value», отсортированные по ключу, через \n.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600  # initData / данные виджета не старше суток


class AuthError(Exception):
    pass


def _check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}={fields[k]}" for k in sorted(fields))


def _check_age(auth_date: str | None, max_age: int, now: float | None) -> None:
    try:
        ts = int(auth_date or "")
    except ValueError as exc:
        raise AuthError("нет auth_date") from exc
    now = time.time() if now is None else now
    if now - ts > max_age:
        raise AuthError("данные устарели")
    if ts - now > 300:
        raise AuthError("auth_date из будущего")


def validate_webapp_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS,
                              now: float | None = None) -> dict:
    """Возвращает словарь user из initData Mini App или бросает AuthError."""
    if not init_data:
        raise AuthError("пустые initData")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", "")
    # signature (Ed25519 для сторонних проверок) НЕ исключается: по документации Telegram
    # data_check_string для HMAC — все поля, кроме hash.
    if not received:
        raise AuthError("нет hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, _check_string(fields).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("неверная подпись")
    _check_age(fields.get("auth_date"), max_age, now)
    try:
        user = json.loads(fields.get("user") or "")
        int(user["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthError("нет user") from exc
    return user


def validate_login_widget(params: dict[str, str], bot_token: str, max_age: int = MAX_AGE_SECONDS,
                          now: float | None = None) -> int:
    """Возвращает Telegram user_id из данных Login Widget или бросает AuthError."""
    fields = {k: v for k, v in params.items() if v is not None}
    received = fields.pop("hash", "")
    if not received:
        raise AuthError("нет hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, _check_string(fields).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("неверная подпись")
    _check_age(fields.get("auth_date"), max_age, now)
    try:
        return int(fields["id"])
    except (KeyError, ValueError) as exc:
        raise AuthError("нет id") from exc


# ---------- для тестов и отладки: формирование корректно подписанных данных
def sign_webapp_init_data(fields: dict[str, str], bot_token: str) -> str:
    from urllib.parse import urlencode
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, _check_string(fields).encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def sign_login_widget(fields: dict[str, str], bot_token: str) -> dict[str, str]:
    secret = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret, _check_string(fields).encode(), hashlib.sha256).hexdigest()
    return {**fields, "hash": h}
