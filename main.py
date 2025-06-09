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

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, filename="app.log", format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Flask & Database setup
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "default-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "postgresql://localhost/nitro_bot").replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["FLASK_ADMIN_SWATCH"] = "cerulean"
db = SQLAlchemy(app)

# Paths
FILE_DIR = "files"

# Global state
deposit_requests = {}
pending_purchases = {}

# Telegram and encryption setup
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY") or Fernet.generate_key().decode()
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
BASE_URL = os.getenv("BASE_URL")
fernet = Fernet(ENCRYPTION_KEY.encode())

# Encryption functions
def encrypt_data(data):
    if not data:
        return None
    try:
        return fernet.encrypt(data.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return None

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return None
    try:
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return "Decryption Error"

def encrypt_file_content(content):
    try:
        if isinstance(content, str):
            content = content.encode()
        return fernet.encrypt(content)
    except Exception as e:
        logger.error(f"File encryption failed: {e}")
        return None

def decrypt_file_content(encrypted_content):
    try:
        return fernet.decrypt(encrypted_content)
    except Exception as e:
        logger.error(f"File decryption failed: {e}")
        return None

# Models
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.BigInteger, primary_key=True)
    balance = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(10), default="user")
    username = db.Column(db.String(256), nullable=True)

class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    price = db.Column(db.Float)
    category = db.Column(db.String(50))
    seller_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    details = db.Column(db.JSON, nullable=True)

class Sale(db.Model):
    __tablename__ = "sales"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    timestamp = db.Column(db.DateTime, default=func.now())

class Deposit(db.Model):
    __tablename__ = "deposits"
    order_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    invoice_url = db.Column(db.String(512))
    status = db.Column(db.String(32), default="pending")
    timestamp = db.Column(db.DateTime, default=func.now())
    amount = db.Column(db.Float, default=0.0)

class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(64), unique=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"))
    raw_data = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=func.now())

class Settings(db.Model):
    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    batch_price = db.Column(db.Float, default=0.0)

# Initialize database
with app.app_context():
    try:
        db.create_all()
        if not Settings.query.first():
            settings = Settings(batch_price=0.0)
            db.session.add(settings)
            db.session.commit()
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

# Admin panel setup
admin = Admin(app, name="Nitro Panel", template_mode="bootstrap4", endpoint="admin")

class DashboardView(BaseView):
    @expose("/")
    def index(self):
        try:
            now = datetime.utcnow()
            periods = {
                "Daily": now - timedelta(days=1),
                "Weekly": now - timedelta(weeks=1),
                "Monthly": now - timedelta(days=30)
            }
            sales_stats = {label: db.session.query(Sale).filter(Sale.timestamp >= start).count()
                           for label, start in periods.items()}
            total_sales = db.session.query(Sale).count()
            total_users = db.session.query(User).count()
            recent_uploads = db.session.query(Product).order_by(Product.id.desc()).limit(5).all()
            total_products = db.session.query(Product).count()
            categories = [cat[0] for cat in db.session.query(Product.category).distinct().all()]
            return self.render("admin/dashboard.html",
                              sales_stats=sales_stats,
                              total_sales=total_sales,
                              total_users=total_users,
                              recent_uploads=recent_uploads,
                              total_products=total_products,
                              categories=categories)
        except Exception as e:
            logger.error(f"Dashboard rendering failed: {e}")
            return self.render("admin/error.html", error=str(e)), 500

