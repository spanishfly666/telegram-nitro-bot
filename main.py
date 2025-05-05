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

# Ensure file directory and DB exist at startup
os.makedirs(FILE_DIR, exist_ok=True)
init_db()

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
    if request.args.get('secret') != WEBHOOK_SECRET:
        return abort(403)
    data = request.get_json(force=True) or {}
    update_id = data.get('update_id')
    # Log raw update
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        user_part = data.get('message', {}).get('from', {}).get('id') or data.get('callback_query', {}).get('from', {}).get('id')
        c.execute("INSERT OR IGNORE INTO messages (update_id, user_id, raw_data) VALUES (?, ?, ?)",
                  (str(update_id), user_part, json.dumps(data)))
        conn.commit()
    except sqlite3.OperationalError:
        init_db()
        c.execute("INSERT INTO messages (update_id, user_id, raw_data) VALUES (?, ?, ?)",
                  (str(update_id), user_part, json.dumps(data)))
        conn.commit()
    finally:
        conn.close()
    print(f"[DEBUG] Logged message {update_id}")

    # Handle text messages
    if 'message' in data:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '').strip()
        # Custom deposit
        if deposit_requests.get(chat_id) == 'custom':
            try:
                usd_amount = float(text)
                if usd_amount < 10:
                    send_message(chat_id, "Minimum $10 required.")
                else:
                    order_id = f"{chat_id}_{int(time.time())}"
                    invoice = requests.post(
                        "https://api.nowpayments.io/v1/invoice",
                        json={"price_amount": usd_amount, "price_currency": "usd", "pay_currency": "btc", "order_id": order_id, "ipn_callback_url": f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}", "is_fixed_rate": True},
                        headers={"x-api-key": NOWPAYMENTS_API_KEY}
                    ).json()
                    send_message(chat_id, f"Send exactly {invoice['pay_amount']} BTC to:\n{invoice['pay_address']}")
                    deposit_requests.pop(chat_id, None)
            except ValueError:
                send_message(chat_id, "Enter a valid number.")
            return '', 200
        if text == '/start':
            buttons = [
                [{"text": "💰 Deposit", "callback_data": "deposit"}],
                [{"text": "📥 Buy Product", "callback_data": "buy_categories"}],
                [{"text": "📊 Check Balance", "callback_data": "balance"}]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([{"text": "🔧 Admin", "callback_data": "admin"}])
            send_message(chat_id, "Welcome!", buttons)

    # Callback queries
    if 'callback_query' in data:
        cb = data['callback_query']
        chat_id = cb['from']['id']
        action = cb['data']
        answer_callback(cb['id'])
        print(f"[DEBUG] action={action}")
        if action == 'deposit':
            buttons = [[{"text": f"${amt}", "callback_data": f"deposit_{amt}"}] for amt in (10,15,25,50)] + [[{"text":"Custom","callback_data":"deposit_custom"}]]
            send_message(chat_id, "Select deposit amount:", buttons)
        elif action.startswith('deposit_'):
            part = action.split('_',1)[1]
            if part == 'custom':
                deposit_requests[chat_id] = 'custom'
                send_message(chat_id, "Enter custom USD amount (>=10):")
            else:
                usd = float(part)
                order_id = f"{chat_id}_{int(time.time())}"
                inv = requests.post(
                    "https://api.nowpayments.io/v1/invoice",
                    json={"price_amount": usd, "price_currency":"usd","pay_currency":"btc","order_id":order_id,"ipn_callback_url":f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}","is_fixed_rate":True},
                    headers={"x-api-key":NOWPAYMENTS_API_KEY}
                ).json()
                send_message(chat_id, f"Send exactly {inv['pay_amount']} BTC to:\n{inv['pay_address']}")
        elif action == 'balance':
            bal = get_balance(chat_id)
            send_message(chat_id, f"Your balance: {bal:.8f} BTC")
        elif action == 'buy_categories':
            buttons = [[{"text":"Fullz","callback_data":"category_fullz"}],
                       [{"text":"Fullz with CS","callback_data":"category_fullz_cs"}],
                       [{"text":"CPN's","callback_data":"category_cpn"}]]
            send_message(chat_id, "Choose category:", buttons)
        elif action.startswith('category_'):
            prods = get_products()
            buttons = [[{"text":f"{n} - {p:.8f} BTC","callback_data":f"buy_{i}"}] for i,n,p in prods]
            send_message(chat_id, "Products:", buttons)
        elif action.startswith('buy_'):
            pid = int(action.split('_')[1])
            bal = get_balance(chat_id)
            conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("SELECT name,filename,price FROM products WHERE id=?",(pid,)); row=c.fetchone();conn.close()
            if not row: send_message(chat_id,"Not found.")
            else:
                name,fn,pr=row
                if bal<pr: send_message(chat_id,"Insufficient.")
                else:
                    update_balance(chat_id,-pr)
                    send_document(chat_id,fn)
                    send_message(chat_id,f"Bought {name}!")

    return '',200

if __name__=='__main__':
    print("[DEBUG] Starting Flask...")
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)))
