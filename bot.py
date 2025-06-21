import os
import telebot
from telebot import types
import logging
from datetime import datetime, timedelta, timezone
import re
import json
import requests
from dotenv import load_dotenv

from flask import Flask, request

import psycopg2
from psycopg2 import sql as pg_sql
from psycopg2 import extras

# --- Ініціалізація ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL")  # напр. https://your-app.onrender.com

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "Webhook received!", 200
    return "Invalid request", 403


@app.before_first_request
def setup_webhook():
    bot.remove_webhook()
    full_webhook_url = f"{WEBHOOK_BASE_URL}/{BOT_TOKEN}"
    bot.set_webhook(url=full_webhook_url)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


# Завантажуємо змінні оточення
load_dotenv()

# Конфігурація Бота та змінні оточення
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '0'))
MONOBANK_CARD_NUMBER = os.getenv('MONOBANK_CARD_NUMBER', 'Не вказано')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = os.getenv('GEMINI_API_URL', "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent")
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
DATABASE_URL = os.getenv('DATABASE_URL')

# Конфігурація логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Базова перевірка змінних оточення
def validate_env_vars():
    """Перевіряє наявність критично важливих змінних оточення."""
    missing_vars = []
    if not TOKEN: missing_vars.append('TELEGRAM_BOT_TOKEN')
    if not WEBHOOK_URL: missing_vars.append('WEBHOOK_URL')
    if not DATABASE_URL: missing_vars.append('DATABASE_URL')
    if ADMIN_CHAT_ID == 0: missing_vars.append('ADMIN_CHAT_ID')
    if CHANNEL_ID == 0: missing_vars.append('CHANNEL_ID')

    if missing_vars:
        logger.critical(f"Критична помилка: Відсутні наступні змінні оточення: {', '.join(missing_vars)}. Бот не може працювати.")
        exit(1)

validate_env_vars()

# Ініціалізація TeleBot та Flask
app = Flask(__name__)
bot = telebot.TeleBot(TOKEN)

# НАЛАШТУВАННЯ МЕРЕЖЕВИХ ЗАПИТІВ (RETRY-МЕХАНІЗМ)
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

# Декоратор для обробки помилок
def error_handler(func):
    """Декоратор для централізованої обробки помилок у функціях бота."""
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

