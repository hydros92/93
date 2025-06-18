import os
import telebot
from telebot import types
import logging
from datetime import datetime, timedelta, timezone, date
import re
import json
import requests
from dotenv import load_dotenv
import random

# Імпорти для Webhook (Flask)
from flask import Flask, request

# Імпорти для PostgreSQL
import psycopg2
from psycopg2 import sql as pg_sql
from psycopg2 import extras

# Завантажуємо змінні оточення з файлу .env. Це для локальної розробки.
load_dotenv()

# --- 1. Конфігурація Бота та змінні оточення ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '0'))
MONOBANK_CARD_NUMBER = os.getenv('MONOBANK_CARD_NUMBER', 'Не вказано')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = os.getenv('GEMINI_API_URL', "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent")
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
DATABASE_URL = os.getenv('DATABASE_URL')

# --- 2. Конфігурація логування ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- 3. Базова перевірка змінних оточення ---
def validate_env_vars():
    """
    Перевіряє наявність критично важливих змінних оточення.
    Якщо будь-яка з них відсутня, програма завершує роботу.
    Це запобігає запуску бота в некоректному стані.
    """
    missing_vars = []
    if not TOKEN:
        missing_vars.append('TELEGRAM_BOT_TOKEN')
    if not WEBHOOK_URL:
        missing_vars.append('WEBHOOK_URL')
    if not DATABASE_URL:
        missing_vars.append('DATABASE_URL')
    if ADMIN_CHAT_ID == 0:
        missing_vars.append('ADMIN_CHAT_ID')
    if CHANNEL_ID == 0:
        missing_vars.append('CHANNEL_ID')

    if missing_vars:
        logger.critical(f"Критична помилка: Відсутні наступні змінні оточення: {', '.join(missing_vars)}. Бот не може працювати.")
        exit(1)

validate_env_vars()

# --- 4. Ініціалізація TeleBot та Flask ---
app = Flask(__name__)
bot = telebot.TeleBot(TOKEN)

# --- 4.1. НАЛАШТУВАННЯ МЕРЕЖЕВИХ ЗАПИТІВ (RETRY-МЕХАНІЗМ) ---
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(['HEAD', 'GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'TRACE']),
        backoff_factor=1,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = telebot.apihelper._get_req_session()
    session.mount("https://", adapter)
    logger.info("Мережевий адаптер з механізмом повторних спроб успішно налаштовано.")
except ImportError:
    logger.warning("Не вдалося імпортувати 'requests' або 'urllib3'. Механізм повторних спроб не активовано.")


# --- 5. Декоратор для обробки помилок ---
def error_handler(func):
    """
    Декоратор для централізованої обробки помилок у функціях бота.
    Логує помилки та сповіщає адміністратора.
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Помилка в {func.__name__}: {e}", exc_info=True)
            chat_id_to_notify = ADMIN_CHAT_ID

            if args:
                first_arg = args[0]
                if isinstance(first_arg, types.Message):
                    chat_id_to_notify = first_arg.chat.id
                elif isinstance(first_arg, types.CallbackQuery):
                    chat_id_to_notify = first_arg.message.chat.id
            
            try:
                bot.send_message(ADMIN_CHAT_ID, f"🚨 Критична помилка в боті!\nФункція: `{func.__name__}`\nПомилка: `{e}`\nДивіться деталі в логах Render.")
                if chat_id_to_notify != ADMIN_CHAT_ID:
                    bot.send_message(chat_id_to_notify, "😔 Вибачте, сталася внутрішня помилка. Адміністратор вже сповіщений.")
            except Exception as e_notify:
                logger.error(f"Не вдалося надіслати повідомлення про помилку: {e_notify}")
    return wrapper

# --- 6. Підключення та ініціалізація Бази Даних (PostgreSQL) ---
def get_db_connection():
    """
    Встановлює з'єднання з базою даних PostgreSQL.
    Використовує DATABASE_URL зі змінних оточення.
    """
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return conn
    except Exception as e:
        logger.error(f"Помилка підключення до бази даних: {e}", exc_info=True)
        return None

@error_handler
def init_db():
    """
    Ініціалізує таблиці бази даних, якщо вони ще не існують.
    Викликається при запуску бота.
    Також додає нові стовпці до існуючих таблиць, якщо їх немає (міграція схеми).
    """
    conn = get_db_connection()
    if not conn:
        logger.critical("Не вдалося підключитися до БД для ініціалізації.")
        exit(1)
    
    try:
        with conn.cursor() as cur:
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    blocked_by BIGINT,
                    blocked_at TIMESTAMP WITH TIME ZONE,
                    commission_paid REAL DEFAULT 0,
                    commission_due REAL DEFAULT 0,
                    last_activity TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    referrer_id BIGINT
                );
            """))
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    seller_username TEXT,
                    product_name TEXT NOT NULL,
                    price TEXT NOT NULL,
                    description TEXT NOT NULL,
                    photos TEXT,
                    geolocation TEXT,
                    status TEXT DEFAULT 'pending',
                    commission_rate REAL DEFAULT 0.10,
                    commission_amount REAL DEFAULT 0,
                    moderator_id BIGINT,
                    moderated_at TIMESTAMP WITH TIME ZONE,
                    admin_message_id BIGINT,
                    channel_message_id BIGINT,
                    views INTEGER DEFAULT 0,
                    likes_count INTEGER DEFAULT 0,
                    republish_count INTEGER DEFAULT 0,
                    last_republish_date DATE,
                    shipping_options TEXT,
                    hashtags TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS favorites (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    UNIQUE(user_chat_id, product_id)
                );
            """))
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    product_id INTEGER,
                    message_text TEXT,
                    sender_type TEXT,
                    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS commission_transactions (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    status TEXT DEFAULT 'pending_payment',
                    payment_details TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP WITH TIME ZONE
                );
            """))
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS statistics (
                    id SERIAL PRIMARY KEY,
                    action TEXT NOT NULL,
                    user_id BIGINT,
                    product_id INTEGER,
                    details TEXT,
                    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))
            
            migrations = {
                'products': [
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS republish_count INTEGER DEFAULT 0;",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_republish_date DATE;",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS shipping_options TEXT;",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS hashtags TEXT;",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS likes_count INTEGER DEFAULT 0;"
                ],
                'users': [
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;"
                ]
            }
            for table, columns in migrations.items():
                for column_sql in columns:
                    try:
                        cur.execute(pg_sql.SQL(column_sql))
                        conn.commit()
                        logger.info(f"Міграція для таблиці '{table}' успішно застосована: {column_sql}")
                    except psycopg2.Error as e:
                        logger.warning(f"Помилка міграції '{column_sql}': {e}")
                        conn.rollback()
            conn.commit()
            logger.info("Таблиці бази даних успішно ініціалізовано або оновлено.")
    except Exception as e:
        logger.critical(f"Критична помилка ініціалізації бази даних: {e}", exc_info=True)
        conn.rollback()
        exit(1)
    finally:
        if conn:
            conn.close()

# --- 7. Зберігання даних користувача для багатошагових процесів ---
user_data = {}

# --- 8. Функції роботи з користувачами та загальні допоміжні функції ---
@error_handler
def save_user(message_or_user, referrer_id=None):
    """Зберігає або оновлює інформацію про користувача в базі даних."""
    user = None
    chat_id = None

    if isinstance(message_or_user, types.Message):
        user = message_or_user.from_user
        chat_id = message_or_user.chat.id
    elif isinstance(message_or_user, types.User):
        user = message_or_user
        chat_id = user.id
    else:
        logger.warning(f"save_user отримав невідомий тип: {type(message_or_user)}")
        return

    if not user or not chat_id:
        logger.warning("save_user: user або chat_id не визначено.")
        return

    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT chat_id, referrer_id FROM users WHERE chat_id = %s;"), (chat_id,))
        existing_user = cur.fetchone()

        if existing_user:
            cur.execute(pg_sql.SQL("""
                UPDATE users SET username = %s, first_name = %s, last_name = %s, last_activity = CURRENT_TIMESTAMP
                WHERE chat_id = %s;
            """), (user.username, user.first_name, user.last_name, chat_id))
            logger.info(f"Користувача {chat_id} оновлено.")
        else:
            cur.execute(pg_sql.SQL("""
                INSERT INTO users (chat_id, username, first_name, last_name, referrer_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chat_id) DO NOTHING;
            """), (chat_id, user.username, user.first_name, user.last_name, referrer_id))
            logger.info(f"Нового користувача {chat_id} додано. Реферер: {referrer_id}")
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка при збереженні користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
    finally:
        if conn:
            conn.close()

@error_handler
def is_user_blocked(chat_id):
    """Перевіряє, чи заблокований користувач у базі даних."""
    conn = get_db_connection()
    if not conn: return True
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT is_blocked FROM users WHERE chat_id = %s;"), (chat_id,))
        result = cur.fetchone()
        return result and result['is_blocked']
    except Exception as e:
        logger.error(f"Помилка перевірки блокування для {chat_id}: {e}", exc_info=True)
        return True
    finally:
        if conn:
            conn.close()

