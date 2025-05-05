# main.py
import os
import sqlite3
import json
from flask import Flask, request, abort
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import requests
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID

app = Flask(__name__)
DB_PATH = 'database.sqlite'
FILE_DIR = 'files'

# === DATABASE SETUP ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, filename TEXT, price REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# === TELEGRAM BOT SETUP ===
application = Application.builder().token(TELEGRAM_TOKEN).build()

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("📥 Buy Product", callback_data="buy")],
        [InlineKeyboardButton("📊 Check Balance", callback_data="balance")]
    ]
    if update.effective_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("🔧 Admin", callback_data="admin")])
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(buttons))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "deposit":
        await handle_deposit(query, user_id)
    elif data == "balance":
        balance = get_balance(user_id)
        await query.edit_message_text(f"Your BTC balance: {balance:.8f}")
    elif data == "buy":
        await show_products(query)
    elif data.startswith("buy_"):
        product_id = int(data.split("_")[1])
        await handle_purchase(query, user_id, product_id)
    elif data == "admin" and user_id == ADMIN_ID:
        await query.edit_message_text("Admin panel features coming soon.")

# === CORE FUNCTIONS ===
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

def show_products_markup():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, price FROM products")
    products = c.fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(f"{name} - {price:.8f} BTC", callback_data=f"buy_{pid}")] for pid, name, price in products]
    return InlineKeyboardMarkup(buttons) if buttons else None

async def show_products(query, text="Available products:"):
    markup = show_products_markup()
    if markup:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await query.edit_message_text("No products available.")

async def handle_purchase(query, user_id, product_id):
    balance = get_balance(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, filename, price FROM products WHERE id = ?", (product_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await query.edit_message_text("Product not found.")
        return

    name, filename, price = row
    if balance < price:
        await query.edit_message_text("Insufficient balance.")
        return

    update_balance(user_id, -price)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO sales (user_id, product_id) VALUES (?, ?)", (user_id, product_id))
    conn.commit()
    conn.close()

    await query.message.reply_document(document=open(os.path.join(FILE_DIR, filename), 'rb'), filename=filename)
    await query.edit_message_text(f"You bought {name}!")

async def handle_deposit(query, user_id):
    payment = requests.post("https://api.nowpayments.io/v1/invoice", json={
        "price_amount": 10,
        "price_currency": "usd",
        "pay_currency": "btc",
        "order_id": str(user_id),
        "ipn_callback_url": f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}",
        "is_fixed_rate": True
    }, headers={"x-api-key": NOWPAYMENTS_API_KEY}).json()

    pay_address = payment.get("pay_address")
    pay_amount = payment.get("pay_amount")

    if pay_address:
        await query.edit_message_text(
            f"Send exactly {pay_amount} BTC to this address:\n\n`{pay_address}`",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text("Failed to create payment. Try again later.")

# === FLASK WEBHOOK ===
@app.route("/webhook", methods=["POST"])
def webhook():
    if request.args.get("secret") != WEBHOOK_SECRET:
        return abort(403)

    data = request.get_json()

    # Process Telegram webhook updates
    if "message" in data or "callback_query" in data:
        update = Update.de_json(data, application.bot)
        application.update_queue.put(update)

    # Optional: process NOWPayments webhook here too
    if data.get("payment_status") == "confirmed":
        user_id = int(data.get("order_id"))
        amount = float(data.get("pay_amount", 0))
        update_balance(user_id, amount)

    return '', 200


# === STARTUP ===
if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    init_db()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        webhook_url=f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}"
    )