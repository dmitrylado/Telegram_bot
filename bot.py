import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
import asyncio
import os
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

logging.basicConfig(level=logging.INFO)

# --- Настройки ---
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # @username или chat_id вида -100...

CATALOG_PATH = "catalog.xlsx"
PAGE_SIZE = 10
CACHE_TTL = 60 * 30

# --- Инициализация бота ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}  # Кэш сайта
CATEGORY_MAP = {}  # id -> категория
SEARCH_CACHE = {}  # Кэш поиска IKEA
SEARCH_PAGE_SIZE = 10

# --- FSM для поиска ---
class SearchState(StatesGroup):
    waiting_for_query = State()

# --- Вспомогательные функции ---
def safe_int(x):
    try:
        return int(x)
    except Exception:
        return 0

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def format_rub_price(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return ""
    s = s.replace(" ", "").replace(",", ".")
    try:
        num = float(s)
        return f"{int(round(num))} руб."
    except:
        return f"{s} руб."

def load_catalog():
    return pd.read_excel(CATALOG_PATH)

def find_in_file(df: pd.DataFrame, query: str, limit: int = 5):
    q = normalize(query)
    df2 = df.copy()
    df2["__text"] = (df2["title"].astype(str) + " " + df2.get("sku", "").astype(str)).map(normalize)
    hits = df2[df2["__text"].str.contains(q, na=False)].head(limit)
    return hits.to_dict("records")

def fetch_site_data(url: str) -> dict:
    if not url:
        return {}
    now = time.time()
    if url in CACHE and (now - CACHE[url][0]) < CACHE_TTL:
        return CACHE[url][1]
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    price_el = soup.select_one("[data-testid='price'], .price, .product-price")
    title_el = soup.select_one("h1")
    data = {
        "site_price": price_el.get_text(strip=True) if price_el else None,
        "site_title": title_el.get_text(strip=True) if title_el else None,
    }
    CACHE[url] = (now, data)
    return data

def format_card(item: dict, site_data: dict) -> str:
    title = site_data.get("site_title") or item.get("title", "(без названия)")
    lines = [f"**{title}**"]
    if item.get("sku"):
        lines.append(f"Артикул: `{item['sku']}`")
    if item.get("price") is not None:
        lines.append(f"Цена: {item['price']}")
    if item.get("stock") is not None:
        lines.append(f"Наличие: {item['stock']}")
    if site_data.get("site_price"):
        lines.append(f"Цена на сайте: {site_data['site_price']}")
    if item.get("url"):
        lines.append(f"[Ссылка]({item['url']})")
    return "\n".join(lines)

# --- Меню ---
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

# --- Работа с категориями ---
def build_category_keyboard(df_available: pd.DataFrame) -> types.InlineKeyboardMarkup:
    global CATEGORY_MAP
    CATEGORY_MAP = {}
    if "category_1" not in df_available.columns:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")]
        ])
    categories = df_available["category_1"].astype(str).map(str.strip)
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
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
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
        await message.answer("Больше товаров нет ✅", reply_markup=main_kb)
        return
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await message.answer(
        f"✅ В наличии — {cat_title}\nСтраница {page+1}/{total_pages} (позиции {start+1}-{min(end, total)} из {total})",
        reply_markup=main_kb
    )
    for _, row in page_df.iterrows():
        item = row.to_dict()
        text = format_card(item, {})
        if item.get("photo"):
            try:
                await message.answer_photo(item["photo"], caption=text, parse_mode="Markdown")
            except:
                await message.answer(text, parse_mode="Markdown")
        else:
            await message.answer(text, parse_mode="Markdown")
    nav_rows = []
    if page > 0:
        nav_rows.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"catpage:{code}:{page-1}"))
    nav_rows.append(types.InlineKeyboardButton("📂 Категории", callback_data="menu:cats"))
    if end < total:
        nav_rows.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"catpage:{code}:{page+1}"))
    if nav_rows:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_rows])
        await message.answer("Навигация:", reply_markup=kb)

