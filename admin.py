"""Веб-админка (FastAPI + Jinja2): показания, абоненты, псевдонимы, пользователи, отправки, графики.

Работает отдельным процессом с той же БД, что и бот.
Запуск: python admin.py (адрес — ADMIN_HOST / ADMIN_PORT, по умолчанию 127.0.0.1:8080).
Вход — только через Telegram: кнопка «🛠 Админ-панель» или /admin в боте (для администраторов).
"""
from __future__ import annotations

import hmac
import logging
import secrets
import sys
import time
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from admin_auth import AuthError, validate_login_widget, validate_webapp_init_data
from apartments import apartment_label
from captions import archive_file_name, build_caption
from config import BASE_DIR, HOUSES, AdminSettings, ConfigError, load_admin_settings, load_settings
from crm_ingest import describe_config, push_ledger_ticket
from logging_setup import add_secret, setup_logging
from db import (ROLE_ADMIN, ROLE_NONE, ROLE_USER, ROLES, Database, LastAdminError, current_period,
                parse_period, previous_period, utcnow)
from excel_report import MONTHS_RU
from ledgers import COMMERCE, LEDGERS, RESIDENTIAL

logger = logging.getLogger("admin")

ROLE_TITLES = {ROLE_ADMIN: "Администратор", ROLE_USER: "Пользователь бота", ROLE_NONE: "Нет доступа"}
SOURCE_TITLES = {"bot": "бот", "admin": "админка", "archive": "архив", "template": "шаблон"}
STATUS_TITLES = {"sent": "отправлено", "failed": "ошибка"}
MODE_PLAIN, MODE_TICKET = "plain", "ticket"
MODE_TITLES = {MODE_PLAIN: "Просто сообщение", MODE_TICKET: "С тикетом"}
RESEND_STATUS = {"sending": "отправляется…", "sent": "отправлено", "failed": "ошибка",
                 "unknown": "неизвестно"}
STALE_RESEND_MINUTES = 10
UNKNOWN_DELIVERY = "Telegram не ответил вовремя — файл мог дойти, проверьте чат"


def is_timeout(exc: BaseException) -> bool:
    """Таймаут при отправке: запрос мог дойти до Telegram, поэтому это не «ошибка», а «неизвестно»."""
    import asyncio

    import httpx
    from telegram.error import TimedOut
    return isinstance(exc, (TimedOut, httpx.TimeoutException, asyncio.TimeoutError, TimeoutError))


class TelegramSender:
    """Отправка файла в группу из процесса админки через Bot API (python-telegram-bot).
    В тестах подменяется объектом с тем же методом send_document."""

    def __init__(self, token: str):
        self.token = token

    async def send_document(self, chat_id: int, data: bytes, filename: str, caption: str) -> int:
        """Возвращает message_id отправленного сообщения; при ошибке — исключение."""
        from telegram import Bot
        async with Bot(self.token) as bot:
            msg = await bot.send_document(chat_id, document=data, filename=filename, caption=caption)
        return msg.message_id


# ======================================================================== помощники
def period_title(period: str) -> str:
    y, m = parse_period(period)
    return f"{MONTHS_RU[m - 1]} {y}"


def recent_periods(n: int = 18) -> list[str]:
    """Последние n месяцев, начиная с текущего (для выпадающих списков)."""
    out, p = [], current_period()
    for _ in range(n):
        out.append(p)
        p = previous_period(p)
    return out


def fmt(v) -> str:
    """Число для таблиц: 12.0 → «12», 12.5 → «12,5», None → пусто."""
    if v is None:
        return ""
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def parse_number(raw: str) -> float:
    """Число из формы («1 234,5» → 1234.5). Отрицательное, NaN или бесконечность — ValueError."""
    raw = (raw or "").strip().replace(" ", "").replace(",", ".")
    v = float(raw)
    if v < 0 or v != v or v in (float("inf"), float("-inf")):
        raise ValueError
    return v


