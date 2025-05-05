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
deposit_requests = {}  # Track custom deposit state

# === DATABASE SETUP ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)')
    c.execute('CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, filename TEXT, price REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS deposits (order_id TEXT PRIMARY KEY, user_id INTEGER, pay_address TEXT, pay_amount REAL, status TEXT DEFAULT "pending", timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    c.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, update_id TEXT, user_id INTEGER, raw_data TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()

os.makedirs(FILE_DIR, exist_ok=True)
init_db()

# === UTILITIES ===
def get_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (id) VALUES (?)', (user_id,))
    c.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
    bal = c.fetchone()[0]
    conn.close()
    return bal


def update_balance(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (id) VALUES (?)', (user_id,))
    c.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (amount, user_id))
    conn.commit()
    conn.close()


def get_products():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, price FROM products')
    prods = c.fetchall()
    conn.close()
    return prods

# === NOWPAYMENTS HELPERS ===
def create_invoice(usd_amount, order_id):
    # Create invoice
    create_resp = requests.post(
        'https://api.nowpayments.io/v1/invoice',
        json={
            'price_amount': usd_amount,
            'price_currency': 'usd',
            'pay_currency': 'btc',
            'order_id': order_id,
            'ipn_callback_url': f'{BASE_URL}/webhook?secret={WEBHOOK_SECRET}',
            'is_fixed_rate': True
        },
        headers={'x-api-key': NOWPAYMENTS_API_KEY}
    ).json()
    invoice_id = create_resp.get('id')
    if not invoice_id:
        return None, None, create_resp
    # Fetch payment details directly
    detail_resp = requests.get(
        f'https://api.nowpayments.io/v1/invoice/{invoice_id}/payment',
        headers={'x-api-key': NOWPAYMENTS_API_KEY}
    ).json()
    pay_address = detail_resp.get('payment_address')
    pay_amount = detail_resp.get('payment_amount')
    return pay_amount, pay_address, create_resp

# === TELEGRAM HELPERS ===
def send_message(chat_id, text, buttons=None):
    payload = {'chat_id': chat_id, 'text': text}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json=payload)

def answer_callback(callback_id):
    requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery',
        json={'callback_query_id': callback_id}
    )

def send_document(chat_id, filename):
    with open(os.path.join(FILE_DIR, filename), 'rb') as doc:
        files = {'document': doc}
        data = {'chat_id': chat_id}
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument', files=files, data=data)

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
    update_id = data.get('update_id')
    # Log raw update
    uid = None
    if 'message' in data:
        uid = data['message']['from']['id']
    elif 'callback_query' in data:
        uid = data['callback_query']['from']['id']
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT OR IGNORE INTO messages (update_id, user_id, raw_data) VALUES (?, ?, ?)',
        (str(update_id), uid, json.dumps(data))
    )
    conn.commit()
    conn.close()

    # Handle IPN callbacks first
    status = data.get('payment_status')
    if status in ('confirmed', 'partially_paid'):
        uid = int(str(data.get('order_id')).split('_')[0])
        amount = float(data.get('pay_amount') or data.get('payment_amount') or 0)
        update_balance(uid, amount)
        print(f'[DEBUG] Credited {amount} BTC to user {uid} (status={status})')
        return '', 200

    # Handle user messages
    if 'message' in data:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '').strip()
        # Custom deposit flow
        if deposit_requests.get(chat_id) == 'custom':
            try:
                usd = float(text)
                if usd < 10:
                    send_message(chat_id, 'Minimum $10 required.')
                else:
                    order_id = f'{chat_id}_{int(time.time())}'
                    pay_amount, pay_address, _ = create_invoice(usd, order_id)
                    if pay_amount and pay_address:
                        send_message(chat_id, f'Send exactly {pay_amount} BTC to:\n{pay_address}')
                    else:
                        send_message(chat_id, 'Error creating invoice.')
                    deposit_requests.pop(chat_id, None)
            except ValueError:
                send_message(chat_id, 'Enter a valid number.')
            return '', 200
        # /start command
        if text == '/start':
            buttons = [
                [{'text': '💰 Deposit', 'callback_data': 'deposit'}],
                [{'text': '📥 Buy Product', 'callback_data': 'buy_categories'}],
                [{'text': '📊 Check Balance', 'callback_data': 'balance'}]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([{'text': '🔧 Admin', 'callback_data': 'admin'}])
            send_message(chat_id, 'Welcome! Choose an option:', buttons)

    # Handle callback queries
    if 'callback_query' in data:
        cb = data['callback_query']
        chat_id = cb['from']['id']
        action = cb['data']
        answer_callback(cb['id'])
        # Deposit menu
        if action == 'deposit':
            opts = [10, 15, 25, 50]
            buttons = [[{'text': f'${amt}', 'callback_data': f'deposit_{amt}'}] for amt in opts]
            buttons.append([{'text': 'Custom', 'callback_data': 'deposit_custom'}])
            send_message(chat_id, 'Select deposit amount (USD):', buttons)
        elif action.startswith('deposit_'):
            part = action.split('_', 1)[1]
            if part == 'custom':
                deposit_requests[chat_id] = 'custom'
                send_message(chat_id, 'Enter custom USD amount (>=10):')
            else:
                usd = float(part)
                order_id = f'{chat_id}_{int(time.time())}'
                pay_amount, pay_address, _ = create_invoice(usd, order_id)
                if pay_amount and pay_address:
                    send_message(chat_id, f'Send exactly {pay_amount} BTC to:\n{pay_address}')
                else:
                    send_message(chat_id, 'Error creating invoice.')
        elif action == 'balance':
            bal = get_balance(chat_id)
            send_message(chat_id, f'Your balance: {bal:.8f} BTC')
        elif action == 'buy_categories':
            buttons = [[{'text': 'Fullz', 'callback_data': 'category_fullz'}],
                       [{'text': 'Fullz with CS', 'callback_data': 'category_fullz_cs'}],
                       [{'text': "CPN's", 'callback_data': 'category_cpn'}]]
            send_message(chat_id, 'Choose category:', buttons)
        elif action.startswith('category_'):
            prods = get_products()
            buttons = [[{'text': f'{n} - {p:.8f} BTC', 'callback_data': f'buy_{i}'}] for i, n, p in prods]
            send_message(chat_id, 'Products:', buttons)
        elif action.startswith('buy_'):
            pid = int(action.split('_', 1)[1])
            bal = get_balance(chat_id)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT name,filename,price FROM products WHERE id=?', (pid,))
            row = c.fetchone()
            conn.close()
            if not row:
                send_message(chat_id, 'Product not found.')
            else:
                name, fn, pr = row
                if bal < pr:
                    send_message(chat_id, 'Insufficient balance.')
                else:
                    update_balance(chat_id, -pr)
                    send_document(chat_id, fn)
                    send_message(chat_id, f'You bought {name}!')

    return '', 200

if __name__ == '__main__':
    print('[DEBUG] Starting Flask app...')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
