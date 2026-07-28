import asyncio
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    WebAppInfo,
    TelegramObject
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError(
        "❌ Переменная окружения BOT_TOKEN не установлена! "
        "Проверьте файл .env или настройки хостинга."
    )

ADMIN_ID = os.getenv("ADMIN_ID")
if not ADMIN_ID:
    logger.warning("⚠️ ADMIN_ID не установлен. Админ-команды будут недоступны.")

SITE_URL = os.getenv("SITE_URL", "https://www.metaimperiya.com/")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-webapp.com")
DB_NAME = os.getenv("DB_NAME", "bot_database.db")

# ==================== БАЗА ДАННЫХ (АСИНХРОННАЯ) ====================
class Database:
    """Асинхронный класс для работы с SQLite через aiosqlite"""
    
    def __init__(self, db_name: str = DB_NAME):
        self.db_name = db_name
    
    async def init_db(self):
        """Инициализация таблиц базы данных"""
        async with aiosqlite.connect(self.db_name) as conn:
            # Включаем поддержку внешних ключей
            await conn.execute("PRAGMA foreign_keys = ON")
            
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица заявок
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    name TEXT,
                    service TEXT,
                    contact TEXT,
                    comment TEXT,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'new',
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                )
            """)
            
            # Индексы для оптимизации
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_created_at 
                ON orders(created_at DESC)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_status 
                ON orders(status)
            """)
            
            await conn.commit()
            logger.info("✅ База данных инициализирована")
    
    async def save_user(
        self,
        telegram_id: int,
        username: str,
        first_name: str,
        last_name: str = "",
    ):
        """Сохранение или обновление данных пользователя"""
        async with aiosqlite.connect(self.db_name) as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO users 
                (telegram_id, username, first_name, last_name, last_active)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (telegram_id, username[:50], first_name[:50], last_name[:50]),
            )
            await conn.commit()
    
    async def save_order(
        self,
        telegram_id: int,
        name: str,
        service: str,
        contact: str,
        comment: str,
        username: str,
    ) -> int:
        """Сохранение новой заявки"""
        async with aiosqlite.connect(self.db_name) as conn:
            cursor = await conn.execute(
                """
                INSERT INTO orders 
                (telegram_id, name, service, contact, comment, username)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, name[:100], service[:200], contact[:200], comment[:500], username[:50]),
            )
            await conn.commit()
            return cursor.lastrowid
    
    async def get_stats(self) -> Dict[str, int]:
        """Получение статистики"""
        async with aiosqlite.connect(self.db_name) as conn:
            # Общее количество пользователей
            async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
                total_users = (await cursor.fetchone())[0]
            
            # Заявок за сегодня
            async with conn.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE DATE(created_at) = DATE('now', 'localtime')
            """) as cursor:
                today_orders = (await cursor.fetchone())[0]
            
            # Всего заявок
            async with conn.execute("SELECT COUNT(*) FROM orders") as cursor:
                total_orders = (await cursor.fetchone())[0]
            
            # Новых заявок
            async with conn.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE status = 'new'
            """) as cursor:
                new_orders = (await cursor.fetchone())[0]
            
            return {
                "total_users": total_users,
                "today_orders": today_orders,
                "total_orders": total_orders,
                "new_orders": new_orders,
            }
    
    async def get_recent_orders(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Получение последних заявок"""
        async with aiosqlite.connect(self.db_name) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT id, name, service, contact, username, created_at, status
                FROM orders
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def update_order_status(self, order_id: int, status: str):
        """Обновление статуса заявки"""
        async with aiosqlite.connect(self.db_name) as conn:
            await conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (status, order_id)
            )
            await conn.commit()

db = Database()

# ==================== MIDDLEWARE ДЛЯ ОТСЛЕЖИВАНИЯ ПОЛЬЗОВАТЕЛЕЙ ====================
class UserTrackingMiddleware(BaseMiddleware):
    """Middleware для автоматического сохранения пользователей"""
    
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user:
            try:
                await db.save_user(
                    telegram_id=user.id,
                    username=user.username or "",
                    first_name=user.first_name or "",
                    last_name=user.last_name or "",
                )
            except Exception as e:
                logger.error(f"Ошибка сохранения пользователя {user.id}: {e}")
        
        return await handler(event, data)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())

