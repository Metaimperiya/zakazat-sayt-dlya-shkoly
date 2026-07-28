import asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Токены из переменных окружения
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

SITE_URL = "https://www.metaimperiya.com/"

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Машина состояний для сбора заявок
class OrderForm(StatesGroup):
    service = State()
    name = State()
    contact = State()

# Главное меню (кнопки внизу экрана)
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎓 Выпускной альбом / Класс")],
        [KeyboardButton(text="🎒 Сайт для 1-4 классов")],
        [KeyboardButton(text="🏫 Официальный сайт школы")],
        [KeyboardButton(text="🏆 Портфолио ученика / Учителя")],
        [KeyboardButton(text="🌐 Наш сайт MetaImperiya"), KeyboardButton(text="📞 Оставить заявку")]
    ],
    resize_keyboard=True
)

# 1. СТАРТОВАЯ КОМАНДА
@dp.message(CommandStart())
async def start_cmd(message: Message):
    inline_site = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Перейти на MetaImperiya.com", url=SITE_URL)]
    ])
    
    await message.answer(
        f"Салам, {message.from_user.first_name}! 👋\n\n"
        "Добро пожаловать в **MetaImperiya**! Мы создаем топовые веб-сайты, интерактивные альбомы и цифровые экосистемы.\n\n"
        "Выбирай услугу в меню ниже или заходи к нам на сайт 👇",
        reply_markup=main_keyboard
    )
    await message.answer("Переходи на наш официальный портал:", reply_markup=inline_site)

# 2. КНОПКА ПЕРЕХОДА НА САЙТ
@dp.message(F.text == "🌐 Наш сайт MetaImperiya")
async def open_site(message: Message):
    inline_site = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Открыть MetaImperiya.com", url=SITE_URL)]
    ])
    await message.answer(
        "Вся лента проектов, свежие релизы и портфолио ждут тебя на нашем сайте! Жми кнопку ниже 👇",
        reply_markup=inline_site
    )

# 3. КАРТОЧКА: ВЫПУСКНОЙ АЛЬБОМ
@dp.message(F.text == "🎓 Выпускной альбом / Класс")
async def service1(message: Message):
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Подробнее на сайте", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Заказать альбом", callback_data="order_album")]
    ])
    
    await message.answer_photo(
        photo="https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=800&q=80",
        caption=(
            "🎓 **ИНТЕРАКТИВНЫЙ АЛЬБОМ ДЛЯ ВЫПУСКНИКОВ**\n\n"
            "• Живые фото и видео\n"
            "• Персональная страница каждого ученика\n"
            "• Онлайн-таймер до выпускного\n\n"
            "💰 **Цена:** от 3 000 грн / 8 000 руб."
        ),
        reply_markup=inline_kb
    )

# 4. КАРТОЧКА: 1-4 КЛАССЫ
@dp.message(F.text == "🎒 Сайт для 1-4 классов")
async def service2(message: Message):
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Посмотреть на сайте", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Заказать сайт", callback_data="order_primary")]
    ])
    
    await message.answer_photo(
        photo="https://images.unsplash.com/photo-1509062522246-3755977927d7?auto=format&fit=crop&w=800&q=80",
        caption=(
            "🎒 **САЙТ ДЛЯ НАЧАЛЬНЫХ КЛАССОВ**\n\n"
            "• Расписание уроков и объявлений\n"
            "• Фотоотчеты с мероприятий и экскурсий\n"
            "• Удобный доступ для родителей\n\n"
            "💰 **Цена:** от 2 500 грн / 6 500 руб."
        ),
        reply_markup=inline_kb
    )

# 5. КАРТОЧКА: ОФИЦИАЛЬНЫЙ САЙТ ШКОЛЫ
@dp.message(F.text == "🏫 Официальный сайт школы")
async def service3(message: Message):
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на MetaImperiya.com", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Оставить заявку", callback_data="order_school")]
    ])
    
    await message.answer_photo(
        photo="https://images.unsplash.com/photo-1580582932707-520aed937b7b?auto=format&fit=crop&w=800&q=80",
        caption=(
            "🏫 **ОФИЦИАЛЬНЫЙ ВЕБ-САЙТ ШКОЛЫ / ЛИЦЕЯ**\n\n"
            "• Полное соответствие стандартам\n"
            "• Разделы: Документы, Педсостав, Новости\n"
            "• Высокая защита и быстродействия\n\n"
            "💰 **Цена:** от 8 000 грн / 20 000 руб."
        ),
        reply_markup=inline_kb
    )

# 6. КАРТОЧКА: ПОРТФОЛИО
@dp.message(F.text == "🏆 Портфолио ученика / Учителя")
async def service4(message: Message):
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Примеры на сайте", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Заказать портфолио", callback_data="order_portfolio")]
    ])
    
    await message.answer_photo(
        photo="https://images.unsplash.com/photo-1434030216411-0b793f4b4173?auto=format&fit=crop&w=800&q=80",
        caption=(
            "🏆 **ЛИЧНЫЙ САЙТ-ПОРТФОЛИО**\n\n"
            "• Для аттестации учителя или поступления ученика\n"
            "• Галерея грамот, проектов и достижений\n"
            "• Презентабельный вид на любых устройствах\n\n"
            "💰 **Цена:** от 1 500 грн / 4 000 руб."
        ),
        reply_markup=inline_kb
    )

# 7. СБОР ЗАЯВКИ (ЧЕРЕЗ КНОПКУ ИЛИ CALLBACK)
@dp.callback_query(F.data.startswith("order_"))
async def callback_order(callback: web.Request, state: FSMContext):
    await state.set_state(OrderForm.service)
    await callback.message.answer("Отлично! Напишите, какой проект вас интересует подробнее:")
    await callback.answer()

@dp.message(F.text == "📞 Оставить заявку")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.service)
    await message.answer(
        "Какая услуга вас интересует?",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(OrderForm.service)
async def process_service(message: Message, state: FSMContext):
    await state.update_data(service=message.text)
    await state.set_state(OrderForm.name)
    await message.answer("Как к вам обращаться? (Ваше имя)")

@dp.message(OrderForm.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(OrderForm.contact)
    await message.answer("Укажите ваш телефон или Telegram (@username) для связи:")

@dp.message(OrderForm.contact)
async def process_contact(message: Message, state: FSMContext):
    user_data = await state.get_data()
    contact = message.text
    
    text_to_admin = (
        "🚀 **НОВАЯ ЗАЯВКА С БОТА!**\n\n"
        f"👤 **Имя:** {user_data['name']}\n"
        f"🛠 **Услуга:** {user_data['service']}\n"
        f"📞 **Контакт:** {contact}\n"
        f"🔗 **Юзер:** @{message.from_user.username or 'нет_юзернейма'}"
    )

    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=text_to_admin)
        except Exception as e:
            print(f"Ошибка отправки админу: {e}")

    inline_site = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на MetaImperiya.com", url=SITE_URL)]
    ])

    await message.answer(
        "✅ Принято! Мы уже обрабатываем вашу заявку и скоро свяжемся с вами.",
        reply_markup=main_keyboard
    )
    await message.answer("А пока можете ознакомиться с нашими работами на сайте:", reply_markup=inline_site)
    await state.clear()

# 8. ВЕБ-СЕРВЕР ДЛЯ ПИНГА И РЕНДЕРА (С ДИНАМИЧЕСКИМ ПОРТОМ)
async def handle_ping(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_web_server()
    print("Веб-сервер запущен, начинаем polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
