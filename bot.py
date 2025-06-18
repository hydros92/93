import os
import telebot
from telebot import types
import logging
from datetime import datetime, timedelta, timezone, date # Додано date
import re
import json
import requests
from dotenv import load_dotenv
import random # Додано для переможців розіграшу
import time # Додано імпорт модуля time

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
# RAPIDAPI_KEY та RAPIDAPI_HOST були у першому файлі, але не використовуються у другому, тому прибрав їх для спрощення.
# Якщо вони потрібні, їх потрібно буде інтегрувати у відповідні функції.

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
    # Перевірка ADMIN_CHAT_ID та CHANNEL_ID на ненульові значення
    # (якщо вони були конвертовані в int і отримали 0 за замовчуванням)
    if ADMIN_CHAT_ID == 0:
        missing_vars.append('ADMIN_CHAT_ID')
    if CHANNEL_ID == 0:
        missing_vars.append('CHANNEL_ID')

    if missing_vars:
        logger.critical(f"Критична помилка: Відсутні наступні змінні оточення: {', '.join(missing_vars)}. Бот не може працювати.")
        exit(1)

# Викликаємо функцію перевірки на старті програми
validate_env_vars()

# --- 4. Ініціалізація TeleBot та Flask ---
app = Flask(__name__)
bot = telebot.TeleBot(TOKEN)

