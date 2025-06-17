import os
import telebot
from telebot import types
import logging
from datetime import datetime, timedelta, timezone
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

# --- 3. Базова перевірка змінних ---
def validate_env_vars():
    missing_vars = []
    if not TOKEN: missing_vars.append('TELEGRAM_BOT_TOKEN')
    if not WEBHOOK_URL: missing_vars.append('WEBHOOK_URL')
    if not DATABASE_URL: missing_vars.append('DATABASE_URL')
    if ADMIN_CHAT_ID == 0: missing_vars.append('ADMIN_CHAT_ID')
    if CHANNEL_ID == 0: missing_vars.append('CHANNEL_ID')

    if missing_vars:
        logger.critical(f"Критична помилка: Відсутні змінні оточення: {', '.join(missing_vars)}. Бот не може працювати.")
        exit(1)

validate_env_vars()

# --- 4. Ініціалізація TeleBot та Flask ---
app = Flask(__name__)
bot = telebot.TeleBot(TOKEN)

# --- 5. Декоратор для обробки помилок ---
def error_handler(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Помилка в {func.__name__}: {e}", exc_info=True)
            chat_id_to_notify = ADMIN_CHAT_ID
            if args:
                first_arg = args[0]
                if isinstance(first_arg, (types.Message, types.CallbackQuery)):
                    chat_id_to_notify = first_arg.message.chat.id if isinstance(first_arg, types.CallbackQuery) else first_arg.chat.id
            try:
                bot.send_message(ADMIN_CHAT_ID, f"🚨 Критична помилка в боті!\nФункція: `{func.__name__}`\nПомилка: `{e}`")
                if chat_id_to_notify != ADMIN_CHAT_ID:
                    bot.send_message(chat_id_to_notify, "😔 Вибачте, сталася внутрішня помилка. Адміністратор вже сповіщений.")
            except Exception as e_notify:
                logger.error(f"Не вдалося надіслати повідомлення про помилку: {e_notify}")
    return wrapper

# --- 6. Робота з Базою Даних (PostgreSQL) ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return conn
    except Exception as e:
        logger.error(f"Помилка підключення до бази даних: {e}", exc_info=True)
        return None

@error_handler
def init_db():
    """Ініціалізує та мігрує схему бази даних."""
    conn = get_db_connection()
    if not conn:
        logger.critical("Не вдалося підключитися до БД для ініціалізації.")
        exit(1)
    
    try:
        with conn.cursor() as cur:
            # Таблиця users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
                    is_blocked BOOLEAN DEFAULT FALSE, blocked_by BIGINT, blocked_at TIMESTAMPTZ,
                    commission_paid REAL DEFAULT 0, commission_due REAL DEFAULT 0,
                    last_activity TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    joined_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    referrer_id BIGINT
                );
            """)
            
            # Таблиця products
            cur.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY, seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id),
                    seller_username TEXT, product_name TEXT NOT NULL, price TEXT NOT NULL,
                    description TEXT NOT NULL, photos TEXT, geolocation TEXT,
                    status TEXT DEFAULT 'pending', -- pending, approved, rejected, sold, expired
                    commission_rate REAL DEFAULT 0.10, commission_amount REAL DEFAULT 0,
                    moderator_id BIGINT, moderated_at TIMESTAMPTZ,
                    admin_message_id BIGINT, channel_message_id BIGINT,
                    views INTEGER DEFAULT 0, likes_count INTEGER DEFAULT 0,
                    republish_count INTEGER DEFAULT 0, last_republish_date DATE,
                    shipping_options TEXT, -- JSON array of strings, e.g., ["Наложка НП", "Наложка УП"]
                    hashtags TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Таблиця favorites
            cur.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id),
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    UNIQUE(user_chat_id, product_id)
                );
            """)
            
            # Інші таблиці
            for table_sql in [
                "CREATE TABLE IF NOT EXISTS conversations (id SERIAL PRIMARY KEY, user_chat_id BIGINT NOT NULL REFERENCES users(chat_id), product_id INTEGER, message_text TEXT, sender_type TEXT, timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);",
                "CREATE TABLE IF NOT EXISTS commission_transactions (id SERIAL PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id), amount REAL NOT NULL, status TEXT DEFAULT 'pending_payment', payment_details TEXT, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, paid_at TIMESTAMPTZ);",
                "CREATE TABLE IF NOT EXISTS statistics (id SERIAL PRIMARY KEY, action TEXT NOT NULL, user_id BIGINT, product_id INTEGER, details TEXT, timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);"
            ]:
                cur.execute(table_sql)
            
            # --- Міграції схеми ---
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
                        cur.execute(column_sql)
                        logger.info(f"Міграція для таблиці '{table}' успішно застосована: {column_sql}")
                    except psycopg2.Error as e:
                        logger.warning(f"Помилка міграції '{column_sql}': {e}")
                        conn.rollback() # Відкат у разі помилки міграції
                    else:
                        conn.commit() # Коміт після кожної успішної міграції

            conn.commit()
            logger.info("Таблиці бази даних успішно ініціалізовано або оновлено.")
    except Exception as e:
        logger.critical(f"Критична помилка ініціалізації бази даних: {e}", exc_info=True)
        conn.rollback()
        exit(1)
    finally:
        if conn:
            conn.close()

# --- 7. Зберігання даних для багатоетапних процесів ---
user_data = {} # Стан користувача для додавання товару, зміни ціни тощо.