def item_label(item, kind: str, name: str | None) -> str:
    if kind == COMMERCE:
        return name or item.number
    return apartment_label(item.number)


def ledgers_for(db: Database, house_id: int) -> list[str]:
    return [k for k in LEDGERS if db.has_ledger(house_id, k)]


def consumption_series(values: dict[str, float]) -> dict[str, float]:
    """{месяц: расход} — разница с прошлым месяцем там, где есть оба значения."""
    out = {}
    for p, v in values.items():
        pv = values.get(previous_period(p))
        if pv is not None:
            out[p] = round(v - pv, 3)
    return out


# ======================================================================== HEAD
class HeadAsGetMiddleware:
    """Отвечает на HEAD для любой GET-страницы (curl -I, мониторинг):
    обрабатываем как GET, заголовки отдаём, тело — нет."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "HEAD":
            return await self.app(scope, receive, send)
        scope = dict(scope, method="GET")

        async def send_headers_only(message):
            if message["type"] == "http.response.body":
                if message.get("more_body"):
                    return  # остальные части тела пропускаем
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app(scope, receive, send_headers_only)


# ======================================================================== приложение
def create_app(db: Database, settings: AdminSettings, sender=None) -> FastAPI:
    """Собирает приложение со всеми страницами. Маршруты — вложенные функции, им доступны db и settings.
    sender — чем слать файлы в группу (по умолчанию TelegramSender; в тестах — подделка)."""
    sender = sender or TelegramSender(settings.bot_token)
    app = FastAPI(title="Админ-панель", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="eb_admin",
                       max_age=settings.session_hours * 3600, same_site="lax", https_only=settings.https)
    templates = Jinja2Templates(directory=str(BASE_DIR / "admin_templates"))
    templates.env.globals.update(fmt=fmt, period_title=period_title, HOUSES=HOUSES, LEDGERS=LEDGERS,
                                 ROLE_TITLES=ROLE_TITLES, SOURCE_TITLES=SOURCE_TITLES,
                                 STATUS_TITLES=STATUS_TITLES, MODE_TITLES=MODE_TITLES,
                                 RESEND_STATUS=RESEND_STATUS)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "admin_static")), name="static")
    app.state.db, app.state.settings = db, settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")   # чтобы токен из ссылки не утёк в Referer
        # Mini App открывается во фрейме Telegram — встраивать разрешаем только ему
        resp.headers.setdefault("Content-Security-Policy",
                                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ------------------------------------------------------------ сессия / CSRF
    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = request.session["csrf"] = secrets.token_urlsafe(32)
        return tok

    def check_csrf(request: Request, token: str) -> bool:
        expected = request.session.get("csrf") or ""
        return bool(token) and hmac.compare_digest(expected, token)

    def current_admin(request: Request) -> int | None:
        """user_id вошедшего администратора или None (не вошёл, сессия истекла, роль сняли)."""
        uid = request.session.get("uid")
        login_at = request.session.get("login_at", 0)
        if not uid or time.time() - login_at > settings.session_hours * 3600:
            return None
        return uid if db.is_admin(uid) else None   # роль проверяем на каждом запросе

    def login_session(request: Request, uid: int) -> None:
        request.session.clear()
        request.session.update(uid=uid, login_at=int(time.time()), csrf=secrets.token_urlsafe(32))
        logger.info("Вход в админку: %s", uid)

    def actor(request: Request) -> str:
        return f"admin:{request.session.get('uid')}"

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session["flash"] = [kind, text]

    def render(request: Request, template: str, /, **ctx) -> HTMLResponse:
        uid = request.session.get("uid")
        user = db.get_user(uid) if uid else None
        ctx.update(request=request, csrf=csrf_token(request), me=user, flash=request.session.pop("flash", None),
                   nav=ctx.get("nav"))
        return templates.TemplateResponse(request, template, ctx)

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def guard(request: Request):
        """None — всё ок; иначе готовый ответ: редирект на /login (или 401 для /api/)."""
        if current_admin(request) is not None:
            return None
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return redirect("/login")

    async def form_guard(request: Request, csrf: str):
        """Как guard, плюс проверка CSRF-токена — для всех POST-форм."""
        g = guard(request)
        if g is not None:
            return g
        if not check_csrf(request, csrf):
            return Response("CSRF token invalid", status_code=403)
        return None

    # ------------------------------------------------------------ вход / выход
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html", bot_username=settings.bot_username,
                      error=request.query_params.get("error"))

    @app.get("/auth/tg")
    async def auth_token(request: Request, token: str = ""):
        uid = db.consume_login_token(token)
        if uid is None:
            return redirect("/login?error=" + quote("Ссылка недействительна или устарела. Запросите новую: /admin"))
        if not db.is_admin(uid):
            return redirect("/login?error=" + quote("Нет доступа"))
        login_session(request, uid)
        return redirect("/")

    @app.get("/auth/webapp", response_class=HTMLResponse)
    async def webapp_page(request: Request):
        return render(request, "webapp.html")

    @app.post("/auth/webapp")
    async def webapp_login(request: Request):
        # CSRF тут не нужен: вход подтверждает подпись Telegram (HMAC, не старше суток).
        try:
            payload = await request.json()
            user = validate_webapp_init_data(str(payload.get("init_data", "")), settings.bot_token)
        except (AuthError, ValueError) as e:
            return JSONResponse({"ok": False, "error": f"Не удалось проверить вход: {e}"}, status_code=403)
        uid = int(user["id"])
        if not db.is_admin(uid):
            return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
        login_session(request, uid)
        return JSONResponse({"ok": True, "redirect": "/"})

    @app.get("/auth/widget")
    async def widget_login(request: Request):
        try:
            uid = validate_login_widget(dict(request.query_params), settings.bot_token)
        except AuthError as e:
            return redirect("/login?error=" + quote(f"Не удалось проверить вход: {e}"))
        if not db.is_admin(uid):
            return redirect("/login?error=" + quote("Нет доступа"))
        login_session(request, uid)
        return redirect("/")

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("")):
        if not check_csrf(request, csrf):
            return Response("CSRF token invalid", status_code=403)
        request.session.clear()
        return redirect("/login")

    # ------------------------------------------------------------ главная
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if (g := guard(request)) is not None:
            return g
        period = current_period()
        return render(request, "index.html", nav="index", period=period, summary=missing_report(period),
                      pending=[u for u in db.list_users() if u.role == ROLE_NONE and u.last_seen])

    # ------------------------------------------------------------ показания
    def sel(request: Request) -> tuple[int, str, str]:
        """Дом, ведомость и месяц из адреса страницы; кривые значения заменяем на умолчания."""
        q = request.query_params
        first = next(iter(HOUSES))
        try:
            house = int(q.get("house", first))
        except ValueError:
            house = first
        house = house if house in HOUSES else first
        kind = q.get("kind", RESIDENTIAL)
        kind = kind if kind in LEDGERS else RESIDENTIAL
        period = q.get("period") or current_period()
        try:
            parse_period(period)
        except ValueError:
            period = current_period()
        return house, kind, period

    def readings_table(house: int, kind: str, period: str) -> list[dict]:
        """Строки для страницы «Показания»: прошлое, текущее, расход (отрицательный — подсветим)."""
        items = [i for i in db.list_items(house, kind) if i.active or i.origin == "admin"]
        names = db.names_for_period(house, kind, period, include_inactive=True)
        prev_p = previous_period(period)
        rows = db.reading_rows([i.id for i in items], [period, prev_p])
        out = []
        for i in items:
            cur, prev = rows.get((i.id, period)), rows.get((i.id, prev_p))
            cons = round(cur.value - prev.value, 3) if cur and prev else None
            out.append(dict(item=i, label=item_label(i, kind, names.get(i.number)),
                            owner=names.get(i.number) if kind == RESIDENTIAL else None,
                            cur=cur, prev=prev, cons=cons, negative=cons is not None and cons < 0))
        return out

    @app.get("/readings", response_class=HTMLResponse)
    async def readings(request: Request):
        if (g := guard(request)) is not None:
            return g
        house, kind, period = sel(request)
        rows = readings_table(house, kind, period)
        audit = db.audit_log([r["item"].id for r in rows], period, limit=30)
        labels = {r["item"].id: r["label"] for r in rows}
        return render(request, "readings.html", nav="readings", house=house, kind=kind, period=period,
                      periods=recent_periods(), rows=rows, audit=audit, labels=labels,
                      kinds=ledgers_for(db, house), filled=sum(1 for r in rows if r["cur"]),
                      query=urlencode({"house": house, "kind": kind, "period": period}))

    @app.post("/readings/save")
    async def readings_save(request: Request, csrf: str = Form(""), apartment_id: int = Form(...),
                            period: str = Form(...), value: str = Form(""), ret: str = Form("/readings")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            v = parse_number(value)
            res = db.admin_set_reading(apartment_id, period, v, actor(request))
            msg = f"Сохранено: {fmt(v)}" + (" ⚠️ меньше прошлого месяца" if res.decreased else "")
            flash(request, msg, "warn" if res.decreased else "ok")
        except ValueError:
            flash(request, f"«{value}» — не число", "error")
        # возвращаемся только на свои страницы (защита от open redirect)
        return redirect(ret if ret.startswith("/") and not ret.startswith("//") else "/readings")

    @app.post("/readings/delete")
    async def readings_delete(request: Request, csrf: str = Form(""), apartment_id: int = Form(...),
                              period: str = Form(...), ret: str = Form("/readings")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        flash(request, "Показание удалено" if db.admin_delete_reading(apartment_id, period, actor(request))
              else "Показания не было", "ok")
        return redirect(ret if ret.startswith("/") and not ret.startswith("//") else "/readings")

    # ------------------------------------------------------------ квартиры / абоненты
    @app.get("/items", response_class=HTMLResponse)
    async def items(request: Request):
        if (g := guard(request)) is not None:
            return g
        house, kind, period = sel(request)
        names = db.names_for_period(house, kind, current_period(), include_inactive=True)
        lst = [dict(item=i, label=item_label(i, kind, names.get(i.number)), name=names.get(i.number))
               for i in db.list_items(house, kind)]
        return render(request, "items.html", nav="items", house=house, kind=kind, items=lst,
                      kinds=ledgers_for(db, house))

    @app.post("/items/add")
    async def items_add(request: Request, csrf: str = Form(""), house: int = Form(...), kind: str = Form(...),
                        number: str = Form(""), title: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            db.add_item(house, kind, number, title, actor(request))
            flash(request, f"Добавлено: {number}")
        except ValueError as e:
            flash(request, str(e), "error")
        return redirect(f"/items?house={house}&kind={kind}")

    @app.post("/items/{item_id}/toggle")
    async def items_toggle(request: Request, item_id: int, csrf: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        item = db.get_item(item_id)
        if item is None:
            return Response("not found", status_code=404)
        db.set_disabled(item_id, not item.disabled)
        flash(request, "Включено" if item.disabled else "Отключено (в боте и Excel не участвует)")
        return redirect(f"/items?house={item.house_id}&kind={item.kind}")

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    async def item_detail(request: Request, item_id: int):
        if (g := guard(request)) is not None:
            return g
        item = db.get_item(item_id)
        if item is None:
            return Response("not found", status_code=404)
        names = db.names_for_period(item.house_id, item.kind, current_period(), include_inactive=True)
        return render(request, "item.html", nav="items", item=item, history=db.name_history(item_id),
                      label=item_label(item, item.kind, names.get(item.number)), name=names.get(item.number),
                      period=current_period(), periods=recent_periods(24)[::-1] + _future(3))

    def _future(n):
        """Следующие n месяцев — чтобы переименование можно было запланировать наперёд."""
        out, p = [], current_period()
        for _ in range(n):
            y, m = parse_period(p)
            p = f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"
            out.append(p)
        return out

    @app.post("/items/{item_id}/rename")
    async def item_rename(request: Request, item_id: int, csrf: str = Form(""), new_name: str = Form(""),
                          effective_period: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            db.rename_item(item_id, new_name, effective_period or current_period(), actor(request))
            flash(request, f"Название изменено с {period_title(effective_period or current_period())}")
        except ValueError as e:
            flash(request, str(e), "error")
        return redirect(f"/items/{item_id}")

    # ------------------------------------------------------------ псевдонимы
    def alias_matches(alias, bound: dict) -> list[str]:
        """Кого найдёт псевдоним — так же, как бот: привязанные по id + поиск фразы
        по всем названиям. Показываем текущие названия абонентов."""
        apts = {a.id: a for a in bound.get(alias.id, [])}
        apts.update({a.id: a for a in db.resolve_alias_target(alias.house_id, alias.target)})
        names = {h: db.names_for_period(h, COMMERCE, current_period(), include_inactive=True)
                 for h in {a.house_id for a in apts.values()}}
        return [f"{HOUSES[a.house_id]['name']}: {names[a.house_id].get(a.number) or a.number}"
                for a in sorted(apts.values(), key=lambda a: (a.house_id, a.row_order or 10**6, a.id))]

    @app.get("/aliases", response_class=HTMLResponse)
    async def aliases(request: Request):
        if (g := guard(request)) is not None:
            return g
        bound = db.alias_targets()
        rows = [dict(a=a, matches=alias_matches(a, bound)) for a in db.list_aliases()]
        edit_id = request.query_params.get("edit")
        return render(request, "aliases.html", nav="aliases", rows=rows,
                      edit=next((r["a"] for r in rows if str(r["a"].id) == edit_id), None))

    @app.post("/aliases/save")
    async def aliases_save(request: Request, csrf: str = Form(""), alias_id: str = Form(""), house: str = Form(""),
                           alias: str = Form(""), target: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            house_id = int(house) if house and house != "*" else None
            db.save_alias(int(alias_id) if alias_id else None, house_id, alias, target, actor(request))
            flash(request, f"Псевдоним «{alias}» сохранён — бот учитывает его сразу")
        except ValueError as e:
            flash(request, str(e), "error")
        return redirect("/aliases")

    @app.post("/aliases/{alias_id}/delete")
    async def aliases_delete(request: Request, alias_id: int, csrf: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        db.delete_alias(alias_id)
        flash(request, "Псевдоним удалён")
        return redirect("/aliases")

    # ------------------------------------------------------------ пользователи
    @app.get("/users", response_class=HTMLResponse)
    async def users(request: Request):
        if (g := guard(request)) is not None:
            return g
        lst = db.list_users()
        return render(request, "users.html", nav="users", users=lst, roles=ROLES,
                      pending=[u for u in lst if u.role == ROLE_NONE and u.last_seen])

    @app.post("/users/{user_id}/role")
    async def users_role(request: Request, user_id: int, csrf: str = Form(""), role: str = Form("")):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            db.set_role(user_id, role, actor(request))
            flash(request, f"Роль {user_id}: {ROLE_TITLES.get(role, role)}")
        except LastAdminError as e:
            flash(request, f"Нельзя: {e}", "error")
        except ValueError as e:
            flash(request, str(e), "error")
        return redirect("/users")

    @app.post("/users/add")
    async def users_add(request: Request, csrf: str = Form(""), user_id: str = Form(""), role: str = Form(ROLE_USER)):
        if (g := await form_guard(request, csrf)) is not None:
            return g
        try:
            db.set_role(int(user_id), role, actor(request))
            flash(request, f"Добавлен {user_id}: {ROLE_TITLES.get(role, role)}")
        except ValueError:
            flash(request, "Telegram ID — это число", "error")
        return redirect("/users")

    # ------------------------------------------------------------ отправки / Excel
    @app.get("/submissions", response_class=HTMLResponse)
    async def submissions(request: Request):
        if (g := guard(request)) is not None:
            return g
        users_by_id = {u.user_id: u for u in db.list_users()}
        house, kind, period = sel(request)
        return render(request, "submissions.html", nav="submissions", subs=db.list_submissions(),
                      users=users_by_id, periods=recent_periods(), house=house, kind=kind, period=period)

    @app.get("/download")
    async def download(request: Request):
        if (g := guard(request)) is not None:
            return g
        house, kind, period = sel(request)
        if not db.has_ledger(house, kind):
            return Response("нет шаблона", status_code=404)
        data = db.build_excel(house, period, kind)
        tag = "" if kind == RESIDENTIAL else "commerce_"
        name = f"{period}_house{house}_{tag}{HOUSES[house]['name'].replace(' ', '_')}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=\"house{house}_{period}.xlsx\"; "
                                                        f"filename*=UTF-8''{quote(name)}"})

    # ------------------------------------------------------------ отправить в чат
    def resend_targets() -> list[dict]:
        """Что можно отправить: по каждому дому и ведомости — текущий месяц и все месяцы с показаниями."""
        out = []
        for house in HOUSES:
            for kind in ledgers_for(db, house):
                counts = db.periods_with_readings(house, kind)
                periods = sorted(set(counts) | {current_period()}, reverse=True)
                rows = []
                for p in periods:
                    t = db.get_crm_ticket(house, p, kind)
                    rows.append(dict(period=p, count=counts.get(p, 0), ticket=t,
                                     submitted=db.is_submitted(house, p, kind)))
                out.append(dict(house=house, kind=kind, rows=rows))
        return out

    def resend_allowed(house: int, kind: str, period: str) -> bool:
        if house not in HOUSES or kind not in LEDGERS or not db.has_ledger(house, kind):
            return False
        return period == current_period() or period in db.periods_with_readings(house, kind)

    def resend_ctx() -> dict:
        return dict(chat_ok=bool(settings.group_chat_id), crm_ok=settings.crm.enabled)

    @app.get("/resend", response_class=HTMLResponse)
    async def resend_page(request: Request):
        if (g := guard(request)) is not None:
            return g
        users_by_id = {u.user_id: u for u in db.list_users()}
        db.expire_stale_resends(STALE_RESEND_MINUTES)
        return render(request, "resend.html", nav="resend", targets=resend_targets(), log=db.list_resends(),
                      users=users_by_id, **resend_ctx())

    @app.post("/resend/confirm", response_class=HTMLResponse)
    async def resend_confirm(request: Request, csrf: str = Form(""), house: int = Form(...),
                             kind: str = Form(...), period: str = Form(...), mode: str = Form(MODE_PLAIN),
                             ticket: str = Form("new")):
        """Шаг подтверждения: показываем, что именно уйдёт, и выдаём одноразовый токен."""
        if (g := await form_guard(request, csrf)) is not None:
            return g
        if not settings.group_chat_id:
            flash(request, "GROUP_CHAT_ID не задан — отправлять некуда", "error")
            return redirect("/resend")
        if mode not in MODE_TITLES or not resend_allowed(house, kind, period):
            flash(request, "Нельзя отправить: неизвестный дом, ведомость, месяц или режим", "error")
            return redirect("/resend")
        if mode == MODE_TICKET and not settings.crm.enabled:
            flash(request, "Режим «С тикетом» выключен: не заданы CRM_INGEST_URL / CRM_INGEST_TOKEN", "error")
            return redirect("/resend")
        existing = db.get_crm_ticket(house, period, kind) if mode == MODE_TICKET else None
        reply_to = existing.ticket_number if existing and ticket != "new" else None
        uid = current_admin(request)
        user = db.get_user(uid)
        token = db.create_resend(uid, user.display if user else str(uid), house, period, kind, mode, reply_to)
        tag = settings.target_bot_tag if mode == MODE_TICKET else ""
        return render(request, "resend_confirm.html", nav="resend", token=token, house=house, kind=kind,
                      period=period, mode=mode, reply_to=reply_to, existing=existing,
                      caption=build_caption(tag, house, period, kind),
                      count=db.count_readings(house, period, kind), chat_id=settings.group_chat_id)

    @app.post("/resend/send")
    async def resend_send(request: Request, csrf: str = Form(""), token: str = Form("")):
        """Отправка. Токен гасится атомарно, так что повторный POST ничего не отправит."""
        if (g := await form_guard(request, csrf)) is not None:
            return g
        uid = current_admin(request)
        row = db.claim_resend(token, uid)
        if row is None:
            flash(request, "Эта отправка уже выполнена или подтверждение устарело — файл повторно не отправлен",
                  "warn")
            return redirect("/resend")
        house, kind, period = row.house_id, row.kind, row.period
        ticket_mode = row.mode == MODE_TICKET
        caption = build_caption(settings.target_bot_tag if ticket_mode else "", house, period, kind)
        file_name = archive_file_name(house, kind=kind)
        try:
            data = db.build_excel(house, period, kind)
            message_id = await sender.send_document(settings.group_chat_id, data, file_name, caption)
        except Exception as e:  # noqa: BLE001
            if is_timeout(e):
                # Файл мог дойти: не «ошибка», а «неизвестно», и CRM не трогаем (нет message_id).
                logger.warning("Отправка в чат из админки: таймаут (%s) — файл мог дойти", e)
                db.finish_resend(row.id, status="unknown", error=UNKNOWN_DELIVERY, chat_id=settings.group_chat_id,
                                 file_name=file_name)
                flash(request, f"⚠️ {UNKNOWN_DELIVERY}. Если файла нет — отправьте ещё раз.", "warn")
                return redirect("/resend")
            logger.exception("Отправка в чат из админки не удалась")
            db.finish_resend(row.id, status="failed", error=str(e)[:2000], chat_id=settings.group_chat_id,
                             file_name=file_name)
            flash(request, f"Не удалось отправить: {e}", "error")
            return redirect("/resend")
        db.finish_resend(row.id, status="sent", sent_at=utcnow(), chat_id=settings.group_chat_id,
                         message_id=message_id, file_name=file_name)
        try:
            db.mark_submitted(house, period, uid, file_name, source="admin", kind=kind)
        except Exception:  # noqa: BLE001
            logger.exception("Файл отправлен, но запись в журнал отправок не удалась")
        text = f"Отправлено в чат: {HOUSES[house]['name']}, {LEDGERS[kind].title}, {period_title(period)}"
        level = "ok"
        if ticket_mode:
            res = await push_ledger_ticket(
                settings.crm, db, house_id=house, house_name=HOUSES[house]["name"], period=period, kind=kind,
                source="admin_resend", user_id=uid, author=row.requested_name or str(uid), text=caption,
                chat_id=settings.group_chat_id, message_id=message_id, file_name=file_name, data=data,
                reply_to_ticket=row.reply_to_ticket)
            db.finish_resend(row.id, crm_status="ok" if res.ok else "error", crm_ticket_number=res.ticket_number,
                             crm_created=res.created, crm_message=res.summary()[:2000])
            text += f". CRM: {res.summary()}"
            level = "ok" if res.ok and not res.warnings else "warn"
        flash(request, text, level)
        return redirect("/resend")

    # ------------------------------------------------------------ кто не сдал
    def missing_report(period: str) -> list[dict]:
        """Сводка «Кто не сдал» за месяц по всем домам и ведомостям."""
        out = []
        for house in HOUSES:
            for kind in ledgers_for(db, house):
                names = db.names_for_period(house, kind, period)
                have = db.get_readings(house, period, kind)
                items = db.apartment_map(house, kind=kind)
                missing = [item_label(a, kind, names.get(k)) for k, a in items.items() if k not in have]
                out.append(dict(house=house, kind=kind, total=len(items), done=len(items) - len(missing),
                                missing=missing, submitted=db.is_submitted(house, period, kind)))
        return out

    @app.get("/missing", response_class=HTMLResponse)
    async def missing(request: Request):
        if (g := guard(request)) is not None:
            return g
        _, _, period = sel(request)
        return render(request, "missing.html", nav="missing", period=period, periods=recent_periods(),
                      report=missing_report(period))

    @app.get("/api/missing")
    async def api_missing(request: Request):
        if (g := guard(request)) is not None:
            return g
        _, _, period = sel(request)
        return {"period": period, "report": missing_report(period)}

    # ------------------------------------------------------------ графики
    @app.get("/charts", response_class=HTMLResponse)
    async def charts(request: Request):
        if (g := guard(request)) is not None:
            return g
        house, kind, _ = sel(request)
        names = db.names_for_period(house, kind, current_period(), include_inactive=True)
        options = [(i.id, item_label(i, kind, names.get(i.number))) for i in db.list_items(house, kind)]
        return render(request, "charts.html", nav="charts", house=house, kind=kind, options=options,
                      kinds=ledgers_for(db, house), item=request.query_params.get("item", ""))

    @app.get("/api/charts/totals")
    async def api_totals(request: Request):
        if (g := guard(request)) is not None:
            return g
        house, kind, _ = sel(request)
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for values in db.value_history(house, kind).values():
            for p, c in consumption_series(values).items():
                totals[p] = round(totals.get(p, 0) + c, 3)
                counts[p] = counts.get(p, 0) + 1
        periods = sorted(totals)
        return {"house": house, "kind": kind, "periods": periods,
                "labels": [period_title(p) for p in periods],
                "totals": [totals[p] for p in periods], "counts": [counts[p] for p in periods]}

    @app.get("/api/charts/item/{item_id}")
    async def api_item(request: Request, item_id: int):
        if (g := guard(request)) is not None:
            return g
        item = db.get_item(item_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        values = db.value_history(item.house_id, item.kind).get(item_id, {})
        cons = consumption_series(values)
        periods = sorted(values)
        return {"item": item_id, "periods": periods, "labels": [period_title(p) for p in periods],
                "values": [values[p] for p in periods], "consumption": [cons.get(p) for p in periods]}

    app.add_middleware(HeadAsGetMiddleware)  # последним — значит, самым внешним слоем
    return app


# ======================================================================== запуск
def main(argv=None) -> int:
    """Точка входа: настройки, БД, роли из .env — и запускаем uvicorn."""
    setup_logging()
    try:
        settings = load_admin_settings()
        bot_settings = load_settings(require_telegram=False)
    except ConfigError as e:
        logger.critical("Ошибка конфигурации админки: %s. См. .env.example", e)
        return 1
    add_secret(settings.bot_token)
    add_secret(settings.crm.token)
    db = Database(settings.database_url)
    db.init()
    db.seed_roles(bot_settings.admin_user_ids, bot_settings.allowed_user_ids)
    logger.info(describe_config(settings.crm))
    if n := db.expire_stale_resends(STALE_RESEND_MINUTES):
        logger.warning("Отправки в чат, прерванные на полпути (статус «неизвестно»): %d", n)
    if not settings.group_chat_id:
        logger.warning("GROUP_CHAT_ID не задан — «Отправить в чат» в админке выключено")
    if db.count_admins() == 0:
        logger.error("Нет ни одного администратора — войти в панель не сможет никто. "
                     "Укажите ADMIN_USER_IDS в .env и перезапустите.")
    if not settings.https:
        logger.warning("ADMIN_PUBLIC_URL не https — Mini App в Telegram работать не будет, cookie без флага "
                       "Secure. Для доступа из интернета поставьте панель за nginx + HTTPS (см. README).")
    import uvicorn
    # log_config=None: uvicorn не ставит свои обработчики, его логи идут через наши (с фильтром секретов)
    uvicorn.run(create_app(db, settings), host=settings.host, port=settings.port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info", log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