class UserAdmin(ModelView):
    column_list = ("id", "username", "balance", "total_deposits", "purchase_count", "last_seen")
    form_columns = ("id", "username", "balance", "role")
    can_create = False
    column_labels = {
        "id": "Telegram ID",
        "total_deposits": "Total Deposits",
        "purchase_count": "Purchases",
        "last_seen": "Last Seen"
    }
    column_formatters = {
        "username": lambda view, context, model, name: decrypt_data(model.username) or "N/A",
        "total_deposits": lambda view, context, model, name: f"{db.session.query(func.sum(Deposit.amount)).filter(Deposit.user_id == model.id, Deposit.status == 'completed').scalar() or 0.0:.2f} credits",
        "purchase_count": lambda view, context, model, name: db.session.query(Sale).filter(Sale.user_id == model.id).count(),
        "last_seen": lambda view, context, model, name: db.session.query(Message.timestamp).filter(Message.user_id == model.id).order_by(Message.timestamp.desc()).first()[0] if db.session.query(Message).filter(Message.user_id == model.id).count() > 0 else "Never"
    }

    @expose("/deposit/", methods=["GET", "POST"])
    @tenacity.retry(stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_fixed(1))
    def deposit_view(self):
        msg = ""
        error = False
        new_user = False
        batch_price = Settings.query.first().batch_price if Settings.query.first() else 0.0
        if request.method == "POST":
            try:
                uid = request.form.get("user_id", type=int)
                amt = request.form.get("amount", type=float)
                batch_price_input = request.form.get("batch_price", type=float)
                if batch_price_input is not None and batch_price_input >= 0:
                    settings = Settings.query.first() or Settings(batch_price=batch_price_input)
                    settings.batch_price = batch_price_input
                    db.session.add(settings)
                    db.session.commit()
                    batch_price = batch_price_input
                    msg = f"Batch price set to {batch_price:.2f} credits. "
                
                if uid and amt:
                    if uid <= 0:
                        msg += "Invalid Telegram User ID."
                        error = True
                    elif amt <= 0:
                        msg += "Invalid amount."
                        error = True
                    else:
                        user = User.query.get(uid)
                        if not user:
                            user = User(id=uid, balance=0.0, role="user", username=encrypt_data(f"User_{uid}"))
                            db.session.add(user)
                            db.session.commit()
                            new_user = True
                            msg += f"New user created with ID {uid}. "
                        
                        user.balance += amt
                        db.session.commit()
                        deposit = Deposit(
                            order_id=f"manual_{uid}_{int(datetime.utcnow().timestamp())}",
                            user_id=uid,
                            invoice_url="manual_deposit",
                            status="completed",
                            amount=amt,
                            timestamp=datetime.utcnow()
                        )
                        db.session.add(deposit)
                        db.session.commit()
                        msg += f"Deposited {amt:.2f} credits to user ID {uid}."
                        try:
                            app.bot.send_message(chat_id=uid, text=f"Your account has been credited with {amt:.2f} credits.")
                        except Exception as e:
                            logger.error(f"Failed to notify user {uid}: {e}")
                            msg += " (Failed to notify user)"
            except Exception as e:
                db.session.rollback()
                logger.error(f"Deposit processing failed: {e}")
                msg = f"Failed to process deposit: {str(e)}"
                error = True
        return self.render("admin/deposit.html", message=msg, error=error, new_user=new_user, batch_price=batch_price)

class DataUploadView(BaseView):
    @expose("/", methods=["GET", "POST"])
    def index(self):
        msg = ""
        error = False
        categories = ["Fullz", "Fullz with CS", "CPN's"]
        batch_price = Settings.query.first().batch_price if Settings.query.first() else 0.0
        if request.method == "POST":
            text = request.form.get("data_text", "").strip()
            cat = request.form.get("category", "")
            price = request.form.get("price", type=float, default=batch_price)
            if not text:
                msg = "No data provided."
                error = True
            elif cat not in categories:
                msg = "Invalid category."
                error = True
            elif price <= 0:
                msg = "Price must be positive."
                error = True
            else:
                try:
                    count = 0
                    os.makedirs(FILE_DIR, exist_ok=True)
                    for idx, line in enumerate(text.splitlines(), 1):
                        parts = line.split(";")
                        if len(parts) != 10:
                            msg = f"Invalid format in line {idx}. Expected 10 fields."
                            error = True
                            break
                        details = {
                            "first_name": parts[0].split("|")[0],
                            "year_born": parts[2].split("|")[0],
                            "city": parts[5].split("|")[0]
                        }
                        name = f"{cat}_{idx}"
                        filename = f"{cat.lower().replace(' ', '_')}_{idx}.txt"
                        file_path = os.path.join(FILE_DIR, filename)
                        encrypted_content = encrypt_file_content(line)
                        if not encrypted_content:
                            msg = f"Encryption failed for line {idx}."
                            error = True
                            break
                        with open(file_path, "wb") as f:
                            f.write(encrypted_content)
                        encrypted_filename = encrypt_data(filename)
                        prod = Product(
                            name=name,
                            filename=encrypted_filename,
                            price=price,
                            category=cat,
                            seller_id=ADMIN_ID,
                            details=details
                        )
                        db.session.add(prod)
                        count += 1
                    if not error:
                        db.session.commit()
                        msg = f"Imported {count} products into {cat} at {price:.2f} credits each."
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"Data upload failed: {e}")
                    msg = f"Failed to import products: {str(e)}"
                    error = True
        return self.render("admin/upload.html", message=msg, categories=categories, error=error, batch_price=batch_price)