# --- 4.1. НАЛАШТУВАННЯ МЕРЕЖЕВИХ ЗАПИТІВ (RETRY-МЕХАНІЗМ) ---
# Додано для підвищення стабільності бота. Цей блок автоматично
# повторює запити до Telegram API у випадку тимчасових мережевих проблем.
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=3,  # Загальна кількість спроб
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP коди, при яких повторювати
        allowed_methods=frozenset(['HEAD', 'GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'TRACE']), # Методи для повторення
        backoff_factor=1,  # Затримка між спробами (1с, 2с, 4с)
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
            chat_id_to_notify = ADMIN_CHAT_ID # За замовчуванням надсилаємо адміну

            # Спроба визначити chat_id користувача, який викликав помилку
            if args:
                first_arg = args[0]
                if isinstance(first_arg, types.Message):
                    chat_id_to_notify = first_arg.chat.id
                elif isinstance(first_arg, types.CallbackQuery):
                    chat_id_to_notify = first_arg.message.chat.id
            
            try:
                # Надсилаємо детальне сповіщення адміну
                bot.send_message(ADMIN_CHAT_ID, f"🚨 Критична помилка в боті!\nФункція: `{func.__name__}`\nПомилка: `{e}`\nДивіться деталі в логах Render.")
                # Сповіщаємо користувача про внутрішню помилку (якщо це не адмін)
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
        # Використання DictCursor для отримання результатів у вигляді словників,
        # що зручніше для доступу до даних за назвами колонок.
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
            # Таблиця users для зберігання інформації про користувачів бота
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
                    referrer_id BIGINT -- Додано для реферальної системи
                );
            """))
            # Таблиця products для зберігання інформації про товари
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    seller_username TEXT,
                    product_name TEXT NOT NULL,
                    price TEXT NOT NULL,
                    description TEXT NOT NULL,
                    photos TEXT, -- Зберігатиметься як JSON рядок з file_id фотографій
                    geolocation TEXT, -- Зберігатиметься як JSON рядок {latitude: ..., longitude: ...}
                    status TEXT DEFAULT 'pending', -- pending, approved, rejected, sold, expired
                    commission_rate REAL DEFAULT 0.10,
                    commission_amount REAL DEFAULT 0,
                    moderator_id BIGINT,
                    moderated_at TIMESTAMP WITH TIME ZONE,
                    admin_message_id BIGINT, -- ID повідомлення адміністратору для модерації
                    channel_message_id BIGINT, -- ID повідомлення в каналі після публікації
                    views INTEGER DEFAULT 0,
                    likes_count INTEGER DEFAULT 0, -- Додано для функціоналу "Обране" / лайків
                    republish_count INTEGER DEFAULT 0,
                    last_republish_date DATE,
                    shipping_options TEXT, -- Додано для варіантів доставки (JSON array)
                    hashtags TEXT, -- Додано для збереження згенерованих хештегів
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))
            # Таблиця favorites для зберігання обраних товарів користувачів
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS favorites (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    UNIQUE(user_chat_id, product_id) -- Забезпечує, що користувач може додати товар в обране лише один раз
                );
            """))
            # Таблиця conversations для зберігання історії чату з AI
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    product_id INTEGER, -- Може бути NULL, якщо розмова не стосується конкретного товару
                    message_text TEXT,
                    sender_type TEXT, -- 'user' або 'ai' (для Gemini API це 'model')
                    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """))
            # Таблиця commission_transactions для обліку комісій
            cur.execute(pg_sql.SQL("""
                CREATE TABLE IF NOT EXISTS commission_transactions (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    seller_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    status TEXT DEFAULT 'pending_payment', -- pending_payment, paid, cancelled
                    payment_details TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP WITH TIME ZONE
                );
            """))
            # Таблиця statistics для збору різних даних про використання бота
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
            
            # --- Міграція схеми для існуючих таблиць (додавання нових стовпців) ---
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
                        # Якщо стовпець вже існує або інша помилка, просто логуємо
                        logger.warning(f"Помилка міграції '{column_sql}': {e}")
                        conn.rollback() # Відкат у разі помилки міграції
            conn.commit() # Фінальний коміт після всіх операцій
            logger.info("Таблиці бази даних успішно ініціалізовано або оновлено.")
    except Exception as e:
        logger.critical(f"Критична помилка ініціалізації бази даних: {e}", exc_info=True)
        conn.rollback() # Відкат всіх змін у випадку критичної помилки
        exit(1) # Завершуємо роботу, якщо БД не може бути ініціалізована
    finally:
        if conn:
            conn.close()

# --- 7. Зберігання даних користувача для багатошагових процесів ---
# Це словник, що тимчасово зберігає стан користувача під час багатошагових операцій (наприклад, додавання товару).
# Дані зберігаються в пам'яті сервера і втрачаються при перезапуску.
user_data = {}

# --- 8. Функції роботи з користувачами та загальні допоміжні функції ---
@error_handler
def save_user(message_or_user, referrer_id=None):
    """
    Зберігає або оновлює інформацію про користувача в базі даних PostgreSQL.
    Викликається при кожній взаємодії, щоб оновити останню активність.
    Також зберігає ID реферера, якщо користувач прийшов за реферальним посиланням.
    """
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
        # Перевіряємо, чи користувач вже існує
        cur.execute(pg_sql.SQL("SELECT chat_id, referrer_id FROM users WHERE chat_id = %s;"), (chat_id,))
        existing_user = cur.fetchone()

        if existing_user:
            # Оновлюємо існуючого користувача
            cur.execute(pg_sql.SQL("""
                UPDATE users SET username = %s, first_name = %s, last_name = %s, last_activity = CURRENT_TIMESTAMP
                WHERE chat_id = %s;
            """), (user.username, user.first_name, user.last_name, chat_id))
            logger.info(f"Користувача {chat_id} оновлено.")
        else:
            # Додаємо нового користувача
            cur.execute(pg_sql.SQL("""
                INSERT INTO users (chat_id, username, first_name, last_name, referrer_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chat_id) DO NOTHING; -- Запобігає помилкам, якщо раптом race condition
            """), (chat_id, user.username, user.first_name, user.last_name, referrer_id))
            logger.info(f"Нового користувача {chat_id} додано. Реферер: {referrer_id}")
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка при збереженні користувача {chat_id}: {e}", exc_info=True)
        conn.rollback() # Відкат змін у випадку помилки
    finally:
        if conn:
            conn.close()

@error_handler
def is_user_blocked(chat_id):
    """Перевіряє, чи заблокований користувач у базі даних."""
    conn = get_db_connection()
    if not conn: return True # У випадку помилки з'єднання, вважаємо заблокованим для безпеки
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT is_blocked FROM users WHERE chat_id = %s;"), (chat_id,))
        result = cur.fetchone()
        return result and result['is_blocked'] # Повертає True, якщо користувач заблокований
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
        if status: # Блокування користувача
            cur.execute(pg_sql.SQL("""
                UPDATE users SET is_blocked = TRUE, blocked_by = %s, blocked_at = CURRENT_TIMESTAMP
                WHERE chat_id = %s;
            """), (admin_id, chat_id))
        else: # Розблокування користувача
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
    """
    Генерує хештеги з опису товару.
    Видаляє стоп-слова та повторення, обмежує кількість хештегів.
    """
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
    unique_words = list(dict.fromkeys(filtered_words)) # Зберігаємо порядок, але тільки унікальні
    hashtags = ['#' + word for word in unique_words[:num_hashtags]] # Беремо перші N унікальних слів
    return " ".join(hashtags) if hashtags else ""

@error_handler
def log_statistics(action, user_id=None, product_id=None, details=None):
    """
    Логує дії користувачів та адміністраторів для збору статистики.
    """
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
        conn.rollback()
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
# Головна клавіатура бота з кнопками швидкого доступу.
main_menu_markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
main_menu_markup.add(types.KeyboardButton("📦 Додати товар"), types.KeyboardButton("📋 Мої товари"))
main_menu_markup.add(types.KeyboardButton("⭐ Обрані"), types.KeyboardButton("❓ Допомога")) # Додано "Обрані"
main_menu_markup.add(types.KeyboardButton("📺 Наш канал"), types.KeyboardButton("🤖 AI Помічник"))

# Кнопки для процесу додавання товару
back_button = types.KeyboardButton("🔙 Назад")
cancel_button = types.KeyboardButton("❌ Скасувати") # Змінено текст з "Скасувати додавання" на "Скасувати"

# --- 11. Обробники команд ---
@bot.message_handler(commands=['start'])
@error_handler
def send_welcome(message):
    """
    Обробник команди /start.
    Вітає нового/існуючого користувача та показує головне меню.
    Зберігає ID реферера, якщо користувач прийшов за реферальним посиланням.
    """
    chat_id = message.chat.id
    # Перевіряємо, чи не заблокований користувач
    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return

    referrer_id = None
    parts = message.text.split()
    if len(parts) > 1 and parts[0] == '/start':
        try:
            potential_referrer_id = int(parts[1])
            if potential_referrer_id != chat_id: # Користувач не може бути своїм реферером
                referrer_id = potential_referrer_id
        except (ValueError, IndexError):
            pass # Ігноруємо, якщо параметр не є числом або відсутній

    # Зберігаємо або оновлюємо інформацію про користувача в БД, передаючи referrer_id
    save_user(message, referrer_id)
    # Логуємо статистику використання команди /start
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
        "⭐ Додаю товари до обраного\n" # Додано
        "🏆 Організовую розіграші для активних користувачів\n\n" # Додано
        "Оберіть дію з меню або просто напишіть мені!"
    )
    # Надсилаємо вітальне повідомлення з головним меню
    bot.send_message(chat_id, welcome_text, reply_markup=main_menu_markup, parse_mode='Markdown')

@bot.message_handler(commands=['admin'])
@error_handler
def admin_panel(message):
    """
    Обробник команди /admin.
    Надає доступ до адмін-панелі тільки для ADMIN_CHAT_ID.
    """
    if message.chat.id != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас немає прав доступу.")
        return

    # Створюємо інлайн-клавіатуру для адмін-панелі
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("⏳ На модерації", callback_data="admin_pending"),
        types.InlineKeyboardButton("👥 Користувачі", callback_data="admin_users"),
        types.InlineKeyboardButton("🚫 Блокування", callback_data="admin_block"),
        types.InlineKeyboardButton("💰 Комісії", callback_data="admin_commissions"),
        types.InlineKeyboardButton("🤖 AI Статистика", callback_data="admin_ai_stats"),
        types.InlineKeyboardButton("🏆 Реферали", callback_data="admin_referrals") # Додано
    )
    bot.send_message(message.chat.id, "🔧 *Адмін-панель*", reply_markup=markup, parse_mode='Markdown')


# --- 12. Потік додавання товару ---
# Конфігурація кроків для додавання нового товару.
# Кожен крок має назву, підказку, наступний крок, попередній крок,
# та опції для пропуску (для фото та геолокації).
# Додано крок для вибору опцій доставки.
ADD_PRODUCT_STEPS = {
    1: {'name': 'waiting_name', 'prompt': "📝 *Крок 1/6: Назва товару*\n\nВведіть назву товару:", 'next_step': 2, 'prev_step': None},
    2: {'name': 'waiting_price', 'prompt': "💰 *Крок 2/6: Ціна*\n\nВведіть ціну (наприклад, `500 грн`, `100 USD` або `Договірна`):", 'next_step': 3, 'prev_step': 1},
    3: {'name': 'waiting_photos', 'prompt': "📸 *Крок 3/6: Фотографії*\n\nНадішліть до 5 фото (по одному). Коли закінчите - натисніть 'Далі':", 'next_step': 4, 'allow_skip': True, 'skip_button': 'Пропустити фото', 'prev_step': 2},
    4: {'name': 'waiting_location', 'prompt': "📍 *Крок 4/6: Геолокація*\n\nНадішліть геолокацію або натисніть 'Пропустити':", 'next_step': 5, 'allow_skip': True, 'skip_button': 'Пропустити геолокацію', 'prev_step': 3},
    5: {'name': 'waiting_shipping', 'prompt': "🚚 *Крок 5/6: Доставка*\n\nОберіть доступні способи доставки (можна обрати декілька):", 'next_step': 6, 'prev_step': 4}, # Новий крок
    6: {'name': 'waiting_description', 'prompt': "✍️ *Крок 6/6: Опис*\n\nНапишіть детальний опис товару:", 'next_step': 'confirm', 'prev_step': 5}
}

@error_handler
def start_add_product_flow(message):
    """Починає процес додавання нового товару, ініціалізуючи user_data."""
    chat_id = message.chat.id
    user_data[chat_id] = {
        'flow': 'add_product', # Додано для розрізнення потоків
        'step_number': 1, 
        'data': {
            'photos': [], 
            'geolocation': None,
            'shipping_options': [], # Додано для доставки
            'product_name': '',
            'price': '',
            'description': '',
            'hashtags': '' # Додано для хештегів
        }
    }
    send_product_step_message(chat_id)
    log_statistics('start_add_product', chat_id)

@error_handler
def send_product_step_message(chat_id):
    """Надсилає користувачу повідомлення для поточного кроку додавання товару."""
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        return # Вийти, якщо користувач не в цьому потоці

    current_step_number = user_data[chat_id]['step_number']
    step_config = ADD_PRODUCT_STEPS[current_step_number]
    user_data[chat_id]['step'] = step_config['name'] # Зберігаємо назву поточного кроку

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    
    # Додаємо специфічні кнопки для кроків з фото, локацією та доставкою
    if step_config['name'] == 'waiting_photos':
        markup.add(types.KeyboardButton("Далі"))
        markup.add(types.KeyboardButton(step_config['skip_button']))
    elif step_config['name'] == 'waiting_location':
        markup.add(types.KeyboardButton("📍 Надіслати геолокацію", request_location=True))
        markup.add(types.KeyboardButton(step_config['skip_button']))
    elif step_config['name'] == 'waiting_shipping':
        # Для кроку доставки використовуємо інлайн-клавіатуру
        inline_markup = types.InlineKeyboardMarkup(row_width=2)
        shipping_options_list = ["Наложка Нова Пошта", "Наложка Укрпошта", "Особиста зустріч"] # Додано варіанти
        selected_options = user_data[chat_id]['data'].get('shipping_options', [])

        buttons = []
        for opt in shipping_options_list:
            emoji = '✅ ' if opt in selected_options else ''
            buttons.append(types.InlineKeyboardButton(f"{emoji}{opt}", callback_data=f"shipping_{opt}"))
        
        inline_markup.add(*buttons)
        inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
        
        bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=inline_markup)
        return # Важливо вийти, оскільки ми вже надіслали інлайн-клавіатуру
    
    # Додаємо кнопку "Назад", якщо це не перший крок
    if step_config['prev_step'] is not None:
        markup.add(back_button)
    
    # Завжди додаємо кнопку "Скасувати"
    markup.add(cancel_button)
    
    bot.send_message(chat_id, step_config['prompt'], parse_mode='Markdown', reply_markup=markup)

@error_handler
def process_product_step(message):
    """
    Обробляє текстовий ввід користувача під час багатошагового процесу додавання товару.
    Виконує валідацію вводу та перехід між кроками.
    """
    chat_id = message.chat.id
    # Перевіряємо, чи користувач дійсно знаходиться в процесі додавання товару
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'add_product':
        # Якщо ні, ігноруємо або просимо використати меню
        bot.send_message(chat_id, "Ви не в процесі додавання товару. Скористайтеся меню.", reply_markup=main_menu_markup)
        return

    current_step_number = user_data[chat_id]['step_number']
    step_config = ADD_PRODUCT_STEPS[current_step_number]
    user_text = message.text if message.content_type == 'text' else ""

    # Обробка скасування процесу
    if user_text == cancel_button.text:
        del user_data[chat_id] # Очищуємо дані користувача
        bot.send_message(chat_id, "Додавання товару скасовано.", reply_markup=main_menu_markup)
        return

    # Обробка кнопки "Назад"
    if user_text == back_button.text:
        if step_config['prev_step'] is not None:
            user_data[chat_id]['step_number'] = step_config['prev_step']
            send_product_step_message(chat_id)
        else:
            bot.send_message(chat_id, "Ви вже на першому кроці.")
        return

    # Обробка пропуску кроку (для фото та локації)
    if step_config.get('allow_skip') and user_text == step_config.get('skip_button'):
        go_to_next_step(chat_id)
        return

    # Валідація та збереження даних для кожного кроку
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
        if user_text == "Далі": # Якщо користувач натиснув "Далі" після додавання фото
            go_to_next_step(chat_id)
        else:
            bot.send_message(chat_id, "Надішліть фото або натисніть 'Далі'/'Пропустити фото'.")

    elif step_config['name'] == 'waiting_location':
        # Якщо користувач ввів текст замість локації або пропуску
        bot.send_message(chat_id, "Надішліть геолокацію або натисніть 'Пропустити геолокацію'.")
    
    elif step_config['name'] == 'waiting_shipping':
        # Цей крок обробляється інлайн-клавіатурою, тому тут текстовий ввід не очікується
        bot.send_message(chat_id, "Будь ласка, скористайтесь кнопками для вибору способу доставки.")

    elif step_config['name'] == 'waiting_description':
        if user_text and 10 <= len(user_text) <= 1000:
            user_data[chat_id]['data']['description'] = user_text
            user_data[chat_id]['data']['hashtags'] = generate_hashtags(user_text) # Генеруємо хештеги
            confirm_and_send_for_moderation(chat_id) # Останній крок - відправка на модерацію
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
            file_id = message.photo[-1].file_id # Беремо фото найвищої якості
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
        if message.location: # Перевіряємо, чи це дійсно об'єкт геолокації
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
    """
    Зберігає товар у БД після завершення всіх кроків,
    сповіщає користувача та адміністратора про новий товар на модерації.
    """
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
            json.dumps(data['photos']) if data['photos'] else None, # Зберігаємо список фото як JSON рядок
            json.dumps(data['geolocation']) if data['geolocation'] else None, # Зберігаємо геолокацію як JSON рядок
            json.dumps(data['shipping_options']) if data['shipping_options'] else None, # Зберігаємо опції доставки
            data['hashtags'], # Зберігаємо хештеги
        ))
        
        product_id = cur.fetchone()[0] # Отримуємо ID щойно вставленого товару
        conn.commit()
        
        # Сповіщення користувача про успішне відправлення на модерацію
        bot.send_message(chat_id, 
            f"✅ Товар '{data['product_name']}' відправлено на модерацію!\n"
            f"Ви отримаєте сповіщення після перевірки.",
            reply_markup=main_menu_markup)
        
        # Сповіщення адміністратора про новий товар
        send_product_for_admin_review(product_id) # Змінено: передаємо тільки product_id
        
        # Очищуємо тимчасові дані користувача після завершення процесу
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
    """
    Формує та надсилає повідомлення адміністратору для модерації нового товару.
    Отримує всі дані про товар з БД.
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
            f"🆔 ID: {product_id}\n"
            f"📝 Назва: {data['product_name']}\n"
            f"💰 Ціна: {data['price']}\n"
            f"📄 Опис: {data['description'][:500]}...\n" # Обрізаємо опис, якщо він занадто довгий
            f"📸 Фото: {len(photos)} шт.\n"
            f"📍 Геолокація: {'Так' if geolocation else 'Ні'}\n"
            f"🚚 Доставка: {shipping_options_text}\n" # Додано інформацію про доставку
            f"🏷️ Хештеги: {hashtags}\n\n"
            f"👤 Продавець: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={seller_chat_id})"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
            types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
        )
        # Додаємо кнопки модерації хештегів та фото
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
                # Зберігаємо message_id адмінського повідомлення
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
    """
    Основний обробник для всіх вхідних повідомлень (текст, фото, локація).
    Визначає, який функціонал має бути активований (додавання товару, AI чат, меню).
    """
    chat_id = message.chat.id
    user_text = message.text if message.content_type == 'text' else ""

    # Перевіряємо статус блокування користувача
    if is_user_blocked(chat_id):
        bot.send_message(chat_id, "❌ Ваш акаунт заблоковано.")
        return
    
    # Оновлюємо останню активність користувача
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

    # Пріоритетна обробка: якщо користувач знаходиться в багатошаговому процесі
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
        elif current_flow == 'mod_edit_tags': # Для модератора
            process_new_hashtags_mod(message)
        return # Важливо, щоб не переходити до інших обробників

    # Обробка кнопок головного меню за текстом
    if user_text == "📦 Додати товар":
        start_add_product_flow(message)
    elif user_text == "📋 Мої товари":
        send_my_products(message)
    elif user_text == "⭐ Обрані": # Додано обробник для "Обрані"
        send_favorites(message)
    elif user_text == "❓ Допомога":
        send_help_message(message)
    elif user_text == "📺 Наш канал":
        send_channel_link(message)
    elif user_text == "🤖 AI Помічник":
        bot.send_message(chat_id, "Привіт! Я ваш AI помічник. Задайте мені будь-яке питання про товари, продажі, або просто поспілкуйтесь!\n\n(Напишіть '❌ Скасувати' для виходу з режиму AI чату.)", reply_markup=types.ReplyKeyboardRemove())
        # Реєструємо наступний обробник для AI чату
        bot.register_next_step_handler(message, handle_ai_chat)
    elif message.content_type == 'text': 
        # Якщо це звичайне текстове повідомлення і воно не є командою/кнопкою меню,
        # і користувач не знаходиться в іншому потоці, передаємо його AI.
        handle_ai_chat(message)
    elif message.content_type == 'photo':
        bot.send_message(chat_id, "Я отримав ваше фото, але не знаю, що з ним робити поза процесом додавання товару. 🤔")
    elif message.content_type == 'location':
        bot.send_message(chat_id, f"Я бачу вашу геоточку: {message.location.latitude}, {message.location.longitude}. Як я можу її використати?")
    else:
        bot.send_message(chat_id, "Я не зрозумів ваш запит. Спробуйте використати кнопки меню.")

