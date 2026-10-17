import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

# Завантажуємо токен
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Не знайдено BOT_TOKEN у файлі .env")

# Створюємо бота
bot = Bot(token=TOKEN)
dp = Dispatcher()


# Головне меню
def main_menu():
    keyboard = ReplyKeyboardBuilder()

    keyboard.button(text="🎨 Замовити роботи")
    keyboard.button(text="💰 Розрахувати вартість")
    keyboard.button(text="📞 Контакти")

    keyboard.adjust(2, 1)

    return keyboard.as_markup(resize_keyboard=True)


# Команда /start
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Вітаємо в Noliki Business!\n\n"
        "Малярні та ремонтні роботи.\n"
        "Оберіть потрібний розділ:",
        reply_markup=main_menu()
    )


# Замовлення
@dp.message(F.text == "🎨 Замовити роботи")
async def order(message: Message):
    await message.answer(
        "🎨 Вітаємо!\n\n"
        "Опишіть, які роботи потрібно виконати.\n"
        "Наш менеджер зв'яжеться з вами."
    )


# Калькулятор
@dp.message(F.text == "💰 Розрахувати вартість")
async def calculate(message: Message):
    await message.answer(
        "💰 Калькулятор вартості робіт.\n\n"
        "Незабаром тут можна буде розрахувати ціну."
    )


# Контакти
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
