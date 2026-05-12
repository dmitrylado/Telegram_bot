import re
import time
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")  # токен бота
CHANNEL_ID = os.getenv("CHANNEL_ID")  # для публикации, например "@channelusername"

bot = Bot(token=TOKEN)
dp = Dispatcher()

CACHE = {}
CACHE_TTL = 60 * 30  # 30 минут кэш
PAGE_SIZE = 10

# FSM для поиска
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

class SearchStates(StatesGroup):
    waiting_for_query = State()


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def parse_ikea_search(query: str):
    """
    Парсим IKEA (название, цену, фото) по поисковому запросу.
    Возвращает список dict {'title', 'price', 'photo'}
    """
    url = f"https://www.ikea.com/lt/ru/search/?q={query}"
    now = time.time()
    if url in CACHE and (now - CACHE[url][0]) < CACHE_TTL:
        return CACHE[url][1]

    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    # Основные карточки товаров на IKEA
    for item in soup.select("div[data-testid='product-card']"):
        title_el = item.select_one("span[data-testid='product-card__name']")
        price_el = item.select_one("span[data-testid='product-price__integer']")  # цена без валюты
        photo_el = item.select_one("img[data-testid='product-card__image']")

        if title_el:
            title = title_el.get_text(strip=True)
        else:
            title = "Без названия"

        if price_el:
            price = price_el.get_text(strip=True) + " руб."
        else:
            price = "—"

        if photo_el and photo_el.has_attr("src"):
            photo = photo_el["src"]
        else:
            photo = None

        results.append({
            "title": title,
            "price": price,
            "photo": photo
        })

    CACHE[url] = (now, results)
    return results


def build_nav_keyboard(page: int, total_pages: int) -> types.InlineKeyboardMarkup:
    kb = []
    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"searchpage:{page-1}"))
    row.append(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    if page < total_pages - 1:
        row.append(types.InlineKeyboardButton("Далее ➡️", callback_data=f"searchpage:{page+1}"))
    kb.append(row)
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# --- Главное меню ---
main_kb = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton("Текущая доступность")],
        [types.KeyboardButton("Под заказ"), types.KeyboardButton("Найти")],
    ],
    resize_keyboard=True
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


@dp.message(F.text == "Под заказ")
async def cmd_order(message: types.Message, state: FSMContext):
    await message.answer("Введите поисковый запрос для товаров IKEA:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(SearchStates.waiting_for_query)


@dp.message(SearchStates.waiting_for_query)
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Пустой запрос. Попробуйте ещё раз.")
        return

    await state.update_data(query=query)
    results = parse_ikea_search(query)
    if not results:
        await message.answer("По вашему запросу ничего не найдено 😔", reply_markup=main_kb)
        await state.clear()
        return

    # Сохраняем результаты в состоянии для постраничной навигации
    await state.update_data(results=results, page=0)

    # Показываем первую страницу
    await send_search_page(message, state, page=0)


async def send_search_page(message_or_call, state: FSMContext, page: int):
    data = await state.get_data()
    results = data.get("results", [])
    total = len(results)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_items = results[start_idx:end_idx]

    if not page_items:
        await message_or_call.answer("Больше товаров нет ✅", reply_markup=main_kb)
        await state.clear()
        return

    for item in page_items:
        caption = f"**{item['title']}**\nЦена: {item['price']}"
        if item['photo']:
            await message_or_call.answer_photo(item['photo'], caption=caption, parse_mode="Markdown")
        else:
            await message_or_call.answer(caption, parse_mode="Markdown")

    kb = build_nav_keyboard(page, total_pages)
    await message_or_call.answer("Навигация:", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("searchpage:"))
async def search_page_callback(call: types.CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[1])
    await call.answer()
    await send_search_page(call.message, state, page)


@dp.callback_query(lambda c: c.data.startswith("menu:"))
async def menu_callback(call: types.CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "start":
        await call.message.answer("Выберите действие:", reply_markup=start_inline_kb)
    elif action == "order":
        await call.message.answer("Введите поисковый запрос для товаров IKEA:", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(SearchStates.waiting_for_query)
    elif action == "available":
        await call.message.answer("Раздел «Текущая доступность» пока заглушка 🙂", reply_markup=main_kb)
    elif action == "search":
        await call.message.answer("Раздел «Найти» пока заглушка 🙂", reply_markup=main_kb)


@dp.message(F.text == "Найти")
async def cmd_find_stub(message: types.Message):
    await message.answer("Раздел «Найти» пока в разработке 🙂", reply_markup=main_kb)


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())