# --- Команды / старт ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    await message.answer("Раздел «Под заказ» пока в разработке 🙂", reply_markup=main_kb)

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message, state: FSMContext):
    await message.answer("Напишите, что ищем на сайте IKEA:")
    await state.set_state(SearchState.waiting_for_query)

@dp.message(F.text == "Текущая доступность")
async def cmd_available(message: types.Message):
    df = load_catalog()
    if "stock" not in df.columns:
        await message.answer("В файле нет колонки stock. Проверьте catalog.xlsx", reply_markup=main_kb)
        return
    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()
    if available.empty:
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        return
    kb = build_category_keyboard(available)
    await message.answer(f"В наличии позиций: {len(available)}.\nВыберите категорию:", reply_markup=kb)

# --- Парсинг IKEA ---
def fetch_ikea_search(query: str):
    if query in SEARCH_CACHE:
        return SEARCH_CACHE[query]
    url = f"https://www.ikea.com/lt/ru/search/?q={query.replace(' ', '%20')}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    products = soup.select("[data-testid='product-pip']")
    for p in products:
        title_el = p.select_one("[data-testid='product-pip__title']")
        price_el = p.select_one("[data-testid='product-pip__price']")
        img_el = p.select_one("img")
        title = title_el.get_text(strip=True) if title_el else "Без названия"
        price = price_el.get_text(strip=True) if price_el else ""
        photo = img_el['src'] if img_el else None
        results.append({"title": title, "price": price, "photo": photo})
    SEARCH_CACHE[query] = results
    return results

async def send_search_page(message: types.Message, query: str, page: int = 0):
    results = fetch_ikea_search(query)
    total = len(results)
    if total == 0:
        await message.answer("По вашему запросу ничего не найдено 😔")
        return
    start = page * SEARCH_PAGE_SIZE
    end = start + SEARCH_PAGE_SIZE
    page_items = results[start:end]
    for item in page_items:
        caption = f"**{item['title']}**\nЦена: {item['price']}"
        if item['photo']:
            await message.answer_photo(item['photo'], caption=caption, parse_mode="Markdown")
        else:
            await message.answer(caption, parse_mode="Markdown")
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"searchpage:{query}:{page-1}"))
    if end < total:
        nav_buttons.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"searchpage:{query}:{page+1}"))
    nav_buttons.append(types.InlineKeyboardButton("⬅️ В меню", callback_data="menu:start"))
    kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
    await message.answer("Навигация:", reply_markup=kb)

# --- Callback menu ---
@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    if action == "available":
        await call.message.answer("Показываю товары в наличии…")
        await cmd_available(call.message)
    elif action == "order":
        await call.message.answer("Раздел «Под заказ» пока в разработке 🙂", reply_markup=main_kb)
    elif action == "search":
        await call.message.answer("Напишите, что ищем на сайте IKEA:")
        await state.set_state(SearchState.waiting_for_query)
    await call.answer()

# --- Callback категории и страницы ---
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
    except:
        await call.answer("Ошибка навигации", show_alert=True)
        return
    await call.answer()
    await send_category_page(call.message, code=code, page=page)

@dp.callback_query(lambda c: c.data and c.data.startswith("searchpage:"))
async def search_page_callback(call: types.CallbackQuery):
    try:
        _, query, page_str = call.data.split(":", 2)
        page = int(page_str)
    except:
        await call.answer("Ошибка навигации", show_alert=True)
        return
    await call.answer()
    await send_search_page(call.message, query=query, page=page)

@dp.callback_query(lambda c: c.data == "menu:start")
async def back_to_menu(call: types.CallbackQuery):
    await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)
    await call.answer()

# --- FSM обработка поиска ---
@dp.message(SearchState.waiting_for_query)
async def process_search_query(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Пустой запрос. Попробуйте снова.")
        return
    await state.clear()
    await send_search_page(message, query, page=0)

# --- Запуск бота ---
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())