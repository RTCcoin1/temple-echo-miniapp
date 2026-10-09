"""Telegram front door for the Temple Echo Mini App."""

import asyncio
from html import escape
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

dp = Dispatcher()

PAGES = {
    "home": (
        "<b>☼ TEMPLE ECHO</b>\n"
        "<code>JUNGLE RUINS · TELEGRAM MINI GAME</code>\n\n"
        "<blockquote>Древний колокол прозвучал. Стражи храма пробудились.</blockquote>\n"
        "Исследуй залы, переживи волны и собери печати, чтобы добраться до Сердца храма.\n\n"
        "<b>Твой путь:</b> сюжет · хаос · ко‑оп · онлайн · испытание дня\n\n"
        "Нажми кнопку ниже, чтобы войти в храм."
    ),
    "modes": (
        "<b>⚔ РЕЖИМЫ ХРАМА</b>\n\n"
        "<b>СЮЖЕТ</b> · четыре области, двадцать волн, карта и стражи.\n"
        "<b>ХАОС</b> · переживи всё более плотные волны врагов.\n"
        "<b>КО‑ОП</b> · играйте вдвоём на одном устройстве.\n"
        "<b>СЕТЕВАЯ КОМНАТА</b> · собери команду на 2, 4 или 6 игроков.\n"
        "<b>ИСПЫТАНИЕ ДНЯ</b> · одинаковые условия и общий рекорд.\n\n"
        "Выбери режим и героя уже внутри игры."
    ),
    "help": (
        "<b>🎮 УПРАВЛЕНИЕ</b>\n\n"
        "<b>📱 Телефон</b>\n"
        "Левая панель — движение. Правый стик — прицел и стрельба.\n"
        "Кнопка <b>ОГОНЬ</b> стреляет в выбранном направлении.\n\n"
        "<b>🖥 Компьютер</b>\n"
        "<code>W A S D</code> или стрелки — движение.\n"
        "Мышь — прицел, <code>ЛКМ</code> или <code>ПРОБЕЛ</code> — огонь.\n\n"
        "В сетевой игре удерживай <code>E</code> рядом с павшим союзником, чтобы поднять его."
    ),
    "story": (
        "<b>📜 ЛЕТОПИСЬ ХРАМА</b>\n\n"
        "Под древним городом прозвучал колокол. Его эхо разбудило хранителей затонувшего святилища.\n\n"
        "Путь ведёт через затопленные залы, пепельное крыло, лунный сад и Зал корней. Победи четырёх стражей, собери печати и узнай, что скрывает Сердце храма.\n\n"
        "Каждый поход начинается с выбора героя и оружия. Между волнами находи реликвии и меняй тактику."
    ),
    "daily": "",
}

BOT_DESCRIPTION = (
    "Исследуй древний храм, переживи волны врагов и победи стражей. "
    "Открой Telegram Mini App кнопкой «Играть»."
)
BOT_SHORT_DESCRIPTION = "Пиксельный survival‑экшен о пробуждённом храме."


def game_url(mode: str | None = None) -> str | None:
    """Return a Telegram-compatible HTTPS Mini App URL, if configured."""
    try:
        parsed = urlsplit(WEBAPP_URL)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if mode:
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "mode"]
        query.append(("mode", mode))
        return urlunsplit(parsed._replace(query=urlencode(query)))
    return WEBAPP_URL


