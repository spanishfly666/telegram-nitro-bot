# main.py
import os
import sqlite3
from flask import Flask, request, abort
import requests
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID

# Initialize Flask and Telegram Bot
app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)

DB_PATH = 'database.sqlite'
FILE_DIR = 'files'

# === DATABASE SETUP ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        '''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0
        )'''
    )
    c.execute(
        '''CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            filename TEXT,
            price REAL
        )'''
    )
    c.execute(
        '''CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )'''
    )
    conn.commit()
    conn.close()

# === UTILITIES ===
def get_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    c.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
    balance = c.fetchone()[0]
    conn.close()
    return balance


def update_balance(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_products():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, price FROM products")
    products = c.fetchall()
    conn.close()
    return products

# === ROUTES ===
@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # Verify secret
    if request.args.get('secret') != WEBHOOK_SECRET:
        return abort(403)

    # Parse JSON safely
    try:
        data = request.get_json(force=True)
    except Exception:
        data = {}

    # Debug incoming data
    print("[DEBUG] Incoming webhook data:", data)

    # Handle Telegram message
    if 'message' in data and data['message']:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '')
        if text == '/start':
            buttons = [
                [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
                [InlineKeyboardButton("📥 Buy Product", callback_data="buy")],
                [InlineKeyboardButton("📊 Check Balance", callback_data="balance")]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([InlineKeyboardButton("🔧 Admin", callback_data="admin")])
            # Debug before sending
            print(f"[DEBUG] Sending /start menu to chat_id={chat_id}")
            try:
                bot.send_message(
                    chat_id=chat_id,
                    text="Welcome! Choose an option:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                print(f"[DEBUG] Successfully sent /start menu to {chat_id}")
            except Exception as e:
                print(f"[ERROR] Failed to send /start menu: {e}")

    # Handle Telegram callback query
    if 'callback_query' in data and data['callback_query']:
        cb = data['callback_query']
        chat_id = cb['from']['id']
        cb_id = cb['id']
        action = cb['data']
        # Debug callback reception
        print(f"[DEBUG] Callback '{action}' from chat {chat_id}")
        try:
            bot.answer_callback_query(cb_id)
        except Exception as e:
            print(f"[ERROR] answer_callback_query failed: {e}")

        if action == 'deposit':
            # ... deposit logic remains same, optionally add debug prints
            pass  # placeholder

    # Handle NOWPayments webhook
    if data.get('payment_status') == 'confirmed':
        uid = int(data.get('order_id'))
        amt = float(data.get('pay_amount', 0))
        update_balance(uid, amt)
        print(f"[DEBUG] Updated balance for user {uid} by {amt}")

    return '', 200

# === STARTUP ===
if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    init_db()
    print("[DEBUG] Starting Flask app...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