class SalesReportView(BaseView):
    @expose("/")
    def index(self):
        try:
            now = datetime.utcnow()
            periods = {
                "Daily": now - timedelta(days=1),
                "Weekly": now - timedelta(weeks=1),
                "Monthly": now - timedelta(days=30),
                "Year-to-Date": now.replace(month=1, day=1)
            }
            stats = {label: db.session.query(Sale).filter(Sale.timestamp >= start).count()
                     for label, start in periods.items()}
            return self.render("admin/sales_report.html", stats=stats)
        except Exception as e:
            logger.error(f"Sales report rendering failed: {e}")
            return self.render("admin/error.html", error=str(e)), 500

# Admin views
admin.add_view(DashboardView(name="Dashboard", endpoint="dashboard"))
admin.add_view(UserAdmin(User, db.session, endpoint="useradmin"))
admin.add_view(ModelView(Product, db.session, endpoint="product"))
admin.add_view(ModelView(Sale, db.session, endpoint="sale"))
admin.add_view(ModelView(Deposit, db.session, endpoint="deposit"))
admin.add_view(SalesReportView(name="Sales Report", endpoint="sales"))
admin.add_view(DataUploadView(name="Data Upload", endpoint="upload"))

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
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("🛒 View Inventory", callback_data="buy_categories")],
        [InlineKeyboardButton("💵 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("📜 Purchase History", callback_data="purchase_history")],
        [InlineKeyboardButton("🆔 View User ID", callback_data="view_user_id")],
        [InlineKeyboardButton("📢 Visit Update Channel", url="https://t.me/+0DdVC1LxX5w2ZDVh")],
        [InlineKeyboardButton("📞 Contact Admin", url="https://t.me/goatflow517")]
    ]
    if chat_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("🔧 Admin", callback_data="admin")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    action = query.data

    if action == "deposit":
        keyboard = [
            [InlineKeyboardButton("BTC", callback_data="deposit_btc")],
            [InlineKeyboardButton("Manual Deposit", callback_data="deposit_manual")]
        ]
        await query.message.reply_text("Choose deposit method:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "deposit_btc":
        deposit_requests[chat_id] = "await_amount"
        await query.message.reply_text("Enter USD amount to deposit:")
    elif action == "deposit_manual":
        await query.message.reply_text("Please contact @goatflow517 for manual deposit.")
    elif action == "admin":
        await query.message.reply_text(f"Access the admin panel here:\n{BASE_URL}/admin")
    elif action == "balance":
        bal = get_balance(chat_id)
        await query.message.reply_text(f"Your balance: {bal:.2f} credits")
    elif action == "view_user_id":
        await query.message.reply_text(f"Your User ID: {chat_id}")
    elif action == "purchase_history":
        history = get_purchase_history(chat_id)
        if not history:
            await query.message.reply_text("No purchase history found.")
        else:
            msg = "Your Purchase History:\n\n"
            for name, price, timestamp in history:
                msg += f"Product: {name}\nPrice: {price:.2f} credits\nDate: {timestamp}\n\n"
            await query.message.reply_text(msg)
    elif action == "buy_categories":
        categories = ["Fullz", "Fullz with CS", "CPN's"]
        keyboard = [[InlineKeyboardButton(cat, callback_data=f"category_{cat.replace(' ', '_').lower()}_1")] for cat in categories]
        await query.message.reply_text("Choose a category:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action.startswith("category_"):
        parts = action.split("_")
        cat = " ".join(parts[1:-1]).replace("_", " ").title()
        page = int(parts[-1])
        if cat == "Cpn's":
            cat = "CPN's"
        products = get_products(cat)
        if not products:
            await query.message.reply_text(f"No products available in {cat}.")
            return
        items_per_page = 10
        total_pages = (len(products) + items_per_page - 1) // items_per_page
        start = (page - 1) * items_per_page
        end = start + items_per_page
        keyboard = [[InlineKeyboardButton(f"{p.name} - {p.price:.2f} credits", callback_data=f"buy_{p.id}")] for p in products[start:end]]
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"category_{cat.lower().replace(' ', '_')}_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"category_{cat.lower().replace(' ', '_')}_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("Back to Categories", callback_data="buy_categories")])
        await query.message.reply_text(f"Products in {cat} (Page {page}/{total_pages}):", reply_markup=InlineKeyboardMarkup(keyboard))
    elif action.startswith("buy_"):
        pid = int(action.split("_", 1)[1])
        bal = get_balance(chat_id)
        product = Product.query.get(pid)
        if not product:
            await query.message.reply_text("Product not found.")
            return
        if bal < product.price:
            await query.message.reply_text("Insufficient balance.")
            return
        pending_purchases[chat_id] = pid
        keyboard = [
            [InlineKeyboardButton("Confirm Purchase", callback_data=f"confirm_{pid}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_purchase")]
        ]
        await query.message.reply_text(
            f"Confirm purchase of {product.name} for {product.price:.2f} credits?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif action.startswith("confirm_"):
        pid = int(action.split("_", 1)[1])
        if pending_purchases.get(chat_id) != pid:
            await query.message.reply_text("Invalid purchase confirmation.")
            return
        bal = get_balance(chat_id)
        product = Product.query.get(pid)
        if not product:
            await query.message.reply_text("Product not found.")
            pending_purchases.pop(chat_id, None)
            return
        if bal < product.price:
            await query.message.reply_text("Insufficient balance.")
            pending_purchases.pop(chat_id, None)
            return
        try:
            update_balance(chat_id, -product.price)
            new_balance = get_balance(chat_id)
            sale = Sale(user_id=chat_id, product_id=pid, timestamp=datetime.utcnow())
            db.session.add(sale)
            decrypted_filename = decrypt_data(product.filename)
            if decrypted_filename:
                file_path = os.path.join(FILE_DIR, decrypted_filename)
                if not os.path.exists(file_path):
                    logger.error(f"File not found: {file_path}")
                    await query.message.reply_text("File not found. Please contact @goatflow517.")
                    db.session.rollback()
                    pending_purchases.pop(chat_id, None)
                    return
                with open(file_path, "rb") as f:
                    encrypted_content = f.read()
                decrypted_content = decrypt_file_content(encrypted_content)
                if decrypted_content:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=io.BytesIO(decrypted_content),
                        filename=decrypted_filename,
                        caption=f"Your purchased file: {product.name}",
                        content_type="application/octet-stream"
                    )
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted file: {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete file {file_path}: {e}")
                else:
                    await query.message.reply_text("Failed to decrypt file. Please contact @goatflow517.")
                    db.session.rollback()
                    pending_purchases.pop(chat_id, None)
                    return
            db.session.delete(product)
            db.session.commit()
            await query.message.reply_text(
                f"You bought {product.name}!\nDeducted: {product.price:.2f} credits\nBalance: {new_balance:.2f} credits"
            )
            pending_purchases.pop(chat_id, None)
        except Exception as e:
            logger.error(f"Purchase failed for user {chat_id}, product {pid}: {e}")
            db.session.rollback()
            await query.message.reply_text("Purchase failed. Please try again or contact @goatflow517.")
            pending_purchases.pop(chat_id, None)
    elif action == "cancel_purchase":
        pending_purchases.pop(chat_id, None)
        await query.message.reply_text("Purchase cancelled.")

async def handle_message(update: Update, context):
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    if deposit_requests.get(chat_id) == "await_amount":
        try:
            usd = float(text)
            if usd < 25:
                await update.message.reply_text("Manual deposits are required for BTC load ups UNDER $25. Please contact @goatflow517.")
                deposit_requests.pop(chat_id, None)
                return
            order_id = f"{chat_id}_{int(datetime.utcnow().timestamp())}"
            inv, _ = create_invoice(usd, order_id)
            if inv:
                await update.message.reply_text(f"Complete payment here:\n{inv}")
                deposit = Deposit(
                    order_id=order_id,
                    user_id=chat_id,
                    invoice_url=inv,
                    status="pending",
                    amount=0.0,
                    timestamp=datetime.utcnow()
                )
                db.session.add(deposit)
                db.session.commit()
            else:
                await update.message.reply_text("Failed to create invoice. Please try again or contact @goatflow517.")
        except ValueError:
            await update.message.reply_text("Enter a valid number.")
        deposit_requests.pop(chat_id, None)
        return
    await update.message.reply_text("Sorry, I didn't understand that command. Use /start to begin.")

# Bot setup
async def setup_bot():
    app.bot = Bot(token=TELEGRAM_TOKEN)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.COMMAND, lambda update, context: update.message.reply_text("Unknown command. Use /start to begin.")))
    return application