@error_handler
def handle_ai_chat(message):
    """
    Обробляє повідомлення в режимі AI чату.
    Продовжує діалог з AI, доки користувач не скасує чат.
    """
    chat_id = message.chat.id
    user_text = message.text

    # Перевірка на скасування AI чату
    if user_text.lower() == "скасувати" or user_text == "❌ Скасувати": # Змінено: враховуємо "скасувати" без емодзі
        bot.send_message(chat_id, "Чат з AI скасовано.", reply_markup=main_menu_markup)
        # Важливо: при виході з handle_ai_chat, telebot автоматично скасує register_next_step_handler.
        return

    # Це перевірка на випадок, якщо користувач, перебуваючи в AI чаті,
    # знову натиснув "🤖 AI Помічник" або `/start`.
    if user_text == "🤖 AI Помічник" or user_text == "/start":
        bot.send_message(chat_id, "Ви вже в режимі AI чату. Напишіть '❌ Скасувати' для виходу.", reply_markup=types.ReplyKeyboardRemove())
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


# --- 14. Обробники Callback-запитів ---
@bot.callback_query_handler(func=lambda call: True)
@error_handler
def callback_inline(call):
    """
    Обробник для всіх інлайн-кнопок (callback_data).
    Виконує дії залежно від callback_data.
    """
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data
    log_statistics('callback_query', chat_id, details=data)

    # Перевірка статусу блокування користувача
    if chat_id != ADMIN_CHAT_ID and is_user_blocked(chat_id):
        bot.answer_callback_query(call.id, "❌ Ваш акаунт заблоковано.")
        return

    # --- Адміністративні функції ---
    if data == "admin_stats":
        send_admin_stats(call)
    elif data == "admin_pending":
        send_pending_products_for_moderation(call)
    elif data == "admin_users":
        send_user_list(call)
    elif data == "admin_block":
        send_block_unblock_panel(call)
    elif data == "admin_commissions":
        send_commission_panel(call)
    elif data == "admin_ai_stats":
        send_ai_stats(call) # Додано
    elif data == "admin_referrals": # Додано
        send_referral_stats(call)

    # --- Обробка модерації товару ---
    elif data.startswith("approve_"):
        product_id = int(data.split("_")[1])
        approve_product(product_id, chat_id, message_id)
    elif data.startswith("reject_"):
        product_id = int(data.split("_")[1])
        reject_product(product_id, chat_id, message_id)
    elif data.startswith("block_user_"):
        target_chat_id = int(data.split("_")[2])
        block_user_action(chat_id, target_chat_id, message_id)
    elif data.startswith("unblock_user_"):
        target_chat_id = int(data.split("_")[2])
        unblock_user_action(chat_id, target_chat_id, message_id)
    elif data.startswith("toggle_block_"):
        target_chat_id = int(data.split("_")[2])
        toggle_user_block_status(chat_id, target_chat_id, message_id)
    elif data.startswith("pay_commission_"):
        product_id = int(data.split("_")[2])
        mark_commission_paid(product_id, chat_id, message_id)
    elif data.startswith("mod_edit_tags_"): # Модерація: редагувати хештеги
        product_id = int(data.split("_")[3])
        start_edit_hashtags_flow(chat_id, product_id, message_id)
    elif data.startswith("mod_rotate_photo_"): # Модерація: запит на виправлення фото
        product_id = int(data.split("_")[3])
        request_photo_correction(product_id, chat_id, message_id)

    # --- Обробка "Мої товари" ---
    elif data.startswith("view_my_product_"):
        product_id = int(data.split("_")[3])
        send_product_details_to_seller(chat_id, product_id, message_id)
    elif data.startswith("delete_product_"):
        product_id = int(data.split("_")[2])
        delete_product(chat_id, product_id, message_id)
    elif data.startswith("change_price_"):
        product_id = int(data.split("_")[2])
        start_change_price_flow(chat_id, product_id, message_id)
    elif data.startswith("mark_sold_"):
        product_id = int(data.split("_")[2])
        mark_product_sold(chat_id, product_id, message_id)
    elif data.startswith("republish_"):
        product_id = int(data.split("_")[1])
        republish_product(chat_id, product_id, message_id)
    elif data.startswith("seller_contact_"): # Зворотний зв'язок з продавцем
        product_id = int(data.split("_")[2])
        contact_seller(call.from_user.id, product_id, call.message.chat.id)
    elif data.startswith("next_product_"): # Навігація по товарах
        offset = int(data.split("_")[2])
        send_my_products(call.message, offset=offset)
    elif data.startswith("prev_product_"):
        offset = int(data.split("_")[2])
        send_my_products(call.message, offset=offset)

    # --- Обробка Обраних товарів ---
    elif data.startswith("toggle_favorite_"):
        product_id = int(data.split("_")[2])
        toggle_favorite_product(chat_id, product_id, message_id, is_from_channel=False)
    elif data.startswith("channel_fav_"): # Лайк з каналу
        product_id = int(data.split("_")[2])
        # Отримуємо оригінальне ID повідомлення в каналі
        original_channel_message_id = call.message.message_id 
        toggle_favorite_product(chat_id, product_id, original_channel_message_id, is_from_channel=True)
    elif data.startswith("view_fav_product_"):
        product_id = int(data.split("_")[3])
        send_product_details_to_user(chat_id, product_id, message_id, is_favorite_view=True) # Додано is_favorite_view
    elif data.startswith("next_fav_product_"):
        offset = int(data.split("_")[3])
        send_favorites(call.message, offset=offset)
    elif data.startswith("prev_fav_product_"):
        offset = int(data.split("_")[3])
        send_favorites(call.message, offset=offset)

    # --- Обробка вибору доставки ---
    elif data.startswith("shipping_"):
        if data == "shipping_next":
            go_to_next_step(chat_id)
        else:
            option = data.replace("shipping_", "")
            current_options = user_data[chat_id]['data'].get('shipping_options', [])
            if option in current_options:
                current_options.remove(option)
            else:
                current_options.append(option)
            user_data[chat_id]['data']['shipping_options'] = current_options
            
            # Оновлюємо інлайн-клавіатуру, щоб показати вибрані опції
            inline_markup = types.InlineKeyboardMarkup(row_width=2)
            shipping_options_list = ["Наложка Нова Пошта", "Наложка Укрпошта", "Особиста зустріч"]
            buttons = []
            for opt in shipping_options_list:
                emoji = '✅ ' if opt in current_options else ''
                buttons.append(types.InlineKeyboardButton(f"{emoji}{opt}", callback_data=f"shipping_{opt}"))
            
            inline_markup.add(*buttons)
            inline_markup.add(types.InlineKeyboardButton("Далі ➡️", callback_data="shipping_next"))
            
            bot.edit_message_reply_markup(chat_id, message_id, reply_markup=inline_markup)
            
    bot.answer_callback_query(call.id) # Важливо: завжди відповідати на callback_query