# --- 8. Допоміжні функції ---
@error_handler
def save_user(message, referrer_id=None):
    """Зберігає або оновлює дані користувача, включаючи реферера."""
    user = message.from_user
    chat_id = message.chat.id
    
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # Перевіряємо, чи користувач вже існує
            cur.execute("SELECT chat_id, referrer_id FROM users WHERE chat_id = %s;", (chat_id,))
            existing_user = cur.fetchone()

            if existing_user:
                # Оновлюємо існуючого користувача
                cur.execute("""
                    UPDATE users SET username = %s, first_name = %s, last_name = %s, last_activity = CURRENT_TIMESTAMP
                    WHERE chat_id = %s;
                """, (user.username, user.first_name, user.last_name, chat_id))
            else:
                # Створюємо нового користувача
                cur.execute("""
                    INSERT INTO users (chat_id, username, first_name, last_name, referrer_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id) DO NOTHING;
                """, (chat_id, user.username, user.first_name, user.last_name, referrer_id))
            conn.commit()
            logger.info(f"Користувача {chat_id} збережено/оновлено. Реферер: {referrer_id if not existing_user else 'вже існує'}")
    except Exception as e:
        logger.error(f"Помилка при збереженні користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
    finally:
        if conn:
            conn.close()

@error_handler
def is_user_blocked(chat_id):
    """Перевіряє, чи заблокований користувач."""
    conn = get_db_connection()
    if not conn: return True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_blocked FROM users WHERE chat_id = %s;", (chat_id,))
            result = cur.fetchone()
            return result and result['is_blocked']
    finally:
        if conn:
            conn.close()

def generate_hashtags(description, num_hashtags=5):
    """Генерує хештеги з опису товару."""
    # ... (код залишається без змін)
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
    return " ".join(hashtags)

def log_statistics(action, user_id=None, product_id=None, details=None):
    """Логує дії для статистики."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO statistics (action, user_id, product_id, details) VALUES (%s, %s, %s, %s)",
                (action, user_id, product_id, details)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Помилка логування статистики: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()


# --- 9. Інтеграція з Gemini AI ---
# ... (код залишається без змін, оскільки не було запитів на його зміну)
@error_handler
def get_gemini_response(prompt, conversation_history=None):
    """
    Отримання відповіді від Gemini AI.
    Якщо API ключ не встановлений, генерує заглушку (відповідь в стилі Ілона Маска).
    """
    if not GEMINI_API_KEY:
        logger.warning("Gemini API ключ не налаштований. Використовується заглушка.")
        return generate_elon_style_response(prompt)

    headers = {
        "Content-Type": "application/json"
    }

    # Системний промпт для налаштування стилю відповіді AI
    system_prompt = """Ти - AI помічник для Telegram бота продажу товарів. 
    Відповідай в стилі Ілона Маска: прямолінійно, з гумором, іноді саркастично, 
    але завжди корисно. Використовуй емодзі. Будь лаконічним, але інформативним.
    Допомагай з питаннями про товари, покупки, продажі, переговори.
    Відповідай українською мовою."""

    # Форматуємо історію розмов для Gemini API
    # Gemini API очікує формат: [{"role": "user", "parts": [{"text": "..."}]}, {"role": "model", "parts": [{"text": "..."}]}]
    gemini_messages = [{"role": "user", "parts": [{"text": system_prompt}]}]
    
    if conversation_history:
        for msg in conversation_history:
            role = "user" if msg["sender_type"] == 'user' else "model" # Gemini API використовує 'model' для AI
            gemini_messages.append({"role": role, "parts": [{"text": msg["message_text"]}]})
    
    # Додаємо поточний запит користувача
    gemini_messages.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "contents": gemini_messages
    }

    try:
        api_url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"

        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status() # Викличе HTTPError для 4xx/5xx відповідей (помилки HTTP)
        
        data = response.json()
        if data.get("candidates") and len(data["candidates"]) > 0 and \
           data["candidates"][0].get("content") and data["candidates"][0]["content"].get("parts"):
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            logger.info(f"Gemini відповідь отримана: {content[:100]}...") # Логуємо частину відповіді
            return content.strip()
        else:
            logger.error(f"Неочікувана структура відповіді від Gemini: {data}")
            return generate_elon_style_response(prompt) # Заглушка, якщо відповідь невалідна

    except requests.exceptions.RequestException as e:
        logger.error(f"Помилка HTTP запиту до Gemini API: {e}", exc_info=True)
        return generate_elon_style_response(prompt) # Заглушка при помилці мережі
    except Exception as e:
        logger.error(f"Загальна помилка при отриманні відповіді від Gemini: {e}", exc_info=True)
        return generate_elon_style_response(prompt) # Заглушка при будь-якій іншій помилці

def generate_elon_style_response(prompt):
    """
    Генерує відповіді в стилі Ілона Маска як заглушка, коли AI API недоступне
    або виникають помилки.
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
    
    # Додаємо трохи контексту на основі ключових слів у запиті
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
    """
    Зберігає повідомлення (від користувача або AI) в історії розмов у БД
    для підтримки контексту AI.
    """
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
    finally:
        if conn:
            conn.close()

@error_handler
def get_conversation_history(chat_id, limit=5):
    """
    Отримує історію розмов для конкретного користувача з БД.
    Використовується для надання контексту AI.
    """
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
        
        # Повертаємо історію у зворотному порядку, щоб найстаріші повідомлення були першими
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
# Головна клавіатура
main_menu_markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
main_menu_markup.add(types.KeyboardButton("📦 Додати товар"), types.KeyboardButton("📋 Мої товари"))
# Змінено: Допомога -> Обрані, Комісія -> Допомога
main_menu_markup.add(types.KeyboardButton("⭐ Обрані"), types.KeyboardButton("❓ Допомога"))
main_menu_markup.add(types.KeyboardButton("📺 Наш канал"), types.KeyboardButton("🤖 AI Помічник"))

# Кнопки для скасування/повернення
back_button = types.KeyboardButton("🔙 Назад")
cancel_button = types.KeyboardButton("❌ Скасувати")

# --- 11. Обробники команд ---
@bot.message_handler(commands=['start'])
@error_handler
def send_welcome(message):
    """Обробник команди /start. Вітає користувача, реєструє реферала."""
    chat_id = message.chat.id
    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return

    # Обробка реферального посилання
    referrer_id = None
    parts = message.text.split()
    if len(parts) > 1 and parts[0] == '/start':
        try:
            potential_referrer_id = int(parts[1])
            # Переконуємось, що користувач не може бути своїм власним реферером
            if potential_referrer_id != chat_id:
                referrer_id = potential_referrer_id
                logger.info(f"Користувач {chat_id} прийшов за посиланням від {referrer_id}")
        except (ValueError, IndexError):
            logger.warning(f"Некоректний реферальний код у /start: {parts[1]}")

    save_user(message, referrer_id)
    log_statistics('start', chat_id, details=f"referrer: {referrer_id}")

    welcome_text = (
        "🛍️ *Ласкаво просимо до SellerBot!*\n\n"
        "Я ваш розумний помічник для продажу та купівлі товарів. "
        "Мене підтримує потужний AI! 🚀\n\n"
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
        types.InlineKeyboardButton("🏆 Реферали", callback_data="admin_referrals")
    )
    bot.send_message(message.chat.id, "🔧 *Адмін-панель*", reply_markup=markup, parse_mode='Markdown')

# --- 12. Потік додавання товару ---
ADD_PRODUCT_STEPS = {
    1: {'name': 'waiting_name', 'prompt': "📝 *Крок 1/6: Назва товару*\n\nВведіть назву:", 'next_step': 2, 'prev_step': None},
    2: {'name': 'waiting_price', 'prompt': "💰 *Крок 2/6: Ціна*\n\nВведіть ціну (наприклад, `500 грн` або `Договірна`):", 'next_step': 3, 'prev_step': 1},
    3: {'name': 'waiting_photos', 'prompt': "📸 *Крок 3/6: Фотографії*\n\nНадішліть до 5 фото (по одному). Коли закінчите, натисніть 'Далі'.", 'next_step': 4, 'allow_skip': True, 'skip_button': 'Пропустити фото', 'prev_step': 2},
    4: {'name': 'waiting_location', 'prompt': "📍 *Крок 4/6: Геолокація*\n\nНадішліть геолокацію або пропустіть крок.", 'next_step': 5, 'allow_skip': True, 'skip_button': 'Пропустити геолокацію', 'prev_step': 3},
    # Новий крок для доставки
    5: {'name': 'waiting_shipping', 'prompt': "🚚 *Крок 5/6: Доставка*\n\nОберіть доступні способи доставки:", 'next_step': 6, 'prev_step': 4},
    6: {'name': 'waiting_description', 'prompt': "✍️ *Крок 6/6: Опис*\n\nНапишіть детальний опис товару:", 'next_step': 'confirm', 'prev_step': 5}
}

@error_handler
def start_add_product_flow(message):
    """Починає процес додавання товару."""
    chat_id = message.chat.id
    user_data[chat_id] = {
        'flow': 'add_product',
        'step_number': 1, 
        'data': {'photos': [], 'geolocation': None, 'shipping_options': []}
    }
    send_product_step_message(chat_id)
    log_statistics('start_add_product', chat_id)

@error_handler
def send_product_step_message(chat_id):
    """Надсилає повідомлення для поточного кроку додавання товару."""
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        return

    current_step_number = user_data[chat_id]['step_number']
    step_config = ADD_PRODUCT_STEPS[current_step_number]
    user_data[chat_id]['step'] = step_config['name']

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    
    if step_config['name'] == 'waiting_photos':
        markup.add(types.KeyboardButton("Далі"))
    elif step_config['name'] == 'waiting_location':
        markup.add(types.KeyboardButton("📍 Надіслати геолокацію", request_location=True))
    
    if step_config.get('allow_skip'):
        markup.add(types.KeyboardButton(step_config['skip_button']))
        
    if step_config['name'] == 'waiting_shipping':
        # Для кроку доставки використовуємо інлайн-клавіатуру
        inline_markup = types.InlineKeyboardMarkup(row_width=2)
        options = ["Наложка Нова Пошта", "Наложка Укрпошта"]
        # Відображаємо позначку, якщо опція вже обрана
        selected_options = user_data[chat_id]['data'].get('shipping_options', [])
        buttons = [types.InlineKeyboardButton(f"{'✅ ' if opt in selected_options else ''}{opt}", callback_data=f"shipping_{opt}") for opt in options]
        inline_markup.add(*buttons)
        inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
        bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=inline_markup)
        return # Виходимо, щоб не надсилати ReplyKeyboard

    if step_config['prev_step'] is not None:
        markup.add(back_button)
    markup.add(cancel_button)
    
    bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=markup)

