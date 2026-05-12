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
CHANNEL_ID = os.getenv("CHANNEL_ID")  # можно оставить для публикации

CATALOG_PATH = "catalog.xlsx"
PAGE_SIZE = 10

bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут кэш сайта

# --- Клавиатура главного меню ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Под заказ"), types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

# --- Inline-меню ---
start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🛒 Под заказ", callback_data="menu:order")],
    [types.InlineKeyboardButton(text="🔎 Найти", callback_data="menu:search")],
])

CATEGORY_MAP = {}  # id -> название категории

# --- UTILS ---
def safe_int(x):
    try:
        return int(x)
    except:
        return 0

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def format_rub_price(value) -> str:
    if value is None:
        return ""
    s = str(value).replace(" ", "").replace(",", ".")
    try:
        num = float(s)
        rub = int(round(num))
        return f"{rub} руб."
    except:
        return f"{s} руб."

def load_catalog() -> pd.DataFrame:
    return pd.read_excel(CATALOG_PATH)

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

# --- CATALOG PAGE ---
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
    keyboard = []
    keyboard.append([types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")])
    row = []
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

async def send_category_page(message: types.Message, code: str, page: int, items: list[dict]):
    total = len(items)
    if total == 0:
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        return
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await message.answer(
        f"Страница {page+1}/{total_pages} (позиции {start+1}-{min(end, total)} из {total})",
        reply_markup=main_kb
    )
    for item in page_items:
        caption = item.get("caption")
        photo = item.get("photo")
        if photo:
            try:
                await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML")
            except:
                await message.answer(caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")
    nav_rows = []
    if page > 0:
        nav_rows.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catpage:{code}:{page-1}"))
    nav_rows.append(types.InlineKeyboardButton(text="📂 Меню категорий", callback_data="menu:cats"))
    if end < total:
        nav_rows.append(types.InlineKeyboardButton(text="Далее ➡️", callback_data=f"catpage:{code}:{page+1}"))
    if nav_rows:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_rows])
        await message.answer("Навигация:", reply_markup=kb)

# --- PARSER IKEA ---
def fetch_ikea(query: str) -> list[dict]:
    """
    Парсит IKEA LT RU.
    query может быть на английском или русском.
    Возвращает список: [{'title','price','photo','url'}]
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    search_url = f"https://www.ikea.com/lt/ru/search/?q={query.replace(' ','%20')}"
    try:
        r = requests.get(search_url, headers=headers, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logging.error(f"IKEA fetch error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    # Товары ищем по карточкам
    items = soup.select(".product-compact")
    for item in items:
        title_el = item.select_one(".product-compact__name")
        price_el = item.select_one(".product-compact__price")
        photo_el = item.select_one("img")
        link_el = item.select_one("a.product-compact__spacer")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "price": price_el.get_text(strip=True) if price_el else "",
            "photo": photo_el.get("src") if photo_el else None,
            "url": f"https://www.ikea.com{link_el.get('href')}" if link_el else search_url,
            "caption": f"<b>{title_el.get_text(strip=True)}</b>\nЦена: {price_el.get_text(strip=True) if price_el else ''}\n<a href='{f'https://www.ikea.com{link_el.get('href')}' if link_el else search_url}'>Ссылка на товар</a>"
        })
    return results

# --- HANDLERS ---
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
    await message.answer("Введите название товара или категорию для поиска на IKEA:")

    @dp.message()
    async def ikea_search_query(msg: types.Message):
        query = msg.text.strip()
        results = fetch_ikea(query)
        if not results:
            await msg.answer("Ничего не найдено на IKEA 😔", reply_markup=main_kb)
            return
        await send_category_page(msg, code="order", page=0, items=results)
        dp.message_handlers.unregister(ikea_search_query)  # снимаем этот временный handler

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

# --- CALLBACKS ---
@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":", 1)[1]
    if action == "available":
        await cmd_available(call.message)
    elif action == "order":
        await cmd_under_order(call.message)
    elif action == "search":
        await cmd_search_stub(call.message)
    elif action == "start":
        await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("catpage:"))
async def category_page_callback(call: types.CallbackQuery):
    try:
        _, code, page_str = call.data.split(":", 2)
        page = int(page_str)
    except:
        await call.answer("Ошибка навигации", show_alert=True)
        return
    # Внутри под заказ page_items передаются напрямую
    if code == "order":
        # Повторный поиск не делаем, простая логика: пользователь снова отправит запрос
        await call.answer("Для следующей страницы повторно нажмите кнопку «Под заказ» и введите запрос.", show_alert=True)
        return

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())