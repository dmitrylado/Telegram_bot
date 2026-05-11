import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup

# re — чтобы нормализовать текст (убрать лишние пробелы, привести к lower).
# time — для кэша (храним когда последний раз ходили на сайт).
# requests — скачать HTML страницы товара с сайта.
# pandas, openpyxl (не импортируется напрямую) — читать Excel и фильтровать строки.
# BeautifulSoup — “распарсить” HTML и вытащить цену/заголовок.
# aiogram — каркас Telegram-бота (команды, сообщения, кнопки, callbacks).
# asyncio — запуск асинхронного бота.

import logging
logging.basicConfig(level=logging.INFO)

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
import asyncio

import os

TOKEN = os.getenv("BOT_TOKEN")  # теперь токен берётся из окружения


CATALOG_PATH = "catalog.xlsx"
# Куда постить (можно @username_канала или chat_id вида -100...)
CHANNEL_ID = "@Ikea2Home"

bot = Bot(token=TOKEN)
dp = Dispatcher()
# bot — объект, который умеет отправлять/получать сообщения.
# dp (Dispatcher) — принимает входящие события и направляет их в нужные обработчики.




CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут кэш сайта
# CACHE хранит результаты парсинга сайта по URL, чтобы не ходить на сайт каждый раз.
# CACHE_TTL — сколько секунд хранить (30 минут).

# --- Категории: id -> название (нужно для callback cat:1, cat:2 и т.д.) ---
CATEGORY_MAP: dict[str, str] = {}

PAGE_SIZE = 10

def load_catalog():
    return pd.read_excel(CATALOG_PATH)
# Читает Excel в таблицу (DataFrame). Ожидаются колонки title, sku, price, stock, url, photo.

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


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

    # ВНИМАНИЕ: эти селекторы нужно будет подстроить под ваш сайт
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

# --- Клавиатура главного меню ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Под заказ"), types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

# --- Inline-меню (кнопки прямо в сообщении) ---
start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🕒 Под заказ (скоро)", callback_data="menu:order")],
    [types.InlineKeyboardButton(text="🔎 Найти (скоро)", callback_data="menu:search")],
])

def safe_int(x):
    try:
        return int(x)
    except Exception:
        return 0