@error_handler
def go_to_next_step(chat_id):
    """Переводить до наступного кроку."""
    if chat_id not in user_data: return
    current_step_number = user_data[chat_id]['step_number']
    next_step = ADD_PRODUCT_STEPS[current_step_number]['next_step']
    
    if next_step == 'confirm':
        confirm_and_send_for_moderation(chat_id)
    else:
        user_data[chat_id]['step_number'] = next_step
        send_product_step_message(chat_id)

@error_handler
def process_product_step(message):
    """Обробляє текстовий ввід під час додавання товару."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        return # Ігноруємо, якщо користувач не в процесі

    step_name = user_data[chat_id].get('step')
    user_text = message.text
    
    # Обробка універсальних кнопок
    if user_text == cancel_button.text:
        del user_data[chat_id]
        bot.send_message(chat_id, "Додавання товару скасовано.", reply_markup=main_menu_markup)
        return
    if user_text == back_button.text:
        prev_step = ADD_PRODUCT_STEPS[user_data[chat_id]['step_number']].get('prev_step')
        if prev_step:
            user_data[chat_id]['step_number'] = prev_step
            send_product_step_message(chat_id)
        return

    # Обробка пропуску кроків
    step_config = ADD_PRODUCT_STEPS[user_data[chat_id]['step_number']]
    if step_config.get('allow_skip') and user_text == step_config.get('skip_button'):
        go_to_next_step(chat_id)
        return

    # Валідація та збереження даних
    if step_name == 'waiting_name':
        if 3 <= len(user_text) <= 100:
            user_data[chat_id]['data']['product_name'] = user_text
            go_to_next_step(chat_id)
        else:
            bot.reply_to(message, "Назва має бути від 3 до 100 символів.")
    elif step_name == 'waiting_price':
        if len(user_text) <= 50:
            user_data[chat_id]['data']['price'] = user_text
            go_to_next_step(chat_id)
        else:
            bot.reply_to(message, "Ціна занадто довга.")
    elif step_name == 'waiting_photos':
        if user_text == "Далі":
            go_to_next_step(chat_id)
        else:
            bot.reply_to(message, "Надішліть фото або натисніть 'Далі'.")
    elif step_name == 'waiting_description':
        if 10 <= len(user_text) <= 1000:
            user_data[chat_id]['data']['description'] = user_text
            # Генерація хештегів на цьому етапі
            user_data[chat_id]['data']['hashtags'] = generate_hashtags(user_text)
            go_to_next_step(chat_id) # Перехід до підтвердження
        else:
            bot.reply_to(message, "Опис має бути від 10 до 1000 символів.")
    else:
        # Для кроків, що очікують нетекстовий ввід
        bot.reply_to(message, "Будь ласка, дотримуйтесь інструкцій або скористайтесь кнопками.")

@error_handler
def process_product_photo(message):
    """Обробляє фото товару."""
    chat_id = message.chat.id
    if chat_id in user_data and user_data[chat_id].get('step') == 'waiting_photos':
        if len(user_data[chat_id]['data']['photos']) < 5:
            file_id = message.photo[-1].file_id
            user_data[chat_id]['data']['photos'].append(file_id)
            count = len(user_data[chat_id]['data']['photos'])
            bot.reply_to(message, f"✅ Фото {count}/5 додано. Надішліть ще або натисніть 'Далі'.")
        else:
            bot.reply_to(message, "Максимум 5 фото. Натисніть 'Далі'.")

@error_handler
def process_product_location(message):
    """Обробляє геолокацію товару."""
    chat_id = message.chat.id
    if chat_id in user_data and user_data[chat_id].get('step') == 'waiting_location':
        user_data[chat_id]['data']['geolocation'] = json.dumps({
            'latitude': message.location.latitude,
            'longitude': message.location.longitude
        })
        bot.reply_to(message, "✅ Геолокацію додано!")
        go_to_next_step(chat_id)

@error_handler
def confirm_and_send_for_moderation(chat_id):
    """Зберігає товар у БД та відправляє на модерацію."""
    data = user_data[chat_id]['data']
    
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        with conn.cursor() as cur:
            user_info = bot.get_chat(chat_id)
            seller_username = user_info.username or 'Немає'
            
            cur.execute("""
                INSERT INTO products (seller_chat_id, seller_username, product_name, price, description, photos, geolocation, shipping_options, hashtags, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING id;
            """, (
                chat_id, seller_username, data.get('product_name'), data.get('price'),
                data.get('description'), json.dumps(data.get('photos')), data.get('geolocation'),
                json.dumps(data.get('shipping_options')), data.get('hashtags')
            ))
            product_id = cur.fetchone()[0]
            conn.commit()

            bot.send_message(chat_id, 
                f"✅ Товар '{data['product_name']}' відправлено на модерацію!",
                reply_markup=main_menu_markup)
            
            send_product_for_admin_review(product_id)
            
            del user_data[chat_id]
            log_statistics('product_added', chat_id, product_id)
    except Exception as e:
        conn.rollback()
        logger.error(f"Помилка збереження товару: {e}", exc_info=True)
        bot.send_message(chat_id, "Помилка збереження товару.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_product_for_admin_review(product_id):
    """Надсилає товар на модерацію адміну з новими кнопками."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            if not product:
                logger.error(f"Не знайдено товар {product_id} для відправки на модерацію.")
                return

    finally:
        if conn:
            conn.close()

    data = dict(product) # Конвертуємо в словник
    photos = json.loads(data.get('photos') or '[]')
    shipping_options = json.loads(data.get('shipping_options') or '[]')
    
    review_text = (
        f"📦 *Новий товар на модерацію*\n\n"
        f"🆔 ID: `{data['id']}`\n"
        f"📝 Назва: {data['product_name']}\n"
        f"💰 Ціна: {data['price']}\n"
        f"🚚 Доставка: {', '.join(shipping_options) or 'Не вказано'}\n"
        f"📄 Опис: {data['description'][:200]}...\n"
        f"📍 Геолокація: {'Так' if data['geolocation'] else 'Ні'}\n"
        f"🏷️ Хештеги: `{data['hashtags']}`\n\n"
        f"👤 Продавець: [{f'@{data["seller_username"]}' if data['seller_username'] != 'Немає' else 'Користувач'}](tg://user?id={data['seller_chat_id']})"
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
        types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
    )
    # Нові кнопки для модератора
    markup.add(
        types.InlineKeyboardButton("✏️ Ред. хештеги", callback_data=f"mod_edit_tags_{product_id}"),
        types.InlineKeyboardButton("🔄 Повернути фото", callback_data=f"mod_rotate_photo_{product_id}")
    )
    
    try:
        if photos:
            media = [types.InputMediaPhoto(p, caption=review_text if i == 0 else '', parse_mode='Markdown') for i, p in enumerate(photos)]
            sent_messages = bot.send_media_group(ADMIN_CHAT_ID, media)
            admin_msg = bot.send_message(ADMIN_CHAT_ID, f"Оберіть дію для товару ID: `{product_id}`", reply_markup=markup, parse_mode='Markdown', reply_to_message_id=sent_messages[0].message_id)
        else:
            admin_msg = bot.send_message(ADMIN_CHAT_ID, review_text, parse_mode='Markdown', reply_markup=markup)
        
        # Зберігаємо ID повідомлення модерації
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE products SET admin_message_id = %s WHERE id = %s", (admin_msg.message_id, product_id))
                conn.commit()
            conn.close()

    except Exception as e:
        logger.error(f"Помилка при відправці товару {product_id} адміністратору: {e}", exc_info=True)


# --- 13. Обробники повідомлень та кнопок меню ---
@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'location'])
@error_handler
def handle_messages(message):
    """Основний обробник повідомлень."""
    chat_id = message.chat.id
    user_text = message.text if message.content_type == 'text' else ""

    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return
    
    # Оновлюємо активність користувача, але без реферера, бо це не /start
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE chat_id = %s", (chat_id,))
                conn.commit()
        finally: conn.close()
    
    # Пріоритет - багатоетапні процеси
    if chat_id in user_data and 'flow' in user_data[chat_id]:
        flow = user_data[chat_id]['flow']
        if flow == 'add_product':
            if message.content_type == 'text':
                process_product_step(message)
            elif message.content_type == 'photo':
                process_product_photo(message)
            elif message.content_type == 'location':
                process_product_location(message)
        elif flow == 'change_price':
            process_new_price(message)
        elif flow == 'mod_edit_tags':
            process_new_hashtags_mod(message)
        return

    # Обробка кнопок меню
    if user_text == "📦 Додати товар": start_add_product_flow(message)
    elif user_text == "📋 Мої товари": send_my_products(message)
    elif user_text == "⭐ Обрані": send_favorites(message)
    elif user_text == "❓ Допомога": send_help_message(message)
    elif user_text == "📺 Наш канал": send_channel_link(message)
    elif user_text == "🤖 AI Помічник":
        bot.send_message(chat_id, "Привіт! Задайте мені будь-яке питання. (Напишіть 'скасувати' для виходу.)", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, handle_ai_chat)
    elif message.content_type == 'text':
        handle_ai_chat(message) # За замовчуванням обробляємо як запит до AI
    else:
        bot.reply_to(message, "Не розумію ваш запит. Скористайтесь меню.")


