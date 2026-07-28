import asyncio
import os
import logging
from datetime import datetime
from typing import Optional, Set, Dict, Any, List, Union

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    WebAppInfo,
    TelegramObject,
    InputMediaPhoto,
    FSInputFile,
    URLInputFile
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

# Канал для публикаций
CHANNEL_ID = os.getenv("CHANNEL_ID", "@zakazat_sayt_dlya_shkoly")

# Группа для заявок
GROUP_ID = os.getenv("GROUP_ID", "@zakazatsaytdlyashkoly")

# Сайт
SITE_URL = os.getenv("SITE_URL", "https://www.metaimperiya.com/")

# Админы (личные ID для доступа к админке)
ADMIN_IDS: Set[int] = set()
raw_admin_ids = os.getenv("ADMIN_ID", "")
if raw_admin_ids:
    for part in raw_admin_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))
    if ADMIN_IDS:
        logger.info(f"✅ Админы: {ADMIN_IDS}")
    else:
        logger.warning("⚠️ ADMIN_ID не содержит корректных ID")

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = os.getenv("DB_NAME", "bot_database.db")

class Database:
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_name)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA busy_timeout = 5000;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._init_db()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def _init_db(self):
        async with self._conn.cursor() as cursor:
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    name TEXT,
                    service TEXT,
                    contact TEXT,
                    comment TEXT,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'new'
                )
            """)
            
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    text TEXT,
                    photo_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await self._conn.commit()
            logger.info("✅ База данных готова")

    async def save_user(self, telegram_id: int, username: str, first_name: str, last_name: str = ""):
        await self._conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_active = CURRENT_TIMESTAMP
            """,
            (telegram_id, username[:50], first_name[:50], last_name[:50]),
        )
        await self._conn.commit()

    async def save_order(self, telegram_id: int, name: str, service: str, 
                         contact: str, comment: str, username: str) -> int:
        cursor = await self._conn.execute(
            """
            INSERT INTO orders (telegram_id, name, service, contact, comment, username)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, name[:100], service[:200], contact[:200], comment[:500], username[:50]),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def save_post(self, admin_id: int, text: str, photo_id: Optional[str] = None) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO posts (admin_id, text, photo_id) VALUES (?, ?, ?)",
            (admin_id, text, photo_id)
        )
        await self._conn.commit()
        return cursor.lastrowid

db = Database()

# ==================== MIDDLEWARE ====================
class UserTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                await db.save_user(
                    telegram_id=user.id,
                    username=user.username or "",
                    first_name=user.first_name or "",
                    last_name=user.last_name or "",
                )
            except Exception as e:
                logger.error(f"Ошибка сохранения пользователя: {e}")
        return await handler(event, data)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
dp.update.outer_middleware(UserTrackingMiddleware())

# ==================== ДАННЫЕ УСЛУГ ====================
SERVICES = {
    "album": {
        "emoji": "🎓",
        "name": "Выпускной альбом / Класс",
        "price": "от 3 000 грн / 8 000 руб.",
        "description": "• Живые фото и видео\n• Персональная страница каждого ученика\n• Онлайн-таймер до выпускного",
        "photo": "https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=800&q=80",
    },
    "primary": {
        "emoji": "🎒",
        "name": "Сайт для 1-4 классов",
        "price": "от 2 500 грн / 6 500 руб.",
        "description": "• Расписание уроков и объявлений\n• Фотоотчеты с мероприятий и экскурсий\n• Удобный доступ для родителей",
        "photo": "https://images.unsplash.com/photo-1509062522246-3755977927d7?auto=format&fit=crop&w=800&q=80",
    },
    "school": {
        "emoji": "🏫",
        "name": "Официальный сайт школы",
        "price": "от 8 000 грн / 20 000 руб.",
        "description": "• Полное соответствие стандартам\n• Разделы: Документы, Педсостав, Новости\n• Высокая защита и быстродействие",
        "photo": "https://images.unsplash.com/photo-1580582932707-520aed937b7b?auto=format&fit=crop&w=800&q=80",
    },
    "portfolio": {
        "emoji": "🏆",
        "name": "Портфолио ученика / Учителя",
        "price": "от 1 500 грн / 4 000 руб.",
        "description": "• Для аттестации учителя или поступления ученика\n• Галерея грамот, проектов и достижений\n• Презентабельный вид на любых устройствах",
        "photo": "https://images.unsplash.com/photo-1434030216411-0b793f4b4173?auto=format&fit=crop&w=800&q=80",
    }
}

# ==================== СОСТОЯНИЯ FSM ====================
class PostStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_text = State()
    waiting_for_buttons = State()
    waiting_for_confirmation = State()

class OrderStates(StatesGroup):
    service = State()
    name = State()
    contact = State()
    comment = State()

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎓 Выпускной альбом / Класс")],
            [KeyboardButton(text="🎒 Сайт для 1-4 классов")],
            [KeyboardButton(text="🏫 Официальный сайт школы")],
            [KeyboardButton(text="🏆 Портфолио ученика / Учителя")],
            [KeyboardButton(text="📱 Наш сайт"), KeyboardButton(text="📞 Заявка")],
            [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="💼 Вакансии")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите услугу..."
    )

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True
    )

def get_post_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="post_publish")],
        [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="post_edit_text")],
        [InlineKeyboardButton(text="🔄 Изменить фото", callback_data="post_edit_photo")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="post_cancel")]
    ])

# ==================== ФУНКЦИЯ ПРОВЕРКИ АДМИНА ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ==================== ПУБЛИКАЦИЯ В КАНАЛ (POST MAKER) ====================
@dp.message(Command("post"))
async def start_post_creation(message: Message, state: FSMContext):
    """Начало создания поста"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен! Эта команда только для администраторов.")
        return

    await state.set_state(PostStates.waiting_for_photo)
    await message.answer(
        "📝 <b>Создание поста для канала</b>\n\n"
        "Отправьте <b>фотографию</b> для поста или нажмите /skip, если пост будет без фото.\n\n"
        "🔄 Чтобы отменить создание поста, нажмите /cancel"
    )