# Підключення та ініціалізація Бази Даних (PostgreSQL)
def get_db_connection():
    """Встановлює з'єднання з базою даних PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return conn
    except Exception as e:
        logger.error(f"Помилка підключення до бази даних: {e}", exc_info=True)
        return None

@error_handler
def init_db():
    """Ініціалізує таблиці бази даних, якщо вони ще не існують."""
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
                    status TEXT DEFAULT 'pending', -- pending, approved, rejected, sold, expired
                    commission_rate REAL DEFAULT 0.10,
                    commission_amount REAL DEFAULT 0,
                    moderator_id BIGINT,
                    moderated_at TIMESTAMP WITH TIME ZONE,
                    admin_message_id BIGINT,
                    channel_message_id BIGINT,
                    views INTEGER DEFAULT 0,
                    likes_count INTEGER DEFAULT 0,
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
            
            # Міграція схеми для існуючих таблиць (додавання нових стовпців)
            migrations = {
                'products': [
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

# Зберігання даних користувача для багатошагових процесів
user_data = {}

# Функції роботи з користувачами та загальні допоміжні функції
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

# Gemini AI інтеграція
@error_handler
def get_gemini_response(prompt, conversation_history=None):
    """Отримання відповіді від Gemini AI."""
    if not GEMINI_API_KEY:
        logger.warning("Gemini API ключ не налаштований. Використовується заглушка.")
        return generate_elon_style_response(prompt)

    headers = {"Content-Type": "application/json"}
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

    payload = {"contents": gemini_messages}

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
    """Генерує відповіді в стилі Ілона Маска як заглушка."""
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

# Клавіатури
main_menu_markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
main_menu_markup.add(types.KeyboardButton("📦 Додати товар"), types.KeyboardButton("📋 Мої товари"))
main_menu_markup.add(types.KeyboardButton("⭐ Обрані"), types.KeyboardButton("❓ Допомога"))
main_menu_markup.add(types.KeyboardButton("📺 Наш канал"), types.KeyboardButton("🤖 AI Помічник"))

back_button = types.KeyboardButton("🔙 Назад")
cancel_button = types.KeyboardButton("❌ Скасувати")

# Обробники команд
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

# Потік додавання товару
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
    """Зберігає товар у БД та сповіщає адміністратора про новий товар на модерації."""
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
            return

        seller_chat_id = data['seller_chat_id']
        seller_username = data['seller_username'] if data['seller_username'] else "Не вказано"
        photos = json.loads(data['photos']) if data['photos'] else []
        geolocation = json.loads(data['geolocation']) if data['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(data['shipping_options'])) if data['shipping_options'] else "Не вказано"
        hashtags = data['hashtags'] if data['hashtags'] else ""

        review_text = (
            f"📦 *Новий товар на модерацію*\n\n"
            f"ID: {product_id}\n" # Змінено: прибрано "🆔"
            f"Назва: {data['product_name']}\n"
            f"Ціна: {data['price']}\n"
            f"Опис: {data['description'][:500]}...\n"
            f"Фото: {len(photos)} шт.\n"
            f"Геолокація: {'Так' if geolocation else 'Ні'}\n"
            f"Доставка: {shipping_options_text}\n"
            f"Хештеги: {hashtags}\n\n"
            f"👤 Продавець: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={seller_chat_id})"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
            types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
        )
        markup.add(
            types.InlineKeyboardButton("✏️ Редагувати хештеги", callback_data=f"mod_edit_tags_{product_id}"),
            types.InlineKeyboardButton("🔄 Запросити виправлення фото", callback_data=f"mod_request_photo_fix_{product_id}") 
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

# Обробники текстових повідомлень та кнопок меню
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

# Функції розділів меню
@error_handler
def send_my_products(message):
    """Надсилає користувачу список його товарів з бази даних."""
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "❌ Не вдалося отримати список ваших товарів (помилка БД).")
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT id, product_name, status, price, created_at, channel_message_id, views, likes_count
            FROM products
            WHERE seller_chat_id = %s
            ORDER BY created_at DESC
        """), (chat_id,))
        user_products = cur.fetchall()
    except Exception as e:
        logger.error(f"Помилка при отриманні товарів для користувача {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "❌ Не вдалося отримати список ваших товарів.")
        return
    finally:
        if conn:
            conn.close()

    if user_products:
        response_intro = "📋 *Ваші товари:*\n\n"
        bot.send_message(chat_id, response_intro, parse_mode='Markdown')

        for i, product in enumerate(user_products, 1):
            product_id = product['id']
            status_emoji = {
                'pending': '⏳',
                'approved': '✅',
                'rejected': '❌',
                'sold': '💰',
                'expired': '🗑️'
            }
            status_ukr = {
                'pending': 'на розгляді',
                'approved': 'опубліковано',
                'rejected': 'відхилено',
                'sold': 'продано',
                'expired': 'термін дії закінчився'
            }.get(product['status'], product['status'])

            created_at_local = product['created_at'].astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')

            # Формат тексту для "Мої товари" - прибрати ID та фото, прибрати "Назва", "Ціна"
            product_text = f"{i}. {status_emoji.get(product['status'], '❓')} *{product['product_name']}*\n"
            product_text += f"   {product['price']}\n" # Прибрано "💰 "
            product_text += f"   📅 {created_at_local}\n"
            product_text += f"   📊 Статус: {status_ukr}\n"
            
            markup = types.InlineKeyboardMarkup(row_width=2)

            if product['status'] == 'approved':
                product_text += f"   👁️ Перегляди: {product['views']}\n"
                product_text += f"   ❤️ Лайки: {product['likes_count']}\n"
                
                channel_link_part = str(CHANNEL_ID).replace("-100", "") 
                channel_url = f"https://t.me/c/{channel_link_part}/{product['channel_message_id']}" if product['channel_message_id'] else None
                
                if channel_url:
                    markup.add(types.InlineKeyboardButton("👀 Переглянути в каналі", url=channel_url))
                
                # Переопублікувати (без лімітів)
                markup.add(types.InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"republish_{product_id}"))

                markup.add(types.InlineKeyboardButton("✅ Продано", callback_data=f"sold_my_{product_id}"))
                markup.add(types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{product_id}"))
                markup.add(types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_my_{product_id}"))

            elif product['status'] in ['sold', 'pending', 'rejected', 'expired']:
                markup.add(types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_my_{product_id}"))
            
            bot.send_message(chat_id, product_text, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)

    else:
        bot.send_message(chat_id, "📭 Ви ще не додавали жодних товарів.\n\nНатисніть '📦 Додати товар' щоб створити своє перше оголошення!")

@error_handler
def send_favorites(message):
    """Надсилає користувачу список його обраних товарів."""
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "❌ Не вдалося отримати список обраних товарів (помилка БД).")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT p.id, p.product_name, p.price, p.status, p.channel_message_id, p.likes_count
            FROM products p
            JOIN favorites f ON p.id = f.product_id
            WHERE f.user_chat_id = %s AND p.status = 'approved'
            ORDER BY p.created_at DESC;
        """), (chat_id,))
        favorites = cur.fetchall()

        if not favorites:
            bot.send_message(chat_id, "📜 Ваш список обраних порожній. Ви можете додати товар, натиснувши ❤️ під ним у каналі.")
            return

        bot.send_message(chat_id, "⭐ *Ваші обрані товари:*", parse_mode='Markdown')
        for fav in favorites:
            channel_link_part = str(CHANNEL_ID).replace("-100", "")
            url = f"https://t.me/c/{channel_link_part}/{fav['channel_message_id']}" if fav['channel_message_id'] else None

            text = (
                f"*{fav['product_name']}*\n"
                f"   {fav['price']}\n"
                f"   ❤️ Лайки: {fav['likes_count']}\n"
            )
            markup = types.InlineKeyboardMarkup()
            if url:
                markup.add(types.InlineKeyboardButton("👀 Переглянути в каналі", url=url))
            
            markup.add(types.InlineKeyboardButton("💔 Видалити з обраного", callback_data=f"toggle_favorite_{fav['id']}_{fav['channel_message_id']}"))

            bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)
            
    except Exception as e:
        logger.error(f"Помилка при отриманні обраних товарів для користувача {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "❌ Не вдалося отримати список обраних товарів.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_help_message(message):
    """Надсилає користувачу довідкову інформацію про бота та його функції."""
    help_text = (
        "🆘 *Довідка*\n\n"
        "🤖 Я ваш AI-помічник для купівлі та продажу. Ви можете:\n"
        "📦 *Додати товар* - створити оголошення.\n"
        "📋 *Мої товари* - переглянути ваші активні та продані товари.\n"
        "⭐ *Обрані* - переглянути товари, які ви позначили як улюблені.\n"
        "📺 *Наш канал* - переглянути всі актуальні пропозиції та взяти участь у розіграшах.\n"
        "🤖 *AI Помічник* - поспілкуватися з AI.\n\n"
        "✍️ *Правила сервісу*:\n"
        "– Доставку оплачує *покупець*.\n"
        "– Комісію сервісу сплачує *продавець*.\n\n"
        "🗣️ *Спілкування:* Просто пишіть мені ваші запитання або пропозиції, і мій вбудований AI спробує вам допомогти!\n\n"
        f"Якщо виникли технічні проблеми, зверніться до адміністратора."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💰 Детальніше про комісію", callback_data="show_commission_info"))
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown', reply_markup=markup)

@error_handler
def send_commission_info(call):
    """Надсилає користувачу інформацію про комісію бота."""
    commission_rate_percent = 10
    text = (
        f"💰 *Інформація про комісію*\n\n"
        f"За успішний продаж товару через нашого бота стягується комісія у розмірі **{commission_rate_percent}%** від кінцевої ціни продажу.\n\n"
        f"Після того, як ви позначите товар як 'Продано', система розрахує суму комісії, і ви отримаєте інструкції щодо її сплати.\n\n"
        f"Реквізити для сплати комісії (Monobank):\n`{MONOBANK_CARD_NUMBER}`\n\n"
        f"Будь ласка, сплачуйте комісію вчасно, щоб уникнути обмежень на використання бота.\n\n"
        f"Детальніше про ваші поточні нарахування та сплати можна буде дізнатися в розділі 'Мої товари'."
    )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, parse_mode='Markdown')

@error_handler
def send_channel_link(message):
    """Надсилає посилання на канал з оголошеннями та інформацію про реферальну систему."""
    chat_id = message.chat.id
    try:
        if not CHANNEL_ID:
            raise ValueError("CHANNEL_ID не встановлено у .env. Неможливо сформувати посилання на канал.")

        chat_info = bot.get_chat(CHANNEL_ID)
        channel_link = ""
        if chat_info.invite_link:
            channel_link = chat_info.invite_link
        elif chat_info.username:
            channel_link = f"https://t.me/{chat_info.username}"
        else:
            try:
                invite_link_obj = bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                channel_link = invite_link_obj.invite_link
                logger.info(f"Згенеровано нове посилання на запрошення для каналу: {channel_link}")
            except telebot.apihelper.ApiTelegramException as e:
                logger.warning(f"Не вдалося створити посилання на запрошення для каналу {CHANNEL_ID}: {e}")
                channel_link_part = str(CHANNEL_ID).replace("-100", "") 
                channel_link = f"https://t.me/c/{channel_link_part}"


        if not channel_link:
             raise Exception("Не вдалося сформувати посилання на канал.")

        bot_username = bot.get_me().username
        referral_link = f"https://t.me/{bot_username}?start={chat_id}"

        invite_text = (
            f"📺 *Наш канал з оголошеннями*\n\n"
            f"Приєднуйтесь до нашого каналу, щоб не пропустити нові товари!\n\n"
            f"👉 [Перейти до каналу]({channel_link})\n\n"
            f"🏆 *Приводьте друзів та вигравайте гроші!*\n"
            f"Поділіться вашим особистим посиланням з друзями. "
            f"Коли новий користувач приєднається, ви стаєте учасником щотижневих розіграшів!\n\n"
            f"🔗 *Ваше посилання для запрошення:*\n`{referral_link}`"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏆 Переможці розіграшів", callback_data="show_winners_menu"))

        bot.send_message(chat_id, invite_text, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=markup)
        log_statistics('channel_visit', chat_id)

    except Exception as e:
        logger.error(f"Помилка при отриманні або формуванні посилання на канал: {e}", exc_info=True)
        bot.send_message(chat_id, "❌ На жаль, посилання на канал тимчасово недоступне. Зверніться до адміністратора.")

# Обробники Callback Query
@bot.callback_query_handler(func=lambda call: True)
@error_handler
def callback_inline(call):
    """Основний обробник для всіх інлайн-кнопок."""
    action, *params = call.data.split('_')

    if action == 'admin':
        handle_admin_callbacks(call)
    elif action == 'approve' or action == 'reject':
        handle_product_moderation_callbacks(call)
    elif action == 'mod':
        # Розширена обробка для mod_edit_tags_ та mod_request_photo_fix_
        if call.data.startswith("mod_edit_tags_"):
            product_id = int(call.data.split("_")[-1])
            user_data[call.from_user.id] = {'flow': 'mod_edit_tags', 'product_id': product_id}
            bot.send_message(call.from_user.id, f"Введіть нові хештеги для товару ID {product_id} (через пробіл, без #):", 
                             reply_markup=types.ForceReply(selective=True))
            bot.answer_callback_query(call.id)
        elif call.data.startswith("mod_request_photo_fix_"):
            product_id = int(call.data.split("_")[-1])
            conn = get_db_connection()
            if not conn:
                bot.answer_callback_query(call.id, "❌ Помилка БД.")
                return
            cur = conn.cursor()
            try:
                cur.execute(pg_sql.SQL("SELECT seller_chat_id, product_name FROM products WHERE id = %s"), (product_id,))
                product = cur.fetchone()
                if product:
                    bot.send_message(product['seller_chat_id'], 
                                     f"❗️ *Модератор просить вас виправити фото для товару '{product['product_name']}'* (ID: {product_id}).\n"
                                     "Будь ласка, видаліть це оголошення та додайте заново з коректними фотографіями.",
                                     parse_mode='Markdown')
                    bot.answer_callback_query(call.id, "Запит на виправлення фото відправлено продавцю.")
                else:
                    bot.answer_callback_query(call.id, "Товар не знайдено.")
            except Exception as e:
                logger.error(f"Помилка при запиті виправлення фото для товару {product_id}: {e}", exc_info=True)
                bot.answer_callback_query(call.id, "❌ Виникла помилка при відправці запиту.")
            finally:
                if conn:
                    conn.close()
        else:
            bot.answer_callback_query(call.id, "Невідома дія модератора.")
    elif action == 'user': 
        handle_user_block_callbacks(call)
    
    elif action == 'sold' and len(params) > 1 and params[0] == 'my':
        handle_seller_sold_product(call)
    elif action == 'delete' and len(params) > 1 and params[0] == 'my':
        handle_delete_my_product(call)
    elif action == 'republish':
        handle_republish_product(call)
    elif call.data == "republish_limit_reached":
        # Ця кнопка більше не повинна з'являтися, але залишимо обробник на випадок
        bot.answer_callback_query(call.id, "Ліміт переопублікацій знято, ця кнопка застаріла.")
    elif action == 'change' and len(params) > 0 and params[0] == 'price':
        handle_change_price_init(call)

    elif action == 'toggle' and len(params) > 0 and params[0] == 'favorite':
        handle_toggle_favorite(call)

    elif action == 'shipping':
        handle_shipping_choice(call)

    elif call.data == 'show_commission_info':
        send_commission_info(call)
    elif call.data == 'show_winners_menu':
        handle_winners_menu(call)
    elif action == 'winners':
        handle_show_winners(call)
    elif action == 'runraffle':
        handle_run_raffle(call)
    
    else:
        bot.answer_callback_query(call.id, "Невідома дія.")

# Callbacks для Адмін-панелі
@error_handler
def handle_admin_callbacks(call):
    """Обробляє колбеки, пов'язані з адмін-панеллю."""
    if call.message.chat.id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Доступ заборонено.")
        return

    action = call.data.split('_')[1]

    if action == "stats":
        send_admin_statistics(call)
    elif action == "pending":
        send_pending_products_for_moderation(call)
    elif action == "users":
        send_users_list(call)
    elif action == "block":
        bot.edit_message_text("Введіть `chat_id` або `@username` користувача для блокування/розблокування:",
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id, parse_mode='Markdown')
        bot.register_next_step_handler(call.message, process_user_for_block_unblock)
    elif action == "commissions":
        send_admin_commissions_info(call)
    elif action == "ai_stats":
        send_admin_ai_statistics(call)
    elif action == "referrals":
        send_admin_referral_stats(call)

    bot.answer_callback_query(call.id)

@error_handler
def send_admin_statistics(call):
    """Надсилає адміністратору загальну статистику бота."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні статистики (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("SELECT status, COUNT(*) FROM products GROUP BY status;"))
        product_stats = dict(cur.fetchall())

        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM users;"))
        total_users = cur.fetchone()[0]

        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM users WHERE is_blocked = TRUE;"))
        blocked_users_count = cur.fetchone()[0]

        today_utc = datetime.now(timezone.utc).date()
        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM products WHERE DATE(created_at) = %s;"), (today_utc,))
        today_products = cur.fetchone()[0]
        
        cur.execute(pg_sql.SQL("SELECT SUM(likes_count) FROM products;"))
        total_likes = cur.fetchone()[0] or 0

    except Exception as e:
        logger.error(f"Помилка при отриманні адміністративної статистики: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні статистики.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    stats_text = (
        f"📊 *Статистика бота*\n\n"
        f"👥 *Користувачі:*\n"
        f"• Всього: {total_users}\n"
        f"• Заблоковані: {blocked_users_count}\n\n"
        f"📦 *Товари:*\n"
        f"• На модерації: {product_stats.get('pending', 0)}\n"
        f"• Опубліковано: {product_stats.get('approved', 0)}\n"
        f"• Відхилено: {product_stats.get('rejected', 0)}\n"
        f"• Продано: {product_stats.get('sold', 0)}\n"
        f"• Термін дії закінчився: {product_stats.get('expired', 0)}\n\n"
        f"📅 *Сьогодні додано:* {today_products}\n"
        f"📈 *Всього товарів:* {sum(product_stats.values())}\n"
        f"❤️ *Всього лайків:* {total_likes}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))

    bot.edit_message_text(stats_text, call.message.chat.id, call.message.message_id,
                         parse_mode='Markdown', reply_markup=markup)

@error_handler
def send_users_list(call):
    """Надсилає адміністратору список останніх зареєстрованих користувачів."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні списку користувачів (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("SELECT chat_id, username, first_name, is_blocked FROM users ORDER BY joined_at DESC LIMIT 20;"))
        users = cur.fetchall()
    except Exception as e:
        logger.error(f"Помилка при отриманні списку користувачів: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні списку користувачів.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    if not users:
        response_text = "🤷‍♂️ Немає зареєстрованих користувачів."
    else:
        response_text = "👥 *Список останніх користувачів:*\n\n"
        for user in users:
            block_status = "🚫 Заблоковано" if user['is_blocked'] else "✅ Активний"
            username = f"@{user['username']}" if user['username'] else "Немає юзернейму"
            first_name = user['first_name'] if user['first_name'] else "Невідоме ім'я"
            response_text += f"- {first_name} ({username}) [ID: `{user['chat_id']}`] - {block_status}\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))

    bot.edit_message_text(response_text, call.message.chat.id, call.message.message_id,
                         parse_mode='Markdown', reply_markup=markup)

@error_handler
def process_user_for_block_unblock(message):
    """Обробляє введення адміністратором chat_id або username для блокування/розблокування користувача."""
    admin_chat_id = message.chat.id
    target_identifier = message.text.strip()
    target_chat_id = None

    conn = get_db_connection()
    if not conn:
        bot.send_message(admin_chat_id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()

    try:
        if target_identifier.startswith('@'):
            username = target_identifier[1:]
            cur.execute(pg_sql.SQL("SELECT chat_id FROM users WHERE username = %s;"), (username,))
            result = cur.fetchone()
            if result:
                target_chat_id = result['chat_id']
            else:
                bot.send_message(admin_chat_id, f"Користувача з юзернеймом `{target_identifier}` не знайдено.")
                return
        else:
            try:
                target_chat_id = int(target_identifier)
                cur.execute(pg_sql.SQL("SELECT chat_id FROM users WHERE chat_id = %s;"), (target_chat_id,))
                if not cur.fetchone():
                    bot.send_message(admin_chat_id, f"Користувача з ID `{target_chat_id}` не знайдено в базі даних.")
                    return
            except ValueError:
                bot.send_message(admin_chat_id, "Будь ласка, введіть дійсний `chat_id` (число) або `@username`.")
                return

        if target_chat_id == ADMIN_CHAT_ID:
            bot.send_message(admin_chat_id, "Ви не можете заблокувати/розблокувати себе.")
            return

        if target_chat_id:
            current_status = is_user_blocked(target_chat_id)
            action_text = "заблокувати" if not current_status else "розблокувати"
            confirmation_text = f"Ви впевнені, що хочете {action_text} користувача з ID `{target_chat_id}` (натисніть кнопку)?\n"

            markup = types.InlineKeyboardMarkup()
            if not current_status:
                markup.add(types.InlineKeyboardButton("🚫 Заблокувати", callback_data=f"user_block_{target_chat_id}"))
            else:
                markup.add(types.InlineKeyboardButton("✅ Розблокувати", callback_data=f"user_unblock_{target_chat_id}"))
            markup.add(types.InlineKeyboardButton("Скасувати", callback_data="admin_panel_main"))

            bot.send_message(admin_chat_id, confirmation_text, reply_markup=markup, parse_mode='Markdown')
        else:
            bot.send_message(admin_chat_id, "Користувача не знайдено.")
    except Exception as e:
        logger.error(f"Помилка при обробці користувача для блокування/розблокування: {e}", exc_info=True)
        bot.send_message(admin_chat_id, "❌ Виникла помилка при обробці запиту.")
    finally:
        if conn:
            conn.close()

@error_handler
def handle_user_block_callbacks(call):
    """Обробляє колбеки блокування/розблокування користувачів від адмін-панелі."""
    admin_chat_id = call.message.chat.id
    data_parts = call.data.split('_')
    action = data_parts[1]
    target_chat_id = int(data_parts[2])

    if action == 'block':
        success = set_user_block_status(admin_chat_id, target_chat_id, True)
        if success:
            bot.edit_message_text(f"Користувача з ID `{target_chat_id}` успішно заблоковано.",
                                  chat_id=admin_chat_id, message_id=call.message.message_id, parse_mode='Markdown')
            try:
                bot.send_message(target_chat_id, "❌ Ваш акаунт було заблоковано адміністратором.")
            except Exception as e:
                logger.warning(f"Не вдалося повідомити заблокованого користувача {target_chat_id}: {e}")
            log_statistics('user_blocked', admin_chat_id, target_chat_id)
        else:
            bot.edit_message_text(f"❌ Помилка при блокуванні користувача з ID `{target_chat_id}`.",
                                  chat_id=admin_chat_id, message_id=call.message.message_id, parse_mode='Markdown')
    elif action == 'unblock':
        success = set_user_block_status(admin_chat_id, target_chat_id, False)
        if success:
            bot.edit_message_text(f"Користувача з ID `{target_chat_id}` успішно розблоковано.",
                                  chat_id=admin_chat_id, message_id=call.message.message_id, parse_mode='Markdown')
            try:
                bot.send_message(target_chat_id, "✅ Ваш акаунт було розблоковано адміністратором. Тепер ви можете користуватися ботом.")
            except Exception as e:
                logger.warning(f"Не вдалося повідомити розблокованого користувача {target_chat_id}: {e}")
            log_statistics('user_unblocked', admin_chat_id, target_chat_id)
        else:
            bot.edit_message_text(f"❌ Помилка при розблокуванні користувача з ID `{target_chat_id}`.",
                                  chat_id=admin_chat_id, message_id=call.message.message_id, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

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
            SELECT id, seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, hashtags, created_at
            FROM products
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 5
        """))
        pending_products = cur.fetchall()
    except Exception as e:
        logger.error(f"Помилка при отриманні товарів на модерацію: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні товарів на модерацію.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    if not pending_products:
        response_text = "🎉 Немає товарів на модерації."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))
        bot.edit_message_text(response_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        return

    for product in pending_products:
        product_id = product['id']
        seller_chat_id = product['seller_chat_id']
        seller_username = product['seller_username'] if product['seller_username'] else "Немає"
        photos = json.loads(product['photos']) if product['photos'] else []
        geolocation_data = json.loads(product['geolocation']) if product['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"
        hashtags = product['hashtags'] if product['hashtags'] else generate_hashtags(product['description'])
        
        created_at_local = product['created_at'].astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')

        admin_message_text = (
            f"📩 *Товар на модерацію (ID: {product_id})*\n\n"
            f"📦 *Назва:* {product['product_name']}\n"
            f"💰 *Ціна:* {product['price']}\n"
            f"📝 *Опис:* {product['description'][:500]}...\n"
            f"📍 Геолокація: {'Так' if geolocation_data else 'Ні'}\n"
            f"🚚 Доставка: {shipping_options_text}\n"
            f"🏷️ *Хештеги:* {hashtags}\n\n"
            f"👤 *Продавець:* [{'@' + seller_username if seller_username != 'Немає' else 'Користувач'}](tg://user?id={seller_chat_id})\n"
            f"📸 *Фото:* {len(photos)} шт.\n"
            f"📅 *Додано:* {created_at_local}"
        )

        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(
            types.InlineKeyboardButton("✅ Опублікувати", callback_data=f"approve_{product_id}"),
            types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
        )
        markup_admin.add(
            types.InlineKeyboardButton("✏️ Редагувати хештеги", callback_data=f"mod_edit_tags_{product_id}"),
            types.InlineKeyboardButton("🔄 Запит на виправлення фото", callback_data=f"mod_request_photo_fix_{product_id}") 
        )
        
        try:
            if photos:
                media = [types.InputMediaPhoto(photo_id, caption=admin_message_text if i == 0 else None, parse_mode='Markdown') 
                         for i, photo_id in enumerate(photos)]
                bot.send_media_group(call.message.chat.id, media)
                
                bot.send_message(call.message.chat.id, f"👆 Модерація товару ID: {product_id} (фото вище)", reply_markup=markup_admin, parse_mode='Markdown')
            else:
                bot.send_message(call.message.chat.id, admin_message_text,
                                   parse_mode='Markdown',
                                   reply_markup=markup_admin)
        except Exception as e:
            logger.error(f"Помилка при відправці товару {product_id} на модерацію адміністратору: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ Не вдалося відправити товар {product_id} для модерації.")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))
    bot.send_message(call.message.chat.id, "⬆️ Перегляньте товари на модерації вище.", reply_markup=markup)

@error_handler
def send_admin_commissions_info(call):
    """Надсилає адміністратору інформацію про комісії та останні транзакції."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні інформації про комісії (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT 
                SUM(CASE WHEN status = 'pending_payment' THEN amount ELSE 0 END) AS total_pending,
                SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS total_paid
            FROM commission_transactions;
        """))
        commission_summary = cur.fetchone()

        cur.execute(pg_sql.SQL("""
            SELECT ct.product_id, p.product_name, p.seller_chat_id, u.username, ct.amount, ct.status, ct.created_at
            FROM commission_transactions ct
            JOIN products p ON ct.product_id = p.id
            JOIN users u ON p.seller_chat_id = u.chat_id
            ORDER BY ct.created_at DESC
            LIMIT 10;
        """))
        recent_transactions = cur.fetchall()

    except Exception as e:
        logger.error(f"Помилка при отриманні інформації про комісії: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні інформації про комісії.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    text = (
        f"💰 *Статистика комісій*\n\n"
        f"• Всього очікується: *{commission_summary['total_pending'] or 0:.2f} грн*\n"
        f"• Всього сплачено: *{commission_summary['total_paid'] or 0:.2f} грн*\n\n"
        f"📊 *Останні транзакції:*\n"
    )

    if recent_transactions:
        for tx in recent_transactions:
            username = f"@{tx['username']}" if tx['username'] else f"ID: {tx['seller_chat_id']}"
            created_at_local = tx['created_at'].astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')
            text += (
                f"- Товар ID `{tx['product_id']}` ({tx['product_name']})\n"
                f"  Продавець: {username}\n"
                f"  Сума: {tx['amount']:.2f} грн, Статус: {tx['status']}\n"
                f"  Дата: {created_at_local}\n\n"
            )
    else:
        text += "  Немає транзакцій комісій.\n\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

@error_handler
def send_admin_ai_statistics(call):
    """Надсилає адміністратору статистику використання AI помічника."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні AI статистики (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM conversations WHERE sender_type = 'user';"))
        total_user_queries = cur.fetchone()[0]

        cur.execute(pg_sql.SQL("""
            SELECT user_chat_id, COUNT(*) as query_count
            FROM conversations
            WHERE sender_type = 'user'
            GROUP BY user_chat_id
            ORDER BY query_count DESC
            LIMIT 5;
        """))
        top_ai_users = cur.fetchall()

        cur.execute(pg_sql.SQL("""
            SELECT DATE(timestamp) as date, COUNT(*) as query_count
            FROM conversations
            WHERE sender_type = 'user'
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
            LIMIT 7;
        """))
        daily_ai_queries = cur.fetchall()

    except Exception as e:
        logger.error(f"Помилка при отриманні AI статистики: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні AI статистики.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    text = (
        f"🤖 *Статистика AI Помічника*\n\n"
        f"• Всього запитів користувачів до AI: *{total_user_queries}*\n\n"
        f"📊 *Найактивніші користувачі AI:*\n"
    )
    if top_ai_users:
        for user_data_row in top_ai_users:
            user_id = user_data_row['user_chat_id']
            query_count = user_data_row['query_count']
            user_info = None
            try:
                user_info = bot.get_chat(user_id)
            except Exception as e:
                logger.warning(f"Не вдалося отримати інформацію про користувача {user_id}: {e}")

            username = f"@{user_info.username}" if user_info and user_info.username else f"ID: {user_id}"
            text += f"- {username}: {query_count} запитів\n"
    else:
        text += "  Немає даних.\n"

    text += "\n📅 *Запити за останні 7 днів:*\n"
    if daily_ai_queries:
        for day_data_row in daily_ai_queries:
            text += f"- {day_data_row['date']}: {day_data_row['query_count']} запитів\n"
    else:
        text += "  Немає даних.\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

@error_handler
def send_admin_referral_stats(call):
    """Надсилає адміністратору статистику рефералів."""
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ Помилка при отриманні реферальної статистики (помилка БД).", call.message.chat.id, call.message.message_id)
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL;"))
        total_referrals = cur.fetchone()[0]

        cur.execute(pg_sql.SQL("""
            SELECT referrer_id, COUNT(*) as invited_count
            FROM users
            WHERE referrer_id IS NOT NULL
            GROUP BY referrer_id
            ORDER BY invited_count DESC
            LIMIT 5;
        """))
        top_referrers = cur.fetchall()

    except Exception as e:
        logger.error(f"Помилка при отриманні реферальної статистики: {e}", exc_info=True)
        bot.edit_message_text("❌ Помилка при отриманні реферальної статистики.", call.message.chat.id, call.message.message_id)
        return
    finally:
        if conn:
            conn.close()

    text = (
        f"🏆 *Статистика рефералів*\n\n"
        f"• Всього запрошених користувачів: *{total_referrals}*\n\n"
        f"📊 *Топ-5 реферерів:*\n"
    )
    if top_referrers:
        for referrer_row in top_referrers:
            referrer_id = referrer_row['referrer_id']
            invited_count = referrer_row['invited_count']
            referrer_info = None
            try:
                referrer_info = bot.get_chat(referrer_id)
            except Exception as e:
                logger.warning(f"Не вдалося отримати інформацію про реферера {referrer_id}: {e}")
            username = f"@{referrer_info.username}" if referrer_info and referrer_info.username else f"ID: {referrer_id}"
            text += f"- {username}: {invited_count} запрошень\n"
    else:
        text += "  Немає даних.\n"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))
    markup.add(types.InlineKeyboardButton("🎲 Провести розіграш", callback_data="runraffle_week"))

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup)