@error_handler
def handle_ai_chat(message):
    """Обробляє повідомлення в режимі AI чату."""
    # ... (код залишається без змін)
    chat_id = message.chat.id
    user_text = message.text

    # Перевірка на скасування AI чату
    if user_text.lower() == "скасувати" or user_text == "❌ Скасувати":
        bot.send_message(chat_id, "Чат з AI скасовано.", reply_markup=main_menu_markup)
        # Важливо: при виході з handle_ai_chat, telebot автоматично скасує register_next_step_handler.
        # Якщо ви хочете явно скинути handler, можна використовувати `bot.clear_step_handler_by_chat_id(chat_id)`.
        return

    # Це перевірка на випадок, якщо користувач, перебуваючи в AI чаті,
    # знову натиснув "🤖 AI Помічник" або `/start`.
    if user_text == "🤖 AI Помічник" or user_text == "/start":
        bot.send_message(chat_id, "Ви вже в режимі AI чату. Напишіть 'скасувати' для виходу.", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(message, handle_ai_chat) # Знову реєструємо для продовження AI чату
        return # Важливо повернутися, щоб уникнути подвійної обробки

    save_conversation(chat_id, user_text, 'user') # Зберігаємо повідомлення користувача в історії
    
    # Отримуємо історію розмов для надання контексту Gemini AI
    conversation_history = get_conversation_history(chat_id, limit=10) # Обмежуємо історію до 10 останніх повідомлень
    
    ai_reply = get_gemini_response(user_text, conversation_history) # Отримуємо відповідь від Gemini
    save_conversation(chat_id, ai_reply, 'ai') # Зберігаємо відповідь AI в історії
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Скасувати"))
    bot.send_message(chat_id, f"🤖 Думаю...\n{ai_reply}", reply_markup=markup)
    bot.register_next_step_handler(message, handle_ai_chat) # Продовжуємо AI чат


# --- 14. Функції розділів меню ---
@error_handler
def send_my_products(message):
    """Надсилає список товарів користувача."""
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, product_name, status, price, created_at, channel_message_id, views, republish_count, last_republish_date
                FROM products WHERE seller_chat_id = %s ORDER BY created_at DESC
            """, (chat_id,))
            products = cur.fetchall()

        if not products:
            bot.send_message(chat_id, "📭 У вас ще немає товарів.")
            return

        bot.send_message(chat_id, "📋 *Ваші товари:*", parse_mode='Markdown')
        for prod in products:
            status_map = {'pending': '⏳ на розгляді', 'approved': '✅ опубліковано', 'rejected': '❌ відхилено', 'sold': '💰 продано'}
            status_text = status_map.get(prod['status'], prod['status'])
            created_at = prod['created_at'].strftime('%d.%m.%Y %H:%M')
            
            text = (
                f"*{prod['product_name']}*\n"
                f"   Ціна: {prod['price']}\n"
                f"   Статус: {status_text}\n"
                f"   Дата: {created_at}\n"
            )
            markup = types.InlineKeyboardMarkup(row_width=3)
            
            if prod['status'] == 'approved':
                text += f"   👁️ Перегляди: {prod['views']}\n"
                channel_link_part = str(CHANNEL_ID).replace("-100", "")
                url = f"https://t.me/c/{channel_link_part}/{prod['channel_message_id']}"
                markup.add(types.InlineKeyboardButton("👀 Переглянути", url=url))
                markup.add(types.InlineKeyboardButton("✅ Продано", callback_data=f"sold_my_{prod['id']}"))
                # Нова кнопка "Змінити ціну"
                markup.add(types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{prod['id']}"))
            
            markup.add(types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_my_{prod['id']}"))
            
            bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)

    finally:
        if conn:
            conn.close()

@error_handler
def send_favorites(message):
    """Надсилає список обраних товарів."""
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.product_name, p.price, p.status, p.channel_message_id
                FROM products p
                JOIN favorites f ON p.id = f.product_id
                WHERE f.user_chat_id = %s AND p.status = 'approved'
                ORDER BY p.created_at DESC;
            """, (chat_id,))
            favorites = cur.fetchall()
        
        if not favorites:
            bot.send_message(chat_id, "📜 Ваш список обраних порожній. Ви можете додати товар, натиснувши ❤️ під ним у каналі.")
            return

        bot.send_message(chat_id, "⭐ *Ваші обрані товари:*", parse_mode='Markdown')
        for fav in favorites:
            channel_link_part = str(CHANNEL_ID).replace("-100", "")
            url = f"https://t.me/c/{channel_link_part}/{fav['channel_message_id']}"
            text = f"*{fav['product_name']}*\n💰 {fav['price']}"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👀 Переглянути в каналі", url=url))
            bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup, disable_web_page_preview=True)
    finally:
        if conn:
            conn.close()