# --- 15. Функції для "Мої товари" ---
PRODUCT_PAGE_SIZE = 5 # Кількість товарів на сторінці

@error_handler
def send_my_products(message, offset=0):
    """
    Надсилає користувачеві список його товарів з пагінацією.
    """
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        # Отримуємо загальну кількість товарів користувача
        cur.execute(pg_sql.SQL("SELECT COUNT(*) FROM products WHERE seller_chat_id = %s;"), (chat_id,))
        total_products = cur.fetchone()[0]

        if total_products == 0:
            bot.send_message(chat_id, "У вас ще немає доданих товарів. 😔", reply_markup=main_menu_markup)
            return

        # Отримуємо товари для поточної сторінки
        cur.execute(pg_sql.SQL("""
            SELECT id, product_name, price, status, views, likes_count, created_at, last_republish_date
            FROM products
            WHERE seller_chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """), (chat_id, PRODUCT_PAGE_SIZE, offset))
        products = cur.fetchall()

        products_text = "📋 *Ваші товари:*\n\n"
        for prod in products:
            status_emoji = {
                'pending': '⏳', 'approved': '✅', 'rejected': '❌', 'sold': '💰', 'expired': '🗑️'
            }.get(prod['status'], '❓')
            
            republish_info = ""
            if prod['status'] == 'approved':
                republish_info = f" | Опубліковано: {prod['republish_count']} разів."
                if prod['last_republish_date']:
                    time_since_republish = (date.today() - prod['last_republish_date']).days
                    republish_info += f" (останнє {time_since_republish} дн. тому)"

            products_text += (
                f"{status_emoji} *{prod['product_name']}* (ID: `{prod['id']}`)\n"
                f"   Ціна: `{prod['price']}`\n"
                f"   Статус: {prod['status'].capitalize()}\n"
                f"   Перегляди: {prod['views']} | ❤️: {prod['likes_count']}{republish_info}\n\n"
            )
            
            # Додаємо кнопки дій для кожного товару
            product_markup = types.InlineKeyboardMarkup(row_width=2)
            product_markup.add(
                types.InlineKeyboardButton("👁️ Деталі", callback_data=f"view_my_product_{prod['id']}"),
                types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{prod['id']}")
            )
            if prod['status'] == 'approved':
                product_markup.add(
                    types.InlineKeyboardButton("♻️ Переопублікувати", callback_data=f"republish_{prod['id']}"),
                    types.InlineKeyboardButton("✅ Продано", callback_data=f"mark_sold_{prod['id']}")
                )
            product_markup.add(types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_product_{prod['id']}"))
            
            bot.send_message(chat_id, products_text, parse_mode='Markdown', reply_markup=product_markup)
            products_text = "" # Очищуємо текст для наступного товару, щоб кожен мав свою клавіатуру

        # Кнопки пагінації
        pagination_markup = types.InlineKeyboardMarkup(row_width=2)
        if offset > 0:
            pagination_markup.add(types.InlineKeyboardButton("⬅️ Попередні", callback_data=f"prev_product_{max(0, offset - PRODUCT_PAGE_SIZE)}"))
        if offset + PRODUCT_PAGE_SIZE < total_products:
            pagination_markup.add(types.InlineKeyboardButton("Наступні ➡️", callback_data=f"next_product_{offset + PRODUCT_PAGE_SIZE}"))
        
        if pagination_markup.keyboard: # Надсилаємо, тільки якщо є кнопки пагінації
            bot.send_message(chat_id, f"Сторінка {offset // PRODUCT_PAGE_SIZE + 1} з {(total_products + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE}", reply_markup=pagination_markup)

        log_statistics('view_my_products', chat_id, details=f"offset: {offset}")

    except Exception as e:
        logger.error(f"Помилка при відправці моїх товарів для {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні ваших товарів.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_product_details_to_seller(chat_id, product_id, message_id_to_edit=None):
    """
    Надсилає продавцю деталі його конкретного товару.
    """
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT id, seller_chat_id, seller_username, product_name, price, description, photos, geolocation, status,
                   commission_amount, views, likes_count, created_at, updated_at, shipping_options, hashtags, channel_message_id, last_republish_date, republish_count
            FROM products WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, chat_id))
        product = cur.fetchone()

        if not product:
            bot.send_message(chat_id, "Товар не знайдено або він не належить вам.")
            return

        photos = json.loads(product['photos']) if product['photos'] else []
        geolocation = json.loads(product['geolocation']) if product['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"
        hashtags = product['hashtags'] if product['hashtags'] else "Немає"

        details_text = (
            f"📦 *Деталі вашого товару (ID: {product['id']})*\n\n"
            f"📝 *Назва*: {product['product_name']}\n"
            f"💰 *Ціна*: {product['price']}\n"
            f"📄 *Опис*: {product['description']}\n"
            f"📸 *Фото*: {len(photos)} шт.\n"
            f"📍 *Геолокація*: {'Так' if geolocation else 'Ні'}\n"
            f"🚚 *Доставка*: {shipping_options_text}\n"
            f"🏷️ *Хештеги*: {hashtags}\n"
            f"📊 *Статус*: {product['status'].capitalize()}\n"
            f"👁️ *Перегляди*: {product['views']}\n"
            f"❤️ *Лайки*: {product['likes_count']}\n"
            f"📆 *Створено*: {product['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
            f"🔄 *Оновлено*: {product['updated_at'].strftime('%Y-%m-%d %H:%M')}\n"
            f"Публікацій: {product['republish_count']}"
        )
        if product['last_republish_date']:
            details_text += f" (остання {product['last_republish_date'].strftime('%Y-%m-%d')})"
        
        details_text += f"\nКомісія до сплати: {product['commission_amount']}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✏️ Змінити ціну", callback_data=f"change_price_{product['id']}"),
            types.InlineKeyboardButton("🗑️ Видалити", callback_data=f"delete_product_{product['id']}")
        )
        if product['status'] == 'approved':
             markup.add(types.InlineKeyboardButton("✅ Продано", callback_data=f"mark_sold_{product['id']}"))
             # Додаємо кнопку переопублікації, якщо пройшло більше 7 днів
             if not product['last_republish_date'] or \
                (date.today() - product['last_republish_date']).days >= 7:
                 markup.add(types.InlineKeyboardButton("♻️ Переопублікувати", callback_data=f"republish_{product['id']}"))
             else:
                 markup.add(types.InlineKeyboardButton(f"Переопубл. через {7 - (date.today() - product['last_republish_date']).days} дн.", callback_data="no_republish"))

        # Додаємо кнопку "Назад до моїх товарів"
        markup.add(types.InlineKeyboardButton("🔙 Мої товари", callback_data="my_products_back"))

        if photos:
            media = [types.InputMediaPhoto(photo_id, caption=details_text if i == 0 else None, parse_mode='Markdown') for i, photo_id in enumerate(photos)]
            
            if message_id_to_edit:
                # Якщо це редагування і фото вже були, Telebot не дозволяє редагувати медіагрупу,
                # тому просто надсилаємо нове повідомлення.
                bot.send_media_group(chat_id, media)
                bot.send_message(chat_id, "👆 Деталі товару (фото вище)", reply_markup=markup, parse_mode='Markdown')
            else:
                bot.send_media_group(chat_id, media)
                bot.send_message(chat_id, "👆 Деталі товару (фото вище)", reply_markup=markup, parse_mode='Markdown')
        else:
            if message_id_to_edit:
                bot.edit_message_text(details_text, chat_id, message_id_to_edit, reply_markup=markup, parse_mode='Markdown')
            else:
                bot.send_message(chat_id, details_text, reply_markup=markup, parse_mode='Markdown')
        
        log_statistics('view_product_details', chat_id, product_id)

    except Exception as e:
        logger.error(f"Помилка при відправці деталей товару {product_id} продавцю {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні деталей товару.")
    finally:
        if conn:
            conn.close()

