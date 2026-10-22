"""Всё про хранение данных: таблицы и класс Database со всеми запросами.

По умолчанию — SQLite-файл, через DATABASE_URL можно подключить PostgreSQL (SQLAlchemy 2.x).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, case, create_engine, delete, event, false, func, inspect,
                        or_, select, text)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from config import BASE_DIR, DEFAULT_ALIASES, HOUSES
from apartments import is_nonstandard, natural_key
from excel_report import read_rows
from ledgers import COMMERCE, LEDGERS, RESIDENTIAL

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- периоды
def period_of(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def current_period() -> str:
    return period_of(datetime.now())


def parse_period(period: str) -> tuple[int, int]:
    year, month = period.split("-")
    return int(year), int(month)


def previous_period(period: str) -> str:
    year, month = parse_period(period)
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


# ---------------------------------------------------------------- модели
class Base(DeclarativeBase):
    pass


class House(Base):
    __tablename__ = "houses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1, 2 — как в боте
    name: Mapped[str] = mapped_column(String(200), unique=True)
    hashtag: Mapped[str] = mapped_column(String(100))
    template: Mapped[str] = mapped_column(String(255))
    apartments: Mapped[list["Apartment"]] = relationship(back_populates="house",
                                                         order_by="Apartment.row_order")


class Apartment(Base):
    """Строка ведомости: квартира (kind=residential) или коммерческий абонент (kind=commerce).

    У коммерции number — короткий ключ из названия (ledgers.subscriber_key), само название — в title.
    Ключи вида «к» + 12 hex не пересекаются с номерами квартир, поэтому старый UNIQUE(house_id, number)
    в ранних БД ничему не мешает и таблицу пересоздавать не нужно.
    """
    __tablename__ = "apartments"
    __table_args__ = (UniqueConstraint("house_id", "kind", "number", name="uq_apartment_house_kind_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default=RESIDENTIAL, server_default=RESIDENTIAL)
    number: Mapped[str] = mapped_column(String(20))
    # Базовое имя: у коммерции — название, у квартир — ФИО владельца (колонка B).
    # Переименования из админки title не трогают — они пишутся в name_history.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Как строка последний раз называлась в шаблоне — чтобы узнать её и после переименований.
    template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_order: Mapped[int] = mapped_column(Integer)  # строка в шаблоне; 0 — добавлено в админке
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # есть в шаблоне (или добавлено вручную)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # отключено в админке
    origin: Mapped[str] = mapped_column(String(20), default="template", server_default="template")
    house: Mapped[House] = relationship(back_populates="apartments")


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (UniqueConstraint("apartment_id", "period", name="uq_reading_apartment_period"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # 'YYYY-MM'
    value: Mapped[float] = mapped_column(Float)
    entered_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="bot")  # bot / archive / template / admin
    changed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)  # «tg:123» / «admin:root»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserSession(Base):
    """Текущая сессия пользователя. Живёт в БД, поэтому переживает перезапуск бота."""
    __tablename__ = "user_sessions"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(20), default=RESIDENTIAL, server_default=RESIDENTIAL)
    # Квартира/абонент, выбранные кнопкой: следующее число — их показание.
    pending_apartment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Очередь вопросов «какой подъезд?» (JSON): [{"raw":..., "value":..., "candidates":[...]}]
    pending_choice: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# Колонки, которых не было в первой версии схемы: при запуске доливаем их в старые БД.
_ADDED_COLUMNS = [
    ("user_sessions", "pending_apartment", "VARCHAR(20)"),
    ("user_sessions", "kind", f"VARCHAR(20) NOT NULL DEFAULT '{RESIDENTIAL}'"),
    ("user_sessions", "pending_choice", "TEXT"),
    ("apartments", "kind", f"VARCHAR(20) NOT NULL DEFAULT '{RESIDENTIAL}'"),
    ("apartments", "title", "VARCHAR(255)"),
    ("submissions", "kind", f"VARCHAR(20) NOT NULL DEFAULT '{RESIDENTIAL}'"),
    ("apartments", "template_name", "VARCHAR(255)"),
    ("apartments", "disabled", "BOOLEAN NOT NULL DEFAULT {false}"),
    ("apartments", "origin", "VARCHAR(20) NOT NULL DEFAULT 'template'"),
    ("readings", "changed_by", "VARCHAR(100)"),
    ("submissions", "status", "VARCHAR(20) NOT NULL DEFAULT 'sent'"),
    ("submissions", "error", "TEXT"),
]


class Submission(Base):
    """Журнал отправок ведомости в группу (и удачных, и нет) плюс импортированные архивные файлы."""
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    kind: Mapped[str] = mapped_column(String(20), default=RESIDENTIAL, server_default=RESIDENTIAL)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Не уникально: один и тот же файл могут отправить повторно. Повторный импорт архива
    # отсекается в migrate_archive.py по паре (file_name, source='archive').
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="bot")
    status: Mapped[str] = mapped_column(String(20), default="sent", server_default="sent")  # sent / failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NameHistory(Base):
    """История переименований абонентов и смены владельцев. new_name действует с effective_period."""
    __tablename__ = "name_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id"), index=True)
    old_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_name: Mapped[str] = mapped_column(String(255))
    effective_period: Mapped[str] = mapped_column(String(7))
    changed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alias(Base):
    """Псевдоним коммерческого абонента («рома» → «ромашка»). house_id NULL — для всех домов."""
    __tablename__ = "aliases"
    __table_args__ = (UniqueConstraint("house_id", "alias", name="uq_alias_house_alias"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    house_id: Mapped[int | None] = mapped_column(ForeignKey("houses.id"), nullable=True)
    alias: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AliasTarget(Base):
    """Привязка псевдонима к конкретным абонентам по id — не ломается при переименовании.
    Заполняется при сохранении псевдонима, а для старых псевдонимов — автоматически при запуске."""
    __tablename__ = "alias_targets"
    alias_id: Mapped[int] = mapped_column(ForeignKey("aliases.id", ondelete="CASCADE"), primary_key=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id", ondelete="CASCADE"), primary_key=True)


class BotUser(Base):
    """Все, кто писал боту. role: admin (бот + админка), user (только бот), none (нет доступа)."""
    __tablename__ = "bot_users"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    first_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    @property
    def display(self) -> str:
        name = " ".join(x for x in (self.first_name, self.last_name) if x)
        if self.username:
            name = f"{name} (@{self.username})" if name else f"@{self.username}"
        return name or str(self.user_id)


class LoginToken(Base):
    """Одноразовая ссылка входа в админку. Сам токен не храним — только его SHA-256."""
    __tablename__ = "login_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Meta(Base):
    """Служебные пары ключ-значение (например, отметка «псевдонимы уже перенесены»)."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReadingAudit(Base):
    """Журнал правок показаний: кто, когда, что было и что стало."""
    __tablename__ = "reading_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    action: Mapped[str] = mapped_column(String(20))  # create / update / delete
    old_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CrmTicket(Base):
    """Последний тикет CRM по ведомости (дом + месяц + вид): в него дописываются повторные отправки."""
    __tablename__ = "crm_tickets"
    __table_args__ = (UniqueConstraint("house_id", "period", "kind", name="uq_crm_ticket_ledger"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(20))
    ticket_number: Mapped[int] = mapped_column(Integer)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20))          # bot / admin_resend
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChatResend(Base):
    """Журнал отправок ведомости в чат из админки («Отправить в чат»).

    Строка появляется на шаге подтверждения (status=pending, хеш одноразового токена) и захватывается
    одним UPDATE … WHERE status='pending' — поэтому двойной клик отправит файл только один раз."""
    __tablename__ = "chat_resends"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    requested_by: Mapped[int] = mapped_column(BigInteger, index=True)
    requested_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    house_id: Mapped[int] = mapped_column(ForeignKey("houses.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(20))                    # plain / ticket
    reply_to_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)  # «дописать в тикет #N»
    # pending → sending → sent / failed / unknown (неясно, дошёл ли файл: таймаут или процесс прервался)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # нажали «Да, отправить»
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    crm_status: Mapped[str | None] = mapped_column(String(20), nullable=True)   # None (не нужно) / ok / error
    crm_ticket_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crm_created: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    crm_message: Mapped[str | None] = mapped_column(Text, nullable=True)       # итог / ошибка / предупреждения


ROLE_ADMIN, ROLE_USER, ROLE_NONE = "admin", "user", "none"
ROLES = (ROLE_ADMIN, ROLE_USER, ROLE_NONE)


class LastAdminError(Exception):
    """Нельзя убрать последнего администратора."""


def name_at(base: str | None, history: list, period: str) -> str | None:
    """Какое имя действовало в этом месяце: базовое, а поверх него — переименования не позже period."""
    name = base
    for h in sorted(history, key=lambda h: (h.effective_period, h.changed_at or datetime.min, h.id or 0)):
        if h.effective_period <= period:
            name = h.new_name
    return name


def _norm_name(name: str | None) -> str:
    from ledgers import norm_words
    return " ".join(norm_words(name or ""))


# ---------------------------------------------------------------- результаты
@dataclass
class SaveResult:
    number: str
    value: float
    previous: float | None
    updated: bool  # True — значение за период уже было и перезаписано

    @property
    def decreased(self) -> bool:
        return self.previous is not None and self.value < self.previous


# ---------------------------------------------------------------- репозиторий
class Database:
    def __init__(self, url: str, base_dir: Path = BASE_DIR, echo: bool = False):
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine = create_engine(url, echo=echo, future=True, pool_pre_ping=True,
                                    connect_args=connect_args)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - тривиально
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.base_dir = base_dir

    def dispose(self):
        self.engine.dispose()

    # ---------- схема и начальные данные
    def init(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._drop_submission_file_name_unique()
        self.sync_houses()
        n = self.seed_aliases(DEFAULT_ALIASES)
        if n:
            logger.info("Стартовые псевдонимы перенесены из houses.json в БД: %d", n)
        n = self.bind_aliases()
        if n:
            logger.info("Псевдонимы привязаны к абонентам (по id): %d", n)

    def _add_missing_columns(self) -> None:
        """Мини-миграция: добавляет недостающие колонки в БД от старых версий бота."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table, column, ddl in _ADDED_COLUMNS:
                existing = {c["name"] for c in insp.get_columns(table)}
                if column not in existing:
                    logger.info("Добавляю колонку %s.%s", table, column)
                    ddl = ddl.replace("{false}", "0" if self.engine.dialect.name == "sqlite" else "FALSE")
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

    def _file_name_unique_constraints(self) -> tuple[list[str], list[str]]:
        """Что делает submissions.file_name уникальным: (имена ограничений, имена индексов)."""
        insp = inspect(self.engine)
        constraints = [uc.get("name") or "" for uc in insp.get_unique_constraints("submissions")
                       if uc.get("column_names") == ["file_name"]]
        indexes = [ix["name"] for ix in insp.get_indexes("submissions")
                   if ix.get("unique") and ix.get("column_names") == ["file_name"]]
        return constraints, indexes

    def _drop_submission_file_name_unique(self) -> None:
        """Снимает UNIQUE(file_name), который создавала первая версия схемы.

        PostgreSQL и прочие — обычный DROP CONSTRAINT / DROP INDEX. SQLite так не умеет, поэтому
        там таблица пересоздаётся (см. _rebuild_sqlite_submissions).
        """
        constraints, indexes = self._file_name_unique_constraints()
        if not constraints and not indexes:
            return
        found = [f"ограничение {n or '(без имени)'}" for n in constraints] + [f"индекс {n}" for n in indexes]
        logger.warning("submissions: найдено UNIQUE на колонке file_name (%s) — снимаю", ", ".join(found))
        if self.engine.dialect.name != "sqlite":
            with self.engine.begin() as conn:
                for name in constraints:
                    if name:
                        conn.execute(text(f'ALTER TABLE submissions DROP CONSTRAINT "{name}"'))
                for name in indexes:
                    conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_submissions_file_name "
                                  "ON submissions (file_name)"))
            return
        self._rebuild_sqlite_submissions()

    def _rebuild_sqlite_submissions(self) -> None:
        """Пересоздаёт submissions в SQLite без UNIQUE (стандартный рецепт SQLite «12 шагов»).
        Всё в одной транзакции BEGIN IMMEDIATE: при ошибке — ROLLBACK, данные целы, бот работает дальше."""
        from sqlalchemy.schema import CreateIndex, CreateTable
        dialect = self.engine.dialect
        table = Submission.__table__
        cols = ", ".join(f'"{c.name}"' for c in table.columns)
        ddl = [str(CreateTable(table).compile(dialect=dialect))]
        ddl += [str(CreateIndex(ix).compile(dialect=dialect)) for ix in table.indexes]

        raw = self.engine.raw_connection()
        try:
            dbapi = raw.driver_connection
            old_isolation = dbapi.isolation_level
            dbapi.isolation_level = None  # управляем транзакцией вручную
            cur = dbapi.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                old_cols = {row[1] for row in cur.execute("PRAGMA table_info(submissions)")}
                copy_cols = ", ".join(f'"{c.name}"' for c in table.columns if c.name in old_cols)
                cur.execute("ALTER TABLE submissions RENAME TO submissions_old_unique")
                # старые индексы (кроме автоматических) заняли бы имена новых — удаляем
                for (name,) in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='submissions_old_unique' AND sql IS NOT NULL").fetchall():
                    cur.execute(f'DROP INDEX "{name}"')
                for stmt in ddl:
                    cur.execute(stmt)
                cur.execute(f"INSERT INTO submissions ({copy_cols}) "
                            f"SELECT {copy_cols} FROM submissions_old_unique")
                cur.execute("DROP TABLE submissions_old_unique")
                cur.execute("COMMIT")
                logger.info("Таблица submissions пересоздана без UNIQUE(file_name) (колонки: %s)", cols)
            except Exception:
                cur.execute("ROLLBACK")
                # Запуск не роняем: mark_submitted() в этом случае просто добавит к имени суффикс.
                logger.exception("Не удалось пересоздать submissions — оставляю как есть")
            finally:
                cur.close()
                dbapi.isolation_level = old_isolation
        finally:
            raw.close()

    def template_path(self, house_id: int, kind: str = RESIDENTIAL) -> Path | None:
        """Путь к шаблону ведомости; None — у дома такой ведомости нет в настройках."""
        name = HOUSES[house_id].get(LEDGERS[kind].template_key)
        return self.base_dir / name if name else None

    def has_ledger(self, house_id: int, kind: str) -> bool:
        """Есть ли у дома эта ведомость (файл шаблона лежит на месте)."""
        path = self.template_path(house_id, kind)
        return bool(path and path.exists())

    def sync_houses(self) -> None:
        """Заводит дома, квартиры и абонентов коммерции по шаблонам. Запускать повторно безопасно.

        * Квартиры ищем по номеру. ФИО из колонки B обновляем, только если владельца не меняли в админке.
        * Абонентов ищем по ключу, а если не нашли — по любому известному названию (текущему, из шаблона,
          из истории переименований). Так что и старое, и новое название в шаблоне не создаст дубль.
          Название в БД шаблон не перезаписывает.
        * Пропавшие из шаблона строки получают active=False (добавленные в админке не трогаем).
        * Отключение из админки (disabled) синхронизация не снимает.
        """
        with self.Session.begin() as s:
            for house_id, info in HOUSES.items():
                house = s.get(House, house_id)
                if house is None:
                    house = House(id=house_id, name=info["name"], hashtag=info["hashtag"],
                                  template=info["template"])
                    s.add(house)
                    s.flush()
                else:
                    house.name, house.hashtag, house.template = info["name"], info["hashtag"], info["template"]
                for kind, ledger in LEDGERS.items():
                    tpl = self.template_path(house_id, kind)
                    if tpl is None:
                        continue
                    if not tpl.exists():
                        logger.warning("Нет шаблона %s (%s) для дома %s", tpl, ledger.title, info["name"])
                        continue
                    self._sync_ledger(s, house_id, kind, read_rows(tpl, ledger))
                    logger.info("Дом %s, %s: шаблон синхронизирован", info["name"], ledger.title)

    def _sync_ledger(self, s, house_id: int, kind: str, rows: list[tuple[str, int, str]]) -> None:
        """Сверяет одну ведомость дома со строками шаблона (правила — в sync_houses)."""
        items = list(s.scalars(select(Apartment).where(Apartment.house_id == house_id, Apartment.kind == kind)))
        by_key = {a.number: a for a in items}
        history: dict[int, list[NameHistory]] = {}
        for h in s.scalars(select(NameHistory).where(NameHistory.apartment_id.in_([a.id for a in items] or [-1]))):
            history.setdefault(h.apartment_id, []).append(h)
        by_name: dict[str, Apartment] = {}
        if kind == COMMERCE:
            for a in items:
                names = [a.title, a.template_name] + [n for h in history.get(a.id, []) for n in (h.old_name, h.new_name)]
                for n in names:
                    if n:
                        by_name.setdefault(_norm_name(n), a)
        seen: set[int] = set()
        for number, row, title in rows:
            apt = by_key.get(number)
            if apt is None and kind == COMMERCE:
                apt = by_name.get(_norm_name(title))
            if apt is not None and apt.id in seen:  # две строки шаблона указали на одну запись
                continue
            if apt is None:
                apt = Apartment(house_id=house_id, kind=kind, number=number, title=title or None,
                                template_name=title or None, row_order=row, active=True, origin="template")
                s.add(apt)
                s.flush()
            else:
                apt.row_order, apt.active, apt.origin = row, True, "template"
                if kind == RESIDENTIAL and not history.get(apt.id):
                    apt.title = title or None        # ФИО следует шаблону, пока его не меняли в админке
                if apt.title is None:
                    apt.title = title or None
                apt.template_name = title or None
            seen.add(apt.id)
        for a in items:
            if a.id not in seen and a.origin == "template":
                a.active = False

    # ---------- справочники
    def _items_query(self, house_id: int, kind: str, include_inactive: bool = False):
        q = select(Apartment).where(Apartment.house_id == house_id, Apartment.kind == kind)
        if not include_inactive:
            q = q.where(Apartment.active.is_(True), Apartment.disabled.is_(False))
        # порядок как в шаблоне; добавленные в админке (row_order=0) — в конце
        return q.order_by(case((Apartment.row_order == 0, 1), else_=0), Apartment.row_order, Apartment.id)

    def apartment_map(self, house_id: int, s=None, kind: str = RESIDENTIAL) -> dict[str, Apartment]:
        """{номер/ключ: строка} — только активные и не отключённые, в порядке шаблона."""
        def _q(sess):
            return {a.number: a for a in sess.scalars(self._items_query(house_id, kind))}
        if s is not None:
            return _q(s)
        with self.Session() as sess:
            return _q(sess)

    def list_items(self, house_id: int, kind: str) -> list[Apartment]:
        """Все строки ведомости для админки — включая отключённые."""
        with self.Session() as s:
            return list(s.scalars(self._items_query(house_id, kind, include_inactive=True)))

    def get_item(self, apartment_id: int) -> Apartment | None:
        with self.Session() as s:
            return s.get(Apartment, apartment_id)

    def apartment_numbers(self, house_id: int, kind: str = RESIDENTIAL) -> list[str]:
        """Номера/ключи активных строк в порядке шаблона."""
        return list(self.apartment_map(house_id, kind=kind).keys())

    def nonstandard_apartments(self, house_id: int) -> list[str]:
        """Квартиры, номер которых не только из цифр (например «2ц» — цоколь)."""
        return sorted((n for n in self.apartment_numbers(house_id) if is_nonstandard(n)),
                      key=natural_key)

    def _history(self, s, ids: list[int]) -> dict[int, list[NameHistory]]:
        out: dict[int, list[NameHistory]] = {}
        if ids:
            for h in s.scalars(select(NameHistory).where(NameHistory.apartment_id.in_(ids))):
                out.setdefault(h.apartment_id, []).append(h)
        return out

    def names_for_period(self, house_id: int, kind: str, period: str,
                         include_inactive: bool = False) -> dict[str, str | None]:
        """{ключ: название/ФИО, которое действовало в этом месяце} — с учётом переименований."""
        with self.Session() as s:
            items = list(s.scalars(self._items_query(house_id, kind, include_inactive)))
            hist = self._history(s, [a.id for a in items])
            return {a.number: name_at(a.title, hist.get(a.id, []), period) for a in items}

    def name_history(self, apartment_id: int) -> list[NameHistory]:
        with self.Session() as s:
            return list(s.scalars(select(NameHistory).where(NameHistory.apartment_id == apartment_id)
                                  .order_by(NameHistory.effective_period, NameHistory.changed_at)))

    def subscribers(self, house_id: int, period: str | None = None) -> list[tuple[str, str]]:
        """Абоненты коммерции дома: [(ключ, название на этот месяц)] в порядке шаблона."""
        names = self.names_for_period(house_id, COMMERCE, period or current_period())
        return [(k, n or k) for k, n in names.items()]

    def subscriber_names(self, house_id: int) -> dict[str, list[str]]:
        """Все известные названия каждого абонента (текущее, из шаблона, старые) — чтобы бот узнавал любое."""
        with self.Session() as s:
            items = list(s.scalars(self._items_query(house_id, COMMERCE)))
            hist = self._history(s, [a.id for a in items])
            out = {}
            for a in items:
                names = [a.title, a.template_name] + [n for h in hist.get(a.id, []) for n in (h.old_name, h.new_name)]
                out[a.number] = list(dict.fromkeys(n for n in names if n))
            return out

    def rename_item(self, apartment_id: int, new_name: str, effective_period: str, actor: str) -> NameHistory:
        """Переименовать абонента / сменить владельца начиная с effective_period. Пишется в историю."""
        new_name = " ".join(new_name.split())
        if not new_name:
            raise ValueError("пустое название")
        parse_period(effective_period)
        with self.Session.begin() as s:
            apt = s.get(Apartment, apartment_id)
            if apt is None:
                raise ValueError("строка не найдена")
            hist = self._history(s, [apt.id]).get(apt.id, [])
            old = name_at(apt.title, hist, effective_period)
            h = NameHistory(apartment_id=apt.id, old_name=old, new_name=new_name,
                            effective_period=effective_period, changed_by=actor)
            s.add(h)
            return h

    def add_item(self, house_id: int, kind: str, number_or_name: str, title: str | None, actor: str) -> Apartment:
        """Добавить квартиру или абонента вручную (в Excel строка допишется в конец листа).
        Если такая строка уже есть — просто включаем её обратно."""
        from apartments import normalize_apartment
        from ledgers import subscriber_key
        raw = " ".join(number_or_name.split())
        if kind == RESIDENTIAL:
            number = normalize_apartment(raw)
            if number is None:
                raise ValueError(f"«{raw}» не похоже на номер квартиры")
            name = (title or "").strip() or None
        else:
            if not raw:
                raise ValueError("пустое название")
            number, name = subscriber_key(raw), raw
        with self.Session.begin() as s:
            exists = s.scalar(select(Apartment).where(Apartment.house_id == house_id, Apartment.kind == kind,
                                                      Apartment.number == number))
            if exists is not None:
                exists.disabled = False
                return exists
            apt = Apartment(house_id=house_id, kind=kind, number=number, title=name, template_name=None,
                            row_order=0, active=True, disabled=False, origin="admin")
            s.add(apt)
            s.flush()
            logger.info("%s добавил %s/%s: %s", actor, house_id, kind, raw)
            return apt

    def set_disabled(self, apartment_id: int, disabled: bool) -> None:
        with self.Session.begin() as s:
            apt = s.get(Apartment, apartment_id)
            if apt is not None:
                apt.disabled = disabled

    # ---------- показания
    def get_readings(self, house_id: int, period: str, kind: str = RESIDENTIAL) -> dict[str, float]:
        with self.Session() as s:
            rows = s.execute(
                select(Apartment.number, Reading.value)
                .join(Reading, Reading.apartment_id == Apartment.id)
                .where(Apartment.house_id == house_id, Apartment.kind == kind, Reading.period == period))
            return {number: value for number, value in rows}

    def save_reading(self, house_id: int, number: str, period: str, value: float,
                     user_id: int | None, source: str = "bot", overwrite: bool = True,
                     s=None, kind: str = RESIDENTIAL, actor: str | None = None) -> SaveResult | None:
        """Сохраняет или перезаписывает показание за месяц. None — строки нет или она отключена."""
        if s is None:
            with self.Session.begin() as sess:
                return self.save_reading(house_id, number, period, value, user_id, source, overwrite,
                                         sess, kind, actor)
        apt = s.scalar(select(Apartment).where(Apartment.house_id == house_id,
                                               Apartment.kind == kind,
                                               Apartment.number == number,
                                               Apartment.active.is_(True),
                                               Apartment.disabled.is_(False)))
        if apt is None:
            return None
        return self._write_reading(s, apt, period, value, user_id, source, overwrite, actor)

    def _write_reading(self, s, apt: Apartment, period: str, value: float, user_id: int | None,
                       source: str, overwrite: bool, actor: str | None) -> SaveResult:
        """Пишет показание и строчку в журнал reading_audit. overwrite=False — существующее не трогаем."""
        actor = actor or (f"tg:{user_id}" if user_id else source)
        prev = s.scalar(select(Reading.value).where(Reading.apartment_id == apt.id,
                                                    Reading.period == previous_period(period)))
        reading = s.scalar(select(Reading).where(Reading.apartment_id == apt.id,
                                                 Reading.period == period))
        updated = reading is not None
        if reading is None:
            s.add(Reading(apartment_id=apt.id, period=period, value=value,
                          entered_by=user_id, source=source, changed_by=actor))
            s.add(ReadingAudit(apartment_id=apt.id, period=period, action="create", old_value=None,
                               new_value=value, source=source, actor=actor))
        elif overwrite:
            old = reading.value
            reading.value, reading.entered_by, reading.source = value, user_id, source
            reading.changed_by, reading.updated_at = actor, utcnow()
            s.add(ReadingAudit(apartment_id=apt.id, period=period, action="update", old_value=old,
                               new_value=value, source=source, actor=actor))
        return SaveResult(number=apt.number, value=value, previous=prev, updated=updated)

    def admin_set_reading(self, apartment_id: int, period: str, value: float, actor: str) -> SaveResult:
        """Поставить или исправить показание из админки (source=admin, попадает в журнал)."""
        parse_period(period)
        with self.Session.begin() as s:
            apt = s.get(Apartment, apartment_id)
            if apt is None:
                raise ValueError("строка не найдена")
            return self._write_reading(s, apt, period, value, None, "admin", True, actor)

    def admin_delete_reading(self, apartment_id: int, period: str, actor: str) -> bool:
        """Удалить показание из админки (с записью в журнал). False — удалять было нечего."""
        with self.Session.begin() as s:
            r = s.scalar(select(Reading).where(Reading.apartment_id == apartment_id, Reading.period == period))
            if r is None:
                return False
            s.add(ReadingAudit(apartment_id=apartment_id, period=period, action="delete", old_value=r.value,
                               new_value=None, source="admin", actor=actor))
            s.delete(r)
            return True

    def reading_rows(self, apartment_ids: list[int], periods: list[str]) -> dict[tuple[int, str], Reading]:
        with self.Session() as s:
            if not apartment_ids:
                return {}
            q = select(Reading).where(Reading.apartment_id.in_(apartment_ids), Reading.period.in_(periods))
            return {(r.apartment_id, r.period): r for r in s.scalars(q)}

    def audit_log(self, apartment_ids: list[int] | None = None, period: str | None = None,
                  limit: int = 50) -> list[ReadingAudit]:
        with self.Session() as s:
            q = select(ReadingAudit)
            if apartment_ids is not None:
                q = q.where(ReadingAudit.apartment_id.in_(apartment_ids or [-1]))
            if period:
                q = q.where(ReadingAudit.period == period)
            return list(s.scalars(q.order_by(ReadingAudit.at.desc(), ReadingAudit.id.desc()).limit(limit)))

    def value_history(self, house_id: int, kind: str) -> dict[int, dict[str, float]]:
        """{apartment_id: {месяц: значение}} — данные для графиков."""
        with self.Session() as s:
            q = (select(Reading.apartment_id, Reading.period, Reading.value)
                 .join(Apartment, Reading.apartment_id == Apartment.id)
                 .where(Apartment.house_id == house_id, Apartment.kind == kind))
            out: dict[int, dict[str, float]] = {}
            for aid, period, value in s.execute(q):
                out.setdefault(aid, {})[period] = value
            return out

    def count_readings(self, house_id: int, period: str, kind: str = RESIDENTIAL) -> int:
        return len(self.get_readings(house_id, period, kind))

    # ---------- сессии
    def start_session(self, user_id: int, house_id: int, period: str, kind: str = RESIDENTIAL) -> None:
        with self.Session.begin() as s:
            sess = s.get(UserSession, user_id)
            if sess is None:
                s.add(UserSession(user_id=user_id, house_id=house_id, period=period, kind=kind))
            else:
                sess.house_id, sess.period, sess.kind = house_id, period, kind
                sess.pending_apartment = None
                sess.pending_choice = None
                sess.started_at = sess.updated_at = utcnow()

    def get_session(self, user_id: int) -> UserSession | None:
        with self.Session() as s:
            return s.get(UserSession, user_id)

    def set_pending_apartment(self, user_id: int, number: str | None) -> bool:
        with self.Session.begin() as s:
            sess = s.get(UserSession, user_id)
            if sess is None:
                return False
            sess.pending_apartment = number
            return True

    def get_pending_choices(self, user_id: int) -> list[dict]:
        sess = self.get_session(user_id)
        if sess is None or not sess.pending_choice:
            return []
        try:
            return list(json.loads(sess.pending_choice))
        except ValueError:
            return []

    def set_pending_choices(self, user_id: int, choices: list[dict]) -> None:
        with self.Session.begin() as s:
            sess = s.get(UserSession, user_id)
            if sess is not None:
                sess.pending_choice = json.dumps(choices, ensure_ascii=False) if choices else None

    def end_session(self, user_id: int) -> None:
        with self.Session.begin() as s:
            sess = s.get(UserSession, user_id)
            if sess is not None:
                s.delete(sess)

    # ---------- отправки
    def mark_submitted(self, house_id: int, period: str, user_id: int | None,
                       file_name: str | None, source: str = "bot", kind: str = RESIDENTIAL) -> None:
        """Отмечает удачную отправку. Если в старой БД так и остался UNIQUE(file_name),
        добавляем к имени «#2», «#3»… — запись всё равно сохранится."""
        name = file_name
        for attempt in range(2, 100):
            try:
                with self.Session.begin() as s:
                    s.add(Submission(house_id=house_id, period=period, submitted_by=user_id,
                                     file_name=name, source=source, kind=kind))
                return
            except IntegrityError:
                if file_name is None:
                    raise
                name = f"{file_name}#{attempt}"
        raise RuntimeError("не удалось записать отправку: слишком много одинаковых имён")

    def mark_failed(self, house_id: int, period: str, user_id: int | None, file_name: str | None,
                    error: str, kind: str = RESIDENTIAL) -> None:
        """Неудачная отправка — только для истории в админке; отправленным месяц не считается."""
        try:
            with self.Session.begin() as s:
                s.add(Submission(house_id=house_id, period=period, submitted_by=user_id, kind=kind,
                                 file_name=None, source="bot", status="failed",
                                 error=f"{file_name}: {error}"[:2000]))
        except Exception:  # noqa: BLE001 — журнал не должен ломать бота
            logger.exception("Не удалось записать неудачную отправку")

    def list_submissions(self, limit: int = 200) -> list[Submission]:
        with self.Session() as s:
            return list(s.scalars(select(Submission).order_by(Submission.submitted_at.desc(),
                                                              Submission.id.desc()).limit(limit)))

    def is_submitted(self, house_id: int, period: str, kind: str = RESIDENTIAL) -> bool:
        """Отправлялась ли уже ведомость за этот месяц (неудачные попытки не считаются)."""
        with self.Session() as s:
            return s.scalar(select(Submission.id).where(Submission.house_id == house_id,
                                                        Submission.status != "failed",
                                                        Submission.kind == kind,
                                                        Submission.period == period).limit(1)) is not None

    def has_unsent(self, house_id: int, period: str, kind: str) -> bool:
        """Есть ли показания, введённые или изменённые после последней удачной отправки
        (или отправок ещё не было)."""
        with self.Session() as s:
            last_change = s.scalar(
                select(func.max(Reading.updated_at)).join(Apartment, Reading.apartment_id == Apartment.id)
                .where(Apartment.house_id == house_id, Apartment.kind == kind, Reading.period == period))
            if last_change is None:
                return False
            last_sent = s.scalar(select(func.max(Submission.submitted_at)).where(
                Submission.house_id == house_id, Submission.kind == kind, Submission.period == period,
                Submission.status != "failed"))
            return last_sent is None or last_change > last_sent

    # ---------- псевдонимы
    def _meta_get(self, s, key: str) -> str | None:
        m = s.get(Meta, key)
        return m.value if m else None

    def _meta_set(self, s, key: str, value: str) -> None:
        m = s.get(Meta, key)
        if m is None:
            s.add(Meta(key=key, value=value))
        else:
            m.value = value

    def seed_aliases(self, data: dict[str, dict[str, str]]) -> int:
        """Один раз переносит стартовые псевдонимы (из houses.json) в БД. Дальше всё правится в админке.
        Повторно не срабатывает (отметка aliases_seeded в meta), так что рабочую БД не трогает."""
        with self.Session.begin() as s:
            if self._meta_get(s, "aliases_seeded"):
                return 0
            n = 0
            for scope, mapping in data.items():
                house_id = None if scope == "*" else int(scope)
                if house_id is not None and house_id not in HOUSES:
                    continue
                for alias, target in mapping.items():
                    s.add(Alias(house_id=house_id, alias=alias.strip().lower(), target=target.strip(),
                                updated_by="seed:houses.json"))
                    n += 1
            self._meta_set(s, "aliases_seeded", utcnow().isoformat())
            return n

    def get_aliases(self, house_id: int) -> dict[str, tuple[str, list[str]]]:
        """Псевдонимы дома: {псевдоним: (фраза, [ключи привязанных абонентов этого дома])}.

        Общие + свои для дома (свои главнее). Привязка по id переживает переименования,
        а фразу CommerceMatcher дополнительно ищет по текущим и прошлым названиям."""
        with self.Session() as s:
            rows = list(s.scalars(select(Alias).where(or_(Alias.house_id.is_(None), Alias.house_id == house_id))
                                  .order_by(case((Alias.house_id.is_(None), 0), else_=1), Alias.id)))
            links: dict[int, list[str]] = {}
            if rows:
                q = (select(AliasTarget.alias_id, Apartment.number)
                     .join(Apartment, Apartment.id == AliasTarget.apartment_id)
                     .where(AliasTarget.alias_id.in_([a.id for a in rows]), Apartment.house_id == house_id,
                            Apartment.kind == COMMERCE))
                for aid, number in s.execute(q):
                    links.setdefault(aid, []).append(number)
            return {a.alias: (a.target, links.get(a.id, [])) for a in rows}

    def resolve_alias_target(self, house_id: int | None, target: str) -> list[Apartment]:
        """Абоненты, у которых фраза есть в текущем или прошлом названии (house_id None — во всех домах)."""
        from ledgers import CommerceMatcher, norm_words
        out = []
        for h in ([house_id] if house_id else list(HOUSES)):
            m = CommerceMatcher(self.subscribers(h), {}, extra_names=self.subscriber_names(h))
            keys = m._containing(norm_words(target))
            items = self.apartment_map(h, kind=COMMERCE)
            out += [items[k] for k in keys if k in items]
        return out

    def _bind_alias(self, s, alias: Alias) -> int:
        """Перепривязывает псевдоним к найденным абонентам. Возвращает, к скольким привязали."""
        s.execute(delete(AliasTarget).where(AliasTarget.alias_id == alias.id))
        apts = self.resolve_alias_target(alias.house_id, alias.target)
        for a in apts:
            s.add(AliasTarget(alias_id=alias.id, apartment_id=a.id))
        return len(apts)

    def bind_aliases(self) -> int:
        """Привязывает к абонентам по id старые псевдонимы, у которых была только текстовая фраза."""
        n = 0
        with self.Session.begin() as s:
            bound = set(s.scalars(select(AliasTarget.alias_id).distinct()))
            for a in s.scalars(select(Alias)):
                if a.id not in bound and self._bind_alias(s, a):
                    n += 1
        return n

    def alias_targets(self) -> dict[int, list[Apartment]]:
        """{alias_id: [привязанные абоненты]} — для страницы псевдонимов в админке."""
        with self.Session() as s:
            out: dict[int, list[Apartment]] = {}
            q = select(AliasTarget.alias_id, Apartment).join(Apartment, Apartment.id == AliasTarget.apartment_id)
            for aid, apt in s.execute(q):
                out.setdefault(aid, []).append(apt)
            return out

    def list_aliases(self) -> list[Alias]:
        with self.Session() as s:
            return list(s.scalars(select(Alias).order_by(Alias.house_id, Alias.alias)))

    def save_alias(self, alias_id: int | None, house_id: int | None, alias: str, target: str, actor: str) -> Alias:
        """Создать (alias_id=None) или изменить псевдоним и сразу привязать его к абонентам."""
        alias, target = " ".join(alias.lower().split()), " ".join(target.split())
        if not alias or not target:
            raise ValueError("псевдоним и название не должны быть пустыми")
        with self.Session.begin() as s:
            row = s.get(Alias, alias_id) if alias_id else None
            dup = s.scalar(select(Alias).where(Alias.alias == alias, Alias.house_id.is_(None) if house_id is None
                                               else Alias.house_id == house_id))
            if dup is not None and (row is None or dup.id != row.id):
                raise ValueError(f"псевдоним «{alias}» уже есть")
            if row is None:
                row = Alias(house_id=house_id, alias=alias, target=target, updated_by=actor)
                s.add(row)
            else:
                row.house_id, row.alias, row.target, row.updated_by = house_id, alias, target, actor
            s.flush()
            self._bind_alias(s, row)
            return row

    def delete_alias(self, alias_id: int) -> None:
        with self.Session.begin() as s:
            s.execute(delete(AliasTarget).where(AliasTarget.alias_id == alias_id))
            s.execute(delete(Alias).where(Alias.id == alias_id))

    # ---------- пользователи и роли
    def seed_roles(self, admin_ids, user_ids) -> None:
        """Роли из .env (ADMIN_USER_IDS, ALLOWED_USER_IDS). Трогаем только тех, кому роль ещё
        не меняли в админке (updated_by пуст) — после ручной правки главнее админка."""
        wanted = {uid: ROLE_USER for uid in user_ids}
        wanted.update({uid: ROLE_ADMIN for uid in admin_ids})
        with self.Session.begin() as s:
            for uid, role in wanted.items():
                u = s.get(BotUser, uid)
                if u is None:
                    s.add(BotUser(user_id=uid, role=role, messages=0, first_seen=None, last_seen=None))
                elif u.updated_by is None and u.role != role:
                    u.role = role

    def touch_user(self, user_id: int, username: str | None = None, first_name: str | None = None,
                   last_name: str | None = None) -> str:
        """Отмечает, что человек написал боту, и возвращает его роль."""
        now = utcnow()
        with self.Session.begin() as s:
            u = s.get(BotUser, user_id)
            if u is None:
                u = BotUser(user_id=user_id, role=ROLE_NONE, first_seen=now, messages=0)
                s.add(u)
            u.username, u.first_name, u.last_name = username, first_name, last_name
            u.first_seen = u.first_seen or now
            u.last_seen = now
            u.messages = (u.messages or 0) + 1
            return u.role

    def get_user(self, user_id: int) -> BotUser | None:
        with self.Session() as s:
            return s.get(BotUser, user_id)

    def user_role(self, user_id: int) -> str:
        u = self.get_user(user_id)
        return u.role if u else ROLE_NONE

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and self.user_role(user_id) == ROLE_ADMIN

    def list_users(self) -> list[BotUser]:
        with self.Session() as s:
            return list(s.scalars(select(BotUser).order_by(
                case((BotUser.role == ROLE_ADMIN, 0), (BotUser.role == ROLE_USER, 1), else_=2),
                BotUser.last_seen.desc())))

    def count_admins(self) -> int:
        with self.Session() as s:
            return s.scalar(select(func.count()).select_from(BotUser).where(BotUser.role == ROLE_ADMIN)) or 0

    def count_active_users(self) -> int:
        with self.Session() as s:
            return s.scalar(select(func.count()).select_from(BotUser)
                            .where(BotUser.role.in_((ROLE_ADMIN, ROLE_USER)))) or 0

    def set_role(self, user_id: int, role: str, actor: str) -> None:
        """Сменить роль из админки. Последнего администратора понизить нельзя (LastAdminError)."""
        if role not in ROLES:
            raise ValueError("неизвестная роль")
        with self.Session.begin() as s:
            u = s.get(BotUser, user_id)
            if u is None:
                u = BotUser(user_id=user_id, role=ROLE_NONE, messages=0)
                s.add(u)
            if u.role == ROLE_ADMIN and role != ROLE_ADMIN:
                admins = s.scalar(select(func.count()).select_from(BotUser).where(BotUser.role == ROLE_ADMIN))
                if admins <= 1:
                    raise LastAdminError("должен остаться хотя бы один администратор")
            u.role, u.updated_by = role, actor

    # ---------- одноразовые ссылки входа
    def create_login_token(self, user_id: int, ttl_seconds: int = 300) -> str:
        """Новый одноразовый токен входа в админку. В БД кладём только его SHA-256,
        заодно подчищаем давно истёкшие."""
        import hashlib
        import secrets
        from datetime import timedelta
        raw = secrets.token_urlsafe(32)
        with self.Session.begin() as s:
            s.execute(delete(LoginToken).where(LoginToken.expires_at < utcnow() - timedelta(days=1)))
            s.add(LoginToken(token_hash=hashlib.sha256(raw.encode()).hexdigest(), user_id=user_id,
                             expires_at=utcnow() + timedelta(seconds=ttl_seconds)))
        return raw

    def consume_login_token(self, raw: str) -> int | None:
        """Гасит токен и возвращает user_id; None — токена нет, он истёк или уже использован.

        Всё делается одним UPDATE … WHERE used_at IS NULL, так что из двух одновременных
        переходов по одной ссылке сработает только один."""
        import hashlib
        from sqlalchemy import update
        if not raw:
            return None
        h = hashlib.sha256(raw.encode()).hexdigest()
        now = utcnow()
        with self.Session.begin() as s:
            res = s.execute(update(LoginToken)
                            .where(LoginToken.token_hash == h, LoginToken.used_at.is_(None),
                                   LoginToken.expires_at > now)
                            .values(used_at=now)
                            .execution_options(synchronize_session=False))
            if res.rowcount != 1:
                return None
            return s.scalar(select(LoginToken.user_id).where(LoginToken.token_hash == h))

    # ---------- тикеты CRM
    def get_crm_ticket(self, house_id: int, period: str, kind: str) -> CrmTicket | None:
        with self.Session() as s:
            return s.scalar(select(CrmTicket).where(CrmTicket.house_id == house_id, CrmTicket.period == period,
                                                    CrmTicket.kind == kind))

    def save_crm_ticket(self, house_id: int, period: str, kind: str, ticket_number: int,
                        ticket_id: int | None, source: str) -> None:
        """Запоминает последний тикет ведомости (новый номер заменяет старый)."""
        with self.Session.begin() as s:
            row = s.scalar(select(CrmTicket).where(CrmTicket.house_id == house_id, CrmTicket.period == period,
                                                   CrmTicket.kind == kind))
            if row is None:
                s.add(CrmTicket(house_id=house_id, period=period, kind=kind, ticket_number=ticket_number,
                                ticket_id=ticket_id, source=source))
            else:
                row.ticket_number, row.ticket_id, row.source = ticket_number, ticket_id, source
                row.updated_at = utcnow()

    # ---------- «Отправить в чат» из админки
    def periods_with_readings(self, house_id: int, kind: str) -> dict[str, int]:
        """{месяц: сколько показаний} — месяцы ведомости, за которые в базе есть показания."""
        with self.Session() as s:
            q = (select(Reading.period, func.count()).join(Apartment, Reading.apartment_id == Apartment.id)
                 .where(Apartment.house_id == house_id, Apartment.kind == kind).group_by(Reading.period))
            return {p: n for p, n in s.execute(q)}

    def create_resend(self, user_id: int, user_name: str | None, house_id: int, period: str, kind: str,
                      mode: str, reply_to_ticket: int | None = None) -> str:
        """Шаг подтверждения: заводит строку pending и возвращает одноразовый токен (в БД — только хеш)."""
        import hashlib
        import secrets
        from datetime import timedelta
        raw = secrets.token_urlsafe(32)
        with self.Session.begin() as s:
            s.execute(delete(ChatResend).where(ChatResend.status == "pending",
                                               ChatResend.created_at < utcnow() - timedelta(days=1)))
            s.add(ChatResend(token_hash=hashlib.sha256(raw.encode()).hexdigest(), requested_by=user_id,
                             requested_name=user_name, house_id=house_id, period=period, kind=kind,
                             mode=mode, reply_to_ticket=reply_to_ticket, status="pending"))
        return raw

    def claim_resend(self, raw: str, user_id: int, ttl_seconds: int = 900) -> ChatResend | None:
        """Подтверждение: pending → sending одним UPDATE. Второй раз (двойной клик, повтор POST,
        чужой или устаревший токен) вернёт None — значит, отправлять нельзя."""
        import hashlib
        from datetime import timedelta
        from sqlalchemy import update
        if not raw:
            return None
        h = hashlib.sha256(raw.encode()).hexdigest()
        with self.Session.begin() as s:
            res = s.execute(update(ChatResend)
                            .where(ChatResend.token_hash == h, ChatResend.status == "pending",
                                   ChatResend.requested_by == user_id,
                                   ChatResend.created_at > utcnow() - timedelta(seconds=ttl_seconds))
                            .values(status="sending", claimed_at=utcnow())
                            .execution_options(synchronize_session=False))
            if res.rowcount != 1:
                return None
            return s.scalar(select(ChatResend).where(ChatResend.token_hash == h))

    def peek_resend(self, raw: str) -> ChatResend | None:
        import hashlib
        with self.Session() as s:
            return s.scalar(select(ChatResend).where(
                ChatResend.token_hash == hashlib.sha256((raw or "").encode()).hexdigest()))

    def finish_resend(self, resend_id: int, **values) -> None:
        """Итог отправки: status, chat_id, message_id, file_name, error, crm_*."""
        with self.Session.begin() as s:
            row = s.get(ChatResend, resend_id)
            if row is not None:
                for k, v in values.items():
                    setattr(row, k, v)

    def expire_stale_resends(self, minutes: int = 10) -> int:
        """«Зависшие» отправки (status=sending дольше minutes — например, админку перезапустили
        посреди отправки) помечаем unknown: файл мог дойти, а мог и нет."""
        from datetime import timedelta
        from sqlalchemy import update
        with self.Session.begin() as s:
            res = s.execute(update(ChatResend)
                            .where(ChatResend.status == "sending",
                                   ChatResend.claimed_at < utcnow() - timedelta(minutes=minutes))
                            .values(status="unknown",
                                    error="отправка прервалась — файл мог дойти, проверьте чат")
                            .execution_options(synchronize_session=False))
            return res.rowcount or 0

    def list_resends(self, limit: int = 100) -> list[ChatResend]:
        """Журнал для админки — без неподтверждённых (pending)."""
        with self.Session() as s:
            return list(s.scalars(select(ChatResend).where(ChatResend.status != "pending")
                                  .order_by(ChatResend.created_at.desc(), ChatResend.id.desc()).limit(limit)))

    # ---------- Excel
    def build_excel(self, house_id: int, period: str, kind: str = RESIDENTIAL) -> bytes:
        """Ведомость за месяц в виде xlsx (байты): показания за этот и прошлый месяц из БД
        и названия/ФИО, которые действовали в этом месяце."""
        from excel_report import SheetRow, build_workbook
        year, month = parse_period(period)
        prev = self.get_readings(house_id, previous_period(period), kind)
        cur = self.get_readings(house_id, period, kind)
        with self.Session() as s:
            items = list(s.scalars(self._items_query(house_id, kind)))
            hist = self._history(s, [a.id for a in items])
        rows = []
        for a in items:
            h = hist.get(a.id, [])
            name = name_at(a.title, h, period)
            if kind == RESIDENTIAL and not h and a.origin == "template":
                name = None  # ФИО не меняли — оставляем ячейку шаблона как есть
            if kind == COMMERCE and a.origin == "template" and name == a.template_name:
                name = None  # название как в шаблоне — ячейку тоже не трогаем
            rows.append(SheetRow(key=a.number, row=a.row_order or None, prev=prev.get(a.number),
                                 cur=cur.get(a.number), name=name, label=a.number))
        return build_workbook(self.template_path(house_id, kind), year, month, ledger=kind, rows=rows)
