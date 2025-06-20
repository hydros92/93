import os
import telebot
from telebot import types
import logging
from datetime import datetime, timezone
import json
from dotenv import load_dotenv

from flask import Flask, request

import psycopg2
from psycopg2 import sql as pg_sql
from psycopg2 import extras

# Load environment variables
load_dotenv()

# Bot Configuration and Environment Variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
CHANNEL_ID = int(os.getenv('CHANNEL_ID', '0'))
MONOBANK_CARD_NUMBER = os.getenv('MONOBANK_CARD_NUMBER', 'Не вказано')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = os.getenv('GEMINI_API_URL', "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent")
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
DATABASE_URL = os.getenv('DATABASE_URL')

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# --- Database Functions ---
def get_db_connection():
    """Establishes and returns a database connection."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.critical(f"Помилка підключення до бази даних: {e}", exc_info=True)
        exit(1)

def init_db():
    """Initializes database schema if tables do not exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            full_name VARCHAR(255),
            phone_number VARCHAR(20),
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_admin BOOLEAN DEFAULT FALSE,
            balance DECIMAL(10, 2) DEFAULT 0.00
        );

        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            price DECIMAL(10, 2) NOT NULL,
            description TEXT,
            photo_id VARCHAR(255),
            post_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'pending', -- pending, active, sold, rejected, moderation
            moderation_notes TEXT,
            channel_message_id BIGINT,
            last_republish_date TIMESTAMP,
            tags TEXT,
            city VARCHAR(255),
            delivery_options TEXT,
            views INT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS support_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            request_text TEXT NOT NULL,
            request_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id),
            amount DECIMAL(10, 2) NOT NULL,
            transaction_type VARCHAR(50) NOT NULL, -- deposit, withdrawal, product_payment
            transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            product_id INT REFERENCES products(id) ON DELETE SET NULL,
            status VARCHAR(50) DEFAULT 'completed'
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            reviewer_id BIGINT REFERENCES users(id),
            product_id INT REFERENCES products(id) ON DELETE CASCADE,
            rating INT CHECK (rating >= 1 AND rating <= 5),
            comment TEXT,
            review_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            product_id INT REFERENCES products(id) ON DELETE CASCADE,
            seller_id BIGINT REFERENCES users(id),
            buyer_id BIGINT REFERENCES users(id),
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_message_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            conversation_id INT REFERENCES conversations(id) ON DELETE CASCADE,
            sender_id BIGINT REFERENCES users(id),
            message_text TEXT NOT NULL,
            message_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS favorites (
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            product_id INT REFERENCES products(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS commission_transactions (
            id SERIAL PRIMARY KEY,
            product_id INT REFERENCES products(id) ON DELETE CASCADE,
            seller_id BIGINT REFERENCES users(id),
            buyer_id BIGINT REFERENCES users(id),
            amount DECIMAL(10, 2) NOT NULL,
            transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'pending' -- pending, completed, refunded
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

def db_execute(query, params=(), fetch_one=False, fetch_all=False):
    """Executes a SQL query with given parameters."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        cur.execute(query, params)
        conn.commit()
        if fetch_one:
            return cur.fetchone()
        if fetch_all:
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error: {e}, Query: {query}, Params: {params}", exc_info=True)
        conn.rollback()
    finally:
        cur.close()
        conn.close()
    return None

def db_insert(table, data):
    """Inserts data into a specified table."""
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['%s'] * len(data))
    query = pg_sql.SQL(f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING id").format(
        columns=pg_sql.SQL(columns),
        placeholders=pg_sql.SQL(placeholders)
    )
    result = db_execute(query, list(data.values()), fetch_one=True)
    return result['id'] if result else None

def db_update(table, data, condition):
    """Updates data in a specified table based on a condition."""
    set_clause = ', '.join([f"{col} = %s" for col in data.keys()])
    where_clause = ' AND '.join([f"{col} = %s" for col in condition.keys()])
    query = pg_sql.SQL(f"UPDATE {table} SET {set_clause} WHERE {where_clause}").format(
        set_clause=pg_sql.SQL(set_clause),
        where_clause=pg_sql.SQL(where_clause)
    )
    db_execute(query, list(data.values()) + list(condition.values()))

def db_delete(table, condition):
    """Deletes data from a specified table based on a condition."""
    where_clause = ' AND '.join([f"{col} = %s" for col in condition.keys()])
    query = pg_sql.SQL(f"DELETE FROM {table} WHERE {where_clause}").format(where_clause=pg_sql.SQL(where_clause))
    db_execute(query, list(condition.values()))

# --- Bot functions ---
user_states = {}
product_drafts = {}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Handles the /start command, registers user if new."""
    user_id = message.from_user.id
    user = db_execute("SELECT * FROM users WHERE id = %s", (user_id,), fetch_one=True)
    if not user:
        db_insert('users', {'id': user_id, 'username': message.from_user.username, 'full_name': f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()})
        bot.send_message(message.chat.id, "Ласкаво просимо! Будь ласка, надішліть ваш номер телефону для реєстрації.", reply_markup=get_phone_number_markup())
        user_states[user_id] = 'awaiting_phone'
    else:
        bot.send_message(message.chat.id, "Ви вже зареєстровані. Чим можу допомогти?", reply_markup=get_main_menu_markup())

def get_phone_number_markup():
    """Returns a ReplyKeyboardMarkup for phone number request."""
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    button_phone = types.KeyboardButton(text="Надати номер телефону", request_contact=True)
    markup.add(button_phone)
    return markup

@bot.message_handler(content_types=['contact'])
def contact_handler(message):
    """Handles contact sharing for registration."""
    user_id = message.from_user.id
    if user_states.get(user_id) == 'awaiting_phone':
        phone_number = message.contact.phone_number
        db_update('users', {'phone_number': phone_number}, {'id': user_id})
        bot.send_message(message.chat.id, "Дякуємо! Ваш номер телефону збережено.", reply_markup=get_main_menu_markup())
        del user_states[user_id]
    else:
        bot.send_message(message.chat.id, "Будь ласка, використовуйте головне меню.", reply_markup=get_main_menu_markup())

def get_main_menu_markup():
    """Returns the main menu ReplyKeyboardMarkup."""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add("🔥 Мої товари", "➕ Створити оголошення", "💸 Мій баланс", "⚙️ Налаштування", "❓ Підтримка", "⭐️ Мої відгуки")
    return markup

def get_product_manage_markup(product_id):
    """Returns an InlineKeyboardMarkup for product management by user."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📝 Редагувати", callback_data=f"edit_product_{product_id}"),
        types.InlineKeyboardButton("❌ Видалити", callback_data=f"delete_product_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("🔁 Переопублікувати", callback_data=f"republish_product_{product_id}"),
        types.InlineKeyboardButton("💰 Змінити ціну", callback_data=f"change_price_{product_id}")
    )
    return markup

def get_moderation_markup(product_id, user_id):
    """Returns an InlineKeyboardMarkup for product moderation by admin."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Схвалити", callback_data=f"approve_{product_id}"),
        types.InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📝 Редагувати назву", callback_data=f"mod_edit_name_{product_id}"),
        types.InlineKeyboardButton("📝 Редагувати опис", callback_data=f"mod_edit_description_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📝 Редагувати ціну", callback_data=f"mod_edit_price_{product_id}"),
        types.InlineKeyboardButton("📍 Редагувати геолокацію", callback_data=f"mod_edit_city_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("🚚 Редагувати доставку", callback_data=f"mod_edit_delivery_{product_id}"),
        types.InlineKeyboardButton("🏷️ Редагувати хештеги", callback_data=f"mod_edit_tags_{product_id}")
    )
    markup.add(
        types.InlineKeyboardButton("📸 Запросити виправлення фото", callback_data=f"mod_request_photo_fix_{product_id}")
    )
    markup.add(types.InlineKeyboardButton("✉️ Зв'язатись з продавцем", url=f"tg://user?id={user_id}"))
    return markup

def format_product_for_channel(product, user_contact_info):
    """Formats product details for channel publication, removing field names and conditional geolocation."""
    name = product['name']
    price = product['price']
    description = product['description']
    city = product['city']
    delivery_options = product['delivery_options']
    tags = product['tags']

    text_parts = []
    text_parts.append(f"{name}")
    text_parts.append(f"{price:.2f} UAH")
    text_parts.append(f"{description}")
    if city: # Only include city if it's not None/empty
        text_parts.append(f"{city}")
    text_parts.append(f"{delivery_options}")
    if tags:
        text_parts.append(f"{tags}")
    text_parts.append(f"Контакт: {user_contact_info}")
    
    return "\n\n".join(text_parts)

@bot.message_handler(regexp="^🔥 Мої товари$")
def my_products(message):
    """Displays user's products with management options."""
    user_id = message.from_user.id
    products = db_execute("SELECT * FROM products WHERE user_id = %s", (user_id,), fetch_all=True)

    if not products:
        bot.send_message(message.chat.id, "У вас ще немає доданих товарів.")
        return

    for product in products:
        status_emoji = {
            'pending': '⏳',
            'active': '✅',
            'sold': '⛔',
            'rejected': '❌',
            'moderation': '🔍'
        }.get(product['status'], '❓')
        
        caption = (f"{product['name']}\n" # Removed "Назва:"
                   f"{product['price']:.2f} UAH\n" # Removed "Ціна:"
                   f"Статус: {status_emoji} {product['status']}")
        
        if product['photo_id']:
            bot.send_photo(message.chat.id, product['photo_id'], caption=caption, reply_markup=get_product_manage_markup(product['id']))
        else:
            bot.send_message(message.chat.id, caption, reply_markup=get_product_manage_markup(product['id']))

@bot.message_handler(regexp="^➕ Створити оголошення$")
def create_ad(message):
    """Starts the product creation flow."""
    user_id = message.from_user.id
    product_drafts[user_id] = {'stage': 'awaiting_name'}
    bot.send_message(message.chat.id, "Надішліть назву товару:")
    user_states[user_id] = 'awaiting_name' # Ensure user_states is updated

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_name')
def get_product_name(message):
    """Gets product name from user."""
    user_id = message.from_user.id
    product_drafts[user_id]['name'] = message.text
    user_states[user_id] = 'awaiting_price'
    bot.send_message(message.chat.id, "Надішліть ціну товару (наприклад, 100.50):")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_price')
def get_product_price(message):
    """Gets product price from user."""
    user_id = message.from_user.id
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            raise ValueError
        product_drafts[user_id]['price'] = price
        user_states[user_id] = 'awaiting_description'
        bot.send_message(message.chat.id, "Надішліть опис товару:")
    except ValueError:
        bot.send_message(message.chat.id, "Будь ласка, введіть коректну ціну.")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_description')
def get_product_description(message):
    """Gets product description from user."""
    user_id = message.from_user.id
    product_drafts[user_id]['description'] = message.text
    user_states[user_id] = 'awaiting_photo'
    bot.send_message(message.chat.id, "Надішліть фото товару:")

@bot.message_handler(content_types=['photo'], func=lambda message: user_states.get(message.from_user.id) == 'awaiting_photo')
def get_product_photo(message):
    """Gets product photo from user."""
    user_id = message.from_user.id
    product_drafts[user_id]['photo_id'] = message.photo[-1].file_id
    user_states[user_id] = 'awaiting_city'
    bot.send_message(message.chat.id, "Надішліть вашу геолокацію (місто): (можна пропустити, написавши '-')")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_city')
def get_product_city(message):
    """Gets product city from user."""
    user_id = message.from_user.id
    city = message.text.strip()
    product_drafts[user_id]['city'] = city if city != '-' else None
    user_states[user_id] = 'awaiting_delivery'
    bot.send_message(message.chat.id, "Надішліть варіанти доставки (наприклад, 'Нова Пошта, Укрпошта'):")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_delivery')
def get_product_delivery(message):
    """Gets product delivery options from user."""
    user_id = message.from_user.id
    product_drafts[user_id]['delivery_options'] = message.text
    user_states[user_id] = 'awaiting_tags'
    bot.send_message(message.chat.id, "Надішліть хештеги через кому (наприклад, '#одяг, #футболка'):")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == 'awaiting_tags')
def get_product_tags(message):
    """Gets product tags from user and submits for moderation."""
    user_id = message.from_user.id
    tags = ", ".join([tag.strip() for tag in message.text.split(',') if tag.strip().startswith('#')])
    product_drafts[user_id]['tags'] = tags
    
    product_data = product_drafts[user_id]
    product_id = db_insert('products', {
        'user_id': user_id,
        'name': product_data['name'],
        'price': product_data['price'],
        'description': product_data['description'],
        'photo_id': product_data['photo_id'],
        'status': 'moderation',
        'tags': product_data['tags'],
        'city': product_data['city'],
        'delivery_options': product_data['delivery_options']
    })
    
    del user_states[user_id]
    del product_drafts[user_id]
    
    bot.send_message(message.chat.id, "Ваше оголошення відправлено на модерацію.")

    user = db_execute("SELECT * FROM users WHERE id = %s", (user_id,), fetch_one=True)
    if not user:
        logger.error(f"User {user_id} not found when creating product.")
        return
    user_contact_info = user['username'] if user['username'] else user['phone_number']

    moderation_text = format_product_for_channel(product_data, user_contact_info)
    
    if product_data['photo_id']:
        bot.send_photo(ADMIN_CHAT_ID, product_data['photo_id'], caption=moderation_text, reply_markup=get_moderation_markup(product_id, user_id))
    else:
        bot.send_message(ADMIN_CHAT_ID, moderation_text, reply_markup=get_moderation_markup(product_id, user_id))

# --- Callback query handler for moderation and product management ---
@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    """Handles all inline keyboard callback queries."""
    parts = call.data.split('_')
    action = parts[0]
    
    if action == 'approve' and len(parts) > 1:
        product_id = int(parts[1])
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        if product:
            user = db_execute("SELECT * FROM users WHERE id = %s", (product['user_id'],), fetch_one=True)
            user_contact_info = user['username'] if user['username'] else user['phone_number']

            caption = format_product_for_channel(product, user_contact_info)

            try:
                if product['photo_id']:
                    message = bot.send_photo(CHANNEL_ID, product['photo_id'], caption=caption)
                else:
                    message = bot.send_message(CHANNEL_ID, caption)
                
                db_update('products', {'status': 'active', 'channel_message_id': message.message_id}, {'id': product_id})
                bot.send_message(product['user_id'], f"Ваше оголошення '{product['name']}' було схвалено і опубліковано в каналі!")
                bot.answer_callback_query(call.id, "Оголошення схвалено!")
            except Exception as e:
                logger.error(f"Error publishing to channel: {e}", exc_info=True)
                bot.send_message(ADMIN_CHAT_ID, f"Помилка при публікації оголошення {product_id} в канал: {e}")
                bot.answer_callback_query(call.id, "Помилка при публікації.")
        else:
            bot.answer_callback_query(call.id, "Оголошення не знайдено.")

    elif action == 'reject' and len(parts) > 1:
        product_id = int(parts[1])
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        if product:
            db_update('products', {'status': 'rejected'}, {'id': product_id})
            bot.send_message(product['user_id'], f"Ваше оголошення '{product['name']}' було відхилено модератором.")
            bot.answer_callback_query(call.id, "Оголошення відхилено.")
        else:
            bot.answer_callback_query(call.id, "Оголошення не знайдено.")

    elif action == 'edit_product' and len(parts) > 1:
        product_id = int(parts[1])
        bot.answer_callback_query(call.id, "Функціонал редагування в розробці.")
        # TODO: Implement full product editing for users

    elif action == 'delete_product' and len(parts) > 1:
        product_id = int(parts[1])
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        if product:
            try:
                # Delete related entries first to avoid FOREIGN KEY violation
                db_delete('commission_transactions', {'product_id': product_id})
                db_delete('favorites', {'product_id': product_id})
                db_delete('conversations', {'product_id': product_id}) # This will cascade delete messages

                db_delete('products', {'id': product_id})
                if product['channel_message_id']:
                    bot.delete_message(CHANNEL_ID, product['channel_message_id'])
                bot.send_message(product['user_id'], f"Ваше оголошення '{product['name']}' було успішно видалено.")
                bot.answer_callback_query(call.id, "Оголошення видалено.")
            except Exception as e:
                logger.error(f"Error deleting product {product_id}: {e}", exc_info=True)
                bot.send_message(product['user_id'], f"Помилка при видаленні оголошення '{product['name']}'. Спробуйте пізніше.")
                bot.answer_callback_query(call.id, "Помилка видалення.")
        else:
            bot.answer_callback_query(call.id, "Оголошення не знайдено.")

    elif action == 'republish_product' and len(parts) > 1:
        product_id = int(parts[1])
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        if product and product['status'] == 'active':
            user = db_execute("SELECT * FROM users WHERE id = %s", (product['user_id'],), fetch_one=True)
            user_contact_info = user['username'] if user['username'] else user['phone_number']

            try:
                # Delete old message from channel if it exists
                if product['channel_message_id']:
                    bot.delete_message(CHANNEL_ID, product['channel_message_id'])

                # Publish new message to channel
                caption = format_product_for_channel(product, user_contact_info)
                if product['photo_id']:
                    new_message = bot.send_photo(CHANNEL_ID, product['photo_id'], caption=caption)
                else:
                    new_message = bot.send_message(CHANNEL_ID, caption)
                
                # Update product with new message_id and republish date (no limits enforced)
                db_update('products', {'last_republish_date': datetime.now(timezone.utc), 'channel_message_id': new_message.message_id}, {'id': product_id})
                bot.send_message(product['user_id'], f"Ваше оголошення '{product['name']}' було успішно переопубліковано.")
                bot.answer_callback_query(call.id, "Оголошення переопубліковано.")
            except Exception as e:
                logger.error(f"Error republishing product {product_id}: {e}", exc_info=True)
                bot.send_message(product['user_id'], f"Помилка при переопублікації оголошення '{product['name']}'.")
                bot.answer_callback_query(call.id, "Помилка переопублікації.")
        else:
            bot.answer_callback_query(call.id, "Немає активного оголошення для переопублікації.")


    elif action == 'change_price' and len(parts) > 1:
        product_id = int(parts[1])
        user_id = call.from_user.id
        user_states[user_id] = {'stage': 'awaiting_new_price', 'product_id': product_id}
        bot.send_message(user_id, "Введіть нову ціну для товару:")
        bot.answer_callback_query(call.id)

    elif call.data.startswith("mod_edit_tags_"):
        product_id = int(call.data.split("_")[-1]) # Correct parsing
        user_id = call.from_user.id
        user_states[user_id] = {'stage': 'mod_awaiting_tags', 'product_id': product_id}
        bot.send_message(user_id, "Введіть нові хештеги через кому (наприклад, '#одяг, #футболка'):")
        bot.answer_callback_query(call.id)
    
    elif call.data.startswith("mod_request_photo_fix_"):
        product_id = int(call.data.split("_")[-1]) # Correct parsing
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        if product:
            bot.send_message(product['user_id'], f"Модератор запросив виправлення фото для вашого оголошення '{product['name']}'. Будь ласка, надішліть нове фото.")
            user_states[product['user_id']] = {'stage': 'awaiting_photo_fix', 'product_id': product_id}
            bot.answer_callback_query(call.id, "Запит на виправлення фото відправлено.")
        else:
            bot.answer_callback_query(call.id, "Оголошення не знайдено.")

    # --- Handlers for moderator editing product fields ---
    elif call.data.startswith("mod_edit_name_"):
        product_id = int(call.data.split("_")[-1])
        user_states[call.from_user.id] = {'stage': 'mod_awaiting_name', 'product_id': product_id}
        bot.send_message(call.from_user.id, f"Введіть нову назву для товару ID {product_id}:")
        bot.answer_callback_query(call.id)

    elif call.data.startswith("mod_edit_description_"):
        product_id = int(call.data.split("_")[-1])
        user_states[call.from_user.id] = {'stage': 'mod_awaiting_description', 'product_id': product_id}
        bot.send_message(call.from_user.id, f"Введіть новий опис для товару ID {product_id}:")
        bot.answer_callback_query(call.id)

    elif call.data.startswith("mod_edit_price_"):
        product_id = int(call.data.split("_")[-1])
        user_states[call.from_user.id] = {'stage': 'mod_awaiting_price', 'product_id': product_id}
        bot.send_message(call.from_user.id, f"Введіть нову ціну для товару ID {product_id}:")
        bot.answer_callback_query(call.id)

    elif call.data.startswith("mod_edit_city_"):
        product_id = int(call.data.split("_")[-1])
        user_states[call.from_user.id] = {'stage': 'mod_awaiting_city', 'product_id': product_id}
        bot.send_message(call.from_user.id, f"Введіть нову геолокацію (місто) для товару ID {product_id}: (можна пропустити, написавши '-')")
        bot.answer_callback_query(call.id)

    elif call.data.startswith("mod_edit_delivery_"):
        product_id = int(call.data.split("_")[-1])
        user_states[call.from_user.id] = {'stage': 'mod_awaiting_delivery', 'product_id': product_id}
        bot.send_message(call.from_user.id, f"Введіть нові варіанти доставки для товару ID {product_id}:")
        bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'awaiting_new_price')
