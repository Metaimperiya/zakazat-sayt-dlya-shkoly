import asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Токен бота и твой ID в Telegram
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # Твой ID, куда будут прилетать заявки

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Состояния для формы заявки
class OrderForm(StatesGroup):
    service = State()
    name = State()
    contact = State()

# Главное меню
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎓 Выпускной альбом / Класс")],
        [KeyboardButton(text="🎒 Сайт для 1-4 классов")],
        [KeyboardButton(text="🏫 Официальный сайт школы")],
        [KeyboardButton(text="🏆 Портфолио ученика / Учителя")],
        [KeyboardButton(text="📞 Связаться / Оставить заявку")]
    ],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я помогу заказать современный сайт для школы, класса или учителя.\n"
        "Выберите нужную услугу в меню ниже 👇",
        reply_markup=main_keyboard
    )

# Обработчики кнопок услуг
@dp.message(F.text == "🎓 Выпускной альбом / Класс")
async def service1(message: Message):
    await message.answer(
        "🎓 **Интерактивный сайт для выпускников**\n\n"
        "• Онлайн-альбом с фото и видео\n"
        "• Страница каждого ученика\n"
        "• Таймер до выпускного\n"
        "• Память на всю жизнь!\n\n"
        "💰 **Цена:** от 3 000 грн / 8 000 руб.\n\n"
        "Жми «📞 Связаться / Оставить заявку» для заказа!"
    )

@dp.message(F.text == "🎒 Сайт для 1-4 классов")
async def service2(message: Message):
    await message.answer(
        "🎒 **Уютный сайт для начальных классов**\n\n"
        "• Расписание и объявления\n"
        "• Фотоотчеты с мероприятий\n"
        "• Удобная связь с родкомом и учителем\n\n"
        "💰 **Цена:** от 2 500 грн / 6 500 руб."
    )

@dp.message(F.text == "🏫 Официальный сайт школы")
async def service3(message: Message):
    await message.answer(
        "🏫 **Официальный веб-сайт школы / лицея**\n\n"
        "• Соответствие нормам и стандартам\n"
        "• Разделы: Документы, Педсостав, Новости\n"
        "• Адаптив под мобилки\n\n"
        "💰 **Цена:** от 8 000 грн / 20 000 руб."
    )

@dp.message(F.text == "🏆 Портфолио ученика / Учителя")
async def service4(message: Message):
    await message.answer(
        "🏆 **Личный сайт-портфолио**\n\n"
        "• Для аттестации учителя или поступления ученика\n"
        "• Грамоты, проекты, достижения\n\n"
        "💰 **Цена:** от 1 500 грн / 4 000 руб."
    )

# Сценарий формы заявки
@dp.message(F.text == "📞 Связаться / Оставить заявку")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.service)
    await message.answer(
        "Какая услуга вас интересует? (Например: Сайт для 11-А класса)",
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
    await message.answer("Укажите ваш телефон или Telegram для связи:")

@dp.message(OrderForm.contact)
async def process_contact(message: Message, state: FSMContext):
    user_data = await state.get_data()
    contact = message.text
    
    # Текст заявки для админа
    text_to_admin = (
        "📥 **НОВАЯ ЗАЯВКА!**\n\n"
        f"👤 **Имя:** {user_data['name']}\n"
        f"🛠 **Услуга:** {user_data['service']}\n"
        f"📞 **Контакт:** {contact}\n"
        f"🔗 **Юзер:** @{message.from_user.username or 'нет_юзернейма'}"
    )

    # Отправляем тебе в ЛС (если указан ADMIN_ID)
    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=text_to_admin)
        except Exception as e:
            print(f"Ошибка отправки админу: {e}")

    await message.answer(
        "✅ Спасибо! Заявка принята. Я свяжусь с вами в ближайшее время!",
        reply_markup=main_keyboard
    )
    await state.clear()

# Веб-сервер для удержания Render в бодрствовании (Keep-Alive)
async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_web_server()
    print("Веб-сервер и бот запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
