import asyncio
import io
import logging
import signal
import sys
from datetime import datetime

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
                      Update, WebAppInfo)
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from apartments import apartment_label, format_suggestions, input_example, plural_ru, suggest_apartments
from config import ALIASES_FILE, HOUSES, ConfigError, Settings, load_settings
from db import ROLE_ADMIN, ROLE_USER, Database, current_period, parse_period, previous_period
from excel_report import MONTHS_RU
from ledgers import COMMERCE, LEDGERS, RESIDENTIAL, STOP_WORDS, CommerceMatcher, load_aliases
from parsing import fmt_value, parse_readings, parse_value

# Настройка логирования для Bothost
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HOUSE_NAMES = {house_id: info["name"] for house_id, info in HOUSES.items()}
HOUSE_HASHTAG = {house_id: info["hashtag"] for house_id, info in HOUSES.items()}
HOUSE_BY_NAME = {name: house_id for house_id, name in HOUSE_NAMES.items()}

BTN_NEW = "Начать новую сессию"
BTN_FILE = "Файл сейчас"
BTN_DONE = "Готово"
BTN_NONSTD = "Нестандартные квартиры"
BTN_BACK = "Назад"
BTN_ADMIN = "🛠 Админ-панель"
BTN_RESIDENTIAL = LEDGERS[RESIDENTIAL].title   # «Жилые»
BTN_COMMERCE = LEDGERS[COMMERCE].title         # «Коммерция»
BTN_KIND = {BTN_RESIDENTIAL: RESIDENTIAL, BTN_COMMERCE: COMMERCE}
MARK_DONE = "✅ "

# Постоянная кнопка после завершения
BASE_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(BTN_NEW)]], resize_keyboard=True)
ADMIN_BASE_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(BTN_NEW)], [KeyboardButton(BTN_ADMIN)]],
                                          resize_keyboard=True)
HOUSE_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(HOUSE_NAMES[1]), KeyboardButton(HOUSE_NAMES[2])]], resize_keyboard=True
)
KIND_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_RESIDENTIAL), KeyboardButton(BTN_COMMERCE)], [KeyboardButton(BTN_BACK)]],
    resize_keyboard=True,
)
SESSION_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_FILE), KeyboardButton(BTN_DONE)]], resize_keyboard=True
)
BACK_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(BTN_BACK)]], resize_keyboard=True)


# ======================================================================== помощники
def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_matcher(context: ContextTypes.DEFAULT_TYPE, house_id: int) -> CommerceMatcher:
    """Псевдонимы читаются из БД на каждое сообщение — правки в админке действуют сразу.
    Если БД недоступна — запасной вариант из aliases.json."""
    db = get_db(context)
    try:
        merged = db.get_aliases(house_id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось прочитать псевдонимы из БД — использую aliases.json")
        aliases = load_aliases(ALIASES_FILE)
        merged = {**aliases.get("*", {}), **aliases.get(str(house_id), {})}
    return CommerceMatcher(db.subscribers(house_id), merged, extra_names=db.subscriber_names(house_id))


def base_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> ReplyKeyboardMarkup:
    """Кнопка «Админ-панель» — только администраторам."""
    return ADMIN_BASE_KEYBOARD if get_db(context).is_admin(user_id) else BASE_KEYBOARD


def count_word(n: int, kind: str) -> str:
    return f"{n} {plural_ru(n, *LEDGERS[kind].noun)}"


def saved_summary(new: int, updated: int, kind: str = RESIDENTIAL) -> str | None:
    """«✅ Сохранено 3 квартиры, обновлено 1 квартира» / «… 2 абонента»."""
    parts = []
    if new:
        parts.append(f"сохранено {count_word(new, kind)}")
    if updated:
        parts.append(f"обновлено {count_word(updated, kind)}")
    if not parts:
        return None
    text = ", ".join(parts)
    return "✅ " + text[0].upper() + text[1:]


def period_label(period: str) -> str:
    year, month = parse_period(period)
    return f"{month:02d}.{year}"


def period_title(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS_RU[month - 1]} {year}"


def month_tag(period: str) -> str:
    year, month = parse_period(period)
    return f"{MONTHS_RU[month - 1]}_{year}"


def ledger_suffix(kind: str) -> str:
    return "" if kind == RESIDENTIAL else f" — {LEDGERS[kind].title}"


def archive_file_name(house_id: int, when: datetime | None = None, kind: str = RESIDENTIAL) -> str:
    when = when or datetime.now()
    return (f"{when.strftime('%Y%m%d_%H%M%S')}_house{house_id}_{LEDGERS[kind].file_tag}"
            f"{HOUSE_NAMES[house_id].replace(' ', '_')}.xlsx")


def build_caption(tag: str, house_id: int, period: str, kind: str = RESIDENTIAL) -> str:
    tags = f"#{month_tag(period)} {HOUSE_HASHTAG[house_id]}"
    if LEDGERS[kind].hashtag:
        tags += f" {LEDGERS[kind].hashtag}"
    return (f"{tag} Привет! Показания за текущий {period_label(period)} — {HOUSE_NAMES[house_id]}"
            f"{ledger_suffix(kind)}\n{tags}")


# ---------------------------------------------------------------- клавиатуры
def session_keyboard(db: Database, house_id: int, kind: str = RESIDENTIAL, period: str | None = None):
    if kind == COMMERCE:
        return commerce_keyboard(db, house_id, period or current_period())
    if not db.nonstandard_apartments(house_id):
        return SESSION_KEYBOARD
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_FILE), KeyboardButton(BTN_DONE)], [KeyboardButton(BTN_NONSTD)]],
        resize_keyboard=True,
    )


