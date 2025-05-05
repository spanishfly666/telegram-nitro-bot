# main.py
import os
import sqlite3
from flask import Flask, request, abort
import requests
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID

app = Flask(__name__)
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
    # Verify secret
    if request.args.get('secret') != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json(force=True) or {}
    print("[DEBUG] Incoming webhook data:", data)

    # Handle Telegram message
    if 'message' in data and data['message']:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '')
        if text == '/start':
            buttons = [
                [{"text": "💰 Deposit", "callback_data": "deposit"}],
                [{"text": "📥 Buy Product", "callback_data": "buy"}],
                [{"text": "📊 Check Balance", "callback_data": "balance"}]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([{"text": "🔧 Admin", "callback_data": "admin"}])
            print(f"[DEBUG] Sending /start menu to {chat_id}")
            send_message(chat_id, "Welcome! Choose an option:", buttons)

    # Handle Telegram callback query
    if 'callback_query' in data and data['callback_query']:
        cb = data['callback_query']
        chat_id = cb['from']['id']
        cb_id = cb['id']
        action = cb['data']
        print(f"[DEBUG] Callback '{action}' from chat {chat_id}")
        answer_callback(cb_id)

        if action == 'deposit':
            try:
                invoice = requests.post(
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
                address = invoice.get('pay_address')
                amount = invoice.get('pay_amount')
                print(f"[DEBUG] Sending deposit info to {chat_id}")
                send_message(chat_id, f"Send exactly {amount} BTC to this address:\n{address}")
            except Exception as e:
                print(f"[ERROR] Deposit handling failed: {e}")

        elif action == 'balance':
            bal = get_balance(chat_id)
            send_message(chat_id, f"Your BTC balance: {bal:.8f}")

        elif action == 'buy':
            products = get_products()
            if products:
                buttons = [
                    [{"text": f"{name} - {price:.8f} BTC", "callback_data": f"buy_{pid}"}]
                    for pid, name, price in products
                ]
                send_message(chat_id, "Available products:", buttons)
            else:
                send_message(chat_id, "No products available.")

        elif action.startswith('buy_'):
            pid = int(action.split('_')[1])
            balance = get_balance(chat_id)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT name, filename, price FROM products WHERE id = ?", (pid,))
            row = c.fetchone()
            conn.close()
            if not row:
                send_message(chat_id, "Product not found.")
            else:
                name, filename, price = row
                if balance < price:
                    send_message(chat_id, "Insufficient balance.")
                else:
                    update_balance(chat_id, -price)
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("INSERT INTO sales (user_id, product_id) VALUES (?, ?)", (chat_id, pid))
                    conn.commit()
                    conn.close()
                    send_document(chat_id, filename)
                    send_message(chat_id, f"You bought {name}!")

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