@error_handler
def set_user_block_status(admin_id, chat_id, status):
    """Встановлює статус блокування (True/False) для користувача."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        if status:
            cur.execute(pg_sql.SQL("""
                UPDATE users SET is_blocked = TRUE, blocked_by = %s, blocked_at = CURRENT_TIMESTAMP
                WHERE chat_id = %s;
            """), (admin_id, chat_id))
        else:
            cur.execute(pg_sql.SQL("""
                UPDATE users SET is_blocked = FALSE, blocked_by = NULL, blocked_at = NULL
                WHERE chat_id = %s;
            """), (chat_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Помилка при встановленні статусу блокування для користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def generate_hashtags(description, num_hashtags=5):
    """Генерує хештеги з опису товару."""
    words = re.findall(r'\b\w+\b', description.lower())
    stopwords = set([
        'я', 'ми', 'ти', 'ви', 'він', 'вона', 'воно', 'вони', 'це', 'що',
        'як', 'де', 'коли', 'а', 'і', 'та', 'або', 'чи', 'для', 'з', 'на',
        'у', 'в', 'до', 'від', 'по', 'за', 'при', 'про', 'між', 'під', 'над',
        'без', 'через', 'дуже', 'цей', 'той', 'мій', 'твій', 'наш', 'ваш',
        'продам', 'продамся', 'продати', 'продаю', 'продаж', 'купити', 'куплю',
        'бу', 'новий', 'стан', 'модель', 'см', 'кг', 'грн', 'uah', 'usd', 'eur', 
        'один', 'два', 'три', 'чотири', 'пять', 'шість', 'сім', 'вісім', 'девять', 'десять'
    ])
    filtered_words = [word for word in words if len(word) > 2 and word not in stopwords]
    unique_words = list(dict.fromkeys(filtered_words))
    hashtags = ['#' + word for word in unique_words[:num_hashtags]]
    return " ".join(hashtags) if hashtags else ""

@error_handler
def log_statistics(action, user_id=None, product_id=None, details=None):
    """Логує дії користувачів та адміністраторів для збору статистики."""
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL('''
            INSERT INTO statistics (action, user_id, product_id, details)
            VALUES (%s, %s, %s, %s)
        '''), (action, user_id, product_id, details))
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка логування статистики: {e}", exc_info=True)
        conn.rollback()
    finally:
        if conn:
            conn.close()

# --- 9. Gemini AI інтеграція ---
@error_handler
def get_gemini_response(prompt, conversation_history=None):
    """Отримання відповіді від Gemini AI."""
    if not GEMINI_API_KEY:
        logger.warning("Gemini API ключ не налаштований. Використовується заглушка.")
        return generate_elon_style_response(prompt)

    headers = {
        "Content-Type": "application/json"
    }

    system_prompt = """Ти - AI помічник для Telegram бота продажу товарів. 
    Відповідай в стилі Ілона Маска: прямолінійно, з гумором, іноді саркастично, 
    але завжди корисно. Використовуй емодзі. Будь лаконічним, але інформативним.
    Допомагай з питаннями про товари, покупки, продажі, переговори.
    Відповідай українською мовою."""

    gemini_messages = [{"role": "user", "parts": [{"text": system_prompt}]}]
    
    if conversation_history:
        for msg in conversation_history:
            role = "user" if msg["sender_type"] == 'user' else "model"
            gemini_messages.append({"role": role, "parts": [{"text": msg["message_text"]}]})
    
    gemini_messages.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "contents": gemini_messages
    }

    try:
        api_url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"

        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        if data.get("candidates") and len(data["candidates"]) > 0 and \
           data["candidates"][0].get("content") and data["candidates"][0]["content"].get("parts"):
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            logger.info(f"Gemini відповідь отримана: {content[:100]}...")
            return content.strip()
        else:
            logger.error(f"Неочікувана структура відповіді від Gemini: {data}")
            return generate_elon_style_response(prompt)

    except requests.exceptions.RequestException as e:
        logger.error(f"Помилка HTTP запиту до Gemini API: {e}", exc_info=True)
        return generate_elon_style_response(prompt)
    except Exception as e:
        logger.error(f"Загальна помилка при отриманні відповіді від Gemini: {e}", exc_info=True)
        return generate_elon_style_response(prompt)

def generate_elon_style_response(prompt):
    """
    Генерує відповіді в стилі Ілона Маска як заглушка.
    """
    responses = [
        "🚀 Гм, цікаве питання! Як і з SpaceX, тут потрібен системний підхід. Що саме вас цікавить?",
        "⚡ Очевидно! Як кажуть в Tesla - простота це вершина складності. Давайте розберемося.",
        "🤖 *думає як Neuralink* Ваше питання активувало мої нейрони! Ось що я думаю...",
        "🎯 Як і з X (колишній Twitter), іноді краще бути прямолінійним. Скажіть конкретніше?",
        "🔥 Хмм, це нагадує мені час, коли ми запускали Falcon Heavy. Складно, але можливо!",
        "💡 Ах, класика! Як і з Hyperloop - спочатку здається неможливим, потім очевидним.",
        "🌟 Цікаво! У Boring Company ми б просто прокопали тунель під проблемою. А тут...",
        "⚡ Логічно! Як завжди кажу - якщо щось не вибухає, значить недостатньо намагаєшся 😄"
    ]
    
    import random
    base_response = random.choice(responses)
    
    prompt_lower = prompt.lower()
    if any(word in prompt_lower for word in ['ціна', 'вартість', 'гроші']):
        return f"{base_response}\n\n💰 Щодо ціни - як в Tesla, важлива якість, а не тільки вартість!"
    elif any(word in prompt_lower for word in ['фото', 'картинка', 'зображення']):
        return f"{base_response}\n\n📸 Фото - це як перший етап ракети, без них нікуди!"
    elif any(word in prompt_lower for word in ['доставка', 'відправка']):
        return f"{base_response}\n\n🚚 Доставка? Якби у нас був Hyperloop, це б зайняло хвилини! 😉"
    elif any(word in prompt_lower for word in ['продаж', 'купівля']):
        return f"{base_response}\n\n🤝 Продаж - це як запуск ракети: підготовка, виконання, успіх!"
    
    return base_response

@error_handler
def save_conversation(chat_id, message_text, sender_type, product_id=None):
    """Зберігає повідомлення (від користувача або AI) в історії розмов у БД."""
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL('''
            INSERT INTO conversations (user_chat_id, product_id, message_text, sender_type)
            VALUES (%s, %s, %s, %s)
        '''), (chat_id, product_id, message_text, sender_type))
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка збереження розмови: {e}", exc_info=True)
        conn.rollback()
    finally:
        if conn:
            conn.close()

@error_handler
def get_conversation_history(chat_id, limit=5):
    """Отримує історію розмов для конкретного користувача з БД."""
    conn = get_db_connection()
    if not conn: return []
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL('''
            SELECT message_text, sender_type FROM conversations 
            WHERE user_chat_id = %s 
            ORDER BY timestamp DESC LIMIT %s
        '''), (chat_id, limit))
        results = cur.fetchall()
        
        history = [{"message_text": row['message_text'], "sender_type": row['sender_type']} 
                   for row in reversed(results)]
        
        return history
    except Exception as e:
        logger.error(f"Помилка отримання історії розмов: {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

# --- 10. Клавіатури ---
main_menu_markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
main_menu_markup.add(types.KeyboardButton("📦 Додати товар"), types.KeyboardButton("📋 Мої товари"))
main_menu_markup.add(types.KeyboardButton("⭐ Обрані"), types.KeyboardButton("❓ Допомога"))
main_menu_markup.add(types.KeyboardButton("📺 Наш канал"), types.KeyboardButton("🤖 AI Помічник"))

back_button = types.KeyboardButton("🔙 Назад")
cancel_button = types.KeyboardButton("❌ Скасувати")

# --- 11. Обробники команд ---
@bot.message_handler(commands=['start'])
@error_handler
def send_welcome(message):
    """Обробник команди /start."""
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return

    referrer_id = None
    parts = message.text.split()
    if len(parts) > 1 and parts[0] == '/start':
        try:
            potential_referrer_id = int(parts[1])
            if potential_referrer_id != chat_id:
                referrer_id = potential_referrer_id
        except (ValueError, IndexError):
            pass

    save_user(message, referrer_id)
    log_statistics('start', chat_id, details=f"referrer: {referrer_id}")

    welcome_text = (
        "🛍️ *Ласкаво просимо до SellerBot!*\n\n"
        "Я ваш розумний помічник для продажу та купівлі товарів. "
        "Мене підтримує потужний AI! 🚀\n\n"
        "Що я вмію:\n"
        "📦 Допомагаю створювати оголошення\n"
        "🤝 Веду переговори та домовленості\n"
        "📍 Обробляю геолокацію та фото\n"
        "💰 Слідкую за комісіями\n"
        "🎯 Аналізую ринок та ціни\n"
        "⭐ Додаю товари до обраного\n"
        "🏆 Організовую розіграші для активних користувачів\n\n"
        "Оберіть дію з меню або просто напишіть мені!"
    )
    bot.send_message(chat_id, welcome_text, reply_markup=main_menu_markup, parse_mode='Markdown')

@bot.message_handler(commands=['admin'])
@error_handler
def admin_panel(message):
    """Обробник команди /admin."""
    if message.chat.id != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас немає прав доступу.")
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("⏳ На модерації", callback_data="admin_pending"),
        types.InlineKeyboardButton("👥 Користувачі", callback_data="admin_users"),
        types.InlineKeyboardButton("🚫 Блокування", callback_data="admin_block"),
        types.InlineKeyboardButton("💰 Комісії", callback_data="admin_commissions"),
        types.InlineKeyboardButton("🤖 AI Статистика", callback_data="admin_ai_stats"),
        types.InlineKeyboardButton("🏆 Реферали", callback_data="admin_referrals")
    )
    bot.send_message(message.chat.id, "🔧 *Адмін-панель*", reply_markup=markup, parse_mode='Markdown')


# --- 12. Потік додавання товару ---
ADD_PRODUCT_STEPS = {
    1: {'name': 'waiting_name', 'prompt': "📝 *Крок 1/6: Назва товару*\n\nВведіть назву товару:", 'next_step': 2, 'prev_step': None},
    2: {'name': 'waiting_price', 'prompt': "💰 *Крок 2/6: Ціна*\n\nВведіть ціну (наприклад, `500 грн`, `100 USD` або `Договірна`):", 'next_step': 3, 'prev_step': 1},
    3: {'name': 'waiting_photos', 'prompt': "📸 *Крок 3/6: Фотографії*\n\nНадішліть до 5 фото (по одному). Коли закінчите - натисніть 'Далі':", 'next_step': 4, 'allow_skip': True, 'skip_button': 'Пропустити фото', 'prev_step': 2},
    4: {'name': 'waiting_location', 'prompt': "📍 *Крок 4/6: Геолокація*\n\nНадішліть геолокацію або натисніть 'Пропустити':", 'next_step': 5, 'allow_skip': True, 'skip_button': 'Пропустити геолокацію', 'prev_step': 3},
    5: {'name': 'waiting_shipping', 'prompt': "🚚 *Крок 5/6: Доставка*\n\nОберіть доступні способи доставки (можна обрати декілька):", 'next_step': 6, 'prev_step': 4},
    6: {'name': 'waiting_description', 'prompt': "✍️ *Крок 6/6: Опис*\n\nНапишіть детальний опис товару:", 'next_step': 'confirm', 'prev_step': 5}
}

@error_handler
def start_add_product_flow(message):
    """Починає процес додавання нового товару, ініціалізуючи user_data."""
    chat_id = message.chat.id
    user_data[chat_id] = {
        'flow': 'add_product',
        'step_number': 1, 
        'data': {
            'photos': [], 
            'geolocation': None,
            'shipping_options': [],
            'product_name': '',
            'price': '',
            'description': '',
            'hashtags': ''
        }
    }
    send_product_step_message(chat_id)
    log_statistics('start_add_product', chat_id)

@error_handler
def send_product_step_message(chat_id):
    """Надсилає користувачу повідомлення для поточного кроку додавання товару."""
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        return

    current_step_number = user_data[chat_id]['step_number']
    step_config = ADD_PRODUCT_STEPS[current_step_number]
    user_data[chat_id]['step'] = step_config['name']

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    
    if step_config['name'] == 'waiting_photos':
        markup.add(types.KeyboardButton("Далі"))
        markup.add(types.KeyboardButton(step_config['skip_button']))
    elif step_config['name'] == 'waiting_location':
        markup.add(types.KeyboardButton("📍 Надіслати геолокацію", request_location=True))
        markup.add(types.KeyboardButton(step_config['skip_button']))
    elif step_config['name'] == 'waiting_shipping':
        inline_markup = types.InlineKeyboardMarkup(row_width=2)
        shipping_options_list = ["Наложка Нова Пошта", "Наложка Укрпошта", "Особиста зустріч"]
        selected_options = user_data[chat_id]['data'].get('shipping_options', [])

        buttons = []
        for opt in shipping_options_list:
            emoji = '✅ ' if opt in selected_options else ''
            buttons.append(types.InlineKeyboardButton(f"{emoji}{opt}", callback_data=f"shipping_{opt}"))
        
        inline_markup.add(*buttons)
        inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
        
        bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=inline_markup)
        return
    
    if step_config['prev_step'] is not None:
        markup.add(back_button)
    
    markup.add(cancel_button)
    
    bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=markup)

@error_handler
def process_product_step(message):
    """Обробляє текстовий ввід користувача під час багатошагового процесу додавання товару."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        bot.send_message(chat_id, "Ви не в процесі додавання товару. Скористайтеся меню.", reply_markup=main_menu_markup)
        return

    current_step_number = user_data[chat_id]['step_number']
    step_config = ADD_PRODUCT_STEPS[current_step_number]
    user_text = message.text if message.content_type == 'text' else ""

    if user_text == cancel_button.text:
        del user_data[chat_id]
        bot.send_message(chat_id, "Додавання товару скасовано.", reply_markup=main_menu_markup)
        return

    if user_text == back_button.text:
        if step_config['prev_step'] is not None:
            user_data[chat_id]['step_number'] = step_config['prev_step']
            send_product_step_message(chat_id)
        else:
            bot.send_message(chat_id, "Ви вже на першому кроці.")
        return

    if step_config.get('allow_skip') and user_text == step_config.get('skip_button'):
        go_to_next_step(chat_id)
        return

    if step_config['name'] == 'waiting_name':
        if user_text and 3 <= len(user_text) <= 100:
            user_data[chat_id]['data']['product_name'] = user_text
            go_to_next_step(chat_id)
        else:
            bot.send_message(chat_id, "Назва товару повинна бути від 3 до 100 символів. Спробуйте ще раз:")

    elif step_config['name'] == 'waiting_price':
        if user_text and len(user_text) <= 50:
            user_data[chat_id]['data']['price'] = user_text
            go_to_next_step(chat_id)
        else:
            bot.send_message(chat_id, "Будь ласка, вкажіть ціну (до 50 символів):")

    elif step_config['name'] == 'waiting_photos':
        if user_text == "Далі":
            go_to_next_step(chat_id)
        else:
            bot.send_message(chat_id, "Надішліть фото або натисніть 'Далі'/'Пропустити фото'.")

    elif step_config['name'] == 'waiting_location':
        bot.send_message(chat_id, "Надішліть геолокацію або натисніть 'Пропустити геолокацію'.")
    
    elif step_config['name'] == 'waiting_shipping':
        bot.send_message(chat_id, "Будь ласка, скористайтесь кнопками для вибору способу доставки.")

    elif step_config['name'] == 'waiting_description':
        if user_text and 10 <= len(user_text) <= 1000:
            user_data[chat_id]['data']['description'] = user_text
            user_data[chat_id]['data']['hashtags'] = generate_hashtags(user_text)
            confirm_and_send_for_moderation(chat_id)
        else:
            bot.send_message(chat_id, "Опис занадто короткий або занадто довгий (10-1000 символів). Напишіть детальніше:")