@error_handler
def send_help_message(message):
    """Надсилає довідкову інформацію та кнопку для комісії."""
    help_text = (
        "🆘 *Довідка*\n\n"
        "🤖 Я ваш AI-помічник. Ось що я вмію:\n"
        "📦 *Додати товар* - створити оголошення.\n"
        "📋 *Мої товари* - керувати вашими товарами.\n"
        "⭐ *Обрані* - переглянути товари, які ви лайкнули.\n"
        "📺 *Наш канал* - переглянути всі пропозиції.\n"
        "🤖 *AI Помічник* - поспілкуватися з AI.\n\n"
        "✍️ *Правила сервісу*:\n"
        "– Доставку оплачує *покупець*.\n"
        "– Комісію сервісу сплачує *продавець*.\n\n"
        f"Якщо виникли технічні проблеми, зверніться до адміністратора."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💰 Детальніше про комісію", callback_data="show_commission_info"))
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown', reply_markup=markup)

@error_handler
def send_commission_info(call):
    """Надсилає інформацію про комісію."""
    commission_rate_percent = 10
    text = (
        f"💰 *Інформація про комісію*\n\n"
        f"За успішний продаж товару через нашого бота стягується комісія у розмірі **{commission_rate_percent}%** від ціни продажу.\n\n"
        f"Комісію сплачує *продавець*. Після того, як ви позначите товар як 'Продано', система розрахує суму.\n\n"
        f"Реквізити для сплати (Monobank):\n`{MONOBANK_CARD_NUMBER}`\n\n"
        f"Сплачуйте комісію вчасно, щоб уникнути обмежень."
    )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, parse_mode='Markdown')