@error_handler
def start_change_price_flow(chat_id, product_id, message_id_to_edit):
    """Починає потік зміни ціни для товару."""
    user_data[chat_id] = {
        'flow': 'change_price',
        'product_id': product_id,
        'message_id_to_edit': message_id_to_edit # Зберігаємо ID повідомлення для редагування
    }
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(cancel_button)
    bot.send_message(chat_id, f"Введіть нову ціну для товару ID `{product_id}` (наприклад, `600 грн` або `Торг`):", reply_markup=markup, parse_mode='Markdown')

@error_handler
def process_new_price(message):
    """Обробляє нову ціну, введену користувачем."""
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get('flow') != 'change_price':
        return

    product_id = user_data[chat_id]['product_id']
    message_id_to_edit = user_data[chat_id]['message_id_to_edit']
    new_price = message.text

    if new_price == cancel_button.text:
        bot.send_message(chat_id, "Зміна ціни скасована.", reply_markup=main_menu_markup)
        del user_data[chat_id]
        return

    if not new_price or len(new_price) > 50:
        bot.send_message(chat_id, "Будь ласка, введіть коректну ціну (до 50 символів). Спробуйте ще раз:")
        return

    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних. Спробуйте пізніше.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("UPDATE products SET price = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND seller_chat_id = %s;"),
                       (new_price, product_id, chat_id))
        conn.commit()
        bot.send_message(chat_id, f"✅ Ціну для товару ID `{product_id}` оновлено на `{new_price}`.", reply_markup=main_menu_markup, parse_mode='Markdown')
        del user_data[chat_id] # Очищуємо стан після завершення
        send_product_details_to_seller(chat_id, product_id, message_id_to_edit) # Оновлюємо відображення деталей
        log_statistics('change_price', chat_id, product_id, details=f"new_price: {new_price}")
    except Exception as e:
        logger.error(f"Помилка оновлення ціни для товару {product_id} користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
        bot.send_message(chat_id, "Сталася помилка при оновленні ціни.")
    finally:
        if conn:
            conn.close()

@error_handler
def delete_product(chat_id, product_id, message_id_to_edit):
    """Видаляє товар з бази даних."""
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        # Отримуємо channel_message_id, щоб видалити його з каналу
        cur.execute(pg_sql.SQL("SELECT channel_message_id FROM products WHERE id = %s AND seller_chat_id = %s;"),
                       (product_id, chat_id))
        product_info = cur.fetchone()
        channel_message_id = product_info['channel_message_id'] if product_info else None

        cur.execute(pg_sql.SQL("DELETE FROM products WHERE id = %s AND seller_chat_id = %s;"), (product_id, chat_id))
        conn.commit()

        # Видаляємо повідомлення з каналу, якщо воно було опубліковано
        if channel_message_id:
            try:
                bot.delete_message(CHANNEL_ID, channel_message_id)
                logger.info(f"Повідомлення {channel_message_id} видалено з каналу {CHANNEL_ID}.")
            except Exception as e:
                logger.warning(f"Не вдалося видалити повідомлення {channel_message_id} з каналу: {e}")

        bot.edit_message_text(f"🗑️ Товар ID `{product_id}` успішно видалено.", chat_id, message_id_to_edit, parse_mode='Markdown')
        log_statistics('delete_product', chat_id, product_id)
    except Exception as e:
        logger.error(f"Помилка видалення товару {product_id} користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
        bot.edit_message_text(f"Сталася помилка при видаленні товару ID `{product_id}`.", chat_id, message_id_to_edit, parse_mode='Markdown')
    finally:
        if conn:
            conn.close()

@error_handler
def mark_product_sold(chat_id, product_id, message_id_to_edit):
    """Позначає товар як проданий."""
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        # Оновлюємо статус товару
        cur.execute(pg_sql.SQL("""
            UPDATE products SET status = 'sold', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND seller_chat_id = %s RETURNING channel_message_id;
        """), (product_id, chat_id))
        
        product_info = cur.fetchone()
        channel_message_id = product_info['channel_message_id'] if product_info else None
        
        conn.commit()

        # Редагуємо повідомлення в каналі, додаючи мітку "ПРОДАНО"
        if channel_message_id:
            try:
                product_data = get_product_by_id(product_id)
                if product_data:
                    message_text, media = format_product_message(product_data, add_sold_tag=True)
                    if media:
                        # Для медіагрупи не можна редагувати фото, лише текст.
                        # Можливо, краще видалити і переслати, або просто додати тег в адмін-повідомлення.
                        # Для простоти, поки що просто оновимо статус в БД.
                        # Це складно реалізувати без видалення і повторної публікації медіагрупи.
                        # Просто позначимо в тексті, якщо це можливо, або залишаємо як є.
                        # Якщо це просто фото з одним медіа, можна спробувати.
                        if len(media) == 1:
                            bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=channel_message_id, 
                                                     caption=message_text, parse_mode='Markdown')
                        else:
                            # Для медіагруп просто додамо текст "ПРОДАНО" окремим повідомленням
                            bot.send_message(CHANNEL_ID, f"❕ Товар ID `{product_id}` продано! 💰", 
                                             reply_to_message_id=channel_message_id, parse_mode='Markdown')
                    else:
                        bot.edit_message_text(message_text, CHANNEL_ID, channel_message_id, parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Не вдалося оновити повідомлення в каналі для товару {product_id}: {e}")

        bot.edit_message_text(f"✅ Товар ID `{product_id}` позначено як *Проданий*.", chat_id, message_id_to_edit, parse_mode='Markdown')
        log_statistics('mark_sold', chat_id, product_id)
    except Exception as e:
        logger.error(f"Помилка позначення товару {product_id} як проданого для користувача {chat_id}: {e}", exc_info=True)
        conn.rollback()
        bot.edit_message_text(f"Сталася помилка при позначенні товару ID `{product_id}` як проданого.", chat_id, message_id_to_edit, parse_mode='Markdown')
    finally:
        if conn:
            conn.close()

@error_handler
def republish_product(chat_id, product_id, message_id_to_edit):
    """Переопубліковує товар в канал."""
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT product_name, price, description, photos, geolocation, shipping_options, hashtags, status, last_republish_date, republish_count
            FROM products WHERE id = %s AND seller_chat_id = %s;
        """), (product_id, chat_id))
        product_data = cur.fetchone()

        if not product_data:
            bot.send_message(chat_id, "Товар не знайдено або він не належить вам.")
            return
        
        if product_data['status'] != 'approved':
            bot.send_message(chat_id, "Можна переопубліковувати лише схвалені товари.")
            return

        # Перевірка на обмеження переопублікації (раз на 7 днів)
        if product_data['last_republish_date']:
            days_since_last_republish = (date.today() - product_data['last_republish_date']).days
            if days_since_last_republish < 7:
                bot.send_message(chat_id, 
                                 f"♻️ Ви можете переопублікувати цей товар через {7 - days_since_last_republish} дн. "
                                 f"(Останній раз опубліковано: {product_data['last_republish_date'].strftime('%Y-%m-%d')}).")
                return

        # Форматуємо повідомлення для каналу
        message_text, media = format_product_message(product_data, product_id, seller_chat_id=chat_id)

        try:
            sent_message = None
            if media:
                # Telegram API дозволяє відправляти медіагрупи (до 10 елементів).
                # Перший елемент може мати підпис, решта - ні.
                caption_media = types.InputMediaPhoto(media[0].media, caption=message_text, parse_mode='Markdown')
                other_media = [types.InputMediaPhoto(m.media) for m in media[1:]]
                sent_messages = bot.send_media_group(CHANNEL_ID, [caption_media] + other_media)
                if sent_messages:
                    sent_message = sent_messages[0]
            else:
                sent_message = bot.send_message(CHANNEL_ID, message_text, parse_mode='Markdown')

            if sent_message:
                # Оновлюємо канал_меседж_ід, лічильник переопублікацій та дату останньої переопублікації
                cur.execute(pg_sql.SQL("""
                    UPDATE products SET 
                        channel_message_id = %s, 
                        republish_count = republish_count + 1, 
                        last_republish_date = CURRENT_DATE, 
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s;
                """), (sent_message.message_id, product_id))
                conn.commit()
                bot.edit_message_text(f"✅ Товар ID `{product_id}` успішно переопубліковано в канал!", chat_id, message_id_to_edit, parse_mode='Markdown')
                log_statistics('republish_product', chat_id, product_id)
            else:
                bot.send_message(chat_id, "Помилка переопублікації товару в канал.")
        except Exception as e:
            logger.error(f"Помилка відправки товару {product_id} в канал: {e}", exc_info=True)
            bot.send_message(chat_id, "Виникла помилка при переопублікації товару. Можливо, деякі фотографії більше недоступні.")
            conn.rollback() # Відкат змін у БД, якщо відправка в канал не вдалася
    except Exception as e:
        logger.error(f"Помилка при отриманні даних для переопублікації товару {product_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при переопублікації товару.")
    finally:
        if conn:
            conn.close()

# --- 16. Функції для "Обраних" товарів ---
@error_handler
def toggle_favorite_product(user_chat_id, product_id, message_id, is_from_channel):
    """
    Додає/видаляє товар з обраного користувача та оновлює лічильник лайків в каналі.
    """
    conn = get_db_connection()
    if not conn:
        bot.answer_callback_query(message_id, "Помилка БД.")
        return

    try:
        cur = conn.cursor()
        # Перевіряємо, чи товар вже в обраних
        cur.execute(pg_sql.SQL("SELECT id FROM favorites WHERE user_chat_id = %s AND product_id = %s;"),
                       (user_chat_id, product_id))
        is_favorite = cur.fetchone()

        if is_favorite:
            # Видаляємо з обраних
            cur.execute(pg_sql.SQL("DELETE FROM favorites WHERE user_chat_id = %s AND product_id = %s;"),
                           (user_chat_id, product_id))
            action_text = "💔 Видалено з обраного"
            # Зменшуємо лічильник лайків
            cur.execute(pg_sql.SQL("UPDATE products SET likes_count = GREATEST(0, likes_count - 1) WHERE id = %s RETURNING likes_count;"), (product_id,))
        else:
            # Додаємо в обрані
            cur.execute(pg_sql.SQL("INSERT INTO favorites (user_chat_id, product_id) VALUES (%s, %s);"),
                           (user_chat_id, product_id))
            action_text = "❤️ Додано в обране"
            # Збільшуємо лічильник лайків
            cur.execute(pg_sql.SQL("UPDATE products SET likes_count = likes_count + 1 WHERE id = %s RETURNING likes_count;"), (product_id,))
        
        new_likes_count = cur.fetchone()['likes_count']
        conn.commit()

        bot.answer_callback_query(message_id, action_text)
        log_statistics('toggle_favorite', user_chat_id, product_id, details=action_text)

        # Якщо дія прийшла з каналу, оновлюємо повідомлення в каналі
        if is_from_channel:
            product_data = get_product_by_id(product_id)
            if product_data and product_data['channel_message_id']:
                channel_message_id = product_data['channel_message_id']
                try:
                    # Редагуємо текст повідомлення, щоб оновити лічильник лайків
                    # або додаємо реакцію
                    
                    # Оновлюємо клавіатуру, щоб відобразити новий лічильник
                    seller_chat_id = product_data['seller_chat_id']
                    seller_username = get_username_by_chat_id(seller_chat_id)
                    markup = types.InlineKeyboardMarkup(row_width=1)
                    
                    # Кнопка "Написати продавцю"
                    seller_link = f"tg://user?id={seller_chat_id}"
                    contact_button_text = f"✉️ Написати продавцю"
                    markup.add(types.InlineKeyboardButton(contact_button_text, url=seller_link))
                    
                    # Кнопка "Додати/Видалити з обраного" з лічильником
                    fav_emoji = "❤️" if is_favorite else "🤍"
                    markup.add(types.InlineKeyboardButton(f"{fav_emoji} Обране ({new_likes_count})", callback_data=f"channel_fav_{product_id}"))

                    # Для публікацій з фото, треба редагувати caption, а не text
                    # Перевіряємо, чи повідомлення було з фото
                    if product_data['photos']:
                        # Тут потрібно отримати поточний caption, змінити його і відправити назад
                        # Це складніше, оскільки Telebot не дозволяє просто отримати caption з об'єкта Message_id
                        # Простіше просто оновити кнопки з новим лічильником.
                        bot.edit_message_reply_markup(CHANNEL_ID, channel_message_id, reply_markup=markup)
                    else:
                        # Якщо це текстове оголошення, можна редагувати текст
                        # Залишаємо лише оновлення клавіатури, оскільки оновлення тексту може бути складним
                        # без повторного форматування всього оголошення.
                        bot.edit_message_reply_markup(CHANNEL_ID, channel_message_id, reply_markup=markup)

                except Exception as e:
                    logger.warning(f"Не вдалося оновити повідомлення в каналі {channel_message_id} для товару {product_id}: {e}")

    except Exception as e:
        logger.error(f"Помилка перемикання обраного для користувача {user_chat_id}, товару {product_id}: {e}", exc_info=True)
        conn.rollback()
        bot.answer_callback_query(message_id, "Сталася помилка при оновленні обраного.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_favorites(message, offset=0):
    """
    Надсилає користувачеві список його обраних товарів з пагінацією.
    """
    chat_id = message.chat.id
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до бази даних.")
        return
    try:
        cur = conn.cursor()
        # Отримуємо загальну кількість обраних товарів користувача
        cur.execute(pg_sql.SQL("SELECT COUNT(f.product_id) FROM favorites f JOIN products p ON f.product_id = p.id WHERE f.user_chat_id = %s AND p.status = 'approved';"), (chat_id,))
        total_favorites = cur.fetchone()[0]

        if total_favorites == 0:
            bot.send_message(chat_id, "У вас поки що немає обраних товарів. Додайте щось, щоб тут було цікаво! ❤️", reply_markup=main_menu_markup)
            return

        # Отримуємо обрані товари для поточної сторінки
        cur.execute(pg_sql.SQL("""
            SELECT p.id, p.product_name, p.price, p.seller_chat_id, p.seller_username, p.photos, p.description, p.likes_count
            FROM favorites f
            JOIN products p ON f.product_id = p.id
            WHERE f.user_chat_id = %s AND p.status = 'approved'
            ORDER BY f.id DESC -- За порядком додавання в обране
            LIMIT %s OFFSET %s;
        """), (chat_id, PRODUCT_PAGE_SIZE, offset))
        favorite_products = cur.fetchall()

        fav_text = "⭐ *Ваші обрані товари:*\n\n"
        for prod in favorite_products:
            photos = json.loads(prod['photos']) if prod['photos'] else []
            seller_username = prod['seller_username'] if prod['seller_username'] else "Не вказано"

            fav_text += (
                f"✨ *{prod['product_name']}* (ID: `{prod['id']}`)\n"
                f"   Ціна: `{prod['price']}`\n"
                f"   Продавець: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={prod['seller_chat_id']})\n"
                f"   ❤️: {prod['likes_count']} | 📸: {len(photos)} шт.\n\n"
            )
            
            # Додаємо кнопки дій для кожного обраного товару
            product_markup = types.InlineKeyboardMarkup(row_width=2)
            product_markup.add(
                types.InlineKeyboardButton("👁️ Деталі", callback_data=f"view_fav_product_{prod['id']}"),
                types.InlineKeyboardButton("💔 Видалити з обраного", callback_data=f"toggle_favorite_{prod['id']}")
            )
            bot.send_message(chat_id, fav_text, parse_mode='Markdown', reply_markup=product_markup)
            fav_text = "" # Очищуємо текст для наступного товару

        # Кнопки пагінації
        pagination_markup = types.InlineKeyboardMarkup(row_width=2)
        if offset > 0:
            pagination_markup.add(types.InlineKeyboardButton("⬅️ Попередні", callback_data=f"prev_fav_product_{max(0, offset - PRODUCT_PAGE_SIZE)}"))
        if offset + PRODUCT_PAGE_SIZE < total_favorites:
            pagination_markup.add(types.InlineKeyboardButton("Наступні ➡️", callback_data=f"next_fav_product_{offset + PRODUCT_PAGE_SIZE}"))
        
        if pagination_markup.keyboard:
            bot.send_message(chat_id, f"Сторінка {offset // PRODUCT_PAGE_SIZE + 1} з {(total_favorites + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE}", reply_markup=pagination_markup)

        log_statistics('view_favorites', chat_id, details=f"offset: {offset}")

    except Exception as e:
        logger.error(f"Помилка при відправці обраних товарів для {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні обраних товарів.")
    finally:
        if conn:
            conn.close()

@error_handler
def send_product_details_to_user(chat_id, product_id, message_id_to_edit=None, is_favorite_view=False):
    """
    Надсилає користувачеві деталі конкретного товару (для обраних або прямого перегляду).
    """
    conn = get_db_connection()
    if not conn:
        bot.send_message(chat_id, "Помилка підключення до БД.")
        return
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT id, seller_chat_id, seller_username, product_name, price, description, photos, geolocation, status,
                   views, likes_count, created_at, updated_at, shipping_options, hashtags
            FROM products WHERE id = %s AND status = 'approved';
        """), (product_id,))
        product = cur.fetchone()

        if not product:
            bot.send_message(chat_id, "Товар не знайдено або він вже не доступний. 😟")
            return

        photos = json.loads(product['photos']) if product['photos'] else []
        geolocation = json.loads(product['geolocation']) if product['geolocation'] else None
        shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"
        hashtags = product['hashtags'] if product['hashtags'] else "Немає"
        seller_username = product['seller_username'] if product['seller_username'] else "Користувач"

        details_text = (
            f"📦 *Деталі товару (ID: {product['id']})*\n\n"
            f"📝 *Назва*: {product['product_name']}\n"
            f"💰 *Ціна*: {product['price']}\n"
            f"📄 *Опис*: {product['description']}\n"
            f"📸 *Фото*: {len(photos)} шт.\n"
            f"📍 *Геолокація*: {'Так' if geolocation else 'Ні'}\n"
            f"🚚 *Доставка*: {shipping_options_text}\n"
            f"🏷️ *Хештеги*: {hashtags}\n"
            f"👁️ *Перегляди*: {product['views']}\n"
            f"❤️ *Лайки*: {product['likes_count']}\n"
            f"👤 *Продавець*: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={product['seller_chat_id']})"
        )

        markup = types.InlineKeyboardMarkup(row_width=1)
        # Кнопка "Написати продавцю"
        seller_link = f"tg://user?id={product['seller_chat_id']}"
        markup.add(types.InlineKeyboardButton("✉️ Написати продавцю", url=seller_link))

        # Кнопка "Додати/Видалити з обраного"
        cur.execute(pg_sql.SQL("SELECT id FROM favorites WHERE user_chat_id = %s AND product_id = %s;"),
                       (chat_id, product_id))
        is_user_favorite = cur.fetchone()
        fav_button_text = "💔 Видалити з обраного" if is_user_favorite else "❤️ Додати в обране"
        markup.add(types.InlineKeyboardButton(fav_button_text, callback_data=f"toggle_favorite_{product['id']}"))
        
        # Додаємо кнопку "Назад до обраних" для перегляду з "Обраних"
        if is_favorite_view:
            markup.add(types.InlineKeyboardButton("🔙 До обраних", callback_data="my_favorites_back"))

        if photos:
            media = [types.InputMediaPhoto(photo_id, caption=details_text if i == 0 else None, parse_mode='Markdown') for i, photo_id in enumerate(photos)]
            
            if message_id_to_edit:
                bot.send_media_group(chat_id, media)
                bot.send_message(chat_id, "👆 Деталі товару (фото вище)", reply_markup=markup, parse_mode='Markdown')
            else:
                bot.send_media_group(chat_id, media)
                bot.send_message(chat_id, "👆 Деталі товару (фото вище)", reply_markup=markup, parse_mode='Markdown')
        else:
            if message_id_to_edit:
                bot.edit_message_text(details_text, chat_id, message_id_to_edit, reply_markup=markup, parse_mode='Markdown')
            else:
                bot.send_message(chat_id, details_text, reply_markup=markup, parse_mode='Markdown')

        # Збільшуємо лічильник переглядів
        cur.execute(pg_sql.SQL("UPDATE products SET views = views + 1 WHERE id = %s;"), (product_id,))
        conn.commit()
        log_statistics('view_product_details_user', chat_id, product_id)

    except Exception as e:
        logger.error(f"Помилка при відправці деталей товару {product_id} користувачу {chat_id}: {e}", exc_info=True)
        bot.send_message(chat_id, "Сталася помилка при завантаженні деталей товару.")
    finally:
        if conn:
            conn.close()