def page_keyboard(page: str, url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([
            InlineKeyboardButton(
                text="🏆 ИГРАТЬ В ИСПЫТАНИЕ" if page == "daily" else "🎮 ОТКРЫТЬ ИГРУ",
                web_app=WebAppInfo(url=url),
            )
        ])

    if page == "home":
        rows.extend([
            [
                InlineKeyboardButton(text="⚔ Режимы", callback_data="page:modes"),
                InlineKeyboardButton(text="🎮 Управление", callback_data="page:help"),
            ],
            [InlineKeyboardButton(text="🏆 Испытание дня", callback_data="page:daily")],
            [InlineKeyboardButton(text="📜 История храма", callback_data="page:story")],
        ])
    elif page == "daily":
        rows.append([InlineKeyboardButton(text="↻ Обновить таблицу", callback_data="page:daily")])
        rows.append([
            InlineKeyboardButton(text="⚔ Режимы", callback_data="page:modes"),
            InlineKeyboardButton(text="← Главное меню", callback_data="page:home"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="⚔ Режимы", callback_data="page:modes"),
            InlineKeyboardButton(text="🎮 Управление", callback_data="page:help"),
        ])
        rows.append([InlineKeyboardButton(text="← В главное меню", callback_data="page:home")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def daily_page_text() -> str:
    from room_server import daily_challenge, leaderboard

    challenge = daily_challenge()
    try:
        scores = await asyncio.to_thread(leaderboard, challenge["day"])
    except Exception:
        logger.exception("Could not load the daily challenge leaderboard")
        scores = []

    ranking = [
        f"<code>{index:02}</code> <b>{escape(str(entry['name']))}</b> · "
        f"{int(entry['score']):,} очк. · зал {int(entry['level'])}"
        for index, entry in enumerate(scores[:5], start=1)
    ]
    if not ranking:
        ranking = ["<i>Пока нет рекордов. Займи первое место!</i>"]

    return (
        "<b>🏆 ИСПЫТАНИЕ ДНЯ</b>\n"
        f"<code>{escape(challenge['day'])} · {escape(challenge['biome'])}</code>\n\n"
        f"<b>{escape(challenge['title'])}</b>\n"
        f"{escape(challenge['description'])}\n\n"
        "Одинаковые условия для всех. Продержись дольше и попади в таблицу.\n\n"
        "<b>ЛУЧШИЕ ИСКАТЕЛИ</b>\n" + "\n".join(ranking)
    )


async def send_page(message: Message, page: str) -> None:
    text = await daily_page_text() if page == "daily" else PAGES[page]
    await message.answer(
        text,
        reply_markup=page_keyboard(page, game_url("daily") if page == "daily" else game_url()),
        disable_web_page_preview=True,
    )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not game_url():
        await message.answer(
            "<b>☼ TEMPLE ECHO</b>\n\n"
            "Путь в храм пока закрыт. Попробуй зайти чуть позже.",
            reply_markup=page_keyboard("home", None),
        )
        logger.warning("Mini App URL is missing or is not a valid HTTPS URL")
        return
    await send_page(message, "home")


@dp.message(Command("play"))
async def play(message: Message) -> None:
    await send_page(message, "home")


@dp.message(Command("modes"))
async def modes(message: Message) -> None:
    await send_page(message, "modes")


@dp.message(Command("help"))
async def help_page(message: Message) -> None:
    await send_page(message, "help")


@dp.message(Command("story"))
async def story(message: Message) -> None:
    await send_page(message, "story")


@dp.message(Command("daily"))
async def daily(message: Message) -> None:
    await send_page(message, "daily")


@dp.callback_query(F.data.startswith("page:"))
async def navigate(callback: CallbackQuery) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return

    page = (callback.data or "").partition(":")[2]
    if page not in PAGES:
        page = "home"
    text = await daily_page_text() if page == "daily" else PAGES[page]

    try:
        await message.edit_text(
            text,
            reply_markup=page_keyboard(page, game_url("daily") if page == "daily" else game_url()),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@dp.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer(
        "Я помогу открыть игру или подскажу, как пройти храм. Выбери нужный раздел:",
        reply_markup=page_keyboard("home", game_url()),
    )


async def configure_bot(bot: Bot) -> None:
    """Apply the public-facing profile, shortcuts, and chat launch button."""
    url = game_url()
    if not url:
        raise RuntimeError("WEBAPP_URL must be a valid public HTTPS URL")

    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="play", description="Запустить игру"),
        BotCommand(command="modes", description="Посмотреть режимы"),
        BotCommand(command="help", description="Управление и помощь"),
        BotCommand(command="story", description="История храма"),
        BotCommand(command="daily", description="Испытание дня и рекорды"),
    ])
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Играть",
            web_app=WebAppInfo(url=url),
        )
    )


async def run_bot() -> None:
    if not TOKEN:
        logger.info("Telegram bot is disabled because BOT_TOKEN is not set")
        return
    if not game_url():
        logger.warning("Telegram bot is disabled because WEBAPP_URL is not a valid HTTPS URL")
        return

    async with Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    ) as bot:
        await configure_bot(bot)
        logger.info("Temple Echo bot is ready")
        await dp.start_polling(bot)


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в .env (создайте бота через @BotFather).")
    if not game_url():
        raise SystemExit("Укажите публичный HTTPS-адрес игры в WEBAPP_URL.")
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