def commerce_keyboard(db: Database, house_id: int, period: str) -> ReplyKeyboardMarkup:
    """По кнопке на абонента (✅ — уже введён за период) + «Файл сейчас», «Готово», «Назад»."""
    entered = db.get_readings(house_id, period, COMMERCE)
    rows = [[KeyboardButton((MARK_DONE if key in entered else "") + title)]
            for key, title in db.subscribers(house_id)]
    rows.append([KeyboardButton(BTN_FILE), KeyboardButton(BTN_DONE)])
    rows.append([KeyboardButton(BTN_BACK)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def subscriber_by_label(db: Database, house_id: int, text: str) -> str | None:
    label = text[len(MARK_DONE):] if text.startswith(MARK_DONE) else text
    for key, title in db.subscribers(house_id):
        if title == label:
            return key
    return None


def nonstandard_keyboard(numbers: list[str]) -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(apartment_label(n)) for n in numbers]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([KeyboardButton(BTN_BACK)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def nonstandard_by_label(db: Database, house_id: int) -> dict[str, str]:
    return {apartment_label(n): n for n in db.nonstandard_apartments(house_id)}


def nonstandard_hint(numbers: list[str]) -> str | None:
    if not numbers:
        return None
    names = ", ".join(apartment_label(n) for n in numbers)
    return (f"🏠 Нестандартные квартиры: {names}.\n"
            f"Вводи так: {input_example(numbers[0])} — или через кнопку «{BTN_NONSTD}».")


def commerce_hint(context, house_id: int) -> str | None:
    """Пример ввода коммерции в жилой сессии — по отличительному слову одного из абонентов."""
    matcher = get_matcher(context, house_id)
    if not matcher:
        return None
    example = None
    for key in matcher.order:
        for w in matcher.words[key]:
            if w not in STOP_WORDS and not w.isdigit() and matcher.lookup([w]) == [key]:
                example = w
                break
        if example:
            break
    text = "🏪 Коммерцию можно писать прямо здесь, вперемешку с квартирами"
    if example:
        text += f": «12 345 {example} 67»"
    return text + ". Если у абонента несколько подъездов — укажи номер: «лекс 1 67»."


# ======================================================================== доступ / старт
async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Доступ по роли из БД (admin / user). Каждое обращение записывается в bot_users,
    поэтому новые пользователи появляются в админке как «ожидают» — одним нажатием им дают роль."""
    user = update.effective_user
    if user is None:
        return False
    db = get_db(context)
    role = db.touch_user(user.id, getattr(user, "username", None), getattr(user, "first_name", None),
                         getattr(user, "last_name", None))
    if role in (ROLE_ADMIN, ROLE_USER):
        return True
    logger.warning("Доступ запрещён: user_id=%s (%s)", user.id, getattr(user, "username", None))
    if update.message:
        await update.message.reply_text(
            f"⛔ У вас нет доступа к этому боту.\nВаш Telegram ID: {user.id} — передайте его администратору.")
    return False


def house_keyboard(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> ReplyKeyboardMarkup:
    if not get_db(context).is_admin(user_id):
        return HOUSE_KEYBOARD
    return ReplyKeyboardMarkup([[KeyboardButton(HOUSE_NAMES[1]), KeyboardButton(HOUSE_NAMES[2])],
                                [KeyboardButton(BTN_ADMIN)]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - выбор дома"""
    if not await check_access(update, context):
        return
    await show_houses(update, context)


async def show_houses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("kind_house", None)
    await update.message.reply_text("Выбери дом:", reply_markup=house_keyboard(context, update.effective_user.id))


async def choose_house(update: Update, context: ContextTypes.DEFAULT_TYPE, house_id: int):
    """После выбора дома: «Жилые» / «Коммерция» (если у дома есть коммерческий шаблон)."""
    db = get_db(context)
    if db.has_ledger(house_id, COMMERCE) and db.subscribers(house_id):
        context.user_data["kind_house"] = house_id
        await update.message.reply_text(f"Дом: {HOUSE_NAMES[house_id]}\nЧто вводим?",
                                        reply_markup=KIND_KEYBOARD)
        return
    await start_ledger_session(update, context, house_id, RESIDENTIAL)


async def start_ledger_session(update: Update, context: ContextTypes.DEFAULT_TYPE, house_id: int, kind: str):
    db = get_db(context)
    context.user_data.pop("kind_house", None)
    if not db.has_ledger(house_id, kind):
        await update.message.reply_text("Нет шаблона для этого дома")
        return

    period = current_period()
    db.start_session(update.effective_user.id, house_id, period, kind)

    total = len(db.apartment_map(house_id, kind=kind))
    entered = db.count_readings(house_id, period, kind)
    prev_count = db.count_readings(house_id, previous_period(period), kind)

    if kind == COMMERCE:
        lines = [f"Дом: {HOUSE_NAMES[house_id]} — Коммерция", "Сессия начата — нажми на абонента",
                 f"или напиши «название показание», например «{db.subscribers(house_id)[0][1]} 1234»"]
    else:
        lines = [f"Дом: {HOUSE_NAMES[house_id]}", "Сессия начата — вводи показания", "Тишина = всё ок"]
    info = [f"Период: {period_label(period)}. Уже внесено: {entered} из {total}."]
    if prev_count == 0:
        info.append(f"⚠️ Нет показаний за прошлый месяц ({period_label(previous_period(period))}) — "
                    f"колонка «предыдущие» будет пустой.")
    if db.is_submitted(house_id, period, kind):
        info.append("ℹ️ Ведомость за этот месяц уже отправлялась; «Готово» отправит обновлённую.")
    if kind == RESIDENTIAL:
        for hint in (nonstandard_hint(db.nonstandard_apartments(house_id)),
                     commerce_hint(context, house_id) if db.has_ledger(house_id, COMMERCE) else None):
            if hint:
                info.append(hint)
    await update.message.reply_text("\n".join(lines + [""] + info),
                                    reply_markup=session_keyboard(db, house_id, kind, period))


# ======================================================================== файлы
async def send_current_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db(context)
    sess = db.get_session(update.effective_user.id)
    if not sess:
        await update.message.reply_text("Нет активной сессии")
        return
    kinds = [sess.kind]
    if sess.kind == RESIDENTIAL and db.has_ledger(sess.house_id, COMMERCE) \
            and db.count_readings(sess.house_id, sess.period, COMMERCE):
        kinds.append(COMMERCE)  # коммерцию, введённую в жилой сессии, тоже показываем
    for kind in kinds:
        try:
            data = db.build_excel(sess.house_id, sess.period, kind)
        except Exception as e:
            logger.exception("Ошибка формирования файла")
            await update.message.reply_text(f"❌ Ошибка формирования файла: {e}")
            return
        tag = "" if kind == RESIDENTIAL else f"{kind}_"
        with io.BytesIO(data) as buf:
            await update.message.reply_document(
                buf,
                filename=f"session_house{sess.house_id}_{tag}{sess.period}.xlsx",
                caption="Текущий файл" + ("" if kind == RESIDENTIAL else " (Коммерция)"),
            )


async def _send_ledger(context, settings, db, house_id, period, kind, user_id) -> tuple[bool, str | None]:
    """Отправляет ведомость в группу. (отправлено?, предупреждение о записи в БД)."""
    file_name = archive_file_name(house_id, kind=kind)
    try:
        data = db.build_excel(house_id, period, kind)
        with io.BytesIO(data) as buf:
            await context.bot.send_document(
                settings.group_chat_id, buf, filename=file_name,
                caption=build_caption(settings.target_bot_tag, house_id, period, kind),
            )
    except Exception as e:
        db.mark_failed(house_id, period, user_id, file_name, str(e), kind=kind)
        raise
    try:
        db.mark_submitted(house_id, period, user_id, file_name, kind=kind)
    except Exception as e:
        logger.exception("Файл отправлен, но не удалось записать факт отправки в БД")
        return True, str(e)
    return True, None


async def finish_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db(context)
    settings = get_settings(context)
    user_id = update.effective_user.id
    sess = db.get_session(user_id)
    if not sess:
        await update.message.reply_text("Нет активной сессии")
        return
    choices = db.get_pending_choices(user_id)
    if choices:
        await update.message.reply_text(
            f"Сначала уточни «{choices[0]['raw']} {fmt_value(choices[0]['value'])}» "
            f"(или нажми «{BTN_BACK}», чтобы пропустить)")
        return

    house_id, period, kind = sess.house_id, sess.period, sess.kind
    try:
        _, db_error = await _send_ledger(context, settings, db, house_id, period, kind, user_id)
    except Exception as e:
        # Период НЕ помечается отправленным, сессия сохраняется — можно нажать «Готово» ещё раз.
        logger.exception("Ошибка отправки")
        await update.message.reply_text(
            f"❌ Ошибка отправки: {e}\nСессия сохранена — нажми «{BTN_DONE}» ещё раз.",
            reply_markup=session_keyboard(db, house_id, kind, period),
        )
        return

    messages = []
    if db_error:
        messages.append("⚠️ Файл улетел в группу, но отметку об отправке в базе сохранить не удалось "
                        f"({db_error}). Показания сохранены, повторно отправлять не нужно.")
    else:
        messages.append("✅ Файл улетел в группу.")

    # Коммерция, введённая в жилой сессии и ещё не отправленная, — отдельным файлом.
    if kind == RESIDENTIAL and db.has_ledger(house_id, COMMERCE) and db.has_unsent(house_id, period, COMMERCE):
        try:
            _, c_error = await _send_ledger(context, settings, db, house_id, period, COMMERCE, user_id)
            messages.append("✅ Коммерция тоже отправлена." if not c_error else
                            f"⚠️ Коммерция отправлена, но отметка в базе не сохранилась ({c_error}).")
        except Exception as e:
            logger.exception("Ошибка отправки коммерции")
            messages.append(f"❌ Коммерцию отправить не удалось: {e}\n"
                            f"Отправь её отдельно: дом → «{BTN_COMMERCE}» → «{BTN_DONE}».")

    db.end_session(user_id)
    messages.append("Сессия завершена.")
    text = messages[0] + " " + messages[-1] if len(messages) == 2 else "\n".join(messages)
    await update.message.reply_text(text, reply_markup=base_keyboard(context, user_id))


# ======================================================================== ввод показаний
def save_entries(db: Database, sess, entries: list[tuple[str, float]], user_id: int,
                 kind: str = RESIDENTIAL) -> tuple[int, int, list[str]]:
    """Сохраняет пары (ключ, значение) в ведомость kind.
    Возвращает (новых, обновлённых, сообщения об ошибках/предупреждениях)."""
    messages, new, updated = [], 0, 0
    existing = None
    titles = dict(db.subscribers(sess.house_id)) if kind == COMMERCE else {}
    with db.Session.begin() as s:
        for key, value in entries:
            res = db.save_reading(sess.house_id, key, sess.period, value, user_id, s=s, kind=kind)
            name = titles.get(key, key) if kind == COMMERCE else apartment_label(key)
            if res is None:
                if kind == COMMERCE:
                    messages.append(f"❌ Абонент {name} не найден")
                    continue
                if existing is None:
                    existing = db.apartment_numbers(sess.house_id)
                hint = format_suggestions(suggest_apartments(key, existing))
                messages.append(f"❌ Квартира {key} не найдена" + (f". {hint}" if hint else ""))
                continue
            if res.updated:
                updated += 1
            else:
                new += 1
            if res.decreased:
                messages.append(
                    f"⚠️ {name}: новое показание {fmt_value(res.value)} меньше предыдущего "
                    f"{fmt_value(res.previous)} (сохранено, проверь)")
    return new, updated, messages


def choice_keyboard(db: Database, house_id: int, choice: dict) -> ReplyKeyboardMarkup:
    titles = dict(db.subscribers(house_id))
    rows = [[KeyboardButton(titles.get(k, k))] for k in choice["candidates"]]
    rows.append([KeyboardButton(BTN_BACK)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def ask_next_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, sess) -> bool:
    db = get_db(context)
    choices = db.get_pending_choices(update.effective_user.id)
    if not choices:
        return False
    c = choices[0]
    await update.message.reply_text(
        f"❓ «{c['raw']} {fmt_value(c['value'])}» — какой именно? Выбери кнопкой или напиши номер подъезда.",
        reply_markup=choice_keyboard(db, sess.house_id, c))
    return True


async def enter_readings(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, sess=None):
    db = get_db(context)
    user_id = update.effective_user.id
    sess = sess or db.get_session(user_id)
    if not sess:
        await update.message.reply_text("Сначала начни сессию: /start",
                                        reply_markup=base_keyboard(context, user_id))
        return

    matcher = get_matcher(context, sess.house_id) if db.has_ledger(sess.house_id, COMMERCE) else None
    parsed = parse_readings(text, matcher)
    messages = list(parsed.errors)
    apartments = parsed.entries
    if sess.kind == COMMERCE and apartments:
        messages.append(f"❌ Квартиры вводятся в сессии «{BTN_RESIDENTIAL}»: "
                        + ", ".join(a for a, _ in apartments))
        apartments = []
    try:
        new, updated, msgs = save_entries(db, sess, apartments, user_id, RESIDENTIAL)
        c_new, c_updated, c_msgs = save_entries(db, sess, [(k, v) for k, v, _ in parsed.commerce],
                                                user_id, COMMERCE)
    except Exception as e:
        logger.exception("Ошибка сохранения показаний")
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    out = []
    summary = saved_summary(new, updated, RESIDENTIAL)
    if summary:
        out.append(summary)
    if parsed.commerce and sess.kind == RESIDENTIAL:
        titles = dict(db.subscribers(sess.house_id))
        for key, value, _ in parsed.commerce:
            if key in titles:
                out.append(f"✅ Коммерция: {titles[key]} — {fmt_value(value)}")
    elif parsed.commerce:
        summary = saved_summary(c_new, c_updated, COMMERCE)
        if summary:
            out.append(summary)
    out += messages + msgs + c_msgs

    if parsed.ambiguous:
        db.set_pending_choices(user_id, db.get_pending_choices(user_id) + parsed.ambiguous)
    if out:
        kb = session_keyboard(db, sess.house_id, sess.kind, sess.period) if sess.kind == COMMERCE else None
        await update.message.reply_text("\n".join(out), reply_markup=kb)
    if parsed.ambiguous:
        await ask_next_choice(update, context, sess)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, sess, text: str) -> bool:
    """Ответ на «какой подъезд?». False — сообщение не об этом (очередь сброшена, обработать как обычно)."""
    db = get_db(context)
    user_id = update.effective_user.id
    choices = db.get_pending_choices(user_id)
    c = choices[0]
    if text == BTN_BACK:
        db.set_pending_choices(user_id, choices[1:])
        await update.message.reply_text(f"Пропустил «{c['raw']} {fmt_value(c['value'])}»",
                                        reply_markup=session_keyboard(db, sess.house_id, sess.kind, sess.period)
                                        if len(choices) == 1 else None)
        await ask_next_choice(update, context, sess)
        return True
    titles = dict(db.subscribers(sess.house_id))
    key = next((k for k in c["candidates"] if titles.get(k) == text), None)
    if key is None and len(text.split()) <= 2:
        matcher = get_matcher(context, sess.house_id)
        key, _ = matcher.entrance_token(c["candidates"], text.split(), 0)
    if key is None:
        db.set_pending_choices(user_id, [])
        await update.message.reply_text(
            "Пропустил неуточнённое: " + ", ".join(f"«{x['raw']} {fmt_value(x['value'])}»" for x in choices))
        return False
    _, _, msgs = save_entries(db, sess, [(key, c["value"])], user_id, COMMERCE)
    db.set_pending_choices(user_id, choices[1:])
    reply = [f"✅ Коммерция: {titles[key]} — {fmt_value(c['value'])}"] + msgs
    await update.message.reply_text("\n".join(reply),
                                    reply_markup=session_keyboard(db, sess.house_id, sess.kind, sess.period)
                                    if len(choices) == 1 else None)
    await ask_next_choice(update, context, sess)
    return True


async def show_nonstandard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, sess):
    db = get_db(context)
    numbers = db.nonstandard_apartments(sess.house_id)
    if not numbers:
        await update.message.reply_text("В этом доме нет нестандартных квартир",
                                        reply_markup=session_keyboard(db, sess.house_id))
        return
    await update.message.reply_text("Выбери квартиру:", reply_markup=nonstandard_keyboard(numbers))


async def choose_nonstandard(update: Update, context: ContextTypes.DEFAULT_TYPE, sess, number: str):
    get_db(context).set_pending_apartment(update.effective_user.id, number)
    await update.message.reply_text(f"{apartment_label(number)}: введи показание (только число)",
                                    reply_markup=BACK_KEYBOARD)


async def choose_subscriber(update: Update, context: ContextTypes.DEFAULT_TYPE, sess, key: str):
    db = get_db(context)
    db.set_pending_apartment(update.effective_user.id, key)
    title = dict(db.subscribers(sess.house_id)).get(key, key)
    prev_period = previous_period(sess.period)
    prev = db.get_readings(sess.house_id, prev_period, COMMERCE).get(key)
    cur = db.get_readings(sess.house_id, sess.period, COMMERCE).get(key)
    lines = [f"🏪 {title}", f"Прошлый месяц ({period_title(prev_period)}): {fmt_value(prev)}"]
    if cur is not None:
        lines.append(f"Уже введено за {period_title(sess.period)}: {fmt_value(cur)}")
    lines.append("Введи показание (только число)")
    await update.message.reply_text("\n".join(lines), reply_markup=BACK_KEYBOARD)


async def enter_pending_value(update: Update, context: ContextTypes.DEFAULT_TYPE, sess, text: str) -> bool:
    """Показание для квартиры/абонента, выбранных кнопкой. False — сообщение не про неё."""
    db = get_db(context)
    if len(text.split()) > 1:
        db.set_pending_apartment(update.effective_user.id, None)
        return False
    value = parse_value(text)
    number = sess.pending_apartment
    kind = sess.kind
    name = dict(db.subscribers(sess.house_id)).get(number, number) if kind == COMMERCE \
        else apartment_label(number)
    if value is None:
        await update.message.reply_text(
            f"❌ {name}: введи число (например 345 или 345,5) или нажми «{BTN_BACK}»")
        return True
    try:
        new, updated, messages = save_entries(db, sess, [(number, value)], update.effective_user.id, kind)
    except Exception as e:
        logger.exception("Ошибка сохранения показаний")
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return True
    db.set_pending_apartment(update.effective_user.id, None)
    if new or updated:
        verb = "обновлено" if updated else "сохранено"
        line = f"✅ {name}: {verb} {fmt_value(value)}"
        if kind == COMMERCE:
            prev = db.get_readings(sess.house_id, previous_period(sess.period), COMMERCE).get(number)
            if prev is not None:
                line += f" (расход {fmt_value(round(value - prev, 3))})"
        messages.insert(0, line)
    await update.message.reply_text("\n".join(messages),
                                    reply_markup=session_keyboard(db, sess.house_id, kind, sess.period))
    return True


# ======================================================================== админ-панель
def admin_login_markup(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> InlineKeyboardMarkup:
    """Кнопки входа: Mini App (если есть https-адрес) и одноразовая ссылка для обычного браузера."""
    settings = get_settings(context)
    base = settings.admin_public_url or "http://127.0.0.1:8080"
    token = get_db(context).create_login_token(user_id, ttl_seconds=300)
    rows = []
    if base.startswith("https://"):
        rows.append([InlineKeyboardButton("🛠 Открыть в Telegram", web_app=WebAppInfo(url=f"{base}/auth/webapp"))])
    rows.append([InlineKeyboardButton("🌐 Открыть в браузере (ссылка на 5 минут)", url=f"{base}/auth/tg?token={token}")])
    return InlineKeyboardMarkup(rows)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    await admin_entry(update, context)


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin или кнопка «🛠 Админ-панель» — только для администраторов."""
    user_id = update.effective_user.id
    if not get_db(context).is_admin(user_id):
        await update.message.reply_text("Нет доступа")
        return
    await update.message.reply_text(
        "🛠 Админ-панель. Ссылка для браузера одноразовая и действует 5 минут — никому её не пересылайте.",
        reply_markup=admin_login_markup(context, user_id))


# ======================================================================== маршрутизация
async def notify_other_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сессия (например, восстановленная после перезапуска) относится не к текущему месяцу —
    один раз за запуск бота сообщаем, за какой период продолжаем. Период не меняем."""
    sess = get_db(context).get_session(update.effective_user.id)
    if sess is None or sess.period == current_period():
        return
    notified = context.application.bot_data.setdefault("period_notified", set())
    key = (update.effective_user.id, sess.house_id, sess.kind, sess.period)
    if key in notified:
        return
    notified.add(key)
    await update.message.reply_text(
        f"ℹ️ Продолжаем сессию за {period_title(sess.period)} — {HOUSE_NAMES[sess.house_id]}"
        f"{ledger_suffix(sess.kind)}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех сообщений"""
    if not update.message or not update.message.text:
        return
    if not await check_access(update, context):
        return
    text = update.message.text.strip()
    user_id = update.effective_user.id
    logger.info("Получено сообщение от %s: %s", user_id, text)

    if text not in (BTN_NEW, BTN_ADMIN) and text not in HOUSE_BY_NAME and text not in BTN_KIND:
        await notify_other_period(update, context)

    if text == BTN_NEW:
        await show_houses(update, context)
        return
    if text == BTN_ADMIN:
        await admin_entry(update, context)
        return
    if text in HOUSE_BY_NAME:
        await choose_house(update, context, HOUSE_BY_NAME[text])
        return
    if text in BTN_KIND:
        house_id = context.user_data.get("kind_house")
        if house_id is None:
            await update.message.reply_text("Сначала выбери дом:", reply_markup=house_keyboard(context, user_id))
            return
        await start_ledger_session(update, context, house_id, BTN_KIND[text])
        return
    if text == BTN_BACK and context.user_data.get("kind_house") is not None:
        await show_houses(update, context)
        return

    db = get_db(context)
    sess = db.get_session(user_id)
    if sess is not None and db.get_pending_choices(user_id) and text not in (BTN_FILE, BTN_DONE):
        if await handle_choice(update, context, sess, text):
            return
        sess = db.get_session(user_id)

    if text == BTN_FILE:
        await send_current_file(update, context)
        return
    if text == BTN_DONE:
        await finish_session(update, context)
        return
    if sess is None:
        await enter_readings(update, context, text)
        return

    if text == BTN_BACK:
        if sess.pending_apartment:
            db.set_pending_apartment(user_id, None)
            await update.message.reply_text("Ок, вводи показания",
                                            reply_markup=session_keyboard(db, sess.house_id, sess.kind, sess.period))
        elif sess.kind == COMMERCE:
            await show_houses(update, context)
        else:
            await update.message.reply_text("Ок, вводи показания", reply_markup=session_keyboard(db, sess.house_id))
        return

    if sess.kind == RESIDENTIAL:
        if text == BTN_NONSTD:
            await show_nonstandard_menu(update, context, sess)
            return
        chosen = nonstandard_by_label(db, sess.house_id).get(text)
        if chosen:
            await choose_nonstandard(update, context, sess, chosen)
            return
    else:
        key = subscriber_by_label(db, sess.house_id, text)
        if key:
            await choose_subscriber(update, context, sess, key)
            return

    if sess.pending_apartment and await enter_pending_value(update, context, sess, text):
        return
    await enter_readings(update, context, text, sess=sess)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанная ошибка: %s", context.error, exc_info=context.error)


def main():
    """Запуск бота"""
    try:
        settings = load_settings()
    except ConfigError as e:
        logger.critical("Ошибка конфигурации: %s. Проверь .env (см. .env.example)", e)
        sys.exit(1)

    db = Database(settings.database_url)
    db.init()
    db.seed_roles(settings.admin_user_ids, settings.allowed_user_ids)
    if db.count_admins() == 0 and db.count_active_users() == 0:
        logger.error("Ни одного пользователя с доступом! Бот будет отказывать всем. Укажите в .env "
                     "ADMIN_USER_IDS (администраторы) и ALLOWED_USER_IDS (пользователи) — Telegram ID "
                     "через запятую — и перезапустите бота. Свой ID бот сообщит в ответ на любое сообщение.")
    else:
        logger.info("Администраторов: %d, всего с доступом: %d", db.count_admins(), db.count_active_users())
    logger.info("База данных: %s", db.engine.url.render_as_string(hide_password=True))

    app = Application.builder().token(settings.bot_token).build()
    app.bot_data["db"] = db
    app.bot_data["settings"] = settings
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    try:
        asyncio.run(run_until_signal(app))
    finally:
        db.dispose()
    logger.info("Бот остановлен")
    return 0


async def run_until_signal(app: Application, stop_signals=(signal.SIGINT, signal.SIGTERM)) -> None:
    """Запуск polling и аккуратная остановка по SIGTERM/SIGINT.

    Вместо app.run_polling(): в PTB 20.6 при SIGTERM он закрывает цикл событий, пока ещё живы
    задачи/HTTP-клиент, — в лог падает «Event loop is closed», процесс выходит с кодом 1.
    Здесь жизненный цикл ручной (рекомендованный PTB вариант): initialize → start → start_polling,
    ждём сигнал, затем updater.stop → stop → shutdown (ошибки сети при остановке не мешают),
    и только потом asyncio.run закрывает цикл."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = []
    for sig in stop_signals:
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # Windows / не главный поток
            pass
    try:
        await app.initialize()
        try:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("🚀 Бот запущен!")
            await stop.wait()
            logger.info("Получен сигнал остановки, завершаю работу…")
        finally:
            # Каждый шаг — отдельно: если последний запрос к Telegram при остановке упал
            # (таймаут/нет сети), остальные шаги всё равно выполняются и процесс выходит с кодом 0.
            # Без app.stop() внутренняя задача PTB игнорирует отмену и процесс зависает.
            for name, running, step in (("updater.stop", lambda: app.updater.running, app.updater.stop),
                                        ("application.stop", lambda: app.running, app.stop),
                                        ("application.shutdown", lambda: True, app.shutdown)):
                if not running():
                    continue
                try:
                    await step()
                except Exception as e:  # noqa: BLE001
                    logger.warning("Остановка: %s завершился с ошибкой (%s) — продолжаю", name, e)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    sys.exit(main())
