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
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Можно -1001234567890 или @channelusername

CATALOG_PATH = "catalog.xlsx"
PAGE_SIZE = 10
CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут

bot = Bot(token=TOKEN)
dp = Dispatcher()

CATEGORY_MAP: dict[str, str] = {}

# ------------------ Функции для работы с Excel ------------------

def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def safe_int(x):
    try:
        return int(x)
    except Exception:
        return 0

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def find_in_file(df: pd.DataFrame, query: str, limit: int = 5):
    q = normalize(query)
    df2 = df.copy()
    df2["__text"] = (df2["title"].astype(str) + " " + df2.get("sku", "").astype(str)).map(normalize)
    hits = df2[df2["__text"].str.contains(q, na=False)].head(limit)
    return hits.to_dict("records")

def format_rub_price(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return ""
    s = s.replace(" ", "").replace(",", ".")
    try:
        num = float(s)
        rub = int(round(num))
        return f"{rub} руб."
    except Exception:
        return f"{s} руб."

def format_caption_from_excel(item: dict) -> str:
    title = item.get("title", "(без названия)")
    desc = item.get("description", "")
    link = item.get("link", "")
    parts = [f"<b>{title}</b>"]
    if desc:
        parts.append(f"<i>{desc}</i>")
    price_str = format_rub_price(item.get("price"))
    if price_str:
        parts.append(f"<b>Цена:</b> {price_str}")
    if link:
        parts.append(f'<a href="{link}">Купить на сайте</a>')
    return "\n".join(parts)

def build_category_keyboard(df_available: pd.DataFrame) -> types.InlineKeyboardMarkup:
    global CATEGORY_MAP
    CATEGORY_MAP = {}

    if "category_1" not in df_available.columns:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")]
        ])

    categories = df_available["category_1"].astype(str).map(lambda x: x.strip())
    categories = [c for c in categories.unique().tolist() if c and c.lower() != "nan"]
    categories.sort()

    keyboard: list[list[types.InlineKeyboardButton]] = []
    keyboard.append([types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")])
    row: list[types.InlineKeyboardButton] = []
    for i, cat in enumerate(categories, start=1):
        cat_id = str(i)
        CATEGORY_MAP[cat_id] = cat
        row.append(types.InlineKeyboardButton(text=cat, callback_data=f"cat:{cat_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([types.InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:start")])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

async def send_category_page(message: types.Message, code: str, page: int):
    df = load_catalog()
    if "stock" not in df.columns:
        await message.answer("В файле нет колонки stock.")
        return
    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()
    if available.empty:
        await message.answer("Сейчас нет доступных позиций 😔")
        return
    if code == "ALL":
        filtered = available
        cat_title = "Все категории"
    else:
        cat_name = CATEGORY_MAP.get(code)
        if not cat_name:
            await message.answer("Категория устарела. Нажмите «Текущая доступность» ещё раз.")
            return
        filtered = available[available["category_1"].astype(str).str.strip() == cat_name]
        cat_title = cat_name

    total = len(filtered)
    if total == 0:
        await message.answer(f"В категории «{cat_title}» сейчас нет доступных товаров.")
        return

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_df = filtered.iloc[start:end].copy()
    if page_df.empty:
        await message.answer("Больше товаров нет ✅")
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await message.answer(
        f"✅ В наличии — {cat_title}\n"
        f"Страница {page+1}/{total_pages} (позиции {start+1}-{min(end, total)} из {total})"
    )

    for _, row in page_df.iterrows():
        item = row.to_dict()
        caption = format_caption_from_excel(item)
        photo = item.get("photo")
        if photo:
            try:
                await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
            except Exception:
                await message.answer(caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    nav_rows = []
    if page > 0:
        nav_rows.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catpage:{code}:{page-1}"))
    nav_rows.append(types.InlineKeyboardButton(text="📂 Категории", callback_data="menu:cats"))
    if end < total:
        nav_rows.append(types.InlineKeyboardButton(text="Далее ➡️", callback_data=f"catpage:{code}:{page+1}"))
    if nav_rows:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_rows])
        await message.answer("Навигация:", reply_markup=kb)

# ------------------ Клавиатуры ------------------

main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Под заказ"), types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🕒 Под заказ", callback_data="menu:order")],
    [types.InlineKeyboardButton(text="🔎 Найти", callback_data="menu:search")],
])

# ------------------ Обработчики команд ------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Текущая доступность")
async def cmd_available(message: types.Message):
    df = load_catalog()
    if "stock" not in df.columns:
        await message.answer("В файле нет колонки stock.", reply_markup=main_kb)
        return
    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()
    if available.empty:
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        return
    kb = build_category_keyboard(available)
    await message.answer(f"В наличии позиций: {len(available)}.\nВыберите категорию:", reply_markup=kb)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    # Для примера, загружаем те же карточки, что в Excel, но с другим сообщением
    df = load_catalog()
    df["__stock_int"] = df["stock"].apply(safe_int)
    items = df.head(PAGE_SIZE).to_dict("records")
    for item in items:
        caption = format_caption_from_excel(item)
        photo = item.get("photo")
        if photo:
            await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")
    await message.answer("Вы можете оформить заказ на эти позиции.", reply_markup=main_kb)

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

# ------------------ Callback для inline-кнопок ------------------

@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":", 1)[1]
    if action == "available":
        await cmd_available(call.message)
    elif action == "order":
        await cmd_under_order(call.message)
    elif action == "search":
        await cmd_search_stub(call.message)
    elif action == "cats":
        df = load_catalog()
        df["__stock_int"] = df["stock"].apply(safe_int)
        available = df[df["__stock_int"] > 0].copy()
        kb = build_category_keyboard(available)
        await call.message.answer("Выберите категорию:", reply_markup=kb)
    elif action == "start":
        await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("cat:"))
async def category_callback(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]
    await call.answer()
    await send_category_page(call.message, code=code, page=0)

@dp.callback_query(lambda c: c.data and c.data.startswith("catpage:"))
async def category_page_callback(call: types.CallbackQuery):
    try:
        _, code, page_str = call.data.split(":", 2)
        page = int(page_str)
    except Exception:
        await call.answer("Ошибка навигации", show_alert=True)
        return
    await call.answer()
    await send_category_page(call.message, code=code, page=page)

# ------------------ Основной запуск ------------------

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())