# Callbacks для модерації товару (продовження з частини 1)
@error_handler
def handle_product_moderation_callbacks(call):
    """Обробляє колбеки, пов'язані зі схваленням, відхиленням або відміткою "продано" для товару."""
    if call.message.chat.id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Доступ заборонено.")
        return

    data_parts = call.data.split('_')
    action = data_parts[0]
    product_id = int(data_parts[1])

    conn = get_db_connection()
    if not conn:
        bot.answer_callback_query(call.id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()
    product_info = None
    try:
        cur.execute(pg_sql.SQL("""
            SELECT seller_chat_id, product_name, price, description, photos, geolocation, admin_message_id, channel_message_id, status
            FROM products WHERE id = %s;
        """), (product_id,))
        product_info = cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка при отриманні інформації про товар {product_id} для модерації: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Помилка при отриманні інформації про товар.")
        if conn: conn.close()
        return

    if not product_info:
        bot.answer_callback_query(call.id, "Товар не знайдено.")
        if conn: conn.close()
        return

    seller_chat_id = product_info['seller_chat_id']
    product_name = product_info['product_name']
    price_str = product_info['price']
    description = product_info['description']
    photos_str = product_info['photos']
    geolocation_str = product_info['geolocation']
    admin_message_id = product_info['admin_message_id']
    channel_message_id = product_info['channel_message_id']
    current_status = product_info['status']

    photos = json.loads(photos_str) if photos_str else []
    geolocation = json.loads(geolocation_str) if geolocation_str else None
    hashtags = generate_hashtags(description)

    try:
        if action == 'approve':
            if current_status != 'pending':
                bot.answer_callback_query(call.id, f"Товар вже має статус '{current_status}'.")
                return

            shipping_options_text = "Не вказано"
            try:
                cur.execute(pg_sql.SQL("SELECT shipping_options, hashtags FROM products WHERE id = %s;"), (product_id,))
                product_details_for_publish = cur.fetchone()
                if product_details_for_publish:
                    if product_details_for_publish['shipping_options']:
                        shipping_options_text = ", ".join(json.loads(product_details_for_publish['shipping_options']))
                    if product_details_for_publish['hashtags']:
                        hashtags = product_details_for_publish['hashtags']
            except Exception as e:
                logger.warning(f"Не вдалося отримати shipping_options або hashtags для товару {product_id}: {e}")
            
            # Змінено формат тексту для каналу
            channel_text = (
                f"*{product_name}*\n" # Прибрано "📦 Новий товар: "
                f"{price_str}\n" # Прибрано "💰 Ціна: "
                f"{shipping_options_text}\n" # Прибрано "🚚 Доставка: "
                f"{description}\n" # Прибрано "📝 Опис:\n"
            )
            if geolocation: # Додано умову для геолокації
                channel_text += f"{geolocation['latitude']}, {geolocation['longitude']}\n" # Прибрано "📍 Геолокація: "
            
            channel_text += f"{hashtags}\n\n" # Прибрано "🏷️ Хештеги: "
            channel_text += f"Контакт: [Написати продавцю](tg://user?id={seller_chat_id})" # Змінено "👤 Продавець: "

            published_message = None
            if photos:
                media = [types.InputMediaPhoto(photo_id, caption=channel_text if i == 0 else None, parse_mode='Markdown') 
                         for i, photo_id in enumerate(photos)]
                sent_messages = bot.send_media_group(CHANNEL_ID, media)
                published_message = sent_messages[0] if sent_messages else None
            else:
                published_message = bot.send_message(CHANNEL_ID, channel_text, parse_mode='Markdown')

            if published_message:
                like_markup = types.InlineKeyboardMarkup()
                like_markup.add(types.InlineKeyboardButton("❤️ 0", callback_data=f"toggle_favorite_{product_id}_{published_message.message_id}")) 
                
                like_message = bot.send_message(CHANNEL_ID, "👇 Оцініть товар!", 
                                                 reply_to_message_id=published_message.message_id, 
                                                 reply_markup=like_markup,
                                                 parse_mode='Markdown')


                new_channel_message_id = like_message.message_id
                cur.execute(pg_sql.SQL("""
                    UPDATE products SET status = 'approved', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP,
                    channel_message_id = %s, views = 0, likes_count = 0
                    WHERE id = %s;
                """), (call.message.chat.id, new_channel_message_id, product_id))
                conn.commit()
                log_statistics('product_approved', call.message.chat.id, product_id)

                bot.send_message(seller_chat_id,
                                 f"✅ Ваш товар '{product_name}' успішно опубліковано в каналі! [Переглянути](https://t.me/c/{str(CHANNEL_ID).replace('-100', '')}/{published_message.message_id})",
                                 parse_mode='Markdown', disable_web_page_preview=True)
                
                if admin_message_id:
                    bot.edit_message_text(f"✅ Товар *'{product_name}'* (ID: {product_id}) опубліковано.",
                                          chat_id=call.message.chat.id, message_id=admin_message_id, parse_mode='Markdown')
                    markup_sold = types.InlineKeyboardMarkup()
                    markup_sold.add(types.InlineKeyboardButton("💰 Відмітити як продано", callback_data=f"sold_{product_id}"))
                    bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=admin_message_id, reply_markup=markup_sold)
                else:
                    bot.send_message(call.message.chat.id, f"✅ Товар *'{product_name}'* (ID: {product_id}) опубліковано.")

            else:
                raise Exception("Не вдалося опублікувати повідомлення в канал.")

        elif action == 'reject':
            if current_status != 'pending':
                bot.answer_callback_query(call.id, f"Товар вже має статус '{current_status}'.")
                return

            cur.execute(pg_sql.SQL("""
                UPDATE products SET status = 'rejected', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """), (call.message.chat.id, product_id))
            conn.commit()
            log_statistics('product_rejected', call.message.chat.id, product_id)

            bot.send_message(seller_chat_id,
                             f"❌ Ваш товар '{product_name}' було відхилено адміністратором.\n\n"
                             "Можливі причини: невідповідність правилам, низька якість фото, неточний опис.\n"
                             "Будь ласка, перевірте оголошення та спробуйте додати знову.",
                             parse_mode='Markdown')
            
            if admin_message_id:
                bot.edit_message_text(f"❌ Товар *'{product_name}'* (ID: {product_id}) відхилено.",
                                      chat_id=call.message.chat.id, message_id=admin_message_id, parse_mode='Markdown')
                bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=admin_message_id, reply_markup=None)
            else:
                bot.send_message(call.message.chat.id, f"❌ Товар *'{product_name}'* (ID: {product_id}) відхилено.")


        elif action == 'sold':
            if current_status != 'approved':
                bot.answer_callback_query(call.id, f"Товар не опублікований або вже проданий (поточний статус: '{current_status}').")
                return

            if channel_message_id:
                try:
                    cur.execute(pg_sql.SQL("""
                        UPDATE products SET status = 'sold', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP
                        WHERE id = %s;
                    """), (call.message.chat.id, product_id))
                    conn.commit()
                    log_statistics('product_sold', call.message.chat.id, product_id)

                    original_message_for_edit = None
                    try:
                        # Attempt to get the original message from the channel using forward (might fail for media groups directly)
                        # This part is complex due to Telegram API limitations on editing media groups.
                        # For simplicity, we'll construct a new sold message.
                        sold_text = (
                            f"📦 *ПРОДАНО!* {product_name}\n\n"
                            f"{price_str}\n" # Змінено: прибрано "💰 Ціна: "
                            f"{description}\n\n" # Змінено: прибрано "📝 Опис:\n"
                            f"*Цей товар вже продано.*"
                        )
                        # Видаляємо лайк-повідомлення
                        bot.delete_message(CHANNEL_ID, channel_message_id)
                        # Якщо це було медіа-група, то треба видалити й перше повідомлення
                        # Тут припустимо, що channel_message_id - це повідомлення з лайками, яке відповідало на основне.
                        # Якщо основне повідомлення було окремим фото/текстом, його message_id не збережено.
                        # Щоб гарантовано видалити основне оголошення, треба зберігати його message_id окремо.
                        # Наразі, будемо вважати, що канал_месседж_ід - це те, що ми видаляємо.
                    except Exception as e_fetch_original:
                        logger.warning(f"Не вдалося отримати оригінальний текст оголошення для товару {product_id} з каналу: {e_fetch_original}. Використовуємо стандартний текст.")
                        sold_text = (
                            f"📦 *ПРОДАНО!* {product_name}\n\n"
                            f"{price_str}\n"
                            f"{description}\n\n"
                            f"*Цей товар вже продано.*"
                        )

                    # Надсилаємо нове повідомлення з позначкою "ПРОДАНО"
                    if photos:
                        media_group_id = photos[0] # Використовуємо перший фото ID для прив'язки
                        # bot.edit_message_caption не працює для медіа-груп, тому надсилаємо нове повідомлення
                        bot.send_photo(CHANNEL_ID, photos[0], caption=sold_text, parse_mode='Markdown', reply_markup=None)
                        # Можливо, потрібно також видалити оригінальну медіа-групу
                    else:
                        bot.send_message(CHANNEL_ID, sold_text, parse_mode='Markdown', reply_markup=None)
                    
                    bot.send_message(seller_chat_id, f"✅ Ваш товар '{product_name}' відмічено як *'ПРОДАНО'*. Дякуємо за співпрацю!", parse_mode='Markdown')
                    
                    if admin_message_id:
                        bot.edit_message_text(f"💰 Товар *'{product_name}'* (ID: {product_id}) відмічено як проданий.",
                                              chat_id=call.message.chat.id, message_id=admin_message_id, parse_mode='Markdown')
                        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=admin_message_id, reply_markup=None)
                    else:
                        bot.send_message(call.message.chat.id, f"💰 Товар *'{product_name}'* (ID: {product_id}) відмічено як проданий.")

                except telebot.apihelper.ApiTelegramException as e:
                    logger.error(f"Помилка при відмітці товару {product_id} як проданого в каналі: {e}", exc_info=True)
                    bot.send_message(call.message.chat.id, f"❌ Не вдалося оновити статус продажу в каналі для товару {product_id}. Можливо, повідомлення було видалено.")
                    bot.answer_callback_query(call.id, "❌ Помилка оновлення в каналі.")
                    return
            else:
                bot.send_message(call.message.chat.id, "Цей товар ще не опубліковано в каналі, або повідомлення в каналі відсутнє. Не можна відмітити як проданий.")
                bot.answer_callback_query(call.id, "Товар не опубліковано в каналі.")
    except Exception as e:
        logger.error(f"Помилка під час модерації товару {product_id}, дія {action}: {e}", exc_info=True)
        bot.send_message(call.message.chat.id, f"❌ Виникла помилка під час виконання дії '{action}' для товару {product_id}.")
    finally:
        if conn:
            conn.close()
    bot.answer_callback_query(call.id)