@dp.message(Command("cancel"), StateFilter(PostStates))
async def cancel_post(message: Message, state: FSMContext):
    """Отмена создания поста"""
    await state.clear()
    await message.answer(
        "❌ Создание поста отменено.",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("skip"), PostStates.waiting_for_photo)
async def skip_photo(message: Message, state: FSMContext):
    """Пропуск фото"""
    await state.update_data(photo=None)
    await state.set_state(PostStates.waiting_for_text)
    await message.answer(
        "✏️ Теперь введите <b>текст поста</b>.\n\n"
        "Поддерживается HTML-разметка:\n"
        "• <b>жирный</b>\n"
        "• <i>курсив</i>\n"
        "• <a href='url'>ссылка</a>"
    )

@dp.message(PostStates.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    """Обработка фото"""
    photo_id = message.photo[-1].file_id
    await state.update_data(photo=photo_id)
    await state.set_state(PostStates.waiting_for_text)
    await message.answer(
        "✅ Фото принято!\n\n"
        "✏️ Теперь введите <b>текст поста</b>.\n\n"
        "Поддерживается HTML-разметка:\n"
        "• <b>жирный</b>\n"
        "• <i>курсив</i>\n"
        "• <a href='url'>ссылка</a>"
    )

@dp.message(PostStates.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    """Обработка текста"""
    await state.update_data(text=message.text)
    await state.set_state(PostStates.waiting_for_buttons)
    
    example = (
        "🔘 <b>Добавьте кнопки для поста</b>\n\n"
        "Отправьте кнопки в формате:\n"
        "<code>Текст кнопки - https://ссылка.com</code>\n\n"
        "Пример:\n"
        "<code>Заказать сайт - https://t.me/zakazatsaytdlyashkoly_bot?start=order</code>\n"
        "<code>Наш сайт - https://www.metaimperiya.com/</code>\n\n"
        "📌 Если кнопки не нужны, нажмите /skip"
    )
    await message.answer(example)

@dp.message(Command("skip"), PostStates.waiting_for_buttons)
async def skip_buttons(message: Message, state: FSMContext):
    """Пропуск кнопок"""
    await state.update_data(buttons=[])
    await show_post_preview(message, state)

@dp.message(PostStates.waiting_for_buttons)
async def process_buttons(message: Message, state: FSMContext):
    """Обработка кнопок"""
    lines = message.text.strip().split("\n")
    buttons = []
    
    for line in lines:
        if " - " in line:
            parts = line.split(" - ", 1)
            btn_text = parts[0].strip()
            btn_url = parts[1].strip()
            
            # Проверяем URL
            if btn_url.startswith(("http://", "https://", "tg://")):
                buttons.append([InlineKeyboardButton(text=btn_text, url=btn_url)])
                logger.info(f"✅ Кнопка: {btn_text} -> {btn_url}")
            else:
                await message.answer(f"⚠️ Неверный формат ссылки: {btn_url}\nСсылка должна начинаться с http://, https:// или tg://")
                return
    
    if not buttons:
        await message.answer("⚠️ Не найдено валидных кнопок. Попробуйте еще раз или нажмите /skip")
        return
    
    await state.update_data(buttons=buttons)
    await show_post_preview(message, state)

async def show_post_preview(message: Message, state: FSMContext):
    """Показать превью поста перед публикацией"""
    data = await state.get_data()
    text = data.get("text", "")
    photo = data.get("photo")
    buttons = data.get("buttons", [])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    preview_text = (
        "📋 <b>Превью поста</b>\n\n"
        f"📝 <b>Текст:</b>\n{text[:500]}{'...' if len(text) > 500 else ''}\n\n"
        f"🖼 <b>Фото:</b> {'✅ Есть' if photo else '❌ Нет'}\n"
        f"🔘 <b>Кнопки:</b> {len(buttons)} шт.\n\n"
        "👇 Проверьте, как будет выглядеть пост, и нажмите 'Опубликовать'"
    )
    
    try:
        if photo:
            await message.answer_photo(
                photo=photo,
                caption=preview_text,
                reply_markup=get_post_confirm_keyboard()
            )
        else:
            await message.answer(
                preview_text,
                reply_markup=get_post_confirm_keyboard()
            )
        
        await state.set_state(PostStates.waiting_for_confirmation)
        
    except Exception as e:
        logger.error(f"Ошибка создания превью: {e}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(F.data == "post_publish", PostStates.waiting_for_confirmation)
async def publish_post(callback: CallbackQuery, state: FSMContext):
    """Публикация поста в канал"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Доступ запрещен!")
        return
    
    data = await state.get_data()
    text = data.get("text", "")
    photo = data.get("photo")
    buttons = data.get("buttons", [])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    try:
        # Публикуем в канал
        if photo:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo,
                caption=text,
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=keyboard
            )
        
        # Сохраняем в БД
        await db.save_post(
            admin_id=callback.from_user.id,
            text=text,
            photo_id=photo
        )
        
        # Отправляем уведомление в группу
        await bot.send_message(
            chat_id=GROUP_ID,
            text=f"📢 <b>Новый пост в канале!</b>\n\n{text[:200]}{'...' if len(text) > 200 else ''}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Перейти в канал", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
            ])
        )
        
        await callback.message.answer(
            f"✅ <b>Пост успешно опубликован в канале!</b>\n\n"
            f"📢 Канал: {CHANNEL_ID}\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

@dp.callback_query(F.data == "post_edit_text", PostStates.waiting_for_confirmation)
async def edit_post_text(callback: CallbackQuery, state: FSMContext):
    """Редактирование текста"""
    await callback.answer()
    await state.set_state(PostStates.waiting_for_text)
    await callback.message.answer("✏️ Введите новый текст поста:")

@dp.callback_query(F.data == "post_edit_photo", PostStates.waiting_for_confirmation)
async def edit_post_photo(callback: CallbackQuery, state: FSMContext):
    """Изменение фото"""
    await callback.answer()
    await state.set_state(PostStates.waiting_for_photo)
    await callback.message.answer("📸 Отправьте новое фото или нажмите /skip чтобы удалить фото:")

@dp.callback_query(F.data == "post_cancel", PostStates.waiting_for_confirmation)
async def cancel_publish(callback: CallbackQuery, state: FSMContext):
    """Отмена публикации"""
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ Публикация отменена.", reply_markup=get_main_keyboard())

# ==================== ОФОРМЛЕНИЕ ЗАЯВОК ====================
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    """Стартовая команда"""
    await state.clear()
    
    welcome_text = (
        f"👋 <b>Салам, {message.from_user.first_name}!</b>\n\n"
        "Добро пожаловать в <b>MetaImperiya</b>! 🚀\n"
        "Мы создаем современные веб-сайты и цифровые продукты для образования.\n\n"
        "📌 Выберите интересующую услугу в меню ниже:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📞 Заявка")
async def start_order(message: Message, state: FSMContext):
    """Начало оформления заявки"""
    await state.set_state(OrderStates.service)
    await message.answer(
        "📝 <b>Оформление заявки</b>\n\n"
        "Напишите, какой проект вас интересует:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(F.text == "❌ Отменить", StateFilter(OrderStates))
async def cancel_order(message: Message, state: FSMContext):
    """Отмена заявки"""
    await state.clear()
    await message.answer(
        "❌ Заявка отменена.",
        reply_markup=get_main_keyboard()
    )

@dp.message(OrderStates.service)
async def process_service(message: Message, state: FSMContext):
    """Обработка услуги"""
    await state.update_data(service=message.text)
    await state.set_state(OrderStates.name)
    await message.answer(
        "👤 Как к вам обращаться?",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderStates.name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени"""
    await state.update_data(name=message.text)
    await state.set_state(OrderStates.contact)
    await message.answer(
        "📱 Укажите ваш телефон или Telegram (@username) для связи:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderStates.contact)
async def process_contact(message: Message, state: FSMContext):
    """Обработка контакта"""
    await state.update_data(contact=message.text)
    await state.set_state(OrderStates.comment)
    await message.answer(
        "💬 Дополнительный комментарий (необязательно):\n"
        "Нажмите /skip чтобы пропустить",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(Command("skip"), OrderStates.comment)
async def skip_comment(message: Message, state: FSMContext):
    """Пропуск комментария"""
    await state.update_data(comment="Нет")
    await finish_order(message, state)

@dp.message(OrderStates.comment)
async def process_comment(message: Message, state: FSMContext):
    """Обработка комментария"""
    await state.update_data(comment=message.text)
    await finish_order(message, state)

async def finish_order(message: Message, state: FSMContext):
    """Завершение оформления заявки"""
    data = await state.get_data()
    
    # Сохраняем в БД
    try:
        order_id = await db.save_order(
            telegram_id=message.from_user.id,
            name=data.get('name', 'Не указано'),
            service=data.get('service', 'Не указано'),
            contact=data.get('contact', 'Не указано'),
            comment=data.get('comment', 'Нет'),
            username=message.from_user.username or "нет_юзернейма"
        )
        logger.info(f"✅ Заявка #{order_id} создана")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")
        await message.answer("❌ Ошибка при сохранении заявки")
        await state.clear()
        return
    
    # Формируем сообщение для группы
    order_text = (
        "🚀 <b>НОВАЯ ЗАЯВКА!</b>\n"
        f"🆔 <b>№:</b> {order_id}\n"
        f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👤 <b>Клиент:</b> {data.get('name')}\n"
        f"🛠 <b>Услуга:</b> {data.get('service')}\n"
        f"📞 <b>Контакт:</b> {data.get('contact')}\n"
        f"💬 <b>Комментарий:</b> {data.get('comment')}\n"
        f"🔗 <b>Юзернейм:</b> @{message.from_user.username or 'нет'}\n"
        f"🆔 <b>ID:</b> <code>{message.from_user.id}</code>"
    )
    
    # Отправляем в группу
    try:
        await bot.send_message(
            chat_id=GROUP_ID,
            text=order_text
        )
        logger.info(f"📨 Заявка отправлена в группу {GROUP_ID}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в группу: {e}")
    
    # Отправляем админам в личку
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=order_text
            )
        except Exception as e:
            logger.error(f"❌ Ошибка отправки админу {admin_id}: {e}")
    
    # Ответ пользователю
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на сайт", url=SITE_URL)],
        [InlineKeyboardButton(text="📱 Наш канал", url=f"https://t.me/{CHANNEL_ID.replace('@', '')}")]
    ])
    
    await message.answer(
        "✅ <b>Заявка принята!</b>\n\n"
        "Мы свяжемся с вами в ближайшее время.\n"
        "А пока можете посмотреть наши работы:",
        reply_markup=keyboard
    )
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())
    
    await state.clear()

# ==================== ОБРАБОТЧИКИ УСЛУГ ====================
@dp.message(F.text.in_([f"{data['emoji']} {data['name']}" for data in SERVICES.values()]))
async def show_service_card(message: Message):
    """Показать карточку услуги"""
    service_key = None
    for key, value in SERVICES.items():
        if f"{value['emoji']} {value['name']}" == message.text:
            service_key = key
            break
    
    if not service_key:
        return
    
    service = SERVICES[service_key]
    
    caption = (
        f"{service['emoji']} <b>{service['name']}</b>\n\n"
        f"{service['description']}\n\n"
        f"💰 <b>Цена:</b> {service['price']}"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Подробнее на сайте", url=SITE_URL)],
        [InlineKeyboardButton(text="✍️ Заказать", callback_data=f"order_{service_key}")],
        [InlineKeyboardButton(text="📞 Связаться", callback_data="contact_manager")]
    ])
    
    await message.answer_photo(
        photo=service['photo'],
        caption=caption,
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("order_"))
async def order_from_callback(callback: CallbackQuery, state: FSMContext):
    """Заказ через callback"""
    await callback.answer()
    
    service_key = callback.data.replace("order_", "")
    service = SERVICES.get(service_key)
    
    if not service:
        await callback.message.answer("❌ Услуга не найдена")
        return
    
    await state.update_data(service=service['name'])
    await state.set_state(OrderStates.name)
    
    await callback.message.answer(
        f"✅ Вы выбрали: <b>{service['name']}</b>\n\n"
        "Теперь укажите ваше имя:",
        reply_markup=get_cancel_keyboard()
    )

