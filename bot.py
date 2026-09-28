"""Small Telegram launcher bot for the Temple Echo Web App."""
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Игра ещё не опубликована. Администратору нужно указать HTTPS-адрес Mini App в WEBAPP_URL.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🌿 Войти в храм", web_app=WebAppInfo(url=WEBAPP_URL)
    )]])
    await message.answer(
        "🌴 *Temple Echo: Jungle Ruins*\n\nДревние стражи пробудились. Сколько волн ты переживёшь?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в .env (создайте бота через @BotFather).")
    if not WEBAPP_URL.startswith("https://"):
        raise SystemExit("Укажите публичный HTTPS-адрес игры в WEBAPP_URL.")
    bot = Bot(TOKEN)
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Открыть игру", web_app=WebAppInfo(url=WEBAPP_URL)
    ))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
