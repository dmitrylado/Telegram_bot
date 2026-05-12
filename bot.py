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
bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут кэш
PAGE_SIZE = 10
CATEGORY_MAP = {}

# ---------------------- HELPER FUNCTIONS ----------------------

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

def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def find_in_excel(query: str, limit=5):
    df = load_catalog()
    q = normalize(query)
    df2 = df.copy()
    df2["__text"] = (df2["title"].astype(str) + " " + df2.get("sku", "").astype(str)).map(normalize)
    hits = df2[df2["__text"].str.contains(q, na=False)].head(limit)
    return hits.to_dict("records")

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

# ---------------------- KEYBOARDS ----------------------

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

# ---------------------- IKEA PARSING ----------------------

def fetch_ikea(query: str):
    """Ищет товары на https://www.ikea.com/lt/ru/search/?query=..."""
    url = f"https://www.ikea.com/lt/ru/search/?query={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    scripts = soup.find_all("script")
    results = []
    for s in scripts:
        if "window.__PRELOADED_STATE__" in s.text:
            js_text = s.string
            # Простейший поиск json-like данных
            match = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.+\})\s*;', js_text)
            if match:
                import json
                data = json.loads(match.group(1))
                # Берём products
                try:
                    products = data.get("search", {}).get("products", {}).values()
                    for p in products:
                        results.append({
                            "title": p.get("name", ""),
                            "price": p.get("price", {}).get("formatted", ""),
                            "url": f"https://www.ikea.com{p.get('url', '')}" if p.get("url") else "",
                            "photo": p.get("media", [{}])[0].get("url", ""),
                        })
                except Exception:
                    pass
            break
    return results

# ---------------------- CATEGORY LOGIC ----------------------

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

async def send_category_page(message: types.Message, code: str, page: int, items: list):
    total = len(items)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = items[start:end]
    if not page_items:
        await message.answer("Больше товаров нет ✅", reply_markup=main_kb)
        return
    await message.answer(f"Страница {page+1}/{(total + PAGE_SIZE - 1)//PAGE_SIZE} — позиции {start+1}-{min(end,total)} из {total}")
    for item in page_items:
        caption = f"<b>{item['title']}</b>\nЦена: {item['price']}\n<a href='{item['url']}'>Ссылка</a>"
        photo = item.get("photo")
        if photo:
            await message.answer_photo(photo, caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")
    # навигация
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"orderpage:{page-1}"))
    if end < total:
        nav.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"orderpage:{page+1}"))
    if nav:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav])
        await message.answer("Навигация:", reply_markup=kb)

ORDER_RESULTS = []  # Глобальный кэш результатов поиска IKEA

# ---------------------- BOT HANDLERS ----------------------

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
    kb = build_category_keyboard(available)
    await message.answer(f"В наличии позиций: {len(available)}.\nВыберите категорию:", reply_markup=kb)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    await message.answer("Введите поисковый запрос для IKEA (название или категория):")

@dp.message(F.text)
async def search_order(message: types.Message):
    global ORDER_RESULTS
    if message.text in ["Текущая доступность","Под заказ","Найти"]:
        return
    query = message.text.strip()
    await message.answer(f"Ищу на IKEA: {query}…")
    ORDER_RESULTS = fetch_ikea(query)
    if not ORDER_RESULTS:
        await message.answer("Ничего не найдено на IKEA 😔")
        return
    await send_category_page(message, code="ALL", page=0, items=ORDER_RESULTS)

@dp.callback_query(lambda c: c.data and c.data.startswith("orderpage:"))
async def order_page_callback(call: types.CallbackQuery):
    global ORDER_RESULTS
    try:
        page = int(call.data.split(":")[1])
    except:
        page = 0
    await call.answer()
    await send_category_page(call.message, code="ALL", page=page, items=ORDER_RESULTS)

@dp.callback_query(lambda c: c.data == "menu:start")
async def back_to_menu(call: types.CallbackQuery):
    await call.message.answer("Выберите действие:", reply_markup=start_inline_kb)
    await call.answer()

# ---------------------- MAIN ----------------------

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())