# Business logic
def get_balance(user_id):
    try:
        user = User.query.get(user_id)
        if not user:
            user = User(id=user_id, balance=0.0, role="user", username=encrypt_data(f"User_{user_id}"))
            db.session.add(user)
            db.session.commit()
        return user.balance
    except Exception as e:
        logger.error(f"Get balance failed for user {user_id}: {e}")
        return 0.0

def update_balance(user_id, amount):
    try:
        user = User.query.get(user_id)
        if not user:
            user = User(id=user_id, balance=0.0, role="user", username=encrypt_data(f"User_{user_id}"))
            db.session.add(user)
        new_balance = user.balance + amount
        if new_balance < 0:
            raise ValueError("Balance cannot be negative")
        user.balance = new_balance
        db.session.commit()
        logger.info(f"Balance updated for user {user_id}: {user.balance}")
    except Exception as e:
        logger.error(f"Update balance failed for user {user_id}: {e}")
        db.session.rollback()
        raise

def get_products(category=None):
    try:
        query = Product.query
        if category:
            query = query.filter_by(category=category)
        return query.all()
    except Exception as e:
        logger.error(f"Get products failed: {e}")
        return []

def get_purchase_history(user_id):
    try:
        return db.session.query(Product.name, Product.price, Sale.timestamp).join(Sale, Sale.product_id == Product.id).filter(Sale.user_id == user_id).order_by(Sale.timestamp.desc()).all()
    except Exception as e:
        logger.error(f"Purchase history failed for user {user_id}: {e}")
        return []