# Регистрируем middleware
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

# ==================== МАШИНА СОСТОЯНИЙ ====================
class OrderForm(StatesGroup):
    service = State()
    name = State()
    contact = State()
    comment = State()

class AdminStates(StatesGroup):
    mailing = State()

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
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

def get_service_keyboard(service_key: str) -> InlineKeyboardMarkup:
    """Клавиатура для карточки услуги"""
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Подробнее на сайте", url=SITE_URL)
    builder.button(text="✍️ Заказать", callback_data=f"order_{service_key}")
    builder.button(text="📞 Связаться", callback_data="contact_manager")
    builder.adjust(1, 2)
    return builder.as_markup()

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить заявку")]],
        resize_keyboard=True
    )

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 <b>Салам, {first_name}!</b>\n\n"
        "Добро пожаловать в <b>MetaImperiya</b>! 🚀\n"
        "Мы создаем современные веб-сайты и цифровые продукты для образования.\n\n"
        "📌 Выберите интересующую услугу в меню ниже или воспользуйтесь кнопками:"
    ).format(first_name=message.from_user.first_name)
    
    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "❓ <b>Помощь по боту</b>\n\n"
        "📌 <b>Доступные команды:</b>\n"
        "/start - Главное меню\n"
        "/help - Эта справка\n"
        "/services - Все услуги\n"
        "/contacts - Контакты\n\n"
        "📌 <b>Как оставить заявку:</b>\n"
        "1. Выберите услугу из меню\n"
        "2. Нажмите кнопку 'Заказать'\n"
        "3. Заполните форму\n\n"
        "По всем вопросам пишите @metaimperiya_support"
    )
    await message.answer(help_text, reply_markup=get_main_keyboard())

@dp.message(Command("services"))
async def services_cmd(message: Message):
    """Список всех услуг"""
    text = "📋 <b>Наши услуги:</b>\n\n"
    for key, service in SERVICES.items():
        text += f"{service['emoji']} <b>{service['name']}</b>\n   💰 {service['price']}\n\n"
    
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(Command("contacts"))
async def contacts_cmd(message: Message):
    """Контакты"""
    contacts_text = (
        "📱 <b>Наши контакты:</b>\n\n"
        "🌐 Сайт: metaimperiya.com\n"
        "📧 Email: info@metaimperiya.com\n"
        "📞 Телефон: +380 XX XXX XXXX\n"
        "💬 Telegram: @metaimperiya_support\n\n"
        "🕐 Работаем: Пн-Пт 9:00-20:00"
    )
    await message.answer(contacts_text, reply_markup=get_main_keyboard())

