import os
import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
import asyncio
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

CATALOG_PATH = "catalog.xlsx"
bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут
PAGE_SIZE = 10

# --- Состояния ---
WAITING_ORDER_QUERY = set()  # chat.id ожидает поиска на IKEA

# --- Функции для Excel ---
def load_catalog():
    return pd.read_excel(CATALOG_PATH)

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
        parts.append(f'<a href="{link}">Купить на Авито</a>')
    return "\n".join(parts)

def build_category_keyboard(df_available: pd.DataFrame) -> types.InlineKeyboardMarkup:
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

# --- Функции для IKEA ---
CACHE_IKEA = {}

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def fetch_ikea(query: str):
    """Парсим IKEA LT русскую версию, ищем товары по названию или категории."""
    base_url = "https://www.ikea.com/lt/ru/search/?q="
    url = base_url + requests.utils.quote(query)
    now = time.time()
    if url in CACHE_IKEA and (now - CACHE_IKEA[url][0]) < CACHE_TTL:
        return CACHE_IKEA[url][1]
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    # Ищем товары по классу (основной блок на странице)
    product_cards = soup.select(".plp-fragment__products li")  # актуальный селектор может меняться
    for card in product_cards:
        title_el = card.select_one(".product-compact__name, .product-compact__title")
        price_el = card.select_one(".product-compact__price")
        link_el = card.select_one("a")
        photo_el = card.select_one("img")
        if not title_el:
            continue
        item = {
            "title": title_el.get_text(strip=True),
            "price": price_el.get_text(strip=True) if price_el else None,
            "url": "https://www.ikea.com" + link_el["href"] if link_el else None,
            "photo": photo_el["src"] if photo_el else None,
        }
        items.append(item)
        if len(items) >= 50:  # максимум 50 результатов
            break
    CACHE_IKEA[url] = (now, items)
    return items

# --- Постраничная отправка ---
async def send_category_page(message: types.Message, code="ALL", page=0, items=None):
    if items is None:
        # Excel доступность
        df = load_catalog()
        df["__stock_int"] = df["stock"].apply(safe_int)
        available = df[df["__stock_int"] > 0].copy()
        if available.empty:
            await message.answer("Сейчас нет доступных позиций 😔")
            return
        total_items = available.to_dict("records")
    else:
        total_items = items

    total = len(total_items)
    if total == 0:
        await message.answer("Ничего не найдено 😔")
        return

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = total_items[start:end]

    await message.answer(f"Страница {page+1}/{(total + PAGE_SIZE - 1)//PAGE_SIZE}")

    for item in page_items:
        caption = f"<b>{item.get('title','')}</b>\n"
        if item.get("price"):
            caption += f"Цена: {item['price']}\n"
        if item.get("url"):
            caption += f'<a href="{item["url"]}">Ссылка</a>'
        if item.get("photo"):
            try:
                await message.answer_photo(item["photo"], caption=caption, parse_mode="HTML")
            except:
                await message.answer(caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{page-1}"))
    if end < total:
        nav_buttons.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"page:{page+1}"))
    if nav_buttons:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
        await message.answer("Навигация:", reply_markup=kb)

# --- Клавиатуры ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton("Текущая доступность")],
        [types.KeyboardButton("Под заказ"), types.KeyboardButton("Найти")],
    ], resize_keyboard=True
)

start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton("✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton("🕒 Под заказ", callback_data="menu:order")],
    [types.InlineKeyboardButton("🔎 Найти", callback_data="menu:search")],
])

# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Текущая доступность")
async def cmd_available(message: types.Message):
    df = load_catalog()
    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()
    if available.empty:
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        return
    items = available.to_dict("records")
    await send_category_page(message, items=items)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    WAITING_ORDER_QUERY.add(message.chat.id)
    await message.answer("Введите поисковый запрос для IKEA (название или категория):")

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

@dp.message(F.text)
async def handle_text(message: types.Message):
    if message.chat.id in WAITING_ORDER_QUERY:
        WAITING_ORDER_QUERY.remove(message.chat.id)
        query = message.text.strip()
        await message.answer(f"Ищу на IKEA: {query}…")
        results = fetch_ikea(query)
        if not results:
            await message.answer("Ничего не найдено на IKEA 😔")
            return
        await send_category_page(message, items=results)
        return

# --- Inline навигация ---
@dp.callback_query(lambda c: c.data and c.data.startswith("page:"))
async def callback_page(call: types.CallbackQuery):
    try:
        page = int(call.data.split(":")[1])
        await call.answer()
        # Для упрощения, можно сохранять last search в глобальную переменную, но для demo оставим Excel
        await send_category_page(call.message, page=page)
    except Exception as e:
        await call.answer("Ошибка навигации", show_alert=True)

# --- Меню ---
@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":")[1]
    if action == "available":
        await cmd_available(call.message)
    elif action == "order":
        await cmd_under_order(call.message)
    elif action == "search":
        await cmd_search_stub(call.message)
    await call.answer()

# --- Запуск ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())