def handle_new_price(message):
    """Handles user input for changing product price."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'awaiting_new_price':
        product_id = state['product_id']
        try:
            new_price = float(message.text.replace(',', '.'))
            if new_price <= 0:
                raise ValueError
            # No limits on price change
            db_update('products', {'price': new_price}, {'id': product_id})
            bot.send_message(user_id, f"Ціну товару змінено на {new_price:.2f} UAH.")
            del user_states[user_id]
        except ValueError:
            bot.send_message(user_id, "Будь ласка, введіть коректну ціну.")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_tags')
def handle_mod_new_tags(message):
    """Handles moderator input for new product tags."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_tags':
        product_id = state['product_id']
        tags = ", ".join([tag.strip() for tag in message.text.split(',') if tag.strip().startswith('#')])
        db_update('products', {'tags': tags}, {'id': product_id})
        bot.send_message(user_id, f"Хештеги для товару ID {product_id} оновлено.")
        del user_states[user_id]

@bot.message_handler(content_types=['photo'], func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'awaiting_photo_fix')
def handle_photo_fix(message):
    """Handles user providing a fixed photo after moderator request."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'awaiting_photo_fix':
        product_id = state['product_id']
        new_photo_id = message.photo[-1].file_id
        db_update('products', {'photo_id': new_photo_id}, {'id': product_id})
        bot.send_message(user_id, f"Фото для оголошення ID {product_id} оновлено. Оголошення знову на модерації.")
        db_update('products', {'status': 'moderation'}, {'id': product_id})
        
        product = db_execute("SELECT * FROM products WHERE id = %s", (product_id,), fetch_one=True)
        # Assuming ADMIN_CHAT_ID is where moderation requests go
        if product: # admin_user check is removed as ADMIN_CHAT_ID is a constant
            user = db_execute("SELECT * FROM users WHERE id = %s", (product['user_id'],), fetch_one=True)
            user_contact_info = user['username'] if user['username'] else user['phone_number']
            moderation_text = format_product_for_channel(product, user_contact_info)
            bot.send_photo(ADMIN_CHAT_ID, new_photo_id, caption=moderation_text, reply_markup=get_moderation_markup(product_id, product['user_id']))
        del user_states[user_id]

# --- General message handlers for moderator editing ---
@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_name')
def handle_mod_new_name(message):
    """Handles moderator input for new product name."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_name':
        product_id = state['product_id']
        db_update('products', {'name': message.text}, {'id': product_id})
        bot.send_message(user_id, f"Назву товару ID {product_id} оновлено.")
        del user_states[user_id]

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_description')
def handle_mod_new_description(message):
    """Handles moderator input for new product description."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_description':
        product_id = state['product_id']
        db_update('products', {'description': message.text}, {'id': product_id})
        bot.send_message(user_id, f"Опис товару ID {product_id} оновлено.")
        del user_states[user_id]

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_price')
def handle_mod_new_price(message):
    """Handles moderator input for new product price."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_price':
        product_id = state['product_id']
        try:
            new_price = float(message.text.replace(',', '.'))
            if new_price <= 0:
                raise ValueError
            db_update('products', {'price': new_price}, {'id': product_id})
            bot.send_message(user_id, f"Ціну товару ID {product_id} оновлено на {new_price:.2f} UAH.")
            del user_states[user_id]
        except ValueError:
            bot.send_message(user_id, "Будь ласка, введіть коректну ціну.")

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_city')
def handle_mod_new_city(message):
    """Handles moderator input for new product city."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_city':
        product_id = state['product_id']
        city = message.text.strip()
        db_update('products', {'city': city if city != '-' else None}, {'id': product_id})
        bot.send_message(user_id, f"Геолокацію для товару ID {product_id} оновлено.")
        del user_states[user_id]

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('stage') == 'mod_awaiting_delivery')
def handle_mod_new_delivery(message):
    """Handles moderator input for new product delivery options."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if state and state['stage'] == 'mod_awaiting_delivery':
        product_id = state['product_id']
        db_update('products', {'delivery_options': message.text}, {'id': product_id})
        bot.send_message(user_id, f"Варіанти доставки для товару ID {product_id} оновлено.")
        del user_states[user_id]

# --- Webhook setup ---
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook_handler():
    """Handles incoming webhook POST requests from Telegram API."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '!', 200
    else:
        logger.warning("Received request to webhook with unsupported content type: %s", request.headers.get('content-type'))
        return 'Unsupported Media Type', 415

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
