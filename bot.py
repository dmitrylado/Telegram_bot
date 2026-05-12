import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
import logging
import asyncio
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

CATALOG_PATH = "catalog.xlsx"
PAGE_SIZE = 10

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СЕССИИ (ВАЖНО) ---
SESSION = {}  
# {
#   chat_id: {
#       "mode": "excel" | "ikea",
#       "items": [...],
#       "page": 0
#   }
# }

# ---------------- UTIL ----------------
def safe_int(x):
    try:
        return int(x)
    except:
        return 0

def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def format_price(v):
    try:
        return f"{int(round(float(str(v).replace(',', '.'))))} руб."
    except:
        return str(v)

# ---------------- IKEA PARSER ----------------
def fetch_ikea(query: str):
    url = f"https://www.ikea.com/lt/ru/search/?q={query.replace(' ', '%20')}"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        items = []

        # IKEA часто меняет классы — используем максимально мягкий парсинг
        for card in soup.select("div"):
            title = card.get_text(" ", strip=True)

            img = card.find("img")
            if not img:
                continue

            src = img.get("src")

            if title and len(title) < 120:
                items.append({
                    "title": title,
                    "photo": src,
                    "price": ""
                })

        return items[:50]

    except Exception as e:
        print("IKEA ERROR:", e)
        return []

# ---------------- PAGINATION ----------------
async def send_page(chat_id: int, message: types.Message):
    data = SESSION.get(chat_id)
    if not data:
        return

    items = data["items"]
    page = data["page"]

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    chunk = items[start:end]

    if not chunk:
        await message.answer("Больше товаров нет.")
        return

    total_pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE

    await message.answer(f"Страница {page+1}/{total_pages}")

    for it in chunk:
        text = ""

        if data["mode"] == "excel":
            text = (
                f"<b>{it.get('title')}</b>\n"
                f"Цена: {format_price(it.get('price'))}\n"
                f"Наличие: {it.get('stock')}"
            )
        else:
            text = f"<b>{it.get('title')}</b>"

        if it.get("photo"):
            try:
                await message.answer_photo(it["photo"], caption=text, parse_mode="HTML")
            except:
                await message.answer(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")

    kb = []

    if page > 0:
        kb.append(types.InlineKeyboardButton("⬅️ Назад", callback_data="page:prev"))
    if end < len(items):
        kb.append(types.InlineKeyboardButton("➡️ Далее", callback_data="page:next"))

    if kb:
        await message.answer(
            "Навигация",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[kb])
        )

# ---------------- COMMANDS ----------------
@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer(
        "Меню:",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="Текущая доступность")],
                [types.KeyboardButton(text="Под заказ")],
                [types.KeyboardButton(text="Найти")]
            ],
            resize_keyboard=True
        )
    )

# ---------------- EXCEL MODE ----------------
@dp.message(F.text == "Текущая доступность")
async def available(m: types.Message):
    df = load_catalog()
    df["stock"] = df["stock"].apply(safe_int)
    df = df[df["stock"] > 0]

    items = df.to_dict("records")

    SESSION[m.chat.id] = {
        "mode": "excel",
        "items": items,
        "page": 0
    }

    await send_page(m.chat.id, m)

# ---------------- IKEA MODE ----------------
@dp.message(F.text == "Под заказ")
async def order(m: types.Message):
    await m.answer("Введите запрос для IKEA (например: простыня, лампа, chair):")
    SESSION[m.chat.id] = {"mode": "wait_ikea"}

@dp.message()
async def text_handler(m: types.Message):
    chat_id = m.chat.id
    session = SESSION.get(chat_id)

    if not session:
        return

    # ожидание IKEA запроса
    if session.get("mode") == "wait_ikea":
        query = m.text

        await m.answer(f"Ищу IKEA: {query} ...")

        items = fetch_ikea(query)

        if not items:
            await m.answer("Ничего не найдено 😔")
            SESSION.pop(chat_id, None)
            return

        SESSION[chat_id] = {
            "mode": "ikea",
            "items": items,
            "page": 0
        }

        await send_page(chat_id, m)

# ---------------- NAVIGATION ----------------
@dp.callback_query(F.data.startswith("page:"))
async def nav(c: types.CallbackQuery):
    chat_id = c.message.chat.id
    session = SESSION.get(chat_id)

    if not session:
        await c.answer()
        return

    if c.data == "page:next":
        session["page"] += 1
    elif c.data == "page:prev":
        session["page"] -= 1

    await c.answer()
    await send_page(chat_id, c.message)

# ---------------- RUN ----------------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())