@error_handler
def go_to_next_step(chat_id):
    """Переводить користувача до наступного кроку в процесі додавання товару."""
    current_step_number = user_data[chat_id]['step_number']
    next_step_number = ADD_PRODUCT_STEPS[current_step_number]['next_step']
    
    if next_step_number == 'confirm':
        confirm_and_send_for_moderation(chat_id)
    else:
        user_data[chat_id]['step_number'] = next_step_number
        send_product_step_message(chat_id)

@error_handler
def process_product_photo(message):
    """Обробляє завантаження фотографій товару під час відповідного кроку."""
    chat_id = message.chat.id
    if chat_id in user_data and user_data[chat_id].get('step') == 'waiting_photos':
        if len(user_data[chat_id]['data']['photos']) < 5:
            file_id = message.photo[-1].file_id
            user_data[chat_id]['data']['photos'].append(file_id)
            photos_count = len(user_data[chat_id]['data']['photos'])
            bot.send_message(chat_id, f"✅ Фото {photos_count}/5 додано. Надішліть ще або натисніть 'Далі'")
        else:
            bot.send_message(chat_id, "Максимум 5 фото. Натисніть 'Далі' для продовження.")
    else:
        bot.send_message(chat_id, "Будь ласка, надсилайте фотографії тільки на відповідному кроці.")

@error_handler
def process_product_location(message):
    """Обробляє надсилання геолокації для товару під час відповідного кроку."""
    chat_id = message.chat.id
    if chat_id in user_data and user_data[chat_id].get('step') == 'waiting_location':
        if message.location:
            user_data[chat_id]['data']['geolocation'] = {
                'latitude': message.location.latitude,
                'longitude': message.location.longitude
            }
            bot.send_message(chat_id, "✅ Геолокацію додано!")
            go_to_next_step(chat_id)
        else:
            bot.send_message(chat_id, "Будь ласка, надішліть геолокацію через відповідну кнопку, або натисніть 'Пропустити геолокацію'.")
    else:
        bot.send_message(chat_id, "Будь ласка, надсилайте геолокацію тільки на відповідному кроці.")