@error_handler
def send_channel_link(message):
    """Надсилає посилання на канал та інформацію про реферальну систему."""
    chat_id = message.chat.id
    try:
        chat_info = bot.get_chat(CHANNEL_ID)
        channel_link = chat_info.invite_link or f"https://t.me/{chat_info.username}"
        
        # Реферальна система
        referral_link = f"https://t.me/{bot.get_me().username}?start={chat_id}"
        invite_text = (
            f"📺 *Наш канал з оголошеннями*\n\n"
            f"Приєднуйтесь, щоб не пропустити нові товари!\n"
            f"👉 [Перейти до каналу]({channel_link})\n\n"
            f"🏆 *Приводьте друзів та вигравайте гроші!*\n\n"
            f"Поділіться вашим особистим посиланням з друзями. "
            f"Коли новий користувач приєднається за вашим посиланням, ви автоматично стаєте учасником "
            f"щотижневих, щомісячних та щорічних розіграшів!\n\n"
            f"🔗 *Ваше посилання для запрошення:*\n`{referral_link}`"
        )
        bot.send_message(chat_id, invite_text, parse_mode='Markdown', disable_web_page_preview=True)
        log_statistics('channel_visit', chat_id)
    except Exception as e:
        logger.error(f"Помилка при формуванні посилання на канал: {e}", exc_info=True)
        bot.send_message(chat_id, "❌ Посилання на канал тимчасово недоступне.")


# --- 15. Обробники Callback Query ---
@bot.callback_query_handler(func=lambda call: True)
@error_handler
def callback_inline(call):
    """Основний обробник інлайн-кнопок."""
    action = call.data.split('_')[0]
    
    # Модерація
    if action in ['approve', 'reject']: handle_product_moderation_callbacks(call)
    elif action == 'mod': handle_moderator_actions(call)
    
    # Керування товарами користувача
    elif action == 'sold' and call.data.startswith('sold_my'): handle_seller_sold_product(call)
    elif action == 'delete' and call.data.startswith('delete_my'): handle_delete_my_product(call)
    elif action == 'change' and call.data.startswith('change_price'): handle_change_price_init(call)
    
    # Обране (лайки)
    elif action == 'toggle' and call.data.startswith('toggle_favorite'): handle_toggle_favorite(call)
    
    # Доставка
    elif action == 'shipping': handle_shipping_choice(call)
    
    # Допомога
    elif call.data == 'show_commission_info': send_commission_info(call)
    
    # Адмін-панель
    elif action == 'admin': handle_admin_callbacks(call)
    
    else:
        bot.answer_callback_query(call.id, "Невідома дія.")


# --- 16. Логіка модерації та керування товарами ---