def format_rub_price(value) -> str:
    """
    Приводит цену к целым рублям и добавляет 'руб.'
    Примеры: 199.0 -> '199 руб.', '199,5' -> '200 руб.', '199' -> '199 руб.'
    """
    if value is None:
        return ""
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return ""

    # поддержка запятой как десятичного разделителя
    s = s.replace(" ", "").replace(",", ".")
    try:
        num = float(s)
        rub = int(round(num))  # округляем до целых
        return f"{rub} руб."
    except Exception:
        # если не число — вернём как есть, но добавим руб.
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
    """
    Создаёт inline-кнопки категорий по колонке category_1.
    df_available должен быть уже отфильтрован по stock > 0.
    """
    global CATEGORY_MAP
    CATEGORY_MAP = {}

    # Если вдруг колонки нет — просто покажем кнопку "Все категории"
    if "category_1" not in df_available.columns:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")]
        ])

    # Уникальные категории (без пустых и NaN)
    categories = (
        df_available["category_1"]
        .astype(str)
        .map(lambda x: x.strip())
    )
    categories = [c for c in categories.unique().tolist() if c and c.lower() != "nan"]
    categories.sort()

    keyboard: list[list[types.InlineKeyboardButton]] = []
    keyboard.append([types.InlineKeyboardButton(text="📦 Все категории", callback_data="cat:ALL")])

    # Кнопки категорий по 2 в ряд
    row: list[types.InlineKeyboardButton] = []
    for i, cat in enumerate(categories, start=1):
        cat_id = str(i)            # короткий id, чтобы callback_data не был длинным
        CATEGORY_MAP[cat_id] = cat # сохраняем расшифровку id -> текст категории

        row.append(types.InlineKeyboardButton(text=cat, callback_data=f"cat:{cat_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # (опционально) кнопка назад в меню
    keyboard.append([types.InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:start")])

    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)

async def send_category_page(message: types.Message, code: str, page: int):
    """
    Показывает товары выбранной категории постранично.
    code: "ALL" или id категории (строка)
    page: 0,1,2...
    """
    df = load_catalog()

    if "stock" not in df.columns:
        await message.answer("В файле нет колонки stock.")
        return

    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()

    if available.empty:
        await message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        return

    # определяем категорию
    if code == "ALL":
        filtered = available
        cat_title = "Все категории"
    else:
        cat_name = CATEGORY_MAP.get(code)
        if not cat_name:
            await message.answer("Категория устарела. Нажмите «Текущая доступность» ещё раз.")
            return
        if "category_1" not in available.columns:
            await message.answer("В файле нет колонки category_1.")
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

    # Заголовок страницы
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    await message.answer(
        f"✅ В наличии — {cat_title}\n"
        f"Страница {page+1}/{total_pages} (позиции {start+1}-{min(end, total)} из {total})",
        reply_markup=main_kb
    )

    # Отправляем карточки (10 штук максимум)
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

    # Кнопки пагинации
    nav_rows = []

    # Назад (по желанию)
    if page > 0:
        nav_rows.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catpage:{code}:{page-1}"))

    # Кнопка меню всегда
    nav_rows.append(types.InlineKeyboardButton(text="📂 Категории", callback_data="menu:cats"))

    # Далее (если есть ещё)
    if end < total:
        nav_rows.append(types.InlineKeyboardButton(text="Далее ➡️", callback_data=f"catpage:{code}:{page+1}"))

    if nav_rows:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[nav_rows])
        await message.answer("Навигация:", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "menu:cats")
async def back_to_categories(call: types.CallbackQuery):
    df = load_catalog()

    if "stock" not in df.columns:
        await call.message.answer("В файле нет колонки stock.")
        await call.answer()
        return

    df["__stock_int"] = df["stock"].apply(safe_int)
    available = df[df["__stock_int"] > 0].copy()

    if available.empty:
        await call.message.answer("Сейчас нет доступных позиций 😔", reply_markup=main_kb)
        await call.answer()
        return

    kb = build_category_keyboard(available)
    await call.message.answer(
        f"В наличии позиций: {len(available)}.\nВыберите категорию:",
        reply_markup=kb
    )
    await call.answer()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Выберите действие:",
        reply_markup=start_inline_kb
    )

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    await message.answer("Раздел «Под заказ» пока в разработке 🙂", reply_markup=main_kb)

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

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
    await message.answer(
        f"В наличии позиций: {len(available)}.\nВыберите категорию:",
        reply_markup=kb
    )

@dp.message(Command("find"))
async def cmd_find(message: types.Message):
    query = message.text.replace("/find", "", 1).strip()
    if not query:
        await message.answer("Напишите: `/find запрос` например `/find parkla`", parse_mode="Markdown")
        return

    df = load_catalog()
    results = find_in_file(df, query)
    if not results:
        await message.answer("Ничего не нашёл в файле.")
        return

    for item in results:
        site_data = {}
        if item.get("url"):
            try:
                site_data = fetch_site_data(item["url"])
            except Exception:
                site_data = {}

        text = format_card(item, site_data)
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="Опубликовать в канал", callback_data=f"post:{item.get('sku','')}")]
        ])

        if item.get("photo"):
            await message.answer_photo(item["photo"], caption=text, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(lambda c: c.data and c.data.startswith("post:"))
async def post_callback(call: types.CallbackQuery):
    sku = call.data.split(":", 1)[1]

    df = load_catalog()
    row = df[df["sku"].astype(str) == str(sku)]
    if row.empty:
        await call.message.answer("Не нашёл SKU в файле.")
        await call.answer()
        return

    item = row.iloc[0].to_dict()

    site_data = {}
    if item.get("url"):
        try:
            site_data = fetch_site_data(item["url"])
        except Exception:
            site_data = {}

    text = format_card(item, site_data)

    if item.get("photo"):
        await bot.send_photo(CHANNEL_ID, item["photo"], caption=text, parse_mode="Markdown")
    else:
        await bot.send_message(CHANNEL_ID, text, parse_mode="Markdown")

    await call.message.answer("Опубликовано ✅")
    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":", 1)[1]

    if action == "available":
        # Запускаем вашу существующую логику показа доступных товаров
        await call.message.answer("Показываю товары в наличии…")
        await cmd_available(call.message)

    elif action == "order":
        await call.message.answer("Раздел «Под заказ» пока в разработке 🙂", reply_markup=main_kb)

    elif action == "search":
        await call.message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

    await call.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("cat:"))
async def category_callback(call: types.CallbackQuery):
    code = call.data.split(":", 1)[1]  # ALL или id
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

@dp.callback_query(lambda c: c.data == "menu:start")
async def back_to_menu(call: types.CallbackQuery):
    await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)
    await call.answer()


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
