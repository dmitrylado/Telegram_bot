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

TOKEN = os.getenv("BOT_TOKEN")  # берём токен из переменной окружения
CHANNEL_ID = os.getenv("CHANNEL_ID")  # для канала

CATALOG_PATH = "catalog.xlsx"

bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут кэш сайта

PAGE_SIZE = 10

def safe_int(x):
    try:
        return int(x)
    except Exception:
        return 0

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

# --- Клавиатура главного меню ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Под заказ"), types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

# --- Inline-меню стартовое ---
start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🕒 Под заказ", callback_data="menu:order")],
    [types.InlineKeyboardButton(text="🔎 Найти", callback_data="menu:search")],
])

def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

# --- Работа с кнопкой Под заказ: поиск IKEA ---
def fetch_ikea_search(query: str):
    """
    Делаем запрос к поиску IKEA по URL https://www.ikea.com/lt/ru/search/?q=...
    Возвращаем список товаров: {title, price, photo}
    """
    url = f"https://www.ikea.com/lt/ru/search/?q={query}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    product_elements = soup.select("div[data-testid='product-pip']")  # основной блок товара

    for el in product_elements:
        # Название
        title_el = el.select_one("[data-testid='product-pip__name']")
        title = title_el.get_text(strip=True) if title_el else "(без названия)"
        # Цена
        price_el = el.select_one("[data-testid='product-pip__price__integer']")
        price = price_el.get_text(strip=True) + " руб." if price_el else ""
        # Фото
        photo_el = el.select_one("img")
        photo = photo_el["src"] if photo_el and photo_el.get("src") else None

        items.append({
            "title": title,
            "price": price,
            "photo": photo
        })

    return items

async def send_ikea_page(message: types.Message, query: str, page: int):
    """
    Показываем товары IKEA постранично.
    """
    items = fetch_ikea_search(query)
    total = len(items)
    if total == 0:
        await message.answer("По вашему запросу ничего не найдено 😔", reply_markup=main_kb)
        return

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    for item in page_items:
        caption = f"**{item['title']}**\nЦена: {item['price']}"
        if item.get("photo"):
            await message.answer_photo(item["photo"], caption=caption, parse_mode="Markdown")
        else:
            await message.answer(caption, parse_mode="Markdown")

    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"orderpage:{query}:{page-1}"))
    nav_buttons.append(types.InlineKeyboardButton("🏠 Главное меню", callback_data="menu:start"))
    if end < total:
        nav_buttons.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"orderpage:{query}:{page+1}"))

    kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
    await message.answer("Навигация:", reply_markup=kb)

# --- Основная логика кнопок меню ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Под заказ")
async def cmd_order_stub(message: types.Message):
    await message.answer("Введите запрос для поиска на IKEA:", reply_markup=main_kb)

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

@dp.message(F.text == "Текущая доступность")
async def cmd_available(message: types.Message):
    df = load_catalog()
    if "stock" not in df.columns:
        await message.answer("В файле нет колонки stock.", reply_markup=main_kb)
        return
    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()
    await message.answer(f"В наличии позиций: {len(available)}", reply_markup=main_kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":", 1)[1]

    if action == "order":
        await call.message.answer("Введите запрос для поиска на IKEA:", reply_markup=main_kb)
    elif action == "available":
        await call.message.answer("Показываю товары в наличии…")
        await cmd_available(call.message)
    elif action == "search":
        await call.message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)
    elif action == "start":
        await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

    await call.answer()

# --- Обработка запроса пользователя для кнопки «Под заказ» ---
@dp.message(lambda m: m.text and not m.text.startswith("/"))
async def handle_order_search(message: types.Message):
    query = message.text.strip()
    if not query:
        await message.answer("Введите корректный запрос.")
        return
    await send_ikea_page(message, query=query, page=0)

@dp.callback_query(lambda c: c.data and c.data.startswith("orderpage:"))
async def order_page_callback(call: types.CallbackQuery):
    try:
        _, query, page_str = call.data.split(":", 2)
        page = int(page_str)
    except Exception:
        await call.answer("Ошибка навигации", show_alert=True)
        return

    await call.answer()
    await send_ikea_page(call.message, query=query, page=page)

# --- Запуск бота ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())