@error_handler
def handle_seller_sold_product(call):
    """Обробляє дію "Продано" від продавця."""
    seller_chat_id = call.message.chat.id
    product_id = int(call.data.split('_')[2])

    conn = get_db_connection()
    if not conn:
        bot.answer_callback_query(call.id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()
    product_info = None
    try:
        cur.execute(pg_sql.SQL("""
            SELECT product_name, price, description, photos, channel_message_id, status, commission_rate
            FROM products WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, seller_chat_id))
        product_info = cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка при отриманні інформації про товар {product_id} для відмітки продажу: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Помилка при отриманні інформації про товар.")
        if conn: conn.close()
        return

    if not product_info:
        bot.answer_callback_query(call.id, "Товар не знайдено або ви не є його продавцем.")
        if conn: conn.close()
        return

    product_name = product_info['product_name']
    price_str = product_info['price']
    description = product_info['description']
    photos_str = product_info['photos']
    channel_message_id = product_info['channel_message_id']
    current_status = product_info['status']
    commission_rate = product_info['commission_rate']

    photos = json.loads(photos_str) if photos_str else []

    if current_status != 'approved':
        bot.answer_callback_query(call.id, f"Товар має статус '{current_status}'. Відмітити як продано можна лише опублікований товар.")
        return

    try:
        commission_amount = 0.0
        try:
            cleaned_price_str = re.sub(r'[^\d.]', '', price_str)
            if cleaned_price_str:
                numeric_price = float(cleaned_price_str)
                commission_amount = numeric_price * commission_rate
            else:
                bot.send_message(seller_chat_id, f"⚠️ Увага: Ціна товару '{product_name}' не є числовим значенням ('{price_str}'). Комісія не буде розрахована автоматично. Будь ласка, обговоріть її з адміністратором.")
        except ValueError:
            logger.warning(f"Не вдалося конвертувати ціну '{price_str}' товару {product_id} в число. Комісія не розрахована.")
            bot.send_message(seller_chat_id, f"⚠️ Увага: Не вдалося розрахувати комісію для товару '{product_name}' з ціною '{price_str}'. Будь ласка, зв'яжіться з адміністратором.")
            
        cur.execute(pg_sql.SQL("""
            UPDATE products SET status = 'sold', commission_amount = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """), (commission_amount, product_id))

        if commission_amount > 0:
            cur.execute(pg_sql.SQL("""
                INSERT INTO commission_transactions (product_id, seller_chat_id, amount, status)
                VALUES (%s, %s, %s, 'pending_payment');
            """), (product_id, seller_chat_id, commission_amount))
            bot.send_message(seller_chat_id, 
                             f"💰 Ваш товар '{product_name}' (ID: {product_id}) відмічено як *'ПРОДАНО'*! 🎉\n\n"
                             f"Розрахована комісія складає: *{commission_amount:.2f} грн*.\n"
                             f"Будь ласка, сплатіть комісію на картку Monobank:\n`{MONOBANK_CARD_NUMBER}`\n\n"
                             f"Дякуємо за співпрацю!", parse_mode='Markdown')
        else:
            bot.send_message(seller_chat_id, f"✅ Ваш товар '{product_name}' (ID: {product_id}) відмічено як *'ПРОДАНО'*! 🎉\n\n"
                             f"Оскільки ціна була договірна або нечислова, комісія не розрахована автоматично. Якщо комісія є, будь ласка, зв'яжіться з адміністратором.", parse_mode='Markdown')


        conn.commit()
        log_statistics('product_sold_by_seller', seller_chat_id, product_id, f"Комісія: {commission_amount}")

        if channel_message_id:
            try:
                # Видаляємо лайк-повідомлення
                bot.delete_message(CHANNEL_ID, channel_message_id)
                logger.info(f"Видалено повідомлення {channel_message_id} для товару {product_id} з каналу.")
            except telebot.apihelper.ApiTelegramException as e:
                logger.warning(f"Не вдалося видалити повідомлення {channel_message_id} з каналу для товару {product_id}: {e}")

            # Формуємо текст проданого оголошення, прибираючи заголовки
            sold_text = (
                f"*{product_name}*\n" # Прибрано "📦 ПРОДАНО! "
                f"{price_str}\n" # Прибрано "💰 Ціна: "
                f"{description}\n\n" # Прибрано "📝 Опис:\n"
                f"*Цей товар вже продано.*"
            )

            try:
                if photos:
                    # Якщо це медіа-група, відредагувати неможливо, тому надсилаємо нове повідомлення
                    bot.send_photo(CHANNEL_ID, photos[0], caption=sold_text, parse_mode='Markdown', reply_markup=None)
                else:
                    # Якщо це було текстове оголошення, можна відредагувати
                    bot.send_message(CHANNEL_ID, sold_text, parse_mode='Markdown', reply_markup=None)
            except telebot.apihelper.ApiTelegramException as e:
                logger.error(f"Помилка при оновленні повідомлення в каналі для товару {product_id}: {e}", exc_info=True)
                bot.send_message(seller_chat_id, f"⚠️ Не вдалося оновити повідомлення в каналі для товару '{product_name}'. Можливо, воно було видалено.")
        
        # Оновлюємо відображення в "Моїх товарах"
        current_message_text = call.message.text
        updated_message_text_lines = current_message_text.splitlines()
        
        # Фільтруємо рядки, які потрібно прибрати (перегляди, лайки, кнопки переопублікації/ціни)
        filtered_lines = [
            line for line in updated_message_text_lines 
            if not any(keyword in line for keyword in ["👁️ Перегляди:", "❤️ Лайки:", "🔁 Переопублікувати", "❌ Переопублікувати", "✏️ Змінити ціну"])
        ]
        
        # Оновлюємо статус в тексті
        updated_message_text = "\n".join(filtered_lines)
        updated_message_text = updated_message_text.replace("📊 Статус: опубліковано", "📊 Статус: продано")

        bot.edit_message_text(updated_message_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', disable_web_page_preview=True)
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)


    except Exception as e:
        logger.error(f"Помилка при обробці продажу товару {product_id} продавцем: {e}", exc_info=True)
        bot.send_message(seller_chat_id, f"❌ Виникла помилка при відмітці товару '{product_name}' як проданого.")
    finally:
        if conn:
            conn.close()
    bot.answer_callback_query(call.id)


@error_handler
def handle_republish_product(call):
    """Обробляє запит на переопублікацію товару."""
    seller_chat_id = call.message.chat.id
    product_id = int(call.data.split('_')[1])

    conn = get_db_connection()
    if not conn:
        bot.answer_callback_query(call.id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()

    try:
        cur.execute(pg_sql.SQL("""
            SELECT product_name, price, description, photos, channel_message_id, status, geolocation, shipping_options, hashtags
            FROM products WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, seller_chat_id))
        product_info = cur.fetchone()

        if not product_info:
            bot.answer_callback_query(call.id, "Товар не знайдено або ви не є його продавцем.")
            return

        if product_info['status'] != 'approved':
            bot.answer_callback_query(call.id, "Переопублікувати можна лише опублікований товар.")
            return

        if product_info['channel_message_id']:
            try:
                # Видаляємо старе повідомлення з лайком (яке відповідало на основне оголошення)
                bot.delete_message(CHANNEL_ID, product_info['channel_message_id']) 
                logger.info(f"Видалено старе повідомлення з лайком {product_info['channel_message_id']} для товару {product_id} з каналу.")

                # Припустимо, що якщо є фото, то основне оголошення було як медіа-група або окреме фото,
                # і його message_id безпосередньо не зберігається як channel_message_id.
                # Для гарантованого видалення оригінального оголошення в медіа-групі, потрібно було б зберегти ID першого повідомлення.
                # Якщо оголошення було текстовим без фото, channel_message_id буде ID самого оголошення.
                # Оскільки ми створюємо нове оголошення, старе буде "замінено".
            except telebot.apihelper.ApiTelegramException as e:
                logger.warning(f"Не вдалося видалити старе повідомлення {product_info['channel_message_id']} з каналу для товару {product_id}: {e}")
        
        photos = json.loads(product_info['photos']) if product_info['photos'] else []
        shipping_options_text = ", ".join(json.loads(product_info['shipping_options'])) if product_info['shipping_options'] else "Не вказано"
        hashtags = product_info['hashtags'] if product_info['hashtags'] else generate_hashtags(product_info['description'])
        geolocation_data = json.loads(product_info['geolocation']) if product_info['geolocation'] else None

        # Формат тексту для каналу
        channel_text = (
            f"*{product_info['product_name']}*\n" # Прибрано "📦 Новий товар: "
            f"{product_info['price']}\n" # Прибрано "💰 Ціна: "
            f"{shipping_options_text}\n" # Прибрано "🚚 Доставка: "
            f"{product_info['description']}\n" # Прибрано "📝 Опис:\n"
        )
        if geolocation_data: # Додано умову для геолокації
            channel_text += f"{geolocation_data['latitude']}, {geolocation_data['longitude']}\n" # Прибрано "📍 Геолокація: "
        
        channel_text += f"{hashtags}\n\n" # Прибрано "🏷️ Хештеги: "
        channel_text += f"Контакт: [Написати продавцю](tg://user?id={seller_chat_id})" # Змінено "👤 Продавець: "
        
        published_message = None
        if photos:
            media = [types.InputMediaPhoto(photo_id, caption=channel_text if i == 0 else None, parse_mode='Markdown') 
                     for i, photo_id in enumerate(photos)]
            sent_messages = bot.send_media_group(CHANNEL_ID, media)
            published_message = sent_messages[0] if sent_messages else None
        else:
            published_message = bot.send_message(CHANNEL_ID, channel_text, parse_mode='Markdown')

        if published_message:
            # Скидаємо лічильник лайків при переопублікації
            like_markup = types.InlineKeyboardMarkup()
            like_markup.add(types.InlineKeyboardButton("❤️ 0", callback_data=f"toggle_favorite_{product_id}_{published_message.message_id}")) 
            
            like_message = bot.send_message(CHANNEL_ID, "👇 Оцініть товар!", 
                                             reply_to_message_id=published_message.message_id, 
                                             reply_markup=like_markup,
                                             parse_mode='Markdown')

            new_channel_message_id = like_message.message_id
            
            # Прибираємо `republish_count` та `last_republish_date`
            cur.execute(pg_sql.SQL("""
                UPDATE products SET 
                    channel_message_id = %s, 
                    views = 0, 
                    likes_count = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """), (new_channel_message_id, product_id))
            conn.commit()
            log_statistics('product_republished', seller_chat_id, product_id)

            bot.answer_callback_query(call.id, f"Товар '{product_info['product_name']}' успішно переопубліковано!")
            bot.send_message(seller_chat_id,
                             f"✅ Ваш товар '{product_info['product_name']}' успішно переопубліковано! [Переглянути](https://t.me/c/{str(CHANNEL_ID).replace('-100', '')}/{published_message.message_id})",
                             parse_mode='Markdown', disable_web_page_preview=True)
            
            # Оновлюємо повідомлення в "Моїх товарах"
            current_message_text = call.message.text
            updated_message_text_lines = current_message_text.splitlines()
            
            new_lines = []
            for line in updated_message_text_lines:
                if "👁️ Перегляди:" in line:
                    new_lines.append(f"   👁️ Перегляди: 0")
                elif "❤️ Лайки:" in line:
                    new_lines.append(f"   ❤️ Лайки: 0")
                elif "🔁 Переопублікувати" in line or "❌ Переопублікувати" in line: # Оновлюємо кнопку
                    new_lines.append(f"   🔁 Переопублікувати") # Більше без лімітів
                else:
                    new_lines.append(line)
            updated_message_text = "\n".join(new_lines)
            
            markup = types.InlineKeyboardMarkup(row_width=2)
            channel_link_part = str(CHANNEL_ID).replace("-100", "") 
            channel_url = f"https://t.me/c/{channel_link_part}/{published_message.message_id}"
            markup.add(types.InlineKeyboardButton("👀 Переглянути в каналі", url=channel_url))
            
            markup.add(types.InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"republish_{product_id}")) # Кнопка без ліміту

            markup.add(types.InlineKeyboardButton("✅ Продано", callback_data=f"sold_my_{product_id}"))
            markup.add(types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{product_id}"))
            markup.add(types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_my_{product_id}"))

            bot.edit_message_text(updated_message_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)


        else:
            bot.answer_callback_query(call.id, "❌ Не вдалося переопублікувати товар.")
            raise Exception("Не вдалося опублікувати повідомлення в канал при переопублікації.")

    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Помилка при переопублікації товару {product_id} в Telegram API: {e}", exc_info=True)
        bot.answer_callback_query(call.id, f"❌ Помилка Telegram API при переопублікації.")
    except Exception as e:
        logger.error(f"Загальна помилка при переопублікації товару {product_id}: {e}", exc_info=True)
        bot.answer_callback_query(call.id, f"❌ Виникла помилка при переопублікації товару.")
    finally:
        if conn:
            conn.close()

@error_handler
def handle_delete_my_product(call):
    """Обробляє видалення товару продавцем."""
    seller_chat_id = call.message.chat.id
    product_id = int(call.data.split('_')[2]) 

    conn = get_db_connection()
    if not conn:
        bot.answer_callback_query(call.id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()

    try:
        cur.execute(pg_sql.SQL("""
            SELECT product_name, channel_message_id, status FROM products
            WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, seller_chat_id))
        product_info = cur.fetchone()

        if not product_info:
            bot.answer_callback_query(call.id, "Товар не знайдено або ви не є його продавцем.")
            return

        product_name = product_info['product_name']
        channel_message_id = product_info['channel_message_id']
        current_status = product_info['status']

        # Видалення пов'язаних записів
        cur.execute(pg_sql.SQL("DELETE FROM commission_transactions WHERE product_id = %s;"), (product_id,))
        cur.execute(pg_sql.SQL("DELETE FROM favorites WHERE product_id = %s;"), (product_id,))
        cur.execute(pg_sql.SQL("DELETE FROM conversations WHERE product_id = %s;"), (product_id,))
        
        # Видалення товару
        cur.execute(pg_sql.SQL("DELETE FROM products WHERE id = %s;"), (product_id,))
        conn.commit()
        
        if channel_message_id:
            try:
                bot.delete_message(CHANNEL_ID, channel_message_id)
                logger.info(f"Видалено повідомлення {channel_message_id} для товару {product_id} з каналу.")
            except telebot.apihelper.ApiTelegramException as e:
                logger.warning(f"Не вдалося видалити повідомлення {channel_message_id} з каналу для товару {product_id}: {e}")
        
        log_statistics('product_deleted', seller_chat_id, product_id)

        bot.answer_callback_query(call.id, f"Товар '{product_name}' успішно видалено.")
        bot.send_message(seller_chat_id, f"🗑️ Ваш товар '{product_name}' (ID: {product_id}) було видалено.", reply_markup=main_menu_markup)
        
        bot.delete_message(call.message.chat.id, call.message.message_id) 
        
    except Exception as e:
        logger.error(f"Помилка при видаленні товару {product_id} продавцем: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Виникла помилка при видаленні товару.")
    finally:
        if conn:
            conn.close()

@error_handler
def handle_change_price_init(call):
    """Починає процес зміни ціни для товару користувача."""
    chat_id = call.message.chat.id
    product_id = int(call.data.split('_')[2])

    user_data[chat_id] = {
        'flow': 'change_price',
        'product_id': product_id
    }
    
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Введіть нову ціну товару (наприклад, `500 грн` або `Договірна`):", 
                     reply_markup=types.ForceReply(selective=True))

@error_handler
def process_new_price(message):
    """Обробляє введену нову ціну та оновлює товар у БД та каналі."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'change_price':
        bot.send_message(chat_id, "Ви не в процесі зміни ціни. Будь ласка, скористайтеся меню.", reply_markup=main_menu_markup)
        return

    product_id = user_data[chat_id]['product_id']
    new_price = message.text.strip()

    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "❌ Помилка підключення до БД. Спробуйте пізніше.")
        del user_data[chat_id]
        return
    cur = conn.cursor()

    try:
        cur.execute(pg_sql.SQL("SELECT seller_chat_id, product_name, channel_message_id FROM products WHERE id = %s;"), (product_id,))
        product_info = cur.fetchone()

        if not product_info or product_info['seller_chat_id'] != chat_id:
            bot.send_message(chat_id, "❌ Ви не є власником цього товару.")
            return

        cur.execute(pg_sql.SQL("""
            UPDATE products SET price = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """), (new_price, product_id))
        conn.commit()

        bot.send_message(chat_id, f"✅ Ціну для товару '{product_info['product_name']}' (ID: {product_id}) оновлено.", reply_markup=main_menu_markup)
        log_statistics('price_changed', chat_id, product_id, f"Нова ціна: {new_price}")

        # Оновлення повідомлення в каналі з новою ціною
        if product_info['channel_message_id']:
            conn_channel = get_db_connection()
            if conn_channel:
                try:
                    cur_channel = conn_channel.cursor()
                    cur_channel.execute(pg_sql.SQL("SELECT * FROM products WHERE id = %s"), (product_id,))
                    product_for_channel_update = cur_channel.fetchone()
                    if product_for_channel_update:
                        photos = json.loads(product_for_channel_update['photos'] or '[]')
                        shipping = ", ".join(json.loads(product_for_channel_update['shipping_options'] or '[]')) or 'Не вказано'
                        product_hashtags = product_for_channel_update['hashtags'] if product_for_channel_update['hashtags'] else generate_hashtags(product_for_channel_update['description'])
                        geolocation_data = json.loads(product_for_channel_update['geolocation']) if product_for_channel_update['geolocation'] else None

                        channel_text = (
                            f"*{product_for_channel_update['product_name']}*\n"
                            f"{product_for_channel_update['price']}\n"
                            f"{shipping}\n"
                            f"{product_for_channel_update['description']}\n"
                        )
                        if geolocation_data:
                            channel_text += f"{geolocation_data['latitude']}, {geolocation_data['longitude']}\n"
                        
                        channel_text += f"{product_hashtags}\n\n"
                        channel_text += f"Контакт: [Написати продавцю](tg://user?id={product_for_channel_update['seller_chat_id']})"

                        # Оскільки ми оновлюємо ціну, логічніше переопублікувати з новими даними, а не редагувати попереднє повідомлення
                        # Або, якщо є можливість, відредагувати лише частину.
                        # Наразі, будемо видаляти старе і публікувати нове, як при переопублікації
                        if product_for_channel_update['channel_message_id']:
                            try:
                                bot.delete_message(CHANNEL_ID, product_for_channel_update['channel_message_id'])
                                logger.info(f"Видалено старе повідомлення {product_for_channel_update['channel_message_id']} для товару {product_id} з каналу для оновлення ціни.")
                            except Exception as e:
                                logger.warning(f"Не вдалося видалити старе повідомлення {product_for_channel_update['channel_message_id']} з каналу для товару {product_id}: {e}")

                        published_message = None
                        if photos:
                            media = [types.InputMediaPhoto(p, caption=channel_text if i == 0 else '', parse_mode='Markdown') for i, p in enumerate(photos)]
                            sent_messages = bot.send_media_group(CHANNEL_ID, media)
                            published_message = sent_messages[0]
                        else:
                            published_message = bot.send_message(CHANNEL_ID, channel_text, parse_mode='Markdown')

                        if published_message:
                            # Зберігаємо likes_count при оновленні, не скидаємо
                            current_likes_count = product_for_channel_update['likes_count']
                            like_markup = types.InlineKeyboardMarkup()
                            like_markup.add(types.InlineKeyboardButton(f"❤️ {current_likes_count}", callback_data=f"toggle_favorite_{product_id}_{published_message.message_id}")) 
                            
                            like_message = bot.send_message(CHANNEL_ID, "👇 Оцініть товар!", 
                                                             reply_to_message_id=published_message.message_id, 
                                                             reply_markup=like_markup,
                                                             parse_mode='Markdown')
                            
                            cur_channel.execute(pg_sql.SQL("""
                                UPDATE products SET channel_message_id = %s, updated_at = CURRENT_TIMESTAMP
                                WHERE id = %s;
                            """), (like_message.message_id, product_id))
                            conn_channel.commit()
                            bot.send_message(chat_id, "Оголошення в каналі оновлено з новою ціною.")
                        else:
                            bot.send_message(chat_id, "Помилка оновлення оголошення в каналі.")
                finally:
                    conn_channel.close()

    except Exception as e:
        logger.error(f"Помилка при оновленні ціни для товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "❌ Виникла помилка при оновленні ціни.")
    finally:
        if conn:
            conn.close()
        if chat_id in user_data:
            del user_data[chat_id]

# Логіка для модератора
@error_handler
def handle_moderator_actions(call):
    """Обробляє колбеки, пов'язані з діями модератора (редагування хештегів, запит на виправлення фото)."""
    if call.message.chat.id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Доступ заборонено.")
        return
    _, action, product_id_str = call.data.split('_', 2)

    product_id = int(product_id_str)

    if action == 'edit_tags':
        user_data[ADMIN_CHAT_ID] = {
            'flow': 'mod_edit_tags',
            'product_id': product_id
        }
        bot.answer_callback_query(call.id)
        bot.send_message(ADMIN_CHAT_ID, f"Введіть нові хештеги для товару ID {product_id} (через пробіл, без #):",
                         reply_markup=types.ForceReply(selective=True))
    elif action == 'request_photo_fix':
        conn = get_db_connection()
        if not conn:
            bot.answer_callback_query(call.id, "❌ Помилка БД.")
            return
        cur = conn.cursor()
        try:
            cur.execute(pg_sql.SQL("SELECT seller_chat_id, product_name FROM products WHERE id = %s"), (product_id,))
            product = cur.fetchone()
            if product:
                bot.send_message(product['seller_chat_id'], 
                                 f"❗️ *Модератор просить вас виправити фото для товару '{product['product_name']}'* (ID: {product_id}).\n"
                                 "Будь ласка, видаліть це оголошення та додайте заново з коректними фотографіями.",
                                 parse_mode='Markdown')
                bot.answer_callback_query(call.id, "Запит на виправлення фото відправлено продавцю.")
            else:
                bot.answer_callback_query(call.id, "Товар не знайдено.")
        except Exception as e:
            logger.error(f"Помилка при запиті виправлення фото для товару {product_id}: {e}", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Виникла помилка при відправці запиту.")
        finally:
            if conn:
                conn.close()
    else:
        bot.answer_callback_query(call.id, "Невідома дія модератора.")


@error_handler
def process_new_hashtags_mod(message):
    """Обробляє новий ввід хештегів від модератора та оновлює їх в БД."""
    chat_id = message.chat.id
    if chat_id != ADMIN_CHAT_ID or chat_id not in user_data or user_data[chat_id].get('flow') != 'mod_edit_tags':
        return

    product_id = user_data[chat_id]['product_id']
    new_hashtags_raw = message.text.strip()
    
    cleaned_hashtags = [f"#{word.lower()}" for word in re.findall(r'\b\w+\b', new_hashtags_raw) if len(word) > 0]
    final_hashtags_str = " ".join(cleaned_hashtags)

    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "❌ Помилка підключення до БД. Спробуйте пізніше.")
        del user_data[chat_id]
        return
    cur = conn.cursor()

    try:
        cur.execute(pg_sql.SQL("""
            UPDATE products SET hashtags = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s;
        """), (final_hashtags_str, product_id))
        conn.commit()

        bot.send_message(chat_id, f"✅ Хештеги для товару ID {product_id} оновлено на: `{final_hashtags_str}`", parse_mode='Markdown')
        log_statistics('moderator_edited_hashtags', chat_id, product_id, f"Нові хештеги: {final_hashtags_str}")
        
        # Оновлюємо оголошення в каналі з новими хештегами
        publish_product_to_channel(product_id)
        bot.send_message(chat_id, "Оголошення в каналі оновлено з новими хештегами.")

    except Exception as e:
        logger.error(f"Помилка при оновленні хештегів для товару {product_id} модератором: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "❌ Виникла помилка при оновленні хештегів.")
    finally:
        if conn:
            conn.close()
        if chat_id in user_data:
            del user_data[chat_id]


# Логіка для обраного та доставки
@error_handler
def handle_toggle_favorite(call):
    """Обробляє додавання/видалення з обраного (лайк)."""
    user_chat_id = call.from_user.id
    _, _, product_id_str, channel_message_id_str = call.data.split('_')
    product_id = int(product_id_str)
    channel_message_id_for_edit = int(channel_message_id_str)

    conn = get_db_connection()
    if not conn: 
        bot.answer_callback_query(call.id, "❌ Помилка підключення до БД.")
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("SELECT id FROM favorites WHERE user_chat_id = %s AND product_id = %s;"), (user_chat_id, product_id))
        is_favorited = cur.fetchone()

        likes_count = 0 # Ініціалізуємо
        if is_favorited:
            cur.execute(pg_sql.SQL("DELETE FROM favorites WHERE id = %s;"), (is_favorited['id'],))
            cur.execute(pg_sql.SQL("UPDATE products SET likes_count = likes_count - 1 WHERE id = %s RETURNING likes_count;"), (product_id,))
            likes_count = cur.fetchone()['likes_count']
            bot.answer_callback_query(call.id, "💔 Видалено з обраного")
        else:
            cur.execute(pg_sql.SQL("INSERT INTO favorites (user_chat_id, product_id) VALUES (%s, %s);"), (user_chat_id, product_id))
            cur.execute(pg_sql.SQL("UPDATE products SET likes_count = likes_count + 1 WHERE id = %s RETURNING likes_count;"), (product_id,))
            likes_count = cur.fetchone()['likes_count']
            bot.answer_callback_query(call.id, "❤️ Додано до обраного!")
        
        conn.commit()

        new_markup = types.InlineKeyboardMarkup()
        new_markup.add(types.InlineKeyboardButton(f"❤️ {likes_count}", callback_data=call.data)) 
        
        try:
            bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=channel_message_id_for_edit, reply_markup=new_markup)
        except telebot.apihelper.ApiTelegramException as e:
            logger.warning(f"Не вдалося оновити лічильник лайків для повідомлення {channel_message_id_for_edit}: {e}")

    except Exception as e:
        logger.error(f"Помилка при перемиканні обраного для користувача {user_chat_id}, товар {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.answer_callback_query(call.id, "❌ Виникла помилка при обробці обраного.")
    finally:
        if conn:
            conn.close()

@error_handler
def handle_shipping_choice(call):
    """Обробляє вибір опцій доставки під час додавання товару."""
    chat_id = call.message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('step') != 'waiting_shipping':
        bot.answer_callback_query(call.id, "Некоректний запит.")
        return

    if call.data == 'shipping_next':
        if not user_data[chat_id]['data']['shipping_options']:
            bot.answer_callback_query(call.id, "Будь ласка, оберіть хоча б один спосіб доставки.", show_alert=True)
            return
        bot.delete_message(chat_id, call.message.message_id)
        go_to_next_step(chat_id)
        return

    option = call.data.replace('shipping_', '')
    selected = user_data[chat_id]['data'].get('shipping_options', [])

    if option in selected:
        selected.remove(option)
    else:
        selected.append(option)
    user_data[chat_id]['data']['shipping_options'] = selected

    inline_markup = types.InlineKeyboardMarkup(row_width=2)
    shipping_options_list = ["Наложка Нова Пошта", "Наложка Укрпошта", "Особиста зустріч"]

    buttons = []
    for opt in shipping_options_list:
        emoji = '✅ ' if opt in selected else ''
        buttons.append(types.InlineKeyboardButton(f"{emoji}{opt}", callback_data=f"shipping_{opt}"))
    
    inline_markup.add(*buttons)
    inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
    
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=inline_markup)
    except telebot.apihelper.ApiTelegramException as e:
        logger.warning(f"Не вдалося оновити кнопки доставки: {e}")
    
    bot.answer_callback_query(call.id)

# Система переможців та розіграшів
@error_handler
def handle_winners_menu(call):
    """Показує меню для перегляду переможців."""
    text = "🏆 *Переможці розіграшів*\n\nОберіть період для перегляду топ-реферерів:"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("За тиждень", callback_data="winners_week"),
        types.InlineKeyboardButton("За місяць", callback_data="winners_month"),
        types.InlineKeyboardButton("За рік", callback_data="winners_year")
    )
    if call.from_user.id == ADMIN_CHAT_ID:
        markup.add(types.InlineKeyboardButton("🎲 Провести розіграш (Admin)", callback_data="runraffle_week"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@error_handler
def handle_show_winners(call):
    """Показує топ реферерів за обраний період."""
    period = call.data.split('_')[1]
    intervals = {'week': 7, 'month': 30, 'year': 365}
    interval_days = intervals.get(period, 7)

    conn = get_db_connection()
    if not conn: 
        bot.answer_callback_query(call.id, "❌ Помилка БД.")
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT referrer_id, COUNT(*) as referrals_count
            FROM users
            WHERE referrer_id IS NOT NULL AND joined_at >= NOW() - INTERVAL '%s days'
            GROUP BY referrer_id ORDER BY referrals_count DESC LIMIT 10;
        """), (interval_days,))
        top_referrers = cur.fetchall()
            
        text = f"🏆 *Топ реферерів за останній {'тиждень' if period == 'week' else 'місяць' if period == 'month' else 'рік'}:*\n\n"
        if top_referrers:
            for i, r in enumerate(top_referrers, 1):
                try: 
                    user_info = bot.get_chat(r['referrer_id'])
                    username = f"@{user_info.username}" if user_info and user_info.username else f"ID: {r['referrer_id']}"
                except Exception as e:
                    logger.warning(f"Не вдалося отримати інфо про реферера {r['referrer_id']}: {e}")
                    username = f"ID: {r['referrer_id']}"
                text += f"{i}. {username} - {r['referrals_count']} запрошень\n"
        else:
            text += "_Немає даних за цей період._\n"
            
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Помилка при показі переможців за період {period}: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Помилка при отриманні списку переможців.")
    finally:
        if conn: conn.close()

@error_handler
def handle_run_raffle(call):
    """Проводить розіграш серед учасників за останній тиждень (тільки для адміна)."""
    if call.from_user.id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Доступ заборонено.")
        return
        
    conn = get_db_connection()
    if not conn: 
        bot.answer_callback_query(call.id, "❌ Помилка БД.")
        return
    cur = conn.cursor()
    try:
        cur.execute(pg_sql.SQL("""
            SELECT DISTINCT referrer_id FROM users
            WHERE referrer_id IS NOT NULL AND joined_at >= NOW() - INTERVAL '7 days';
        """))
        participants = [row['referrer_id'] for row in cur.fetchall()]
        
        if not participants:
            bot.answer_callback_query(call.id, "Немає учасників для розіграшу за останній тиждень.")
            return

        winner_id = random.choice(participants)
        
        winner_info = None
        try: 
            winner_info = bot.get_chat(winner_id)
        except Exception as e:
            logger.warning(f"Не вдалося отримати інфо про переможця {winner_id}: {e}")

        winner_username = f"@{winner_info.username}" if winner_info and winner_info.username else f"ID: {winner_id}"
        
        text = f"🎉 *Переможець щотижневого розіграшу:*\n\n {winner_username} \n\nВітаємо!"
        
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text, parse_mode='Markdown')
        bot.send_message(CHANNEL_ID, text, parse_mode='Markdown')
        log_statistics('raffle_conducted', ADMIN_CHAT_ID, details=f"winner: {winner_id}")

    except Exception as e:
        logger.error(f"Помилка при проведенні розіграшу: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "❌ Виникла помилка при проведенні розіграшу.")
    finally:
        if conn: conn.close()

# Повернення до адмін-панелі після колбеку
@bot.callback_query_handler(func=lambda call: call.data == "admin_panel_main")
@error_handler
def back_to_admin_panel(call):
    """Повертає адміністратора до головного меню адмін-панелі."""
    if call.message.chat.id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Доступ заборонено.")
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

    bot.edit_message_text("🔧 *Адмін-панель*\n\nОберіть дію:",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=markup, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

# Запуск бота
if __name__ == '__main__':
    init_db()

    logger.info("Бот запускається...")

    if WEBHOOK_URL and TOKEN:
        try:
            bot.remove_webhook()
            
            full_webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
            bot.set_webhook(url=full_webhook_url)
            logger.info(f"Webhook встановлено на: {full_webhook_url}")
        except Exception as e:
            logger.critical(f"Критична помилка встановлення webhook: {e}", exc_info=True)
            exit(1)
    else:
        logger.critical("WEBHOOK_URL або TELEGRAM_BOT_TOKEN не встановлено. Бот не може працювати в режимі webhook. Перевірте змінні оточення.")
        exit(1)

    @app.route(f'/{TOKEN}', methods=['POST'])
    def webhook_handler():
        """Обробник POST-запитів, що надходять від Telegram API."""
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '!', 200
        else:
            logger.warning("Отримано запит до вебхука без правильного Content-Type (application/json).")
            return 'Content-Type must be application/json', 403

    port = int(os.environ.get("PORT", 8443)) 
    logger.info(f"Запуск Flask-додатка на порту {port}...")
    app.run(host="0.0.0.0", port=port)
