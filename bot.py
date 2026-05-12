import re
import time
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, Text
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут
PAGE_SIZE = 10

# --- Главное меню ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="Текущая доступность")],
        [types.KeyboardButton(text="Под заказ"), types.KeyboardButton(text="Найти")],
    ],
    resize_keyboard=True
)

# --- Inline стартовое меню ---
start_inline_kb = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="✅ Текущая доступность", callback_data="menu:available")],
    [types.InlineKeyboardButton(text="🕒 Под заказ", callback_data="menu:order")],
    [types.InlineKeyboardButton(text="🔎 Найти", callback_data="menu:search")],
])

# --- Кеширование результатов поиска ---
SEARCH_CACHE = {}  # query -> list of items

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def fetch_ikea_search(query: str):
    """
    Возвращает список товаров с IKEA по запросу.
    Каждый элемент: {"title": ..., "price": ..., "photo": ...}
    """
    base_url = f"https://www.ikea.com/lt/ru/search/?q={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    now = time.time()
    if query in CACHE and (now - CACHE[query][0]) < CACHE_TTL:
        return CACHE[query][1]

    try:
        r = requests.get(base_url, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        products = []

        # Парсим карточки товаров
        cards = soup.select("div[data-testid='product-card'], .product-compact")
        for c in cards:
            title_el = c.select_one(".product-compact__name, [data-testid='product-card__title']")
            price_el = c.select_one(".product-compact__price, [data-testid='product-price']")
            photo_el = c.select_one("img.product-compact__image, img[data-testid='product-card__image']")

            if title_el:
                title = title_el.get_text(strip=True)
            else:
                continue  # пропускаем если нет названия

            price = price_el.get_text(strip=True) if price_el else "—"
            photo = photo_el["src"] if photo_el and photo_el.has_attr("src") else None

            products.append({"title": title, "price": price, "photo": photo})

        CACHE[query] = (now, products)
        return products
    except Exception as e:
        logging.error(f"Ошибка парсинга IKEA: {e}")
        return []

def build_nav_keyboard(query: str, page: int, total_items: int) -> types.InlineKeyboardMarkup:
    nav_rows = []
    if page > 0:
        nav_rows.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orderpage:{query}:{page-1}"))
    nav_rows.append(types.InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:start"))
    if (page+1)*PAGE_SIZE < total_items:
        nav_rows.append(types.InlineKeyboardButton(text="Далее ➡️", callback_data=f"orderpage:{query}:{page+1}"))
    return types.InlineKeyboardMarkup(inline_keyboard=[nav_rows])

async def send_order_page(message: types.Message, query: str, page: int):
    results = fetch_ikea_search(query)
    if not results:
        await message.answer("По вашему запросу ничего не найдено 😔", reply_markup=main_kb)
        return

    total = len(results)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = results[start:end]

    await message.answer(
        f"Результаты поиска: {query}\nСтраница {page+1}/{(total+PAGE_SIZE-1)//PAGE_SIZE} "
        f"(позиции {start+1}-{min(end,total)} из {total})",
        reply_markup=main_kb
    )

    for item in page_items:
        caption = f"<b>{item['title']}</b>\nЦена: {item['price']}"
        if item["photo"]:
            try:
                await message.answer_photo(photo=item["photo"], caption=caption, parse_mode="HTML")
            except Exception:
                await message.answer(caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")

    kb = build_nav_keyboard(query, page, total)
    if kb.inline_keyboard:
        await message.answer("Навигация:", reply_markup=kb)

# --- Команды / кнопки ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)

@dp.message(F.text == "Под заказ")
async def cmd_under_order(message: types.Message):
    await message.answer("Введите поисковый запрос для IKEA:", reply_markup=types.ReplyKeyboardRemove())
    # Ждем следующий ввод от пользователя
    await OrderSearch.waiting_for_query.set()

@dp.message(F.text == "Найти")
async def cmd_search_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)

# --- FSM для ввода поискового запроса ---
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class OrderSearch(StatesGroup):
    waiting_for_query = State()

@dp.message(OrderSearch.waiting_for_query)
async def process_order_query(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Введите корректный запрос")
        return

    # Сохраняем результаты в кеш для навигации
    SEARCH_CACHE[query] = fetch_ikea_search(query)

    await send_order_page(message, query, page=0)
    await state.clear()

# --- Callback пагинации ---
@dp.callback_query(lambda c: c.data and c.data.startswith("orderpage:"))
async def order_page_callback(call: types.CallbackQuery):
    try:
        _, query, page_str = call.data.split(":", 2)
        page = int(page_str)
    except Exception:
        await call.answer("Ошибка навигации", show_alert=True)
        return

    await call.answer()
    # Статического message передаем как объект call.message
    await send_order_page(call.message, query, page)

# --- Callback меню ---
@dp.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery):
    action = call.data.split(":",1)[1]
    if action == "start":
        await call.message.answer("Привет! Выберите действие:", reply_markup=start_inline_kb)
    elif action == "order":
        await call.message.answer("Введите поисковый запрос для IKEA:", reply_markup=types.ReplyKeyboardRemove())
        await OrderSearch.waiting_for_query.set()
    elif action == "search":
        await call.message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)
    elif action == "available":
        await call.message.answer("Раздел текущей доступности пока не изменён", reply_markup=main_kb)
    await call.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())