# ==================== ОБРАБОТЧИКИ СООБЩЕНИЙ ====================
@dp.message(F.text == "📱 Наш сайт")
async def open_site(message: Message):
    """Кнопка перехода на сайт"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Открыть MetaImperiya.com", url=SITE_URL)],
        [InlineKeyboardButton(text="📱 Открыть в Web App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        "🌐 Наши проекты, портфолио и свежие релизы ждут вас на сайте!",
        reply_markup=keyboard
    )

@dp.message(F.text == "📞 Заявка")
async def start_order(message: Message, state: FSMContext):
    """Начать оформление заявки"""
    await state.set_state(OrderForm.service)
    await message.answer(
        "📝 <b>Оформление заявки</b>\n\n"
        "Напишите, какой проект вас интересует, или выберите из списка услуг выше.",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(F.text == "❓ Помощь")
async def show_help(message: Message):
    """Кнопка помощи"""
    await help_cmd(message)

@dp.message(F.text == "💼 Вакансии")
async def vacancies(message: Message):
    """Вакансии"""
    vacancies_text = (
        "💼 <b>Мы ищем таланты!</b>\n\n"
        "Открыты вакансии:\n"
        "• Frontend-разработчик (React/Vue)\n"
        "• Backend-разработчик (Python/Django)\n"
        "• UI/UX Дизайнер\n"
        "• Менеджер по продажам\n\n"
        "Присылайте резюме: career@metaimperiya.com"
    )
    await message.answer(vacancies_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "❌ Отменить заявку")
async def cancel_order(message: Message, state: FSMContext):
    """Отмена заявки"""
    await state.clear()
    await message.answer(
        "❌ Заявка отменена. Если передумаете - мы всегда на связи!",
        reply_markup=get_main_keyboard()
    )

# ==================== ОБРАБОТЧИКИ УСЛУГ ====================
@dp.message(F.text.in_([f"{data['emoji']} {data['name']}" for data in SERVICES.values()]))
async def show_service_card(message: Message):
    """Показать карточку услуги"""
    service_key = None
    for key, data in SERVICES.items():
        if f"{data['emoji']} {data['name']}" == message.text:
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
    
    await message.answer_photo(
        photo=service['photo'],
        caption=caption,
        reply_markup=get_service_keyboard(service_key)
    )

# ==================== CALLBACK ОБРАБОТЧИКИ ====================
@dp.callback_query(F.data.startswith("order_"))
async def process_order_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка заказа через callback"""
    service_key = callback.data.replace("order_", "")
    service = SERVICES.get(service_key)
    
    if not service:
        await callback.answer("❌ Услуга не найдена", show_alert=True)
        return
    
    await state.update_data(service=service['name'])
    await state.set_state(OrderForm.name)
    
    # Отвечаем на callback
    await callback.answer(f"✅ Вы выбрали: {service['name']}")
    
    # Отправляем новое сообщение, не удаляя карточку
    await callback.message.answer(
        f"✅ Вы выбрали: <b>{service['name']}</b>\n\n"
        "Теперь укажите ваше имя:",
        reply_markup=get_cancel_keyboard()
    )

@dp.callback_query(F.data == "contact_manager")
async def contact_manager(callback: CallbackQuery):
    """Связаться с менеджером"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Позвонить", url="tel:+380XXXXXXXXX")],
        [InlineKeyboardButton(text="💬 Написать", url="https://t.me/metaimperiya_support")]
    ])
    await callback.message.answer(
        "📞 <b>Связаться с менеджером</b>\n\n"
        "Выберите удобный способ связи:",
        reply_markup=keyboard
    )
    await callback.answer()

# ==================== FSM ОБРАБОТЧИКИ ====================
@dp.message(OrderForm.service)
async def process_service(message: Message, state: FSMContext):
    """Обработка выбора услуги"""
    if message.text == "❌ Отменить заявку":
        await cancel_order(message, state)
        return
    
    await state.update_data(service=message.text)
    await state.set_state(OrderForm.name)
    await message.answer(
        "👤 Как к вам обращаться? (Введите ваше имя)",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderForm.name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени"""
    if message.text == "❌ Отменить заявку":
        await cancel_order(message, state)
        return
    
    await state.update_data(name=message.text)
    await state.set_state(OrderForm.contact)
    await message.answer(
        "📱 Укажите ваш телефон или Telegram (@username) для связи:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderForm.contact)
async def process_contact(message: Message, state: FSMContext):
    """Обработка контакта"""
    if message.text == "❌ Отменить заявку":
        await cancel_order(message, state)
        return
    
    await state.update_data(contact=message.text)
    await state.set_state(OrderForm.comment)
    await message.answer(
        "💬 Дополнительный комментарий (необязательно):\n"
        "Можете написать любые пожелания или нажмите /skip",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(Command("skip"))
async def skip_comment(message: Message, state: FSMContext):
    """Пропустить комментарий"""
    await state.update_data(comment="Нет")
    await finish_order(message, state)

@dp.message(OrderForm.comment)
async def process_comment(message: Message, state: FSMContext):
    """Обработка комментария"""
    if message.text == "❌ Отменить заявку":
        await cancel_order(message, state)
        return
    
    await state.update_data(comment=message.text)
    await finish_order(message, state)

async def finish_order(message: Message, state: FSMContext):
    """Завершение оформления заявки"""
    user_data = await state.get_data()
    
    # Сохраняем в базу
    try:
        order_id = await db.save_order(
            telegram_id=message.from_user.id,
            name=user_data.get('name', 'Не указано'),
            service=user_data.get('service', 'Не указано'),
            contact=user_data.get('contact', 'Не указано'),
            comment=user_data.get('comment', 'Нет'),
            username=message.from_user.username or "нет_юзернейма"
        )
        logger.info(f"Заявка #{order_id} создана пользователем {message.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка сохранения заявки: {e}")
        await message.answer("❌ Произошла ошибка при сохранении заявки. Попробуйте позже.")
        await state.clear()
        return
    
    # Формируем сообщение для админа
    order_text = (
        "🚀 <b>НОВАЯ ЗАЯВКА!</b>\n"
        f"🆔 <b>№:</b> {order_id}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👤 <b>Имя:</b> {user_data.get('name', 'Не указано')}\n"
        f"🛠 <b>Услуга:</b> {user_data.get('service', 'Не указано')}\n"
        f"📞 <b>Контакт:</b> {user_data.get('contact', 'Не указано')}\n"
        f"💬 <b>Комментарий:</b> {user_data.get('comment', 'Нет')}\n"
        f"🔗 <b>Юзернейм:</b> @{message.from_user.username or 'нет_юзернейма'}\n"
        f"🆔 <b>ID:</b> {message.from_user.id}"
    )
    
    # Отправляем админу
    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=order_text)
            logger.info(f"Заявка #{order_id} отправлена админу")
        except Exception as e:
            logger.error(f"Ошибка отправки админу: {e}")
    
    # Ответ пользователю
    success_text = (
        "✅ <b>Заявка принята!</b>\n\n"
        "Мы уже обрабатываем вашу заявку и свяжемся с вами в ближайшее время.\n\n"
        "⏱ Обычно мы отвечаем в течение 15-30 минут в рабочее время.\n\n"
        "А пока вы можете посмотреть наши работы на сайте:"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на MetaImperiya.com", url=SITE_URL)],
        [InlineKeyboardButton(text="📱 Наш Instagram", url="https://instagram.com/metaimperiya")]
    ])
    
    await message.answer(success_text, reply_markup=keyboard)
    await message.answer(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )
    
    await state.clear()

