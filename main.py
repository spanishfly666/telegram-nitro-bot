import os
import io
import logging
import json
import requests
from flask import Flask, request, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from sqlalchemy.sql import func
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
import tenacity
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from dotenv import load_dotenv
import asyncio

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, filename="app.log", format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Flask setup
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "default-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///data.db").replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["FLASK_ADMIN_SWATCH"] = "cerulean"
db = SQLAlchemy(app)

# Environment
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
BASE_URL = os.getenv("BASE_URL")
FILE_DIR = "files"

if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY is required")

fernet = Fernet(ENCRYPTION_KEY.encode())
deposit_requests = {}
pending_purchases = {}

# Models
class User(db.Model):
    id = db.Column(db.BigInteger, primary_key=True)
    balance = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(10), default="user")
    username = db.Column(db.String(256), nullable=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    price = db.Column(db.Float)
    category = db.Column(db.String(50))
    seller_id = db.Column(db.BigInteger, db.ForeignKey("user.id"))
    details = db.Column(db.JSON, nullable=True)

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("user.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    timestamp = db.Column(db.DateTime, default=func.now())

class Deposit(db.Model):
    order_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("user.id"))
    invoice_url = db.Column(db.String(512))
    status = db.Column(db.String(32), default="pending")
    timestamp = db.Column(db.DateTime, default=func.now())
    amount = db.Column(db.Float, default=0.0)

# Admin panel
admin = Admin(app, name="Nitro Panel", template_mode="bootstrap4")
admin.add_view(ModelView(User, db.session))
admin.add_view(ModelView(Product, db.session))
admin.add_view(ModelView(Sale, db.session))
admin.add_view(ModelView(Deposit, db.session))

# Encryption helpers
def encrypt_data(data):
    return fernet.encrypt(data.encode()).decode() if data else None

def decrypt_data(data):
    try:
        return fernet.decrypt(data.encode()).decode()
    except:
        return None

def encrypt_file_content(content):
    return fernet.encrypt(content.encode() if isinstance(content, str) else content)

def decrypt_file_content(content):
    try:
        return fernet.decrypt(content)
    except:
        return None

# Bot logic
async def start(update: Update, context):
    chat_id = update.message.chat_id
    username = update.message.from_user.username or f"User_{chat_id}"
    welcome_message = (
        f"HI @{username} Welcome To Nitro Bot, A Full Service shop for your FULLZ and CPN needs!\n"
        f"We are steadily previewing new features and updates so be sure to check out our update channel https://t.me/+0DdVC1LxX5w2ZDVh\n\n"
        f"If any assistance is needed please contact admin @goatflow517!\n\n"
        f"Manual deposits are required for BTC load ups UNDER 25$"
    )
    keyboard = [
        [InlineKeyboardButton("\ud83d\udcb0 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("\ud83d\uded2 View Inventory", callback_data="buy_categories")],
        [InlineKeyboardButton("\ud83d\udcb5 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("\ud83d\udcdc Purchase History", callback_data="purchase_history")],
        [InlineKeyboardButton("\ud83c\udd94 View User ID", callback_data="view_user_id")],
        [InlineKeyboardButton("\ud83d\udce2 Visit Update Channel", url="https://t.me/+0DdVC1LxX5w2ZDVh")],
        [InlineKeyboardButton("\ud83d\udcde Contact Admin", url="https://t.me/goatflow517")]
    ]
    if chat_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("\ud83d\udd27 Admin", callback_data="admin")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update: Update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Callback received!")

async def message_handler(update: Update, context):
    await update.message.reply_text("Send /start to begin.")

# Bot setup
async def setup_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    webhook_url = f"{BASE_URL}/webhook/telegram"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
    logger.info(f"Webhook set: {webhook_url}")
    return application.bot, application

@app.route("/webhook/telegram", methods=["POST"])
async def telegram_webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return Response("Forbidden", status=403)
    data = request.get_json(force=True)
    update = Update.de_json(data, app.bot)
    await app.application.process_update(update)
    return Response("", status=200)

@app.route("/")
def home():
    return "Nitro Bot is live!", 200

# Start the bot
async def init():
    async with app.app_context():
        os.makedirs(FILE_DIR, exist_ok=True)
        db.create_all()
        app.bot, app.application = await setup_bot()
        app.loop = asyncio.get_event_loop()

with app.app_context():
    asyncio.run(init())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
