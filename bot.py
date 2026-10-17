import asyncio
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 307321898

EMAIL_TO = "noliki000@gmail.com"
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

if not TOKEN:
    raise RuntimeError("Не знайдено BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# Етапи оформлення заявки
class OrderForm(StatesGroup):
    name = State()
    service = State()
    address = State()
    phone = State()


# Головне меню
def main_menu():
    keyboard = ReplyKeyboardBuilder()

    keyboard.button(text="🎨 Замовити роботи")
    keyboard.button(text="💰 Розрахувати вартість")
    keyboard.button(text="📞 Контакти")

    keyboard.adjust(2, 1)
    return keyboard.as_markup(resize_keyboard=True)


# Скасування
def cancel_menu():
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="❌ Скасувати")
    return keyboard.as_markup(resize_keyboard=True)


# Категорії послуг
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


# Команда /start
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "👋 Вітаємо в Noliki Business!\n\n"
        "Малярні та ремонтні роботи.\n"
        "Оберіть потрібний розділ:",
        reply_markup=main_menu()
    )


# Скасування заявки
@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Скасувати")
async def cancel(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "Оформлення заявки скасовано.",
        reply_markup=main_menu()
    )


# Початок заявки
@dp.message(F.text == "🎨 Замовити роботи")
async def order(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrderForm.name)

    await message.answer(
        "📝 ОФОРМЛЕННЯ ЗАЯВКИ\n\n"
        "Як до вас звертатися?",
        reply_markup=cancel_menu()
    )


# Ім'я клієнта
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


# Вибір послуги
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


# Адреса об'єкта
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


# Надсилання email
def send_email(subject, body):
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("Email ще не налаштовано")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(EMAIL_USER, EMAIL_PASSWORD)
        smtp.send_message(msg)

    return True


# Телефон і завершення заявки
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
    except Exception as e:
        print(f"Помилка Telegram: {e}")

    try:
        email_sent = await asyncio.to_thread(
            send_email,
            "Нова заявка — Noliki Business",
            order_text
        )
    except Exception as e:
        print(f"Помилка Email: {e}")

    await state.clear()

    if telegram_sent or email_sent:
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


# Калькулятор
@dp.message(F.text == "💰 Розрахувати вартість")
async def calculate(message: Message):
    await message.answer(
        "💰 Калькулятор вартості робіт.\n\n"
        "Незабаром тут можна буде розрахувати ціну."
    )


# Контакти
@dp.message(F.text == "📞 Контакти")
async def contacts(message: Message):
    await message.answer(
        "📞 КОНТАКТИ NOLIKI BUSINESS\n\n"
        "📱 Телефон: +380934002520\n\n"
        "💬 Telegram: @noliki000\n\n"
        "📍 Місто: Київ\n\n"
        "🕘 Графік роботи: Пн–Сб, 09:00–19:00\n\n"
        "🎨 Малярні та ремонтні роботи.\n"
        "Звертайтеся — будемо раді співпраці!"
    )


# Інші повідомлення
@dp.message()
async def other_messages(message: Message):
    await message.answer(
        "Оберіть потрібний розділ у меню.",
        reply_markup=main_menu()
    )


# Запуск
async def main():
    print("🤖 Telegram-бот запущений!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())