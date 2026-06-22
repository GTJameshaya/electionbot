import os
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from openpyxl import load_workbook
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Настройка логирования для Bothost
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# === Конфиги из .env ===
TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", 0))  # Добавьте значение по умолчанию
TARGET_BOT_TAG = os.getenv("TARGET_BOT_TAG", "@supp0rt_dom")

# === Пути ===
BASE_DIR = Path(__file__).parent
TEMPLATES = {
    1: BASE_DIR / "template_1.xlsx", 
    2: BASE_DIR / "template_2.xlsx"
}
ARCHIVE_DIR = BASE_DIR / "archive"
SESSIONS_DIR = BASE_DIR / "sessions"

# Создаем директории, если их нет
ARCHIVE_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

# === Данные ===
active_sessions = {}
MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
HOUSE_NAMES = {1: "Волкова д.9 к.1", 2: "Химиков 4"}
HOUSE_HASHTAG = {1: "#Волкова", 2: "#Химиков"}

# Постоянная кнопка после завершения
BASE_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Начать новую сессию")]], 
    resize_keyboard=True
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - выбор дома"""
    keyboard = [[KeyboardButton("Волкова д.9 к.1"), KeyboardButton("Химиков 4")]]
    await update.message.reply_text(
        "Выбери дом:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех сообщений"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    logger.info(f"Получено сообщение от {user_id}: {text}")

    # 1. Начать новую сессию
    if text == "Начать новую сессию":
        await start(update, context)
        return

    # 2. Выбор дома
    if text in HOUSE_NAMES.values():
        house = 1 if text == HOUSE_NAMES[1] else 2
        
        if not TEMPLATES[house].exists():
            await update.message.reply_text("Нет шаблона для этого дома")
            return

        # Проверяем, есть ли предыдущие файлы
        prev_files = sorted(ARCHIVE_DIR.glob(f"*_house{house}_*.xlsx"), reverse=True)
        prev_file = prev_files[0] if prev_files else None

        # Создаем новый файл сессии
        session_file = SESSIONS_DIR / f"session_{user_id}_{datetime.now().strftime('%H%M%S')}.xlsx"
        wb = load_workbook(TEMPLATES[house])
        ws = wb.active

        today = datetime.now()
        current_month = MONTHS_RU[today.month - 1]
        prev_month = MONTHS_RU[(today.replace(day=1) - timedelta(days=1)).month - 1]

        ws.cell(row=4, column=4, value=prev_month)
        ws.cell(row=4, column=5, value=current_month)
        ws["D2"] = today.replace(day=21)

        # Копируем предыдущие показания
        if prev_file:
            try:
                prev_wb = load_workbook(prev_file, data_only=True)
                prev_ws = prev_wb.active
                for row in range(5, prev_ws.max_row + 1):
                    val = prev_ws.cell(row=row, column=5).value
                    if val is not None:
                        ws.cell(row=row, column=4, value=val)
            except Exception as e:
                logger.error(f"Ошибка загрузки предыдущего файла: {e}")

        # Очищаем текущие показания
        for row in range(5, ws.max_row + 1):
            ws.cell(row=row, column=5).value = None
            ws.cell(row=row, column=6).value = None

        wb.save(session_file)
        active_sessions[user_id] = {"file": session_file, "house": house}

        keyboard = [[KeyboardButton("Файл сейчас"), KeyboardButton("Готово")]]
        await update.message.reply_text(
            f"Дом: {text}\nСессия начата — вводи показания\nТишина = всё ок",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return

    # 3. Файл сейчас
    if text == "Файл сейчас":
        data = active_sessions.get(user_id)
        if data and data["file"].exists():
            await update.message.reply_document(
                open(data["file"], "rb"), 
                caption="Текущий файл"
            )
        else:
            await update.message.reply_text("Нет активной сессии")
        return

    # 4. Готово - отправка в группу
    if text == "Готово":
        data = active_sessions.get(user_id)
        if not data:
            await update.message.reply_text("Нет активной сессии")
            return

        file_path = data["file"]
        house = data["house"]
        month_tag = f"{MONTHS_RU[datetime.now().month - 1]}_{datetime.now().year}"

        archive_name = ARCHIVE_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M')}_house{house}_{HOUSE_NAMES[house].replace(' ', '_')}.xlsx"
        file_path.rename(archive_name)

        # Отправляем в группу
        try:
            with open(archive_name, "rb") as f:
                caption = (
                    f"{TARGET_BOT_TAG} Привет! Показания за текущий {datetime.now().strftime('%m.%Y')} — {HOUSE_NAMES[house]}\n"
                    f"#{month_tag} {HOUSE_HASHTAG[house]}"
                )
                await context.bot.send_document(
                    GROUP_CHAT_ID, 
                    f, 
                    filename=archive_name.name, 
                    caption=caption
                )
            
            await update.message.reply_text(
                "✅ Файл улетел в группу. Сессия завершена.",
                reply_markup=BASE_KEYBOARD
            )
            
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            await update.message.reply_text(f"❌ Ошибка отправки: {str(e)}")
        
        active_sessions.pop(user_id, None)
        return

    # 5. Ввод показаний
    data = active_sessions.get(user_id)
    if not data:
        await update.message.reply_text(
            "Сначала начни сессию: /start",
            reply_markup=BASE_KEYBOARD
        )
        return

    # Парсим показания
    parts = text.replace("\n", " ").split()
    if len(parts) % 2 != 0:
        await update.message.reply_text("❌ Нечётное количество значений")
        return

    # Обновляем файл
    try:
        wb = load_workbook(data["file"])
        ws = wb.active
        found_count = 0

        for i in range(0, len(parts), 2):
            apt = parts[i].strip()
            try:
                val = int(parts[i+1])
            except ValueError:
                await update.message.reply_text(f"❌ Для квартиры {apt} введите число")
                return

            found = False
            for row in range(5, ws.max_row + 1):
                cell_val = ws.cell(row=row, column=1).value
                if cell_val is not None and str(cell_val).strip() == apt:
                    ws.cell(row=row, column=5, value=val)
                    prev = ws.cell(row=row, column=4).value or 0
                    ws.cell(row=row, column=6, value=val - int(prev))
                    found = True
                    found_count += 1
                    break
            
            if not found:
                await update.message.reply_text(f"❌ Квартира {apt} не найдена")

        wb.save(data["file"])
        
        if found_count > 0:
            await update.message.reply_text(f"✅ Обновлено {found_count} квартир")
            
    except Exception as e:
        logger.error(f"Ошибка при обновлении файла: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

def main():
    """Запуск бота"""
    try:
        app = Application.builder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("🚀 Бот запущен!")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
        raise

if __name__ == "__main__":
    main()