@error_handler
def handle_product_moderation_callbacks(call):
    """Обробляє схвалення/відхилення товару."""
    if call.message.chat.id != ADMIN_CHAT_ID: return
    
    action, product_id_str = call.data.split('_')
    product_id = int(product_id_str)

    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE id = %s;", (product_id,))
            product = cur.fetchone()
            if not product:
                bot.answer_callback_query(call.id, "Товар не знайдено.")
                return

            if action == 'approve':
                if product['status'] != 'pending':
                    bot.answer_callback_query(call.id, f"Товар вже має статус '{product['status']}'.")
                    return
                
                # Публікація в каналі
                publish_product_to_channel(product_id)
                
                # Оновлення повідомлення модерації
                bot.edit_message_text(f"✅ Товар *'{product['product_name']}'* (ID: {product_id}) опубліковано.",
                                      call.message.chat.id, call.message.message_id, parse_mode='Markdown')

            elif action == 'reject':
                cur.execute("UPDATE products SET status = 'rejected', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                            (call.message.chat.id, product_id))
                conn.commit()
                bot.send_message(product['seller_chat_id'], f"❌ Ваш товар '{product['product_name']}' було відхилено.")
                bot.edit_message_text(f"❌ Товар *'{product['product_name']}'* (ID: {product_id}) відхилено.",
                                      call.message.chat.id, call.message.message_id, parse_mode='Markdown')
            
            log_statistics(f'product_{action}', call.message.chat.id, product_id)

    finally:
        if conn:
            conn.close()
    bot.answer_callback_query(call.id)

@error_handler
def publish_product_to_channel(product_id):
    """Публікує або оновлює товар в каналі."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            if not product: return

            photos = json.loads(product['photos'] or '[]')
            shipping = ", ".join(json.loads(product['shipping_options'] or '[]')) or 'Не вказано'

            channel_text = (
                f"📦 *{product['product_name']}*\n\n"
                f"💰 *Ціна:* {product['price']}\n"
                f"🚚 *Доставка:* {shipping}\n"
                f"📍 *Геолокація:* {'Присутня' if product['geolocation'] else 'Відсутня'}\n\n"
                f"📝 *Опис:*\n{product['description']}\n\n"
                f"#{product['seller_username']} {product['hashtags']}\n\n"
                f"👤 *Продавець:* [Написати](tg://user?id={product['seller_chat_id']})"
            )
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(f"❤️ {product['likes_count']}", callback_data=f"toggle_favorite_{product_id}"))

            # Видаляємо старе повідомлення, якщо є (для оновлення)
            if product['channel_message_id']:
                try: bot.delete_message(CHANNEL_ID, product['channel_message_id'])
                except: pass

            if photos:
                media = [types.InputMediaPhoto(p, caption=channel_text if i == 0 else '', parse_mode='Markdown') for i, p in enumerate(photos)]
                sent_messages = bot.send_media_group(CHANNEL_ID, media)
                # Додаємо кнопки окремим повідомленням у відповідь
                published_message = bot.send_message(CHANNEL_ID, "👇", reply_markup=markup, reply_to_message_id=sent_messages[0].message_id)
                # Видаляємо "пусте" повідомлення з кнопками, щоб не засмічувати чат. Самі кнопки залишаться під медіа групою.
                bot.delete_message(CHANNEL_ID, published_message.message_id)
                # Головне повідомлення - перше з медіагрупи
                published_message = sent_messages[0]

            else:
                published_message = bot.send_message(CHANNEL_ID, channel_text, parse_mode='Markdown', reply_markup=markup)
            
            # Оновлюємо дані в БД
            cur.execute("""
                UPDATE products SET status = 'approved', moderator_id = %s, moderated_at = CURRENT_TIMESTAMP, channel_message_id = %s
                WHERE id = %s;
            """, (ADMIN_CHAT_ID, published_message.message_id, product_id))
            conn.commit()
            
            # Сповіщаємо продавця
            if product['status'] == 'pending':
                bot.send_message(product['seller_chat_id'], f"✅ Ваш товар '{product['product_name']}' опубліковано!")

    except Exception as e:
        logger.error(f"Помилка публікації товару {product_id} в канал: {e}", exc_info=True)
        conn.rollback()
    finally:
        if conn:
            conn.close()


@error_handler
def handle_seller_sold_product(call):
    """Обробляє, коли продавець відмічає товар як 'Продано'."""
    #...
    pass

@error_handler
def handle_delete_my_product(call):
    """Обробляє видалення товару продавцем."""
    #...
    pass

@error_handler
def handle_change_price_init(call):
    """Починає процес зміни ціни."""
    chat_id = call.message.chat.id
    product_id = int(call.data.split('_')[2])
    
    user_data[chat_id] = {
        'flow': 'change_price',
        'product_id': product_id
    }
    
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "Введіть нову ціну товару:", reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(call.message, process_new_price)

@error_handler
def process_new_price(message):
    """Обробляє нову ціну та оновлює товар."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'change_price':
        return
        
    product_id = user_data[chat_id]['product_id']
    new_price = message.text
    
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # Перевірка, що користувач є власником
            cur.execute("SELECT seller_chat_id FROM products WHERE id = %s", (product_id,))
            product = cur.fetchone()
            if not product or product['seller_chat_id'] != chat_id:
                bot.send_message(chat_id, "Це не ваш товар.")
                return

            cur.execute("UPDATE products SET price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (new_price, product_id))
            conn.commit()
        
        bot.send_message(chat_id, f"✅ Ціну для товару ID {product_id} оновлено на: *{new_price}*", parse_mode='Markdown')
        # Оновлюємо пост в каналі
        publish_product_to_channel(product_id)
    finally:
        if conn:
            conn.close()
        del user_data[chat_id]

# --- 17. Логіка для модератора ---
@error_handler
def handle_moderator_actions(call):
    """Обробляє дії модератора з редагування."""
    if call.message.chat.id != ADMIN_CHAT_ID: return
    
    _, action, product_id_str = call.data.split('_', 2)
    product_id = int(product_id_str)

    if action == 'edit' and call.data.startswith('mod_edit_tags'):
        user_data[ADMIN_CHAT_ID] = {'flow': 'mod_edit_tags', 'product_id': product_id}
        bot.answer_callback_query(call.id)
        bot.send_message(ADMIN_CHAT_ID, f"Введіть нові хештеги для товару ID {product_id}:", reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(call.message, process_new_hashtags_mod)

    elif action == 'rotate' and call.data.startswith('mod_rotate_photo'):
        conn = get_db_connection()
        if not conn: return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT seller_chat_id, product_name FROM products WHERE id = %s", (product_id,))
                product = cur.fetchone()
                if product:
                    # Повідомлення для користувача
                    bot.send_message(product['seller_chat_id'], f"❗️ Модератор просить вас виправити фото для товару '{product['product_name']}'. Будь ласка, видаліть його та додайте заново з коректними фотографіями.")
                    # Повідомлення для модератора
                    bot.answer_callback_query(call.id, "Запит на виправлення фото відправлено користувачу.")
                    bot.edit_message_text(f"Запит на виправлення фото для товару ID {product_id} відправлено.", call.message.chat.id, call.message.message_id)
        finally:
            if conn:
                conn.close()

@error_handler
def process_new_hashtags_mod(message):
    """Обробляє нові хештеги від модератора."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'mod_edit_tags':
        return
    
    product_id = user_data[chat_id]['product_id']
    new_hashtags = message.text
    
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE products SET hashtags = %s WHERE id = %s", (new_hashtags, product_id))
            conn.commit()
        bot.send_message(chat_id, f"✅ Хештеги для товару ID {product_id} оновлено.")
        # Повторно відправляємо повідомлення для модерації з оновленими даними
        send_product_for_admin_review(product_id)
    finally:
        if conn:
            conn.close()
        del user_data[chat_id]

# --- 18. Логіка для обраного та доставки ---
@error_handler
def handle_toggle_favorite(call):
    """Обробляє додавання/видалення з обраного (лайк)."""
    user_chat_id = call.from_user.id
    product_id = int(call.data.split('_')[2])
    
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # Перевіряємо, чи лайк вже існує
            cur.execute("SELECT id FROM favorites WHERE user_chat_id = %s AND product_id = %s", (user_chat_id, product_id))
            is_favorited = cur.fetchone()

            if is_favorited:
                # Видаляємо лайк
                cur.execute("DELETE FROM favorites WHERE id = %s", (is_favorited['id'],))
                cur.execute("UPDATE products SET likes_count = likes_count - 1 WHERE id = %s RETURNING likes_count", (product_id,))
                bot.answer_callback_query(call.id, "💔 Видалено з обраного")
            else:
                # Додаємо лайк
                cur.execute("INSERT INTO favorites (user_chat_id, product_id) VALUES (%s, %s)", (user_chat_id, product_id))
                cur.execute("UPDATE products SET likes_count = likes_count + 1 WHERE id = %s RETURNING likes_count", (product_id,))
                bot.answer_callback_query(call.id, "❤️ Додано до обраного!")
            
            likes_count = cur.fetchone()['likes_count']
            conn.commit()

            # Оновлюємо кнопку в каналі
            new_markup = types.InlineKeyboardMarkup()
            new_markup.add(types.InlineKeyboardButton(f"❤️ {likes_count}", callback_data=f"toggle_favorite_{product_id}"))
            try:
                bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=call.message.message_id, reply_markup=new_markup)
            except Exception as e:
                logger.warning(f"Не вдалося оновити лічильник лайків для повідомлення {call.message.message_id}: {e}")

    finally:
        if conn:
            conn.close()

@error_handler
def handle_shipping_choice(call):
    """Обробляє вибір способів доставки."""
    chat_id = call.message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('step') != 'waiting_shipping':
        bot.answer_callback_query(call.id, "Ця дія застаріла.")
        return

    if call.data == 'shipping_next':
        go_to_next_step(chat_id)
        bot.delete_message(chat_id, call.message.message_id) # Видаляємо повідомлення з кнопками
        return
        
    option = call.data.replace('shipping_', '')
    selected = user_data[chat_id]['data']['shipping_options']

    if option in selected:
        selected.remove(option)
    else:
        selected.append(option)
    
    # Оновлюємо клавіатуру, щоб показати вибір
    inline_markup = types.InlineKeyboardMarkup(row_width=2)
    options = ["Наложка Нова Пошта", "Наложка Укрпошта"]
    buttons = [types.InlineKeyboardButton(f"{'✅ ' if opt in selected else ''}{opt}", callback_data=f"shipping_{opt}") for opt in options]
    inline_markup.add(*buttons)
    inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
    
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=inline_markup)
    except telebot.apihelper.ApiTelegramException: # Якщо повідомлення не змінилось
        pass
    
    bot.answer_callback_query(call.id)
    
# --- 19. Адмін-панель: Реферали ---
@error_handler
def send_admin_referrals(call):
    """Показує статистику рефералів та запускає розіграш."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # Топ-5 реферерів
            cur.execute("""
                SELECT referrer_id, COUNT(*) as referrals_count
                FROM users
                WHERE referrer_id IS NOT NULL
                GROUP BY referrer_id
                ORDER BY referrals_count DESC
                LIMIT 5;
            """)
            top_referrers = cur.fetchall()

            # Учасники розіграшу за останній тиждень
            cur.execute("""
                SELECT DISTINCT referrer_id FROM users
                WHERE referrer_id IS NOT NULL AND joined_at >= NOW() - INTERVAL '7 days';
            """)
            weekly_participants = [row['referrer_id'] for row in cur.fetchall()]
        
        text = "🏆 *Реферальна система*\n\n*Топ-5 реферерів за весь час:*\n"
        if top_referrers:
            for i, r in enumerate(top_referrers, 1):
                try: user_info = bot.get_chat(r['referrer_id'])
                except: user_info = None
                username = f"@{user_info.username}" if user_info and user_info.username else f"ID: {r['referrer_id']}"
                text += f"{i}. {username} - {r['referrals_count']} запрошень\n"
        else:
            text += "_Немає даних_\n"
        
        text += f"\n*Учасників у щотижневому розіграші:* {len(weekly_participants)}\n"
        
        winner_text = ""
        if weekly_participants:
            winner_id = random.choice(weekly_participants)
            try: winner_info = bot.get_chat(winner_id)
            except: winner_info = None
            winner_username = f"@{winner_info.username}" if winner_info and winner_info.username else f"ID: {winner_id}"
            winner_text = f"\n🎉 *Переможець цього тижня:* {winner_username}!"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад до Адмін-панелі", callback_data="admin_panel_main"))

        bot.edit_message_text(text + winner_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

    finally:
        if conn:
            conn.close()

# Розширюємо обробник адмін-колбеків
@error_handler
def handle_admin_callbacks(call):
    """Обробляє колбеки адмін-панелі."""
    if call.message.chat.id != ADMIN_CHAT_ID: return
    
    action = call.data.split('_')[1]

    if action == "stats": pass # send_admin_statistics(call)
    elif action == "pending": pass # send_pending_products_for_moderation(call)
    elif action == "users": pass # send_users_list(call)
    elif action == "block": pass # process_user_for_block_unblock(call)
    elif action == "referrals": send_admin_referrals(call)
    elif action == "panel" and call.data == "admin_panel_main":
        # Повернення до головної адмін-панелі
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
            types.InlineKeyboardButton("⏳ На модерації", callback_data="admin_pending"),
            types.InlineKeyboardButton("👥 Користувачі", callback_data="admin_users"),
            types.InlineKeyboardButton("🚫 Блокування", callback_data="admin_block"),
            types.InlineKeyboardButton("🏆 Реферали", callback_data="admin_referrals")
        )
        bot.edit_message_text("🔧 *Адмін-панель*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')

    bot.answer_callback_query(call.id)

# --- 20. Запуск бота ---
if __name__ == '__main__':
    logger.info("Запуск ініціалізації БД...")
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
        logger.critical("WEBHOOK_URL або TOKEN не встановлено. Бот не може працювати.")
        exit(1)

    @app.route(f'/{TOKEN}', methods=['POST'])
    def webhook_handler():
        if request.headers.get('content-type') == 'application/json':
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '!', 200
        else:
            return 'Content-Type must be application/json', 403

    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)