@error_handler
def confirm_and_send_for_moderation(chat_id):
    """Зберігає товар у БД та сповіщає користувача та адміністратора."""
    data = user_data[chat_id]['data']
    
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    cur = conn.cursor()
    product_id = None
    try:
        user_info = bot.get_chat(chat_id)
        seller_username = user_info.username if user_info.username else None

        cur.execute(pg_sql.SQL('''
            INSERT INTO products 
            (seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, hashtags, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id;
        '''), (
            chat_id,
            seller_username,
            data['product_name'],
            data['price'],
            data['description'],
            json.dumps(data['photos']) if data['photos'] else None,
            json.dumps(data['geolocation']) if data['geolocation'] else None,
            json.dumps(data['shipping_options']) if data['shipping_options'] else None,
            data['hashtags'],
        ))
        
        product_id = cur.fetchone()[0]
        conn.commit()
        
        bot.send_message(chat_id, 
            f"✅ Товар '{data['product_name']}' відправлено на модерацію!\n"
            f"Ви отримаєте сповіщення після перевірки.",
            reply_markup=main_menu_markup)
        
        send_product_for_admin_review(product_id)
        
        del user_data[chat_id]
        
        log_statistics('product_added', chat_id, product_id)
        
    except Exception as e:
        logger.error(f"Помилка збереження товару: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "Помилка збереження товару. Спробуйте пізніше.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_product_for_admin_review(product_id):
    """Формує та надсилає повідомлення адміністратору для модерації нового товару."""
    conn = get_db_connection()
    if not conn: return

    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, hashtags
            FROM products WHERE id = %s;
        """), (product_id,))
        data = cur.fetchone()

        if not data:
            logger.error(f"Товар з ID {product_id} не знайдено для адмін-рев'ю.")
            bot.send_message(ADMIN_CHAT_ID, f"❌ Помилка: Товар ID {product_id} не знайдено для публікації в каналі.")
            return

        seller_chat_id = data['seller_chat_id']
        seller_username = data['seller_username'] if data['seller_username'] else "Не вказано"
        photos = json.loads(data['photos']) if data['photos'] else []
        geolocation = json.loads(data['geolocation']) if data['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(data['shipping_options'])) if data['shipping_options'] else "Не вказано"
        hashtags = data['hashtags'] if data['hashtags'] else ""

        review_text = (
            f"📦 *Новий товар на модерацію*\n\n"
            f"🆔 ID: {product_id}\n"
            f"📝 Назва: {data['product_name']}\n"
            f"💰 Ціна: {data['price']}\n"
            f"📄 Опис: {data['description'][:500]}...\n"
            f"📸 Фото: {len(photos)} шт.\n"
            f"📍 Геолокація: {'Так' if geolocation else 'Ні'}\n"
            f"🚚 Доставка: {shipping_options_text}\n"
            f"🏷️ Хештеги: {hashtags}\n\n"
            f"👤 Продавець: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={seller_chat_id})"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
            types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
        )
        markup.add(
            types.InlineKeyboardButton("✏️ Редагувати хештеги", callback_data=f"mod_edit_tags_{product_id}"),
            types.InlineKeyboardButton("🔄 Запит на виправлення фото", callback_data=f"mod_rotate_photo_{product_id}")
        )
        
        try:
            admin_msg = None
            if photos:
                media = [types.InputMediaPhoto(photo_id, caption=review_text if i == 0 else None, parse_mode='Markdown') 
                         for i, photo_id in enumerate(photos)]
                
                sent_messages = bot.send_media_group(ADMIN_CHAT_ID, media)
                
                if sent_messages:
                    admin_msg = bot.send_message(ADMIN_CHAT_ID, 
                                                 f"👆 Деталі товару ID: {product_id} (фото вище)", 
                                                 reply_markup=markup, 
                                                 parse_mode='Markdown',
                                                 reply_to_message_id=sent_messages[0].message_id)
                else:
                    admin_msg = bot.send_message(ADMIN_CHAT_ID, review_text,
                                               parse_mode='Markdown',
                                               reply_markup=markup)
            else:
                admin_msg = bot.send_message(ADMIN_CHAT_ID, review_text,
                                           parse_mode='Markdown',
                                           reply_markup=markup)
            
            if admin_msg:
                cur.execute(pg_sql.SQL("UPDATE products SET admin_message_id = %s WHERE id = %s;"),
                               (admin_msg.message_id, product_id))
                conn.commit()

        except Exception as e:
            logger.error(f"Помилка при відправці товару {product_id} адміністратору: {e}", exc_info=True)
            conn.rollback()
    finally:
        if conn:
            conn.close()

# --- 13. Обробники текстових повідомлень та кнопок меню ---
@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'location'])
@error_handler
def handle_messages(message):
    """Основний обробник для всіх вхідних повідомлень."""
    chat_id = message.chat.id
    user_text = message.text if message.content_type == 'text' else ""

    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return
    
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(pg_sql.SQL("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE chat_id = %s"), (chat_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Помилка оновлення останньої активності для користувача {chat_id}: {e}")
            conn.rollback()
        finally:
            conn.close()

    if chat_id in user_data and user_data[chat_id].get('flow'):
        current_flow = user_data[chat_id]['flow']
        if current_flow == 'add_product':
            if message.content_type == 'text':
                process_product_step(message)
            elif message.content_type == 'photo':
                process_product_photo(message)
            elif message.content_type == 'location':
                process_product_location(message)
            else:
                bot.send_message(chat_id, "Будь ласка, дотримуйтесь інструкцій для поточного кроку або натисніть '❌ Скасувати' або '🔙 Назад'.")
        elif current_flow == 'change_price':
            process_new_price(message)
        elif current_flow == 'mod_edit_tags':
            process_new_hashtags_mod(message)
        return

    if user_text == "📦 Додати товар":
        start_add_product_flow(message)
    elif user_text == "📋 Мої товари":
        send_my_products(message)
    elif user_text == "⭐ Обрані":
        send_favorites(message)
    elif user_text == "❓ Допомога":
        send_help_message(message)
    elif user_text == "📺 Наш канал":
        send_channel_link(message)
    elif user_text == "🤖 AI Помічник":
        bot.send_message(chat_id, "Привіт! Я ваш AI помічник. Задайте мені будь-яке питання про товари, продажі, або просто поспілкуйтесь!\n\n(Напишіть '❌ Скасувати' для виходу з режиму AI чату.)", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, handle_ai_chat)
    elif message.content_type == 'text': 
        handle_ai_chat(message)
    elif message.content_type == 'photo':
        bot.send_message(chat_id, "Я отримав ваше фото, але не знаю, що з ним робити поза процесом додавання товару. 🤔")
    elif message.content_type == 'location':
        bot.send_message(chat_id, f"Я бачу вашу геоточку: {message.location.latitude}, {message.location.longitude}. Як я можу її використати?")
    else:
        bot.send_message(chat_id, "Я не зрозумів ваш запит. Спробуйте використати кнопки меню.")

@error_handler
def handle_ai_chat(message):
    """Обробляє повідомлення в режимі AI чату."""
    chat_id = message.chat.id
    user_text = message.text

    if user_text.lower() == "скасувати" or user_text == "❌ Скасувати":
        bot.send_message(chat_id, "Чат з AI скасовано.", reply_markup=main_menu_markup)
        return

    if user_text == "🤖 AI Помічник" or user_text == "/start":
        bot.send_message(chat_id, "Ви вже в режимі AI чату. Напишіть '❌ Скасувати' для виходу.", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, handle_ai_chat)
        return

    save_conversation(chat_id, user_text, 'user')
    
    conversation_history = get_conversation_history(chat_id, limit=10)
    
    ai_reply = get_gemini_response(user_text, conversation_history)
    save_conversation(chat_id, ai_reply, 'ai')
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Скасувати"))
    bot.send_message(chat_id, f"🤖 Думаю...\n{ai_reply}", reply_markup=markup)
    bot.register_next_step_handler(message, handle_ai_chat)


# --- 14. Функції для роботи з товарами (відображення, зміна статусу, редагування) ---
@error_handler
def get_product_info(product_id):
    """Отримує повну інформацію про товар за його ID з бази даних."""
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT * FROM products WHERE id = %s;
        """), (product_id,))
        return cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка отримання інформації про товар {product_id}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

@error_handler
def send_product_details_to_channel(product_id, admin_id):
    """
    Публікує схвалений товар у Telegram-каналі.
    Оновлює статус товару та зберігає ID повідомлення в каналі.
    """
    conn = get_db_connection()
    if not conn: return

    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, hashtags
            FROM products WHERE id = %s;
        """), (product_id,))
        data = cur.fetchone()

        if not data:
            logger.error(f"Товар з ID {product_id} не знайдено для публікації.")
            bot.send_message(ADMIN_CHAT_ID, f"❌ Помилка: Товар ID {product_id} не знайдено для публікації в каналі.")
            return

        seller_chat_id = data['seller_chat_id']
        seller_username = data['seller_username'] if data['seller_username'] else "Не вказано"
        photos = json.loads(data['photos']) if data['photos'] else []
        geolocation = json.loads(data['geolocation']) if data['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(data['shipping_options'])) if data['shipping_options'] else "Не вказано"
        hashtags = data['hashtags'] if data['hashtags'] else ""

        post_text = (
            f"✨ *НОВЕ ОГОЛОШЕННЯ* ✨\n\n"
            f"📝 *{data['product_name']}*\n\n"
            f"💰 *Ціна:* {data['price']}\n\n"
            f"📄 *Опис:*\n{data['description']}\n\n"
            f"🚚 *Доставка:* {shipping_options_text}\n"
            f"📍 *Геолокація:* {'Є' if geolocation else 'Відсутня'}\n\n"
            f"👤 *Продавець:* {'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}\n"
            f"🔗 [Зв'язатися з продавцем](tg://user?id={seller_chat_id})\n\n"
            f"{hashtags}\n"
        )
        
        inline_markup = types.InlineKeyboardMarkup()
        inline_markup.add(
            types.InlineKeyboardButton("✍️ Зв'язатися з продавцем", url=f"tg://user?id={seller_chat_id}"),
            types.InlineKeyboardButton("⭐ Додати в обране", callback_data=f"fav_{product_id}")
        )

        try:
            channel_message = None
            if photos:
                media = [types.InputMediaPhoto(photo_id, caption=post_text if i == 0 else None, parse_mode='Markdown') 
                         for i, photo_id in enumerate(photos)]
                
                sent_messages = bot.send_media_group(CHANNEL_ID, media)
                
                if sent_messages:
                    channel_message = bot.send_message(CHANNEL_ID, 
                                                       f"👆 Оголошення ID: {product_id} (фото вище)", 
                                                       reply_markup=inline_markup, 
                                                       parse_mode='Markdown',
                                                       reply_to_message_id=sent_messages[0].message_id)
                else:
                    channel_message = bot.send_message(CHANNEL_ID, post_text,
                                                    parse_mode='Markdown',
                                                    reply_markup=inline_markup)
            else:
                channel_message = bot.send_message(CHANNEL_ID, post_text,
                                                parse_mode='Markdown',
                                                reply_markup=inline_markup)
            
            if channel_message:
                cur.execute(pg_sql.SQL("""
                    UPDATE products SET status = 'approved', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP, 
                    channel_message_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s;
                """), (admin_id, channel_message.message_id, product_id))
                conn.commit()
                bot.send_message(seller_chat_id, 
                                f"🎉 Ваш товар '{data['product_name']}' було *схвалено* та опубліковано в каналі! "
                                f"Посилання: [Переглянути](https://t.me/c/{str(CHANNEL_ID)[4:]}/{channel_message.message_id})",
                                parse_mode='Markdown', reply_markup=main_menu_markup)
                bot.send_message(ADMIN_CHAT_ID, f"✅ Товар ID {product_id} схвалено та опубліковано.", reply_markup=admin_panel_markup())
                log_statistics('product_approved', admin_id, product_id, f"Опубліковано в каналі: {CHANNEL_ID}")
            else:
                bot.send_message(ADMIN_CHAT_ID, f"❌ Помилка публікації товару ID {product_id} у канал. Спробуйте ще раз.", reply_markup=admin_panel_markup())
                conn.rollback()
        except Exception as e:
            logger.error(f"Помилка при публікації товару {product_id} у канал: {e}", exc_info=True)
            bot.send_message(ADMIN_CHAT_ID, f"❌ Критична помилка публікації товару ID {product_id} у канал: {e}. Перевірте логи.", reply_markup=admin_panel_markup())
            conn.rollback()
    finally:
        if conn:
            conn.close()

@error_handler
def reject_product_action(product_id, admin_id):
    """
    Відхиляє товар, видаляє його з бази даних (або змінює статус на 'rejected'),
    та сповіщає продавця.
    """
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT seller_chat_id, product_name FROM products WHERE id = %s;"), (product_id,))
        product_info = cur.fetchone()

        if not product_info:
            logger.warning(f"Спроба відхилити неіснуючий товар ID: {product_id}")
            bot.send_message(ADMIN_CHAT_ID, f"❌ Товар ID {product_id} не знайдено для відхилення.", reply_markup=admin_panel_markup())
            return

        seller_chat_id = product_info['seller_chat_id']
        product_name = product_info['product_name']

        cur.execute(pg_sql.SQL("""
            UPDATE products SET status = 'rejected', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """), (admin_id, product_id))
        conn.commit()

        bot.send_message(seller_chat_id, 
                         f"😔 На жаль, ваш товар '{product_name}' було *відхилено* адміністратором. "
                         "Якщо у вас є питання, зв'яжіться з підтримкою.", parse_mode='Markdown')
        bot.send_message(ADMIN_CHAT_ID, f"❌ Товар ID {product_id} відхилено.", reply_markup=admin_panel_markup())
        log_statistics('product_rejected', admin_id, product_id)
    except Exception as e:
        logger.error(f"Помилка відхилення товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(ADMIN_CHAT_ID, f"❌ Помилка при відхиленні товару ID {product_id}.", reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def admin_panel_markup():
    """Повертає інлайн-клавіатуру для адмін-панелі."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("⏳ На модерації", callback_data="admin_pending"),
        types.InlineKeyboardButton("👥 Користувачі", callback_data="admin_users"),
        types.InlineKeyboardButton("🚫 Блокування", callback_data="admin_block"),
        types.InlineKeyboardButton("💰 Комісії", callback_data="admin_commissions"),
        types.InlineKeyboardButton("🤖 AI Статистика", callback_data="admin_ai_stats"),
        types.InlineKeyboardButton("🏆 Реферали", callback_data="admin_referrals")
    )
    return markup


