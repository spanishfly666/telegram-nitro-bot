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
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            filename TEXT,
            price REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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

    # Handle Telegram update
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
            bot.send_message(chat_id=chat_id, text="Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(buttons))

    # Handle Telegram callback query
    if 'callback_query' in data and data['callback_query']:
        cb = data['callback_query']
        chat_id = cb['from']['id']
        cb_id = cb['id']
        action = cb['data']
        bot.answer_callback_query(cb_id)

        # Deposit
        if action == 'deposit':
            payment = requests.post(
                "https://api.nowpayments.io/v1/invoice",
                json={
                    "price_amount": 10,
                    "price_currency": "usd",
                    "pay_currency": "btc",
                    "order_id": str(chat_id),
                    "ipn_callback_url": f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}",
                    "is_fixed_rate": True
                },
                headers={"x-api-key": NOWPAYMENTS_API_KEY}
            ).json()
            address = payment.get('pay_address')
            amount = payment.get('pay_amount')
            if address:
                bot.send_message(chat_id=chat_id, text=f"Send exactly {amount} BTC to this address:\n{address}")
            else:
                bot.send_message(chat_id=chat_id, text="Failed to create payment. Try again later.")

        # Check balance
        elif action == 'balance':
            bal = get_balance(chat_id)
            bot.send_message(chat_id=chat_id, text=f"Your BTC balance: {bal:.8f}")

        # Show products
        elif action == 'buy':
            products = get_products()
            if products:
                buttons = [[InlineKeyboardButton(f"{name} - {price:.8f} BTC", callback_data=f"buy_{pid}")] for pid, name, price in products]
                bot.send_message(chat_id=chat_id, text="Available products:", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                bot.send_message(chat_id=chat_id, text="No products available.")

        # Handle purchase
        elif action.startswith('buy_'):
            pid = int(action.split('_')[1])
            balance = get_balance(chat_id)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT name, filename, price FROM products WHERE id = ?", (pid,))
            row = c.fetchone()
            conn.close()
            if not row:
                bot.send_message(chat_id=chat_id, text="Product not found.")
            else:
                name, filename, price = row
                if balance < price:
                    bot.send_message(chat_id=chat_id, text="Insufficient balance.")
                else:
                    update_balance(chat_id, -price)
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("INSERT INTO sales (user_id, product_id) VALUES (?, ?)", (chat_id, pid))
                    conn.commit()
                    conn.close()
                    bot.send_document(chat_id=chat_id, document=open(os.path.join(FILE_DIR, filename), 'rb'), filename=filename)
                    bot.send_message(chat_id=chat_id, text=f"You bought {name}!")

    # Handle NOWPayments webhook
    if data.get('payment_status') == 'confirmed':
        uid = int(data.get('order_id'))
        amt = float(data.get('pay_amount', 0))
        update_balance(uid, amt)

    return '', 200

# === STARTUP ===
if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    init_db()
    # Start Flask server for both Telegram and payments
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
