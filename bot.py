import asyncio
import logging
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 307321898

EMAIL_TO = "noliki000@gmail.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

if not TOKEN:
    raise RuntimeError("Не знайдено BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

db_pool = None


# ==========================================
# БАЗА ДАНИХ ТА АНАЛІТИКА
# ==========================================

async def init_database():
    global db_pool

    if not DATABASE_URL:
        logger.warning("DATABASE_URL не налаштовано")
        return

    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=3,
            timeout=10
        )

        async with db_pool.acquire() as conn:

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_seen TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    event_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

        logger.info("PostgreSQL підключено. Таблиці готові.")

    except Exception:
        logger.exception("Помилка підключення PostgreSQL")
        db_pool = None


async def track_event(message: Message, event_type: str):
    if db_pool is None or message.from_user is None:
        return

    user_id = message.from_user.id

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():

                await conn.execute("""
                    INSERT INTO bot_users (user_id)
                    VALUES ($1)
                    ON CONFLICT (user_id)
                    DO UPDATE SET last_seen = NOW()
                """, user_id)

                await conn.execute("""
                    INSERT INTO bot_events (user_id, event_type)
                    VALUES ($1, $2)
                """, user_id, event_type)

    except Exception:
        logger.exception("Помилка запису аналітики")


# ==========================================
# ФОРМА ЗАЯВКИ
# ==========================================

class OrderForm(StatesGroup):
    name = State()
    service = State()
    address = State()
    phone = State()


# ==========================================
# КЛАВІАТУРИ
# ==========================================

def main_menu():
    keyboard = ReplyKeyboardBuilder()

    keyboard.button(text="🎨 Замовити роботи")
    keyboard.button(text="📞 Контакти")

    keyboard.adjust(2)

    return keyboard.as_markup(resize_keyboard=True)


def cancel_menu():
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="❌ Скасувати")

    return keyboard.as_markup(resize_keyboard=True)


SERVICES = [
    "🎨 Фарбування",
    "🖌️ Художній розпис",
    "🧱 Ремонтні роботи",
    "🔧 Інші роботи",
]


def services_menu():
    keyboard = ReplyKeyboardBuilder()

    for service in SERVICES:
        keyboard.button(text=service)

    keyboard.button(text="❌ Скасувати")
    keyboard.adjust(2, 2, 1)

    return keyboard.as_markup(resize_keyboard=True)


# ==========================================
# START
# ==========================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await track_event(message, "start")

    await message.answer(
        "👋 Вітаємо в Noliki Business!\n\n"
        "Малярні та ремонтні роботи.\n"
        "Оберіть потрібний розділ:",
        reply_markup=main_menu()
    )


# ==========================================
# СКАСУВАННЯ
# ==========================================

@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Скасувати")
async def cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state is not None:
        await track_event(message, "order_cancelled")

    await state.clear()

    await message.answer(
        "Оформлення заявки скасовано.",
        reply_markup=main_menu()
    )


# ==========================================
# ПОЧАТОК ЗАЯВКИ
# ==========================================

@dp.message(F.text == "🎨 Замовити роботи")
async def order(message: Message, state: FSMContext):
    await state.clear()

    await track_event(message, "order_started")

    await state.set_state(OrderForm.name)

    await message.answer(
        "📝 ОФОРМЛЕННЯ ЗАЯВКИ\n\n"
        "Як до вас звертатися?",
        reply_markup=cancel_menu()
    )


# ==========================================
# ІМ'Я
# ==========================================

@dp.message(OrderForm.name)
async def order_name(message: Message, state: FSMContext):

    if not message.text or len(message.text.strip()) < 2:
        await message.answer("Введіть ваше ім'я.")
        return

    await state.update_data(name=message.text.strip())
    await state.set_state(OrderForm.service)

    await message.answer(
        "🛠️ Оберіть потрібну послугу:",
        reply_markup=services_menu()
    )


# ==========================================
# ПОСЛУГА
# ==========================================

@dp.message(OrderForm.service)
async def order_service(message: Message, state: FSMContext):

    if not message.text or message.text not in SERVICES:
        await message.answer(
            "Будь ласка, оберіть послугу кнопкою нижче.",
            reply_markup=services_menu()
        )
        return

    await state.update_data(service=message.text)
    await state.set_state(OrderForm.address)

    await message.answer(
        "📍 Де розташований об'єкт?\n\n"
        "Вкажіть місто та адресу.",
        reply_markup=cancel_menu()
    )


