# main.py
import os
import sqlite3
import time
import json
from flask import Flask, request, abort
import requests
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID

app = Flask(__name__)
DB_PATH = 'database.sqlite'
FILE_DIR = 'files'
# In-memory state for custom deposit flow
deposit_requests = {}

# === DATABASE SETUP ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # user, product, sales, deposit tables
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
    c.execute(
        '''CREATE TABLE IF NOT EXISTS deposits (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            pay_address TEXT,
            pay_amount REAL,
            status TEXT DEFAULT 'pending',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )'''
    )
    # conversation log table
    c.execute(
        '''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            update_id TEXT,
            user_id INTEGER,
            raw_data TEXT,
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
    bal = c.fetchone()[0]
    conn.close()
    return bal


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

# === TELEGRAM API HELPERS ===
def send_message(chat_id, text, buttons=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    requests.post(url, json=payload)


def answer_callback(callback_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_id})


def send_document(chat_id, filename):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    file_path = os.path.join(FILE_DIR, filename)
    with open(file_path, 'rb') as doc:
        files = {"document": doc}
        data = {"chat_id": chat_id}
        requests.post(url, files=files, data=data)

# === ROUTES ===
@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    # Verify webhook secret
    if request.args.get('secret') != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json(force=True) or {}
    update_id = data.get('update_id')
    # determine user_id for logging
    user_part = None
    if 'message' in data and data['message']:
        user_part = data['message']['from']['id']
    elif 'callback_query' in data and data['callback_query']:
        user_part = data['callback_query']['from']['id']
    # log raw conversation
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (update_id, user_id, raw_data) VALUES (?, ?, ?)",
        (str(update_id), user_part, json.dumps(data))
    )
    conn.commit()
    conn.close()
    print("[DEBUG] Logged message update_id=", update_id)

    # proceed with existing logic...
    # Telegram message handling
    if 'message' in data and data['message']:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '').strip()

        # Custom deposit amount entry
        if deposit_requests.get(chat_id) == 'custom':
            try:
                usd_amount = float(text)
                if usd_amount < 10:
                    send_message(chat_id, "Minimum deposit is $10. Please enter an amount >= 10.")
                else:
                    order_id = f"{chat_id}_{int(time.time())}"
                    invoice = requests.post(
                        "https://api.nowpayments.io/v1/invoice",
                        json={
                            "price_amount": usd_amount,
                            "price_currency": "usd",
                            "pay_currency": "btc",
                            "order_id": order_id,
                            "ipn_callback_url": f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}",
                            "is_fixed_rate": True
                        },
                        headers={"x-api-key": NOWPAYMENTS_API_KEY}
                    ).json()
                    pay_address = invoice.get('pay_address')
                    pay_amount = invoice.get('pay_amount')
                    # Store deposit
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute(
                        "INSERT INTO deposits (order_id, user_id, pay_address, pay_amount) VALUES (?, ?, ?, ?)",
                        (order_id, chat_id, pay_address, pay_amount)
                    )
                    conn.commit()
                    conn.close()
                    send_message(chat_id, f"Send exactly {pay_amount} BTC to this address:\n`{pay_address}`")
                    deposit_requests.pop(chat_id, None)
            except ValueError:
                send_message(chat_id, "Invalid amount. Enter a number for USD amount.")
            return '', 200

        # /start command
        if text == '/start':
            buttons = [
                [{"text": "💰 Deposit", "callback_data": "deposit"}],
                [{"text": "📥 Buy Product", "callback_data": "buy_categories"}],
                [{"text": "📊 Check Balance", "callback_data": "balance"}]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([{"text": "🔧 Admin", "callback_data": "admin"}])
            send_message(chat_id, "Welcome! Choose an option:", buttons)

    # Callback queries handling remains unchanged...
    # ...

    return '', 200

# === STARTUP ===
if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    init_db()
    print("[DEBUG] Starting Flask app...")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
