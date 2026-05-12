import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
import logging
import os
import asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Можно @username_канала

CATALOG_PATH = "catalog.xlsx"
PAGE_SIZE = 10
CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут

# FSM состояния для поиска "Под заказ"
class OrderSearch(StatesGroup):
    waiting_for_query = State()

# --- Инициализация бота и диспетчера ---
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

# --- Функции ---
def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

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

def fetch_ikea_data(query: str):
    """
    Парсим название, цену и фото товара из поиска IKEA (LT/RU).
    """
    base_url = f"https://www.ikea.com/lt/ru/search/?q={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(base_url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    # Каждый товар на странице
    product_cards = soup.select("div[data-testid='product-card']")
    for card in product_cards:
        title_el = card.select_one("span[data-testid='product-pip__name']")
        price_el = card.select_one("span[data-testid='product-pip__price']")
        img_el = card.select_one("img")
        if title_el and price_el and img_el:
            items.append({
                "title": title_el.get_text(strip=True),
                "price": price_el.get_text(strip=True),
                "photo": img_el.get("src")
            })
    return items

def build_navigation_keyboard(page: int, total_pages: int) -> types.InlineKeyboardMarkup:
    buttons = []
    if page > 0:
        buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"order_page:{page-1}"))
    if page + 1 < total_pages:
        buttons.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"order_page:{page+1}"))
    return types.InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

# --- Основное меню ---
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

# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message, state: FSMContext):
    await message.answer("Введите название товара для поиска на IKEA:")
    await state.set_state(OrderSearch.waiting_for_query)

@dp.message(OrderSearch.waiting_for_query)
async def order_search(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Введите что-нибудь для поиска.")
        return
    items = fetch_ikea_data(query)
    if not items:
        await message.answer("По вашему запросу ничего не найдено 😔")
        await state.clear()
        return

    # Сохраняем результаты в FSMContext
    await state.update_data(items=items, query=query, page=0)
    await send_order_page(message, state, 0)

async def send_order_page(message: types.Message, state: FSMContext, page: int):
    data = await state.get_data()
    items = data.get("items", [])
    total = len(items)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]

    for item in page_items:
        caption = f"<b>{item['title']}</b>\nЦена: {item['price']}"
        photo = item['photo']
        try:
            await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
        except Exception:
            await message.answer(caption, parse_mode="HTML")

    kb = build_navigation_keyboard(page, total_pages)
    if kb:
        await message.answer("Навигация:", reply_markup=kb)
    await state.update_data(page=page)

@dp.callback_query(lambda c: c.data and c.data.startswith("order_page:"))
async def order_page_callback(call: types.CallbackQuery, state: FSMContext):
    try:
        page = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Ошибка навигации", show_alert=True)
        return
    await call.answer()
    await send_order_page(call.message, state, page)

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

@dp.message(F.text == "Текущая доступность")
async def cmd_available(message: types.Message):
    await message.answer("Раздел «Текущая доступность» пока в разработке 🙂", reply_markup=main_kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":", 1)[1]
    if action == "available":
        await call.message.answer("Показываю товары в наличии…")
        await cmd_available(call.message)
    elif action == "order":
        await call.message.answer("Введите название товара для поиска на IKEA:")
        await OrderSearch.waiting_for_query.set()
    elif action == "search":
        await call.message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)
    await call.answer()

# --- Запуск ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())