# --- 17. Допоміжні функції ---
@error_handler
def send_help_message(message):
    """Надсилає користувачеві довідкове повідомлення."""
    help_text = (
        "❓ *Допомога та FAQ*\n\n"
        "Я - SellerBot, ваш розумний помічник у світі продажів та покупок! "
        "Ось що я вмію:\n\n"
        "📦 *Додати товар*: Покроково допоможу вам створити нове оголошення.\n"
        "📋 *Мої товари*: Перегляд, редагування, позначення проданих та переопублікація ваших оголошень.\n"
        "⭐ *Обрані*: Зберігайте товари, які вам сподобались, для швидкого доступу.\n"
        "📺 *Наш канал*: Посилання на наш основний канал з оголошеннями.\n"
        "🤖 *AI Помічник*: Поспілкуйтесь зі мною, я відповім на ваші питання щодо функціоналу бота, "
        "допоможу сформулювати опис товару, або просто поговорю про новітні технології! "
        "(Я відповідаю в стилі Ілона Маска 😉).\n\n"
        "*Як продати товар?*\n"
        "1. Натисніть '📦 Додати товар' та слідуйте інструкціям.\n"
        "2. Після модерації ваш товар буде опубліковано в каналі.\n"
        "3. З вами зв'яжуться потенційні покупці.\n"
        "4. Після продажу позначте товар як 'Проданий' у розділі 'Мої товари'.\n\n"
        "*Як купити товар?*\n"
        "1. Перейдіть до нашого [основного каналу](https://t.me/your_channel_link) (кнопка '📺 Наш канал').\n"
        "2. Знайдіть оголошення, що вас цікавить.\n"
        "3. Натисніть 'Написати продавцю', щоб зв'язатися з ним напряму.\n\n"
        "*Є питання?*\n"
        "Просто напишіть мені або скористайтесь '🤖 AI Помічником'!"
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown', reply_markup=main_menu_markup)
    log_statistics('help_message', message.chat.id)

@error_handler
def send_channel_link(message):
    """Надсилає посилання на Telegram канал."""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Перейти до каналу", url="https://t.me/your_channel_link")) # Замініть на реальне посилання
    bot.send_message(message.chat.id, "📺 *Наш канал з оголошеннями:*\nТут публікуються всі схвалені товари!", reply_markup=markup, parse_mode='Markdown')
    log_statistics('channel_link', message.chat.id)

def format_product_message(product, product_id=None, seller_chat_id=None, add_sold_tag=False):
    """
    Форматує повідомлення про товар для публікації в канал або для адмін-рев'ю.
    Включає фото, деталі, кнопки зв'язку та обраного.
    """
    if product_id is None:
        product_id = product['id']
    if seller_chat_id is None:
        seller_chat_id = product['seller_chat_id']

    photos = json.loads(product['photos']) if product['photos'] else []
    geolocation = json.loads(product['geolocation']) if product['geolocation'] else None
    shipping_options_text = ", ".join(json.loads(product['shipping_options'])) if product['shipping_options'] else "Не вказано"
    hashtags = product['hashtags'] if product['hashtags'] else ""
    seller_username = product['seller_username'] if product['seller_username'] else "Не вказано"
    
    sold_tag = ""
    if add_sold_tag:
        sold_tag = "❌ *ПРОДАНО* ❌\n\n"

    message_text = (
        f"{sold_tag}✨ *{product['product_name']}*\n\n"
        f"💰 *Ціна*: {product['price']}\n"
        f"📄 *Опис*: {product['description']}\n"
        f"📍 *Геолокація*: {'Так' if geolocation else 'Ні'}\n"
        f"🚚 *Доставка*: {shipping_options_text}\n"
        f"👤 *Продавець*: [{'@' + seller_username if seller_username != 'Не вказано' else 'Користувач'}](tg://user?id={seller_chat_id})\n"
        f"🏷️ {hashtags}\n\n"
        f"ID: `{product_id}`"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    # Кнопка "Написати продавцю"
    seller_link = f"tg://user?id={seller_chat_id}"
    contact_button_text = f"✉️ Написати продавцю"
    markup.add(types.InlineKeyboardButton(contact_button_text, url=seller_link))
    
    # Кнопка "Додати в обране" з лічильником
    fav_emoji = "🤍" # Завжди починаємо з білого серця для каналу
    markup.add(types.InlineKeyboardButton(f"{fav_emoji} Обране ({product['likes_count']})", callback_data=f"channel_fav_{product_id}"))

    media = []
    if photos:
        for photo_id in photos:
            media.append(types.InputMediaPhoto(photo_id))
    
    return message_text, media, markup

def get_product_by_id(product_id):
    """Отримує дані товару за ID з БД."""
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("""
            SELECT id, seller_chat_id, seller_username, product_name, price, description, photos, geolocation, 
                   status, commission_amount, views, likes_count, created_at, updated_at, shipping_options, 
                   hashtags, channel_message_id, last_republish_date, republish_count
            FROM products WHERE id = %s;
        """), (product_id,))
        return cur.fetchone()
    except Exception as e:
        logger.error(f"Помилка отримання товару за ID {product_id}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

def get_username_by_chat_id(chat_id):
    """Отримує ім'я користувача за chat_id."""
    conn = get_db_connection()
    if not conn: return "Невідомий користувач"
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("SELECT username FROM users WHERE chat_id = %s;"), (chat_id,))
        result = cur.fetchone()
        return result['username'] if result and result['username'] else "Користувач"
    except Exception as e:
        logger.error(f"Помилка отримання username для {chat_id}: {e}", exc_info=True)
        return "Невідомий користувач"
    finally:
        if conn:
            conn.close()

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