@dp.callback_query(F.data == "contact_manager")
async def contact_manager(callback: CallbackQuery):
    """Связаться с менеджером"""
    await callback.answer()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать", url="https://t.me/metaimperiya_support")]
    ])
    await callback.message.answer(
        "📞 <b>Связаться с менеджером</b>\n\n"
        "Наш менеджер ответит на все ваши вопросы:",
        reply_markup=keyboard
    )

# ==================== АДМИН-КОМАНДЫ ====================
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    """Админ-панель"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Создать пост", callback_data="admin_post")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="admin_orders")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")]
    ])
    
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        f"📢 Канал: {CHANNEL_ID}\n"
        f"💬 Группа: {GROUP_ID}\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "admin_post")
async def admin_post_callback(callback: CallbackQuery, state: FSMContext):
    """Создание поста через админку"""
    await callback.answer()
    await start_post_creation(callback.message, state)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    """Статистика"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return
    
    # Здесь можно добавить реальную статистику из БД
    await callback.message.answer(
        "📊 <b>Статистика</b>\n\n"
        "👥 Пользователей: данные из БД\n"
        "📝 Заявок: данные из БД"
    )

@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    """Список заявок"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return
    
    # Здесь можно получить заявки из БД
    await callback.message.answer(
        "📋 <b>Последние заявки</b>\n\n"
        "Данные будут загружены из БД..."
    )

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(callback: CallbackQuery):
    """Рассылка"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return
    
    await callback.message.answer(
        "📨 <b>Рассылка</b>\n\n"
        "Функция в разработке..."
    )

# ==================== ВЕБ-СЕРВЕР ====================
class WebServer:
    def __init__(self):
        self.app = web.Application()
        self.runner = None
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_ping)
        self.app.router.add_get("/health", self.handle_health)

    async def handle_ping(self, request):
        return web.Response(text="OK", status=200)

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "channel": CHANNEL_ID,
            "group": GROUP_ID
        })

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Веб-сервер запущен на порту {port}")

    async def stop(self):
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()

web_server = WebServer()

# ==================== ЗАПУСК ====================
async def main():
    try:
        logger.info("🚀 Запуск бота...")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"💬 Группа: {GROUP_ID}")
        
        await db.connect()
        await web_server.start()
        await bot.delete_webhook(drop_pending_updates=True)
        
        logger.info("🤖 Бот готов к работе!")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⚠️ Остановка...")
    finally:
        await web_server.stop()
        await db.close()
        await bot.session.close()
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())
