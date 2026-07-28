import asyncio
import os
import logging
import signal
from datetime import datetime
from typing import Dict, List, Optional, Any, Union

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
    TelegramObject
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная окружения BOT_TOKEN не установлена!")

# Парсинг ADMIN_ID с поддержкой как цифровых ID, так и @username каналов/чатов
ADMIN_IDS: List[Union[int, str]] = []
raw_admin_ids = os.getenv("ADMIN_ID", "")

if raw_admin_ids:
    for part in raw_admin_ids.split(","):
        part = part.strip()
        if part.startswith("@"):
            # Если указан логин канала/чата (например, @METAIMPERIYA)
            ADMIN_IDS.append(part)
            logger.info(f"📢 Добавлен получатель по логину: {part}")
        elif part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            # Если указан числовой ID (для личных чатов или групп)
            ADMIN_IDS.append(int(part))
            logger.info(f"👤 Добавлен получатель по ID: {part}")
        else:
            logger.warning(f"⚠️ Неизвестный формат получателя: {part}")

if ADMIN_IDS:
    logger.info(f"✅ Получатели уведомлений загружены: {ADMIN_IDS}")
else:
    logger.warning("⚠️ ADMIN_ID не установлен. Уведомления никуда не будут отправляться.")

SITE_URL = os.getenv("SITE_URL", "https://www.metaimperiya.com/")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-webapp.com")
DB_NAME = os.getenv("DB_NAME", "bot_database.db")
RATE_LIMIT_DELAY = float(os.getenv("RATE_LIMIT_DELAY", "0.05"))