# ==================== АДМИН-КОМАНДЫ ====================
def is_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_ID

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    """Админ-панель"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="📋 Последние заявки", callback_data="admin_orders")],
        [InlineKeyboardButton(text="✅ Обновить статус", callback_data="admin_update_status")],
        [InlineKeyboardButton(text="⏹ Остановить бота", callback_data="admin_stop")]
    ])
    
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    """Статистика бота"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    try:
        stats = await db.get_stats()
        
        stats_text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
            f"📝 Заявок сегодня: <b>{stats['today_orders']}</b>\n"
            f"📋 Заявок всего: <b>{stats['total_orders']}</b>\n"
            f"🆕 Новых заявок: <b>{stats['new_orders']}</b>"
        )
        
        await callback.message.answer(stats_text)
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await callback.message.answer("❌ Ошибка получения статистики")
    
    await callback.answer()

@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    """Последние заявки"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    try:
        orders = await db.get_recent_orders(10)
        
        if not orders:
            await callback.message.answer("📭 Заявок пока нет")
            await callback.answer()
            return
        
        text = "📋 <b>Последние заявки:</b>\n\n"
        for order in orders:
            status_emoji = "🆕" if order['status'] == 'new' else "✅"
            text += (
                f"#{order['id']} {status_emoji} | {order['name']}\n"
                f"  📌 {order['service'][:30]}\n"
                f"  📞 {order['contact'][:30]}\n"
                f"  🕐 {order['created_at'][:16]}\n"
                "  ---\n"
            )
        
        await callback.message.answer(text)
    except Exception as e:
        logger.error(f"Ошибка получения заявок: {e}")
        await callback.message.answer("❌ Ошибка получения заявок")
    
    await callback.answer()

@dp.callback_query(F.data == "admin_update_status")
async def admin_update_status(callback: CallbackQuery):
    """Обновление статуса заявки"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 В работе", callback_data="status_working")],
        [InlineKeyboardButton(text="✅ Выполнено", callback_data="status_done")],
        [InlineKeyboardButton(text="❌ Отклонено", callback_data="status_rejected")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    
    await callback.message.answer(
        "📋 <b>Обновление статуса заявки</b>\n\n"
        "Введите ID заявки, а затем выберите новый статус:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("status_"))
async def process_status_update(callback: CallbackQuery):
    """Обработка обновления статуса"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    # Здесь нужно добавить логику для выбора заявки по ID
    # Для простоты примера - заглушка
    await callback.message.answer(
        "ℹ️ Введите ID заявки через /update_status <id>"
    )
    await callback.answer()

@dp.message(Command("update_status"))
async def update_status_cmd(message: Message):
    """Команда обновления статуса заявки"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "❌ Использование: /update_status <id> <статус>\n"
            "Статусы: working, done, rejected"
        )
        return
    
    try:
        order_id = int(args[1])
        status = args[2]
        
        if status not in ["working", "done", "rejected"]:
            await message.answer("❌ Неверный статус. Доступны: working, done, rejected")
            return
        
        await db.update_order_status(order_id, status)
        await message.answer(f"✅ Статус заявки #{order_id} обновлен на '{status}'")
    except ValueError:
        await message.answer("❌ ID заявки должен быть числом")
    except Exception as e:
        logger.error(f"Ошибка обновления статуса: {e}")
        await message.answer("❌ Ошибка обновления статуса")

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    """Возврат в админ-панель"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    await admin_panel(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(callback: CallbackQuery, state: FSMContext):
    """Начать рассылку"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    await state.set_state(AdminStates.mailing)
    await callback.message.answer(
        "📨 <b>Рассылка</b>\n\n"
        "Введите текст для рассылки (можно с HTML-разметкой):\n"
        "Для отмены введите /cancel"
    )
    await callback.answer()

@dp.message(AdminStates.mailing)
async def process_mailing(message: Message, state: FSMContext):
    """Обработка рассылки"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена", reply_markup=get_main_keyboard())
        return
    
    # Здесь нужно получить список пользователей из БД и отправить
    await message.answer(
        "✅ Рассылка запущена! (В реальном боте тут будет отправка сообщений)"
    )
    await state.clear()

@dp.callback_query(F.data == "admin_stop")
async def admin_stop(callback: CallbackQuery):
    """Остановка бота (корректная)"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    await callback.answer("⏹ Бот останавливается...", show_alert=True)
    logger.warning("⚠️ Бот остановлен админом")
    await callback.message.answer("⏹ Бот остановлен")
    
    # Корректное завершение через остановку диспетчера
    await dp.stop_polling()

# ==================== ВЕБ-СЕРВЕР ====================
class WebServer:
    """Асинхронный веб-сервер для health check"""
    
    def __init__(self):
        self.app = web.Application()
        self.runner = None
        self.setup_routes()
    
    def setup_routes(self):
        self.app.router.add_get("/", self.handle_ping)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/webhook", self.handle_webhook)
    
    async def handle_ping(self, request):
        """Проверка доступности"""
        return web.Response(text="OK", status=200)
    
    async def handle_health(self, request):
        """Health check с детальной информацией"""
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "bot_running": True,
            "database": "connected"
        })
    
    async def handle_webhook(self, request):
        """Обработка webhook (опционально)"""
        try:
            data = await request.json()
            logger.info(f"Webhook received: {data}")
            return web.json_response({"status": "received"})
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.json_response({"status": "error"}, status=400)
    
    async def start(self):
        """Запуск веб-сервера"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Веб-сервер запущен на порту {port}")
    
    async def stop(self):
        """Остановка веб-сервера"""
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()
            logger.info("🌐 Веб-сервер остановлен")

web_server = WebServer()

# ==================== ЗАПУСК ====================
async def main():
    """Главная функция запуска"""
    try:
        logger.info("🚀 Запуск бота...")
        
        # Инициализация базы данных
        await db.init_db()
        logger.info("📊 База данных готова")
        
        # Запуск веб-сервера
        await web_server.start()
        
        # Запуск поллинга
        logger.info("🤖 Бот готов к работе!")
        await dp.start_polling(bot)
        
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        raise
    finally:
        # Корректное завершение
        await web_server.stop()
        await bot.session.close()
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот завершен пользователем")