def create_invoice(usd_amount, order_id):
    try:
        resp = requests.post(
            "https://api.nowpayments.io/v1/invoice",
            json={
                "price_amount": usd_amount,
                "price_currency": "usd",
                "pay_currency": "btc",
                "order_id": order_id,
                "ipn_callback_url": f"{BASE_URL}/webhook",
                "is_fixed_rate": True
            },
            headers={"x-api-key": NOWPAYMENTS_API_KEY}
        ).json()
        return resp.get("invoice_url"), resp
    except Exception as e:
        logger.error(f"Create invoice failed: {e}")
        return None, {}

# Routes
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
async def webhook():
    logger.info(f"Webhook hit: {request.get_data(as_text=True)}")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        logger.error(f"Invalid webhook secret: {request.headers.get('X-Telegram-Bot-Api-Secret-Token')}")
        return Response("Unauthorized", status=403)
    data = request.get_json(force=True) or {}
    update_id = str(data.get("update_id", ""))

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.args.get('secret') != WEBHOOK_SECRET:
        return 'Forbidden', 403
    ...

    # Handle NOWPayments callback
    if data.get("payment_status"):
        status = data.get("payment_status")
        if status in ("confirmed", "partially_paid"):
            try:
                uid = int(str(data.get("order_id")).split("_")[0])
                btc_amt = float(data.get("pay_amount") or data.get("payment_amount") or 0)
                est = requests.get(
                    "https://api.nowpayments.io/v1/estimate",
                    params={"source_currency": "BTC", "target_currency": "USD", "source_amount": btc_amt},
                    headers={"x-api-key": NOWPAYMENTS_API_KEY}
                ).json()
                credits = float(est.get("estimated_amount", 0))
                update_balance(uid, credits)
                deposit = Deposit.query.get(data.get("order_id"))
                if deposit:
                    deposit.status = "completed"
                    deposit.amount = credits
                    db.session.commit()
                await app.bot.send_message(chat_id=uid, text=f"Your deposit has been credited as {credits:.2f} credits.")
            except Exception as e:
                logger.error(f"Payment processing failed for order {data.get('order_id')}: {e}")
            return Response("", status=200)

    # Handle Telegram update
    try:
        update = Update.de_json(data, app.bot)
        if update:
            uid = (update.message.from_user.id if update.message else
                   update.callback_query.from_user.id if update.callback_query else None)
            username = (update.message.from_user.username or f"User_{uid}" if update.message else
                       update.callback_query.from_user.username or f"User_{uid}" if update.callback_query else None)
            if uid and username:
                try:
                    user = User.query.get(uid)
                    encrypted_username = encrypt_data(username)
                    if not user:
                        user = User(id=uid, balance=0.0, role="user", username=encrypted_username)
                        db.session.add(user)
                    else:
                        user.username = encrypted_username
                    db.session.commit()
                    message = Message(
                        update_id=update_id,
                        user_id=uid,
                        raw_data=json.dumps(data, default=str),
                        timestamp=datetime.utcnow()
                    )
                    db.session.add(message)
                    db.session.commit()
                except Exception as e:
                    logger.error(f"Failed to create/update user/message {uid}: {e}")
                    db.session.rollback()
            await app.application.process_update(update)
        return Response("", status=200)
    except Exception as e:
        logger.error(f"Webhook processing failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.errorhandler(Exception)
def handle_error(error):
    logger.error(f"Unhandled error: {error}", exc_info=True)
    return jsonify({"error": "Internal Server Error", "message": str(error)}), 500

# Initialize bot
with app.app_context():
    app.application = None
    import asyncio
    loop = asyncio.get_event_loop()
    app.application = loop.run_until_complete(setup_bot())

if __name__ == "__main__":
    os.makedirs(FILE_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))