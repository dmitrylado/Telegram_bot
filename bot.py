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
CHANNEL_ID = os.getenv("CHANNEL_ID")  # используем только для публикации, если нужно

CATALOG_PATH = "catalog.xlsx"
bot = Bot(token=TOKEN)
dp = Dispatcher()

PAGE_SIZE = 10
CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут

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
    except:
        return f"{s} руб."

# --- Excel: Текущая доступность ---
def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def find_in_file(df: pd.DataFrame, query: str, limit: int = 5):
    q = normalize(query)
    df2 = df.copy()
    df2["__text"] = (df2["title"].astype(str) + " " + df2.get("sku", "").astype(str)).map(normalize)
    hits = df2[df2["__text"].str.contains(q, na=False)].head(limit)
    return hits.to_dict("records")

def format_caption_from_excel(item: dict) -> str:
    title = item.get("title", "(без названия)")
    desc = item.get("description", "")
    price_str = format_rub_price(item.get("price"))
    parts = [f"<b>{title}</b>"]
    if desc:
        parts.append(f"<i>{desc}</i>")
    if price_str:
        parts.append(f"<b>Цена:</b> {price_str}")
    return "\n".join(parts)

# --- Главные клавиатуры ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🔎 Найти", callback_data="menu:search")],
])

# ============================
# Поиск IKEA
# ============================
def fetch_ikea_search(query: str) -> list[dict]:
    """Возвращает список товаров с IKEA по запросу query."""
    url = f"https://www.ikea.com/lt/ru/search/?q={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    
    products = []
    for item in soup.select("[data-testid='product-card']"):
        title_el = item.select_one(".product-compact__name")
        price_el = item.select_one(".product-compact__price")
        img_el = item.select_one("img")
        
        if not title_el:
            continue
        
        title = title_el.get_text(strip=True)
        price = price_el.get_text(strip=True) if price_el else ""
        photo = img_el["src"] if img_el and img_el.has_attr("src") else None
        
        products.append({
            "title": title,
            "price": price,
            "photo": photo
        })
    return products

def format_caption_from_ikea(item: dict) -> str:
    title = item.get("title", "(без названия)")
    price = item.get("price", "")
    parts = [f"<b>{title}</b>"]
    if price:
        parts.append(f"<b>Цена:</b> {price}")
    return "\n".join(parts)

async def send_ikea_page(message: types.Message, products: list, page: int):
    total = len(products)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = products[start:end]

    if not page_items:
        await message.answer("Больше товаров нет.", reply_markup=main_kb)
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await message.answer(f"Результаты поиска IKEA\nСтраница {page+1}/{total_pages} (позиции {start+1}-{min(end,total)})", reply_markup=main_kb)
    
    for item in page_items:
        caption = format_caption_from_ikea(item)
        if item.get("photo"):
            try:
                await message.answer_photo(item["photo"], caption=caption, parse_mode="HTML")
            except:
                await message.answer(caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    # Кнопки навигации
    nav_row = []
    if page > 0:
        nav_row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"ikeapage:{page-1}"))
    if end < total:
        nav_row.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"ikeapage:{page+1}"))
    
    if nav_row:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_row])
        await message.answer("Навигация:", reply_markup=kb)

# ============================
# Команды и колбэки
# ============================

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
    for _, row in available.head(10).iterrows():
        caption = format_caption_from_excel(row.to_dict())
        if row.get("photo"):
            await message.answer_photo(row["photo"], caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

# ============================
# Поиск IKEA: через кнопку
# ============================
IKEASEARCH_CACHE = {}  # храним результаты между страницами

@dp.message(F.text == "Найти")
async def cmd_search(message: types.Message):
    await message.answer("Напишите поисковый запрос для IKEA:", reply_markup=main_kb)

@dp.message()
async def text_search(message: types.Message):
    query = message.text.strip()
    if not query:
        return
    # запускаем поиск IKEA
    try:
        products = fetch_ikea_search(query)
        if not products:
            await message.answer("Ничего не найдено на IKEA.", reply_markup=main_kb)
            return
        IKEASEARCH_CACHE[message.from_user.id] = products
        await send_ikea_page(message, products, page=0)
    except Exception as e:
        await message.answer(f"Ошибка при поиске IKEA: {e}", reply_markup=main_kb)

@dp.callback_query(lambda c: c.data.startswith("ikeapage:"))
async def ikea_page_callback(call: types.CallbackQuery):
    try:
        page = int(call.data.split(":",1)[1])
        products = IKEASEARCH_CACHE.get(call.from_user.id, [])
        await call.answer()
        await send_ikea_page(call.message, products, page=page)
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())