@error_handler
def send_my_products(message):
    """
    Надсилає користувачу список його товарів.
    Додає кнопки для керування кожним товаром (редагування, видалення, позначення як проданий).
    """
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT id, product_name, price, status, views, likes_count, republish_count, last_republish_date
            FROM products WHERE seller_chat_id = %s ORDER BY created_at DESC;
        """), (chat_id,))
        products = cur.fetchall()

        if not products:
            bot.send_message(chat_id, "У вас ще немає доданих товарів. Натисніть '📦 Додати товар', щоб створити перше оголошення!", reply_markup=main_menu_markup)
            return

        for product in products:
            product_id = product['id']
            product_name = product['product_name']
            price = product['price']
            status = product['status']
            views = product['views']
            likes_count = product['likes_count']
            republish_count = product['republish_count']
            last_republish_date = product['last_republish_date']

            status_emoji = {
                'pending': '⏳', 'approved': '✅', 'rejected': '❌', 'sold': '🏷️', 'expired': '🗑️'
            }.get(status, '❓')

            republish_info = ""
            if status == 'approved':
                republish_info = f"Перепублікацій: {republish_count}. Остання: {last_republish_date.strftime('%Y-%m-%d') if last_republish_date else 'ніколи'}"

            product_text = (
                f"{status_emoji} *{product_name}*\n"
                f"ID: `{product_id}`\n"
                f"Ціна: {price}\n"
                f"Статус: `{status}`\n"
                f"Переглядів: {views} | ❤️: {likes_count}\n"
                f"{republish_info}"
            )

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("👁️ Переглянути", callback_data=f"view_prod_{product_id}"),
                types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_prod_{product_id}")
            )
            if status == 'approved':
                markup.add(
                    types.InlineKeyboardButton("🏷️ Позначити як проданий", callback_data=f"mark_sold_{product_id}"),
                    types.InlineKeyboardButton("🔄 Перепублікувати", callback_data=f"republish_{product_id}")
                )
                markup.add(
                    types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{product_id}")
                )

            bot.send_message(chat_id, product_text, parse_mode='Markdown', reply_markup=markup)
        
        log_statistics('view_my_products', chat_id)
    except Exception as e:
        logger.error(f"Помилка при відправці моїх товарів для користувача {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні ваших товарів. Спробуйте пізніше.")
    finally:
        if conn:
            conn.close()

@error_handler
def view_product_details(call, product_id):
    """
    Надсилає користувачеві деталі конкретного товару.
    """
    chat_id = call.message.chat.id
    product = get_product_info(product_id)
    if not product:
        bot.edit_message_text("❌ Товар не знайдено.", chat_id, call.message.message_id)
        return
    
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(pg_sql.SQL("UPDATE products SET views = views + 1 WHERE id = %s;"), (product_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Помилка оновлення переглядів для товару {product_id}: {e}")
            conn.rollback()
        finally:
            conn.close()

    seller_username = product['seller_username'] if product['seller_username'] else "Не вказано"
    photos = json.loads(product['photos']) if product['photos'] else []
    geolocation = json.loads(product['geolocation']) if product['geolocation'] else None
    shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"
    hashtags = product['hashtags'] if product['hashtags'] else ""
    
    product_text = (
        f"📦 *{product['product_name']}*\n\n"
        f"💰 *Ціна:* {product['price']}\n\n"
        f"📄 *Опис:*\n{product['description']}\n\n"
        f"🚚 *Доставка:* {shipping_options_text}\n"
        f"📍 *Геолокація:* {'Є' if geolocation else 'Відсутня'}\n\n"
        f"👤 *Продавець:* {'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}\n"
        f"🔗 [Зв'язатися з продавцем](tg://user?id={product['seller_chat_id']})\n"
        f"❤️ Лайків: {product['likes_count']}\n"
        f"Переглядів: {product['views'] + 1}\n\n"
        f"{hashtags}"
    )

    markup = types.InlineKeyboardMarkup()
    if chat_id != product['seller_chat_id']:
        markup.add(types.InlineKeyboardButton("✍️ Зв'язатися з продавцем", url=f"tg://user?id={product['seller_chat_id']}"))
        
        is_favorite = False
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(pg_sql.SQL("SELECT 1 FROM favorites WHERE user_chat_id = %s AND product_id = %s;"), (chat_id, product_id))
                    is_favorite = cur.fetchone() is not None
            except Exception as e:
                logger.error(f"Помилка перевірки обраного для товару {product_id} та користувача {chat_id}: {e}")
            finally:
                conn.close()
        
        if is_favorite:
            markup.add(types.InlineKeyboardButton("⭐ Видалити з обраного", callback_data=f"unfav_{product_id}"))
        else:
            markup.add(types.InlineKeyboardButton("⭐ Додати в обране", callback_data=f"fav_{product_id}"))
    else:
        markup.add(
            types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{product_id}"),
            types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_prod_{product_id}")
        )
        if product['status'] == 'approved':
             markup.add(
                types.InlineKeyboardButton("🏷️ Позначити як проданий", callback_data=f"mark_sold_{product_id}"),
                types.InlineKeyboardButton("🔄 Перепублікувати", callback_data=f"republish_{product_id}")
            )

    try:
        if photos:
            media = [types.InputMediaPhoto(photo_id, caption=product_text if i == 0 else None, parse_mode='Markdown') 
                     for i, photo_id in enumerate(photos)]
            
            if call.message.photo:
                bot.delete_message(chat_id, call.message.message_id)
                sent_messages = bot.send_media_group(chat_id, media)
                if sent_messages:
                    bot.send_message(chat_id, "Деталі товару:", reply_markup=markup, reply_to_message_id=sent_messages[0].message_id)
            else:
                bot.edit_message_media(types.InputMediaPhoto(photos[0]), chat_id=chat_id, message_id=call.message.message_id, reply_markup=markup)
                bot.edit_message_caption(caption=product_text, chat_id=chat_id, message_id=call.message.message_id, parse_mode='Markdown')
                if len(photos) > 1:
                    for i, photo_id in enumerate(photos[1:]):
                        bot.send_photo(chat_id, photo_id)

        else:
            bot.edit_message_text(product_text, chat_id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logger.error(f"Помилка при відправці деталей товару {product_id} користувачу {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні деталей товару.")
    
    bot.answer_callback_query(call.id)
    log_statistics('view_product_details', chat_id, product_id)


@error_handler
def update_product_status(product_id, new_status, admin_id=None):
    """Оновлює статус товару в базі даних."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            UPDATE products SET status = %s, moderator_id = %s, moderated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """), (new_status, admin_id, product_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Помилка оновлення статусу товару {product_id} на {new_status}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def mark_product_as_sold(product_id, user_id):
    """Позначає товар як проданий."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            UPDATE products SET status = 'sold', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, user_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Помилка позначення товару {product_id} як проданого: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def republish_product(product_id, user_id):
    """
    Перепубліковує товар, оновлюючи дату, статус на 'pending' та збільшуючи лічильник.
    """
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            UPDATE products 
            SET status = 'pending', 
                republish_count = republish_count + 1, 
                last_republish_date = CURRENT_DATE, 
                created_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, user_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Помилка перепублікації товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def delete_product(product_id, user_id):
    """Видаляє товар з бази даних."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            DELETE FROM products WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, user_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Помилка видалення товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def process_new_price(message):
    """Обробляє новий ввід ціни від користувача."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'change_price':
        bot.send_message(chat_id, "Неочікуваний ввід. Спробуйте ще раз.", reply_markup=main_menu_markup)
        return
    
    product_id = user_data[chat_id]['product_id']
    new_price = message.text.strip()

    if not new_price or len(new_price) > 50:
        bot.send_message(chat_id, "Будь ласка, вкажіть дійсну ціну (до 50 символів).")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            UPDATE products SET price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND seller_chat_id = %s;
        """), (new_price, product_id, chat_id))
        conn.commit()
        bot.send_message(chat_id, f"✅ Ціну для товару `{product_id}` успішно оновлено на `{new_price}`.", parse_mode='Markdown', reply_markup=main_menu_markup)
        del user_data[chat_id]
        log_statistics('price_changed', chat_id, product_id, details=new_price)
    except Exception as e:
        logger.error(f"Помилка оновлення ціни для товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "❌ Помилка при оновленні ціни. Спробуйте пізніше.")
    finally:
        if conn:
            conn.close()

@error_handler
def process_new_hashtags_mod(message):
    """Обробляє новий ввід хештегів від модератора."""
    chat_id = message.chat.id
    if chat_id != ADMIN_CHAT_ID or chat_id not in user_data or user_data[chat_id].get('flow') != 'mod_edit_tags':
        bot.send_message(chat_id, "Неочікуваний ввід або ви не авторизовані для цієї дії.")
        return
    
    product_id = user_data[chat_id]['product_id']
    new_hashtags_text = message.text.strip()

    if not new_hashtags_text:
        bot.send_message(chat_id, "Будь ласка, введіть хештеги. Якщо бажаєте прибрати, введіть пробіл.")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            UPDATE products SET hashtags = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;
        """), (new_hashtags_text, product_id))
        conn.commit()
        bot.send_message(chat_id, f"✅ Хештеги для товару `{product_id}` успішно оновлено.", parse_mode='Markdown')
        del user_data[chat_id]
        log_statistics('hashtags_edited', chat_id, product_id, details=new_hashtags_text)
        send_product_for_admin_review(product_id)
    except Exception as e:
        logger.error(f"Помилка оновлення хештегів для товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "❌ Помилка при оновленні хештегів. Спробуйте пізніше.")
    finally:
        if conn:
            conn.close()


@error_handler
def add_to_favorites(user_chat_id, product_id):
    """Додає товар до списку обраних користувача."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT 1 FROM favorites WHERE user_chat_id = %s AND product_id = %s;"), (user_chat_id, product_id))
        if cur.fetchone():
            return False
        
        cur.execute(pg_sql.SQL("INSERT INTO favorites (user_chat_id, product_id) VALUES (%s, %s);"), (user_chat_id, product_id))
        
        cur.execute(pg_sql.SQL("UPDATE products SET likes_count = likes_count + 1 WHERE id = %s;"), (product_id,))

        conn.commit()
        log_statistics('add_to_favorites', user_chat_id, product_id)
        return True
    except Exception as e:
        logger.error(f"Помилка додавання товару {product_id} до обраного для {user_chat_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def remove_from_favorites(user_chat_id, product_id):
    """Видаляє товар зі списку обраних користувача."""
    conn = get_db_connection()
    if not conn: return False
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("DELETE FROM favorites WHERE user_chat_id = %s AND product_id = %s;"), (user_chat_id, product_id))
        
        cur.execute(pg_sql.SQL("UPDATE products SET likes_count = GREATEST(0, likes_count - 1) WHERE id = %s;"), (product_id,))

        conn.commit()
        log_statistics('remove_from_favorites', user_chat_id, product_id)
        return True
    except Exception as e:
        logger.error(f"Помилка видалення товару {product_id} з обраного для {user_chat_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

@error_handler
def send_favorites(message):
    """Надсилає користувачу список його обраних товарів."""
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT p.id, p.product_name, p.price, p.status, p.likes_count, p.views
            FROM products p
            JOIN favorites f ON p.id = f.product_id
            WHERE f.user_chat_id = %s
            ORDER BY f.id DESC;
        """), (chat_id,))
        favorite_products = cur.fetchall()

        if not favorite_products:
            bot.send_message(chat_id, "У вашому списку обраних ще немає товарів. Ви можете додати товар в обране, коли переглядаєте його!", reply_markup=main_menu_markup)
            return

        bot.send_message(chat_id, "⭐ *Ваші обрані товари:*\n", parse_mode='Markdown')
        for product in favorite_products:
            product_id = product['id']
            product_name = product['product_name']
            price = product['price']
            status = product['status']
            likes_count = product['likes_count']
            views = product['views']

            product_text = (
                f"▪️ *{product_name}*\n"
                f"Ціна: {price}\n"
                f"Статус: `{status}`\n"
                f"❤️: {likes_count} | Переглядів: {views}"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("👁️ Переглянути", callback_data=f"view_prod_{product_id}"),
                types.InlineKeyboardButton("❌ Видалити з обраного", callback_data=f"unfav_{product_id}")
            )
            bot.send_message(chat_id, product_text, parse_mode='Markdown', reply_markup=markup)
        
        log_statistics('view_favorites', chat_id)
    except Exception as e:
        logger.error(f"Помилка при відправці обраних товарів для користувача {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні обраних товарів. Спробуйте пізніше.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_help_message(message):
    """Надсилає користувачеві довідкове повідомлення."""
    help_text = (
        "📚 *Довідка SellerBot*\n\n"
        "Я допоможу вам легко продавати та купувати товари в Telegram!\n\n"
        "Наші основні функції:\n"
        "📦 *Додати товар*: Покроковий майстер для створення нового оголошення.\n"
        "📋 *Мої товари*: Перегляд та керування вашими активними та минулими оголошеннями.\n"
        "⭐ *Обрані*: Ваші збережені оголошення, щоб не загубити те, що сподобалось.\n"
        "❓ *Допомога*: Це повідомлення.\n"
        "📺 *Наш канал*: Посилання на наш основний канал з усіма оголошеннями.\n"
        "🤖 *AI Помічник*: Задайте мені будь-яке питання! Я відповім як Ілон Маск.\n\n"
        "Якщо у вас є інші питання, напишіть мені або зверніться до [адміністратора](tg://user?id={ADMIN_CHAT_ID})."
    ).format(ADMIN_CHAT_ID=ADMIN_CHAT_ID)
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown', reply_markup=main_menu_markup)
    log_statistics('help_requested', message.chat.id)

@error_handler
def send_channel_link(message):
    """Надсилає користувачеві посилання на канал з товарами."""
    try:
        channel_info = bot.get_chat(CHANNEL_ID)
        if channel_info.username:
            channel_link = f"https://t.me/{channel_info.username}"
        else:
            channel_link = "На жаль, не можу згенерувати посилання на приватний канал. Зверніться до адміністратора."
            logger.warning(f"Не вдалося отримати публічне посилання для каналу ID: {CHANNEL_ID}. Канал, можливо, приватний.")
    except Exception as e:
        logger.error(f"Помилка отримання інформації про канал {CHANNEL_ID}: {e}", exc_info=True)
        channel_link = "Вибачте, не можу отримати інформацію про канал. Можливо, канал приватний або сталася помилка."
    
    bot.send_message(message.chat.id, f"📺 *Наш канал з усіма оголошеннями:*\n{channel_link}", parse_mode='Markdown', reply_markup=main_menu_markup)
    log_statistics('channel_link_sent', message.chat.id)

# --- 15. Обробник Callback Query ---
@bot.callback_query_handler(func=lambda call: True)
@error_handler
def callback_inline(call):
    """
    Головний обробник всіх інлайн-callback запитів.
    Розбирає `callback_data` та викликає відповідні функції.
    """
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data
    logger.info(f"Отримано callback від {chat_id}: {data}")

    # Перевірка на блокування
    if is_user_blocked(chat_id):
        bot.answer_callback_query(call.id, "❌ Ваш акаунт заблоковано.")
        return

    # --- Обробка callback-ів для додавання товару (крок доставки) ---
    if data.startswith("shipping_"):
        shipping_option = data.replace("shipping_", "")
        if shipping_option == "next": # Користувач натиснув "Далі" на кроці доставки
            if chat_id in user_data and user_data[chat_id].get('flow') == 'add_product' and user_data[chat_id].get('step') == 'waiting_shipping':
                if not user_data[chat_id]['data']['shipping_options']:
                    bot.answer_callback_query(call.id, "Будь ласка, оберіть хоча б один спосіб доставки.", show_alert=True)
                    return
                go_to_next_step(chat_id)
                bot.delete_message(chat_id, message_id) # Видаляємо повідомлення з інлайн-клавіатурою
            else:
                bot.answer_callback_query(call.id, "Щось пішло не так у процесі додавання товару.")
        else: # Користувач обрав/зняв вибір опції доставки
            if chat_id in user_data and user_data[chat_id].get('flow') == 'add_product' and user_data[chat_id].get('step') == 'waiting_shipping':
                selected_options = user_data[chat_id]['data'].get('shipping_options', [])
                if shipping_option in selected_options:
                    selected_options.remove(shipping_option)
                else:
                    selected_options.append(shipping_option)
                user_data[chat_id]['data']['shipping_options'] = selected_options
                send_product_step_message(chat_id) # Оновлюємо повідомлення з новими галочками
                bot.answer_callback_query(call.id, f"Опції доставки оновлено.")
            else:
                bot.answer_callback_query(call.id, "Щось пішло не так у процесі додавання товару.")
        return # Завершуємо обробку callback

    # --- Обробка callback-ів для модерації товару ---
    if data.startswith("approve_"):
        if chat_id != ADMIN_CHAT_ID:
            bot.answer_callback_query(call.id, "У вас немає прав для цієї дії.")
            return
        product_id = int(data.replace("approve_", ""))
        bot.edit_message_text(f"⏳ Схвалення товару ID {product_id}...", chat_id, message_id)
        if update_product_status(product_id, 'approved', chat_id):
            send_product_details_to_channel(product_id, chat_id)
        else:
            bot.edit_message_text(f"❌ Помилка схвалення товару ID {product_id}.", chat_id, message_id, reply_markup=admin_panel_markup())
        bot.answer_callback_query(call.id)
        log_statistics('approve_product_callback', chat_id, product_id)
    
    elif data.startswith("reject_"):
        if chat_id != ADMIN_CHAT_ID:
            bot.answer_callback_query(call.id, "У вас немає прав для цієї дії.")
            return
        product_id = int(data.replace("reject_", ""))
        bot.edit_message_text(f"⏳ Відхилення товару ID {product_id}...", chat_id, message_id)
        reject_product_action(product_id, chat_id)
        bot.answer_callback_query(call.id)
        log_statistics('reject_product_callback', chat_id, product_id)

    elif data.startswith("mod_edit_tags_"):
        if chat_id != ADMIN_CHAT_ID:
            bot.answer_callback_query(call.id, "У вас немає прав для цієї дії.")
            return
        product_id = int(data.replace("mod_edit_tags_", ""))
        user_data[chat_id] = {'flow': 'mod_edit_tags', 'product_id': product_id}
        bot.send_message(chat_id, f"Введіть нові хештеги для товару ID `{product_id}` (через пробіл, наприклад: `#тег1 #тег2`).", parse_mode='Markdown')
        bot.answer_callback_query(call.id, "Очікую ввід хештегів.")
        log_statistics('mod_edit_tags_callback', chat_id, product_id)

    elif data.startswith("mod_rotate_photo_"):
        if chat_id != ADMIN_CHAT_ID:
            bot.answer_callback_query(call.id, "У вас немає прав для цієї дії.")
            return
        product_id = int(data.replace("mod_rotate_photo_", ""))
        product = get_product_info(product_id)
        if product and product['seller_chat_id']:
            bot.send_message(product['seller_chat_id'], 
                             f"📸 Адміністратор просить вас переглянути фото для товару ID `{product_id}`. "
                             "Можливо, потрібно замінити або оновити деякі зображення.", parse_mode='Markdown')
            bot.answer_callback_query(call.id, f"Запит на виправлення фото для товару {product_id} надіслано продавцю.")
            log_statistics('mod_rotate_photo_callback', chat_id, product_id)
        else:
            bot.answer_callback_query(call.id, "Не вдалося надіслати запит продавцю.")

    # --- Обробка callback-ів "Мої товари" ---
    elif data.startswith("view_prod_"):
        product_id = int(data.replace("view_prod_", ""))
        view_product_details(call, product_id)
        log_statistics('view_prod_callback', chat_id, product_id)

    elif data.startswith("delete_prod_"):
        product_id = int(data.replace("delete_prod_", ""))
        if delete_product(product_id, chat_id):
            bot.edit_message_text(f"🗑️ Товар `{product_id}` успішно видалено.", chat_id, message_id, parse_mode='Markdown')
            bot.answer_callback_query(call.id, "Товар видалено.")
            log_statistics('delete_prod_callback', chat_id, product_id)
        else:
            bot.answer_callback_query(call.id, "❌ Помилка видалення товару.", show_alert=True)

    elif data.startswith("mark_sold_"):
        product_id = int(data.replace("mark_sold_", ""))
        if mark_product_as_sold(product_id, chat_id):
            bot.edit_message_text(f"🏷️ Товар `{product_id}` успішно позначено як проданий.", chat_id, message_id, parse_mode='Markdown')
            bot.answer_callback_query(call.id, "Товар позначено як проданий.")
            log_statistics('mark_sold_callback', chat_id, product_id)
        else:
            bot.answer_callback_query(call.id, "❌ Помилка позначення товару як проданого.", show_alert=True)

    elif data.startswith("republish_"):
        product_id = int(data.replace("republish_", ""))
        product = get_product_info(product_id)
        if product and product['status'] == 'approved' and product['seller_chat_id'] == chat_id:
            # Перевіряємо, чи пройшло 7 днів з останньої перепублікації
            last_republish_date = product['last_republish_date']
            if last_republish_date:
                days_since_last_republish = (date.today() - last_republish_date).days
                if days_since_last_republish < 7:
                    remaining_days = 7 - days_since_last_republish
                    bot.answer_callback_query(call.id, f"⏳ Перепублікувати можна раз на 7 днів. Залишилось {remaining_days} дн.", show_alert=True)
                    return
            
            if republish_product(product_id, chat_id):
                bot.edit_message_text(f"🔄 Товар `{product_id}` успішно відправлено на перепублікацію. Він знову буде на модерації.", chat_id, message_id, parse_mode='Markdown')
                send_product_for_admin_review(product_id) # Повторно надсилаємо на модерацію
                bot.answer_callback_query(call.id, "Товар перепубліковано.")
                log_statistics('republish_callback', chat_id, product_id)
            else:
                bot.answer_callback_query(call.id, "❌ Помилка перепублікації товару.", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "Ви не можете перепублікувати цей товар.")

    elif data.startswith("change_price_"):
        product_id = int(data.replace("change_price_", ""))
        user_data[chat_id] = {'flow': 'change_price', 'product_id': product_id}
        bot.send_message(chat_id, f"Введіть нову ціну для товару ID `{product_id}` (наприклад, `500 грн`, `100 USD` або `Договірна`).", parse_mode='Markdown')
        bot.answer_callback_query(call.id, "Очікую нову ціну.")
        log_statistics('change_price_callback', chat_id, product_id)

    # --- Обробка callback-ів для обраних товарів ---
    elif data.startswith("fav_"):
        product_id = int(data.replace("fav_", ""))
        if add_to_favorites(chat_id, product_id):
            bot.answer_callback_query(call.id, "✅ Додано до обраного!")
            # Оновлюємо кнопку, щоб показати "Видалити з обраного"
            # Для цього потрібно отримати поточне повідомлення та оновити його markup
            product = get_product_info(product_id)
            if product:
                inline_markup = types.InlineKeyboardMarkup()
                inline_markup.add(
                    types.InlineKeyboardButton("✍️ Зв'язатися з продавцем", url=f"tg://user?id={product['seller_chat_id']}"),
                    types.InlineKeyboardButton("⭐ Видалити з обраного", callback_data=f"unfav_{product_id}")
                )
                try:
                    # Якщо це media group, потрібно оновити лише message_id що містить caption
                    if call.message.photo: # Перевіряємо, чи є фото в повідомленні
                        # Якщо фото є, ми не можемо просто редагувати markup, бо це media group
                        # Потрібно видалити старе повідомлення і надіслати нове.
                        # Але для простоти зараз оновимо тільки caption (якщо це єдине фото або перше в групі)
                        # або лише markup, якщо повідомлення було текстовим.
                        # Це складно коректно зробити для media_group, тому краще просто оновити caption,
                        # або попросити користувача переглянути товар знову.
                        # Для цього випадку, давайте просто оновимо caption, якщо це можливо, або проігноруємо оновлення кнопки.
                        if call.message.caption: # Якщо повідомлення з фото має caption
                            bot.edit_message_caption(caption=call.message.caption, chat_id=chat_id, message_id=message_id, reply_markup=inline_markup, parse_mode='Markdown')
                        else: # Якщо фото без caption (друге/третє фото в media group), тоді просто відповімо
                            pass # Не можемо змінити markup
                    else: # Якщо повідомлення було текстовим
                        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=inline_markup)
                except Exception as e:
                    logger.warning(f"Не вдалося оновити кнопку 'Обране' для повідомлення {message_id}: {e}")
            log_statistics('add_favorite_callback', chat_id, product_id)
        else:
            bot.answer_callback_query(call.id, "Цей товар вже є у вашому обраному.", show_alert=True)
    
    elif data.startswith("unfav_"):
        product_id = int(data.replace("unfav_", ""))
        if remove_from_favorites(chat_id, product_id):
            bot.answer_callback_query(call.id, "❌ Видалено з обраного!")
            # Оновлюємо кнопку, щоб показати "Додати в обране"
            product = get_product_info(product_id)
            if product:
                inline_markup = types.InlineKeyboardMarkup()
                inline_markup.add(
                    types.InlineKeyboardButton("✍️ Зв'язатися з продавцем", url=f"tg://user?id={product['seller_chat_id']}"),
                    types.InlineKeyboardButton("⭐ Додати в обране", callback_data=f"fav_{product_id}")
                )
                try:
                    if call.message.photo:
                        if call.message.caption:
                            bot.edit_message_caption(caption=call.message.caption, chat_id=chat_id, message_id=message_id, reply_markup=inline_markup, parse_mode='Markdown')
                        else:
                            pass
                    else:
                        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=inline_markup)
                except Exception as e:
                    logger.warning(f"Не вдалося оновити кнопку 'Обране' для повідомлення {message_id}: {e}")
            log_statistics('remove_favorite_callback', chat_id, product_id)
        else:
            bot.answer_callback_query(call.id, "Цього товару немає у вашому обраному.", show_alert=True)

    # --- Обробка callback-ів адмін-панелі ---
    elif data == "admin_stats":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_admin_statistics(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_stats_callback', chat_id)

    elif data == "admin_pending":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_pending_products_for_moderation(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_pending_callback', chat_id)

    elif data == "admin_users":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_users_list_admin(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_users_callback', chat_id)

    elif data == "admin_block":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_block_unblock_menu(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_block_callback', chat_id)

    elif data.startswith("block_user_"):
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        target_chat_id = int(data.replace("block_user_", ""))
        if set_user_block_status(chat_id, target_chat_id, True):
            bot.edit_message_text(f"✅ Користувача `{target_chat_id}` заблоковано.", chat_id, message_id, parse_mode='Markdown')
            try:
                bot.send_message(target_chat_id, "❌ Ваш акаунт було заблоковано адміністратором. Ви більше не можете користуватися ботом.")
            except Exception as e:
                logger.warning(f"Не вдалося повідомити заблокованого користувача {target_chat_id}: {e}")
            log_statistics('user_blocked', chat_id, target_chat_id)
        else:
            bot.edit_message_text(f"❌ Помилка при блокуванні користувача з ID `{target_chat_id}`.", chat_id, message_id, parse_mode='Markdown')
        bot.answer_callback_query(call.id)

    elif data.startswith("unblock_user_"):
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        target_chat_id = int(data.replace("unblock_user_", ""))
        if set_user_block_status(chat_id, target_chat_id, False):
            bot.edit_message_text(f"✅ Користувача `{target_chat_id}` розблоковано.", chat_id, message_id, parse_mode='Markdown')
            try:
                bot.send_message(target_chat_id, "✅ Ваш акаунт було розблоковано адміністратором. Тепер ви можете користуватися ботом.")
            except Exception as e:
                logger.warning(f"Не вдалося повідомити розблокованого користувача {target_chat_id}: {e}")
            log_statistics('user_unblocked', chat_id, target_chat_id)
        else:
            bot.edit_message_text(f"❌ Помилка при розблокуванні користувача з ID `{target_chat_id}`.", chat_id, message_id, parse_mode='Markdown')
        bot.answer_callback_query(call.id)

    elif data == "admin_commissions":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_commission_report(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_commissions_callback', chat_id)

    elif data == "admin_ai_stats":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_ai_statistics(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_ai_stats_callback', chat_id)

    elif data == "admin_referrals":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        send_referral_statistics(call)
        bot.answer_callback_query(call.id)
        log_statistics('admin_referrals_callback', chat_id)

    elif data == "admin_back":
        if chat_id != ADMIN_CHAT_ID: bot.answer_callback_query(call.id, "У вас немає прав."); return
        bot.edit_message_text("🔧 *Адмін-панель*", chat_id, message_id, reply_markup=admin_panel_markup(), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        log_statistics('admin_back_callback', chat_id)

    # Завжди відповідаємо на callback, щоб прибрати "годинник" з кнопки
    bot.answer_callback_query(call.id)


# --- Адміністративні функції (деталізація) ---
@error_handler
def send_pending_products_for_moderation(call):
    """Надсилає адміністратору список товарів, що очікують модерації."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні товарів на модерацію (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT id, seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, created_at
            FROM products
            WHERE status = 'pending'
            ORDER BY created_at ASC;
        """))
        pending_products = cur.fetchall()

        if not pending_products:
            bot.edit_message_text("✅ Наразі немає товарів на модерації.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
            return

        for product in pending_products:
            product_id = product['id']
            seller_chat_id = product['seller_chat_id']
            seller_username = product['seller_username'] if product['seller_username'] else "Не вказано"
            photos = json.loads(product['photos']) if product['photos'] else []
            geolocation = json.loads(product['geolocation']) if product['geolocation'] else None
            shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"


            review_text = (
                f"📦 *Товар на модерацію* (ID: {product_id})\n\n"
                f"📝 Назва: {product['product_name']}\n"
                f"💰 Ціна: {product['price']}\n"
                f"📄 Опис: {product['description'][:500]}...\n"
                f"📸 Фото: {len(photos)} шт.\n"
                f"📍 Геолокація: {'Так' if geolocation else 'Ні'}\n"
                f"🚚 Доставка: {shipping_options_text}\n"
                f"👤 Продавець: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={seller_chat_id})"
            )
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
                types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
            )
            markup.add(
                types.InlineKeyboardButton("✏️ Редагувати хештеги", callback_data=f"mod_edit_tags_{product_id}"),
                types.InlineKeyboardButton("🔄 Запит на виправлення фото", callback_data=f"mod_rotate_photo_{product_id}")
            )

            try:
                if photos:
                    media = [types.InputMediaPhoto(photo_id, caption=review_text if i == 0 else None, parse_mode='Markdown') 
                             for i, photo_id in enumerate(photos)]
                    sent_messages = bot.send_media_group(call.message.chat.id, media)
                    if sent_messages:
                        bot.send_message(call.message.chat.id, 
                                         f"👆 Деталі товару ID: {product_id} (фото вище)", 
                                         reply_markup=markup, 
                                         parse_mode='Markdown',
                                         reply_to_message_id=sent_messages[0].message_id)
                else:
                    bot.send_message(call.message.chat.id, review_text, parse_mode='Markdown', reply_markup=markup)
            except Exception as e:
                logger.error(f"Помилка відправки товару {product_id} на модерацію адміну: {e}", exc_info=True)
                bot.send_message(call.message.chat.id, f"❌ Помилка відображення товару ID {product_id}.")
        
        bot.send_message(call.message.chat.id, "--- Кінець списку товарів на модерації ---", reply_markup=admin_panel_markup())

    except Exception as e:
        logger.error(f"Помилка в send_pending_products_for_moderation: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося отримати товари на модерацію.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def send_users_list_admin(call):
    """Надсилає адміністратору список зареєстрованих користувачів."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні списку користувачів (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT chat_id, username, first_name, last_name, is_blocked, joined_at, last_activity, referrer_id
            FROM users ORDER BY joined_at DESC;
        """))
        users = cur.fetchall()

        if not users:
            bot.edit_message_text("Наразі немає зареєстрованих користувачів.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
            return

        response_text = "👥 *Список користувачів:*\n\n"
        for user in users:
            username_display = f"@{user['username']}" if user['username'] else "Н/Д"
            blocked_status = "🚫 Заблоковано" if user['is_blocked'] else "✅ Активний"
            response_text += (
                f"▪️ ID: `{user['chat_id']}`\n"
                f"   Ім'я: {user['first_name']} {user['last_name'] or ''} ({username_display})\n"
                f"   Статус: {blocked_status}\n"
                f"   Зареєстровано: {user['joined_at'].strftime('%Y-%m-%d %H:%M')}\n"
                f"   Ост. активність: {user['last_activity'].strftime('%Y-%m-%d %H:%M')}\n"
                f"   Реферер: {user['referrer_id'] or 'Немає'}\n\n"
            )
        
        if len(response_text) > 4096:
            response_text = response_text[:4000] + "...\n\n(Повний список дуже довгий, дивіться логи або запитайте конкретніше)"

        bot.edit_message_text(response_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=admin_panel_markup())
    except Exception as e:
        logger.error(f"Помилка в send_users_list_admin: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося отримати список користувачів.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def send_block_unblock_menu(call):
    """Надсилає адміністратору меню для блокування/розблокування користувачів."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні списку користувачів (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT chat_id, username, first_name, last_name, is_blocked
            FROM users ORDER BY is_blocked DESC, joined_at DESC;
        """))
        users = cur.fetchall()

        if not users:
            bot.edit_message_text("Наразі немає користувачів для блокування/розблокування.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        for user in users:
            status_text = "🚫 Заблокувати" if not user['is_blocked'] else "✅ Розблокувати"
            button_data = f"block_user_{user['chat_id']}" if not user['is_blocked'] else f"unblock_user_{user['chat_id']}"
            username_display = f"@{user['username']}" if user['username'] else f"ID: {user['chat_id']}"
            markup.add(types.InlineKeyboardButton(f"{status_text} {user['first_name']} {user['last_name'] or ''} ({username_display})", callback_data=button_data))
        
        markup.add(types.InlineKeyboardButton("🔙 Назад до адмін-панелі", callback_data="admin_back"))
        bot.edit_message_text("👥 *Керування користувачами (блокування/розблокування)*\n\nОберіть користувача:", 
                              call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logger.error(f"Помилка в send_block_unblock_menu: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося завантажити меню блокування.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def send_commission_report(call):
    """Надсилає адміністратору звіт про комісії."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні звіту по комісіях (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT 
                p.id AS product_id,
                p.product_name,
                p.seller_chat_id,
                u.username AS seller_username,
                p.price,
                p.commission_amount,
                p.status AS product_status,
                ct.status AS transaction_status,
                ct.created_at AS transaction_date,
                ct.paid_at AS paid_date
            FROM products p
            LEFT JOIN commission_transactions ct ON p.id = ct.product_id
            LEFT JOIN users u ON p.seller_chat_id = u.chat_id
            WHERE p.status = 'sold' AND (ct.status IS NULL OR ct.status = 'pending_payment')
            ORDER BY ct.created_at ASC;
        """))
        pending_commissions = cur.fetchall()

        total_due = 0.0
        report_text = "💰 *Звіт по комісіях (очікуються до сплати):*\n\n"
        if not pending_commissions:
            report_text += "Наразі немає очікуваних комісій до сплати."
        else:
            for item in pending_commissions:
                commission = item['commission_amount'] if item['commission_amount'] is not None else 0.0
                total_due += commission
                seller_username_display = f"@{item['seller_username']}" if item['seller_username'] else f"ID: {item['seller_chat_id']}"
                report_text += (
                    f"▪️ Товар ID `{item['product_id']}`: *{item['product_name'][:50]}*\n"
                    f"   Продавець: [{seller_username_display}](tg://user?id={item['seller_chat_id']})\n"
                    f"   Ціна: {item['price']}\n"
                    f"   Комісія: `{commission:.2f}`\n"
                    f"   Статус (товар): `{item['product_status']}`\n"
                    f"   Статус (транзакція): `{item['transaction_status'] or 'немає'}`\n"
                    f"   Дата продажу: {item['transaction_date'].strftime('%Y-%m-%d') if item['transaction_date'] else 'Н/Д'}\n\n"
                )
            report_text += f"\n*Загальна сума до сплати: {total_due:.2f} UAH*\n\n"
            report_text += f"Номер картки Monobank для платежів: `{MONOBANK_CARD_NUMBER}`"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад до адмін-панелі", callback_data="admin_back"))
        bot.edit_message_text(report_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

    except Exception as e:
        logger.error(f"Помилка в send_commission_report: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося отримати звіт по комісіях.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def send_ai_statistics(call):
    """Надсилає адміністратору статистику використання AI помічника."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні AI статистики (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT 
                COUNT(*) AS total_messages,
                COUNT(DISTINCT user_chat_id) AS unique_users,
                (SELECT COUNT(*) FROM conversations WHERE sender_type = 'user') AS user_messages,
                (SELECT COUNT(*) FROM conversations WHERE sender_type = 'ai') AS ai_messages
            FROM conversations;
        """))
        stats = cur.fetchone()

        report_text = "🤖 *Статистика AI Помічника:*\n\n"
        if stats:
            report_text += f"▪️ Загальна кількість повідомлень: `{stats['total_messages']}`\n"
            report_text += f"▪️ Унікальних користувачів: `{stats['unique_users']}`\n"
            report_text += f"▪️ Повідомлень від користувачів: `{stats['user_messages']}`\n"
            report_text += f"▪️ Повідомлень від AI: `{stats['ai_messages']}`\n"
        else:
            report_text += "Дані про використання AI відсутні."

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад до адмін-панелі", callback_data="admin_back"))
        bot.edit_message_text(report_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

    except Exception as e:
        logger.error(f"Помилка в send_ai_statistics: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося отримати AI статистику.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

@error_handler
def send_referral_statistics(call):
    """Надсилає адміністратору статистику рефералів."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні реферальної статистики (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT 
                referrer_id, 
                COUNT(chat_id) AS referred_count,
                MAX(r.username) AS referrer_username,
                MAX(r.first_name) AS referrer_first_name,
                MAX(r.last_name) AS referrer_last_name
            FROM users r
            JOIN users u ON r.chat_id = u.referrer_id
            WHERE r.referrer_id IS NOT NULL OR u.referrer_id IS NOT NULL -- Для уникнення випадків, коли реферер ще не в таблиці users
            GROUP BY referrer_id
            ORDER BY referred_count DESC;
        """))
        referrals = cur.fetchall()

        report_text = "🏆 *Реферальна статистика:*\n\n"
        if not referrals:
            report_text += "Наразі немає даних по рефералах."
        else:
            for ref in referrals:
                referrer_username_display = f"@{ref['referrer_username']}" if ref['referrer_username'] else "Н/Д"
                report_text += (
                    f"▪️ Реферер ID: `{ref['referrer_id']}`\n"
                    f"   Ім'я: {ref['referrer_first_name']} {ref['referrer_last_name'] or ''} ({referrer_username_display})\n"
                    f"   Запрошених користувачів: `{ref['referred_count']}`\n\n"
                )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад до адмін-панелі", callback_data="admin_back"))
        bot.edit_message_text(report_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

    except Exception as e:
        logger.error(f"Помилка в send_referral_statistics: {e}", exc_info=True)
        bot.edit_message_text("❌ Не вдалося отримати реферальну статистику.", call.message.chat.id, call.message.message_id, reply_markup=admin_panel_markup())
    finally:
        if conn:
            conn.close()

# --- 16. Запуск Бота ---
if __name__ == '__main__':
    # Ініціалізуємо базу даних при запуску бота
    init_db()
    
    # Налаштування вебхука для Render
    try:
        bot.remove_webhook() # Видаляємо старий вебхук, якщо є
        time.sleep(0.1) # Коротка пауза для впевненості, що вебхук видалено
        bot.set_webhook(url=WEBHOOK_URL + TOKEN)
        logger.info(f"Webhook встановлено на: {WEBHOOK_URL + TOKEN}")
    except Exception as e:
        logger.critical(f"Помилка встановлення вебхука: {e}")
        exit(1) # Завершуємо роботу, якщо вебхук не встановився

    # Обробник вебхуків Flask
    @app.route(f'/{TOKEN}', methods=['POST'])
    def webhook_handler():
        """
        Обробник POST-запитів, що надходять від Telegram API.
        Парсить JSON-оновлення та передає їх telebot для обробки.
        """
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update]) # Обробка вхідних оновлень
            return '!', 200 # Повертаємо 200 OK Telegramу
        else:
            logger.warning("Отримано запит до вебхука без правильного Content-Type (application/json).")
            return 'Content-Type must be application/json', 403 # Відхиляємо некоректні запити

    # Запускаємо Flask-додаток.
    # Render автоматично встановлює змінну середовища PORT.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    logger.info(f"Flask-додаток запущено на порту {port}")