# ==================== БАЗА ДАННЫХ С ПУЛОМ СОЕДИНЕНИЙ ====================
class Database:
    """Асинхронный класс для работы с SQLite с пулом соединений"""
    
    def __init__(self, db_name: str = DB_NAME, pool_size: int = 3):
        self.db_name = db_name
        self.pool_size = pool_size
        self._pool: List[aiosqlite.Connection] = []
        self._lock = asyncio.Lock()
        self._is_connected = False

    async def connect(self):
        """Создание пула соединений"""
        async with self._lock:
            if self._is_connected:
                return
            
            for i in range(self.pool_size):
                conn = await aiosqlite.connect(self.db_name)
                conn.row_factory = aiosqlite.Row
                
                await conn.execute("PRAGMA journal_mode = WAL;")
                await conn.execute("PRAGMA busy_timeout = 10000;")
                await conn.execute("PRAGMA foreign_keys = ON;")
                await conn.execute("PRAGMA cache_size = -10000;")
                await conn.execute("PRAGMA synchronous = NORMAL;")
                
                self._pool.append(conn)
            
            await self._init_db()
            self._is_connected = True
            logger.info(f"✅ База данных подключена (пул: {self.pool_size} соединений)")

    @asynccontextmanager
    async def get_connection(self):
        """Получение соединения из пула"""
        if not self._pool:
            await self.connect()
        
        conn = self._pool.pop(0)
        try:
            yield conn
        finally:
            self._pool.append(conn)

    async def close(self):
        """Закрытие всех соединений"""
        async with self._lock:
            for conn in self._pool:
                try:
                    await conn.close()
                except Exception as e:
                    logger.error(f"Ошибка закрытия соединения: {e}")
            self._pool.clear()
            self._is_connected = False
            logger.info("✅ Все соединения с БД закрыты")

    async def _init_db(self):
        """Инициализация таблиц"""
        async with self.get_connection() as conn:
            async with conn.cursor() as cursor:
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
                
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC)")
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
                
                await conn.commit()
                logger.info("✅ Таблицы базы данных созданы/обновлены")

    async def save_user(self, telegram_id: int, username: str, first_name: str, last_name: str = ""):
        """Сохранение пользователя"""
        async with self.get_connection() as conn:
            await conn.execute(
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
            await conn.commit()

    async def save_order(self, telegram_id: int, name: str, service: str, 
                         contact: str, comment: str, username: str) -> int:
        """Сохранение заявки"""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO orders (telegram_id, name, service, contact, comment, username)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, name[:100], service[:200], contact[:200], comment[:500], username[:50]),
            )
            await conn.commit()
            return cursor.lastrowid

    async def get_all_user_ids(self) -> List[int]:
        """Получение всех пользователей для рассылки"""
        async with self.get_connection() as conn:
            async with conn.execute("SELECT telegram_id FROM users") as cursor:
                rows = await cursor.fetchall()
                return [row["telegram_id"] for row in rows]

    async def get_stats(self) -> Dict[str, int]:
        """Получение статистики"""
        async with self.get_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM users") as c:
                total_users = (await c.fetchone())[0]
            
            async with conn.execute(
                "SELECT COUNT(*) FROM orders WHERE DATE(created_at) = DATE('now', 'localtime')"
            ) as c:
                today_orders = (await c.fetchone())[0]
            
            async with conn.execute("SELECT COUNT(*) FROM orders") as c:
                total_orders = (await c.fetchone())[0]
            
            async with conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'new'") as c:
                new_orders = (await c.fetchone())[0]

        return {
            "total_users": total_users,
            "today_orders": today_orders,
            "total_orders": total_orders,
            "new_orders": new_orders,
        }

    async def get_recent_orders(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Получение последних заявок"""
        async with self.get_connection() as conn:
            async with conn.execute(
                """
                SELECT id, name, service, contact, username, created_at, status 
                FROM orders 
                ORDER BY created_at DESC 
                LIMIT ?
                """,
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def update_order_status(self, order_id: int, status: str):
        """Обновление статуса заявки"""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?", 
                (status, order_id)
            )
            await conn.commit()

db = Database()

# ==================== MIDDLEWARE ====================
class UserTrackingMiddleware(BaseMiddleware):
    """Middleware для отслеживания пользователей"""
    
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
                logger.error(f"Ошибка сохранения пользователя {user.id}: {e}")
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

# ==================== ФУНКЦИЯ ОТПРАВКИ УВЕДОМЛЕНИЙ ====================
async def send_notification_to_admins(order_text: str, order_id: int):
    """
    Отправка уведомления о новой заявке всем админам
    Поддерживает как личные ID, так и @username каналов/чатов
    """
    if not ADMIN_IDS:
        logger.warning("⚠️ Нет получателей для уведомления")
        return

    sent_count = 0
    error_count = 0

    for admin in ADMIN_IDS:
        try:
            if isinstance(admin, str) and admin.startswith("@"):
                # Отправка в канал/чат по логину
                # Для каналов нужно использовать chat_id в формате @username
                try:
                    await bot.send_message(
                        chat_id=admin,  # @METAIMPERIYA
                        text=order_text,
                        parse_mode=ParseMode.HTML
                    )
                    sent_count += 1
                    logger.info(f"📨 Уведомление отправлено в канал/чат {admin}")
                except TelegramForbiddenError:
                    logger.error(f"❌ Бот не является администратором канала {admin}! Добавьте бота в администраторы.")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки в канал {admin}: {e}")
                    error_count += 1
            else:
                # Отправка в личный чат по ID
                await bot.send_message(
                    chat_id=int(admin),
                    text=order_text,
                    parse_mode=ParseMode.HTML
                )
                sent_count += 1
                logger.info(f"📨 Уведомление отправлено админу {admin}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки получателю {admin}: {e}")
            error_count += 1
    
    return sent_count, error_count

# ==================== ГЛОБАЛЬНАЯ ОТМЕНА FSM ====================
@dp.message(F.text == "❌ Отменить заявку", StateFilter("*"))
async def cancel_order(message: Message, state: FSMContext):
    """Глобальная отмена заявки из любого состояния"""
    await state.clear()
    await message.answer(
        "❌ Заявка отменена. Если передумаете — мы всегда на связи!",
        reply_markup=get_main_keyboard()
    )

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
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

@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    """Помощь"""
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
    """Список услуг"""
    text = "📋 <b>Наши услуги:</b>\n\n"
    for service in SERVICES.values():
        text += f"{service['emoji']} <b>{service['name']}</b>\n💰 {service['price']}\n\n"
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.message(Command("contacts"))
async def contacts_cmd(message: Message):
    """Контакты"""
    contacts_text = (
        "📱 <b>Наши контакты:</b>\n\n"
        "🌐 Сайт: metaimperiya.com\n"
        "📧 Email: info@metaimperiya.com\n"
        "💬 Telegram: @metaimperiya_support\n\n"
        "🕐 Работаем: Пн-Пт 9:00-20:00"
    )
    await message.answer(contacts_text, reply_markup=get_main_keyboard())

# ==================== КНОПКИ ГЛАВНОГО МЕНЮ ====================
@dp.message(F.text == "📱 Наш сайт")
async def open_site(message: Message):
    """Переход на сайт"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Открыть MetaImperiya.com", url=SITE_URL)],
        [InlineKeyboardButton(text="📱 Открыть в Web App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer("🌐 Наши проекты и портфолио ждут вас на сайте!", reply_markup=keyboard)

@dp.message(F.text == "💼 Вакансии")
async def vacancies(message: Message):
    """Вакансии"""
    vacancies_text = (
        "💼 <b>Мы ищем таланты!</b>\n\n"
        "Открыты вакансии:\n"
        "• Frontend-разработчик (React/Vue)\n"
        "• Backend-разработчик (Python/Django)\n"
        "• UI/UX Дизайнер\n\n"
        "Присылайте резюме: career@metaimperiya.com"
    )
    await message.answer(vacancies_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📞 Заявка")
async def start_order(message: Message, state: FSMContext):
    """Начало оформления заявки"""
    await state.set_state(OrderForm.service)
    await message.answer(
        "📝 <b>Оформление заявки</b>\n\n"
        "Напишите, какой проект вас интересует, или выберите услугу из меню ниже:",
        reply_markup=get_cancel_keyboard()
    )

# ==================== FSM ОБРАБОТЧИКИ ====================
@dp.message(OrderForm.service)
async def process_service(message: Message, state: FSMContext):
    """Обработка услуги"""
    await state.update_data(service=message.text)
    await state.set_state(OrderForm.name)
    await message.answer(
        "👤 Как к вам обращаться? (Введите ваше имя)",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderForm.name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени"""
    await state.update_data(name=message.text)
    await state.set_state(OrderForm.contact)
    await message.answer(
        "📱 Укажите ваш телефон или Telegram (@username) для связи:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(OrderForm.contact)
async def process_contact(message: Message, state: FSMContext):
    """Обработка контакта"""
    await state.update_data(contact=message.text)
    await state.set_state(OrderForm.comment)
    await message.answer(
        "💬 Дополнительный комментарий (необязательно):\n"
        "Можете написать любые пожелания или нажмите /skip",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(Command("skip"), OrderForm.comment)
async def skip_comment(message: Message, state: FSMContext):
    """Пропуск комментария"""
    await state.update_data(comment="Нет")
    await finish_order(message, state)

@dp.message(OrderForm.comment)
async def process_comment(message: Message, state: FSMContext):
    """Обработка комментария"""
    await state.update_data(comment=message.text)
    await finish_order(message, state)

async def finish_order(message: Message, state: FSMContext):
    """Завершение оформления заявки"""
    user_data = await state.get_data()
    
    try:
        order_id = await db.save_order(
            telegram_id=message.from_user.id,
            name=user_data.get('name', 'Не указано'),
            service=user_data.get('service', 'Не указано'),
            contact=user_data.get('contact', 'Не указано'),
            comment=user_data.get('comment', 'Нет'),
            username=message.from_user.username or "нет_юзернейма"
        )
        logger.info(f"✅ Заявка #{order_id} создана")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения заявки: {e}")
        await message.answer(
            "❌ Произошла ошибка при сохранении. Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
        return

    # Формируем красивое сообщение для канала
    order_text = (
        "🚀 <b>НОВАЯ ЗАЯВКА!</b>\n"
        f"🆔 <b>№:</b> {order_id}\n"
        f"🕐 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"👤 <b>Клиент:</b> {user_data.get('name')}\n"
        f"🛠 <b>Услуга:</b> {user_data.get('service')}\n"
        f"📞 <b>Контакт:</b> {user_data.get('contact')}\n"
        f"💬 <b>Комментарий:</b> {user_data.get('comment')}\n"
        f"🔗 <b>Юзернейм:</b> @{message.from_user.username or 'нет'}\n"
        f"🆔 <b>ID:</b> <code>{message.from_user.id}</code>\n\n"
        f"📌 <i>Заявка отправлена через @MetaImperiyaBot</i>"
    )

    # Отправляем уведомление в канал/чат @METAIMPERIYA и админам
    sent_count, error_count = await send_notification_to_admins(order_text, order_id)
    
    if sent_count > 0:
        logger.info(f"📨 Уведомления отправлены: {sent_count} получателям")
    if error_count > 0:
        logger.warning(f"⚠️ Ошибок при отправке: {error_count}")

    # Ответ пользователю
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на MetaImperiya.com", url=SITE_URL)]
    ])
    
    await message.answer(
        "✅ <b>Заявка принята!</b>\n\n"
        "Мы свяжемся с вами в ближайшее время.\n"
        "А пока можете посмотреть наши работы на сайте:",
        reply_markup=keyboard
    )
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())
    
    await state.clear()

# ==================== КАРТОЧКИ УСЛУГ ====================
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

    await message.answer_photo(
        photo=service['photo'],
        caption=caption,
        reply_markup=get_service_keyboard(service_key)
    )

# ==================== CALLBACK ОБРАБОТЧИКИ ====================
@dp.callback_query(F.data.startswith("order_"))
async def process_order_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка заказа через callback"""
    await callback.answer()
    
    service_key = callback.data.replace("order_", "")
    service = SERVICES.get(service_key)
    
    if not service:
        await callback.message.answer("❌ Услуга не найдена")
        return

    await state.update_data(service=service['name'])
    await state.set_state(OrderForm.name)
    
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

# ==================== АДМИН-ПАНЕЛЬ ====================
def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    # Проверяем только числовые ID (личные чаты)
    return user_id in [uid for uid in ADMIN_IDS if isinstance(uid, int)]

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    """Админ-панель"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Последние заявки", callback_data="admin_orders")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="✅ Обновить статус", callback_data="admin_update_status")]
    ])
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    """Статистика"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
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

@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery):
    """Последние заявки"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return

    try:
        orders = await db.get_recent_orders(10)
        
        if not orders:
            await callback.message.answer("📭 Заявок пока нет")
            return

        text = "📋 <b>Последние заявки:</b>\n\n"
        for order in orders:
            status_emoji = "🆕" if order['status'] == 'new' else "✅"
            text += (
                f"{status_emoji} #{order['id']} | <b>{order['name']}</b>\n"
                f"  📌 {order['service'][:40]}\n"
                f"  📞 {order['contact'][:30]}\n"
                f"  🕐 {order['created_at'][:16]}\n"
                "  ---\n"
            )
        
        await callback.message.answer(text)
    except Exception as e:
        logger.error(f"Ошибка получения заявок: {e}")
        await callback.message.answer("❌ Ошибка получения заявок")

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(callback: CallbackQuery, state: FSMContext):
    """Начало рассылки"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.mailing)
    await callback.message.answer(
        "📨 <b>Рассылка</b>\n\n"
        "Введите текст для рассылки (можно с HTML-разметкой):\n"
        "Для отмены введите /cancel"
    )

@dp.message(AdminStates.mailing)
async def process_mailing(message: Message, state: FSMContext):
    """Обработка и отправка рассылки всем пользователям с rate limiting"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена", reply_markup=get_main_keyboard())
        return

    user_ids = await db.get_all_user_ids()
    await message.answer(f"🚀 Старт рассылки на {len(user_ids)} пользователей...")

    count = 0
    blocked_count = 0
    error_count = 0
    
    for i, user_id in enumerate(user_ids):
        try:
            await bot.send_message(chat_id=user_id, text=message.text)
            count += 1
            
            if i % 30 == 29:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(RATE_LIMIT_DELAY)
                
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramAPIError as e:
            logger.error(f"Ошибка при отправке пользователю {user_id}: {e}")
            error_count += 1
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке {user_id}: {e}")
            error_count += 1

        if (i + 1) % 100 == 0:
            await message.answer(
                f"📊 Прогресс: {i + 1}/{len(user_ids)} "
                f"(доставлено: {count}, заблокировали: {blocked_count})"
            )

    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"Успешно доставлено: <b>{count}</b>\n"
        f"Заблокировали бота: <b>{blocked_count}</b>\n"
        f"Ошибок: <b>{error_count}</b>",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "admin_update_status")
async def admin_update_status(callback: CallbackQuery):
    """Обновление статуса заявки"""
    await callback.answer()
    
    if not is_admin(callback.from_user.id):
        return

    await callback.message.answer(
        "ℹ️ Для обновления статуса заявки используйте команду:\n"
        "/update_status <ID заявки> <статус>\n\n"
        "Доступные статусы: working, done, rejected"
    )

@dp.message(Command("update_status"))
async def update_status_cmd(message: Message):
    """Команда обновления статуса"""
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
        status = args[2].lower()
        
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

# ==================== ВЕБ-СЕРВЕР ====================
class WebServer:
    """Веб-сервер для health check"""
    
    def __init__(self):
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self._setup_routes()

    def _setup_routes(self):
        """Настройка маршрутов"""
        self.app.router.add_get("/", self._handle_ping)
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/ready", self._handle_ready)
        self.app.router.add_get("/live", self._handle_live)

    async def _handle_ping(self, request):
        """Проверка доступности"""
        return web.Response(text="OK", status=200)

    async def _handle_health(self, request):
        """Health check с информацией"""
        return web.json_response({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "database": "connected" if db._is_connected else "disconnected",
            "admins_count": len(ADMIN_IDS)
        })

    async def _handle_ready(self, request):
        """Readiness probe"""
        if db._is_connected:
            return web.json_response({"status": "ready"})
        return web.json_response({"status": "not ready"}, status=503)

    async def _handle_live(self, request):
        """Liveness probe"""
        return web.json_response({"status": "alive"})

    async def start(self):
        """Запуск веб-сервера"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Health-check сервер запущен на порту {port}")

    async def stop(self):
        """Остановка веб-сервера"""
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()
            logger.info("🌐 Веб-сервер остановлен")

web_server = WebServer()

# ==================== ОБРАБОТКА СИГНАЛОВ ====================
async def shutdown():
    """Graceful shutdown"""
    logger.info("🛑 Получен сигнал завершения, выполняем graceful shutdown...")
    
    await dp.stop_polling()
    await web_server.stop()
    await db.close()
    await bot.session.close()
    
    logger.info("👋 Бот успешно остановлен")

def signal_handler():
    """Обработчик сигналов для graceful shutdown"""
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

# ==================== ТОЧКА ВХОДА ====================
async def main():
    """Главная функция запуска"""
    try:
        logger.info("🚀 Запуск бота...")
        logger.info(f"📢 Получатели уведомлений: {ADMIN_IDS}")
        
        signal_handler()
        
        await db.connect()
        await web_server.start()
        await bot.delete_webhook(drop_pending_updates=True)
        
        logger.info("🤖 Бот готов к работе!")
        await dp.start_polling(bot)
        
    except asyncio.CancelledError:
        logger.info("⚠️ Задача была отменена")
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки (Ctrl+C)")
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка: {e}", exc_info=True)
        raise
    finally:
        await shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа завершена пользователем")