# ==========================================
# АДРЕСА
# ==========================================

@dp.message(OrderForm.address)
async def order_address(message: Message, state: FSMContext):

    if not message.text or len(message.text.strip()) < 3:
        await message.answer("Вкажіть адресу об'єкта.")
        return

    await state.update_data(address=message.text.strip())
    await state.set_state(OrderForm.phone)

    await message.answer(
        "📱 Вкажіть контактний номер телефону.",
        reply_markup=cancel_menu()
    )


# ==========================================
# EMAIL
# ==========================================

def send_email(subject, body):

    if not EMAIL_USER or not EMAIL_PASSWORD:
        logger.warning("Email не налаштовано")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=20
    ) as smtp:

        smtp.login(EMAIL_USER, EMAIL_PASSWORD)
        smtp.send_message(msg)

    return True


# ==========================================
# ТЕЛЕФОН І ЗАВЕРШЕННЯ ЗАЯВКИ
# ==========================================

@dp.message(OrderForm.phone)
async def order_phone(message: Message, state: FSMContext):

    if not message.text:
        await message.answer("Введіть номер телефону.")
        return

    phone = re.sub(r"[\s()+-]", "", message.text)

    if not phone.isdigit() or not 9 <= len(phone) <= 15:
        await message.answer(
            "❗ Неправильний формат номера.\n"
            "Приклад: +380XXXXXXXXX"
        )
        return

    await state.update_data(phone=message.text.strip())
    data = await state.get_data()

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "Не вказано"
    )

    client_id = message.from_user.id
    date = datetime.now().strftime("%d.%m.%Y %H:%M")

    order_text = (
        "🔔 НОВА ЗАЯВКА — NOLIKI BUSINESS\n\n"
        f"👤 Ім'я: {data['name']}\n"
        f"🛠️ Послуга: {data['service']}\n"
        f"📍 Адреса: {data['address']}\n"
        f"📱 Телефон: {data['phone']}\n\n"
        f"💬 Telegram: {username}\n"
        f"🆔 Telegram ID: {client_id}\n"
        f"📅 Дата: {date}"
    )

    telegram_sent = False
    email_sent = False

    try:
        await bot.send_message(ADMIN_ID, order_text)
        telegram_sent = True
    except Exception:
        logger.exception("Помилка Telegram")

    try:
        email_sent = await asyncio.to_thread(
            send_email,
            "Нова заявка — Noliki Business",
            order_text
        )
    except Exception:
        logger.exception("Помилка Email")

    await state.clear()

    if telegram_sent or email_sent:

        await track_event(message, "order_completed")

        await message.answer(
            "✅ Дякуємо! Вашу заявку прийнято.\n\n"
            "Наш менеджер зв'яжеться з вами найближчим часом "
            "для уточнення деталей.",
            reply_markup=main_menu()
        )

    else:
        await message.answer(
            "⚠️ Не вдалося передати заявку менеджеру.\n\n"
            "Будь ласка, зв'яжіться з нами:\n"
            "📱 +380934002520\n"
            "💬 @noliki000",
            reply_markup=main_menu()
        )


# ==========================================
# КОНТАКТИ
# ==========================================

@dp.message(F.text == "📞 Контакти")
async def contacts(message: Message):

    await track_event(message, "contacts_opened")

    await message.answer(
        "📞 КОНТАКТИ NOLIKI BUSINESS\n\n"
        "📱 Телефон: +380934002520\n\n"
        "💬 Telegram: @noliki000\n\n"
        "📍 Місто: Київ\n\n"
        "🕘 Графік роботи: Пн–Сб, 09:00–19:00\n\n"
        "🎨 Малярні та ремонтні роботи.\n"
        "Звертайтеся — будемо раді співпраці!"
    )


# ==========================================
# ІНШІ ПОВІДОМЛЕННЯ
# ==========================================

@dp.message()
async def other_messages(message: Message):

    await message.answer(
        "Оберіть потрібний розділ у меню.",
        reply_markup=main_menu()
    )


# ==========================================
# ЗАПУСК
# ==========================================

async def main():

    await init_database()

    logger.info("Telegram-бот запущений")

    try:
        await dp.start_polling(bot)

    finally:
        if db_pool is not None:
            await db_pool.close()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
