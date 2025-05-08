import os
import sqlite3
import time
import json
import requests
from flask import Flask, request, abort, render_template
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from datetime import datetime
from cryptography.fernet import Fernet
import base64
import tenacity
import io

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
NOWPAYMENTS_API_KEY = os.environ.get('NOWPAYMENTS_API_KEY')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '123456789'))
OWNER_ID = int(os.environ.get('OWNER_ID', '123456789'))
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')

# --- Encryption Setup ---
if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY must be set in environment variables")
fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_data(data):
    if not data:
        return None
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return None
    try:
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception as e:
        app.logger.error(f"Decryption failed: {e}")
        return "Decryption Error"

def encrypt_file_content(content):
    return fernet.encrypt(content.encode())

def decrypt_file_content(encrypted_content):
    try:
        return fernet.decrypt(encrypted_content).decode()
    except Exception as e:
        app.logger.error(f"File decryption failed: {e}")
        return None

# --- Flask & Database Setup ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///database.sqlite').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'change-me')

db = SQLAlchemy(app)

# Paths
DB_PATH = 'database.sqlite'
FILE_DIR = 'files'

deposit_requests = {}

# --- Models ---
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    balance = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(10), default='user')
    username = db.Column(db.String(100), nullable=True)  # Encrypted

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    filename = db.Column(db.String(256))  # Encrypted
    price = db.Column(db.Float)
    category = db.Column(db.String(50))
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id'))

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'))
    timestamp = db.Column(db.DateTime, default=db.func.now())

class Deposit(db.Model):
    order_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    invoice_url = db.Column(db.String(512))
    status = db.Column(db.String(32), default='pending')
    timestamp = db.Column(db.DateTime, default=db.func.now())
    amount = db.Column(db.Float, default=0.0)

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(64), unique=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    raw_data = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=db.func.now())

class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    batch_price = db.Column(db.Float, default=0.0)

# --- Ensure Tables Exist ---
with app.app_context():
    try:
        db.create_all()
        # Initialize settings if empty
        if not Settings.query.first():
            settings = Settings(batch_price=0.0)
            db.session.add(settings)
            db.session.commit()
    except Exception as e:
        app.logger.error(f"db.create_all() failed: {e}")

    # SQLite fallback for local testing
    if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            '''CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY,
                   balance REAL,
                   role TEXT,
                   username TEXT
               );'''
        )
        c.execute(
            '''CREATE TABLE IF NOT EXISTS products (
                   id INTEGER PRIMARY KEY,
                   name TEXT,
                   filename TEXT,
                   price REAL,
                   category TEXT,
                   seller_id INTEGER,
                   FOREIGN KEY(seller_id) REFERENCES users(id)
               );'''
        )
        c.execute(
            '''CREATE TABLE IF NOT EXISTS messages (
                   id INTEGER PRIMARY KEY,
                   update_id TEXT UNIQUE,
                   user_id INTEGER,
                   raw_data TEXT,
                   timestamp DATETIME,
                   FOREIGN KEY(user_id) REFERENCES users(id)
               );'''
        )
        c.execute(
            '''CREATE TABLE IF NOT EXISTS deposits (
                   order_id TEXT PRIMARY KEY,
                   user_id INTEGER,
                   invoice_url TEXT,
                   status TEXT,
                   timestamp DATETIME,
                   amount REAL,
                   FOREIGN KEY(user_id) REFERENCES users(id)
               );'''
        )
        c.execute(
            '''CREATE TABLE IF NOT EXISTS sales (
                   id INTEGER PRIMARY KEY,
                   user_id INTEGER,
                   product_id INTEGER,
                   timestamp DATETIME,
                   FOREIGN KEY(user_id) REFERENCES users(id),
                   FOREIGN KEY(product_id) REFERENCES products(id)
               );'''
        )
        c.execute(
            '''CREATE TABLE IF NOT EXISTS settings (
                   id INTEGER PRIMARY KEY,
                   batch_price REAL DEFAULT 0.0
               );'''
        )
        conn.commit()
        conn.close()

# --- Admin Panel ---
admin = Admin(app, name='Nitro Panel', template_mode='bootstrap4')

class DashboardView(BaseView):
    @expose('/')
    def index(self):
        try:
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            periods = {
                'Daily': now - timedelta(days=1),
                'Weekly': now - timedelta(weeks=1),
                'Monthly': now - timedelta(days=30)
            }
            sales_stats = {}
            total_sales = 0
            try:
                sales_stats = {label: db.session.query(Sale).filter(Sale.timestamp >= start).count()
                              for label, start in periods.items()}
                total_sales = db.session.query(Sale).count()
            except Exception as e:
                app.logger.error(f"Sales stats query failed: {e}")
                sales_stats = {label: 0 for label in periods}
                total_sales = 0

            total_users = 0
            recent_uploads = []
            try:
                total_users = db.session.query(User).count()
                recent_uploads = db.session.query(Product).order_by(Product.id.desc()).limit(5).all()
            except Exception as e:
                app.logger.error(f"User stats query failed: {e}")

            total_products = 0
            categories = []
            try:
                total_products = db.session.query(Product).count()
                categories = db.session.query(Product.category).distinct().all()
            except Exception as e:
                app.logger.error(f"Product stats query failed: {e}")

            return self.render('admin/dashboard.html',
                             sales_stats=sales_stats,
                             total_sales=total_sales,
                             total_users=total_users,
                             recent_uploads=recent_uploads,
                             total_products=total_products,
                             categories=categories)
        except Exception as e:
            app.logger.error(f"Dashboard rendering failed: {e}")
            return render_template('admin/error.html', error=str(e)), 500

class UserAdmin(ModelView):
    column_list = ('id', 'username', 'balance', 'total_deposits', 'purchase_count', 'last_seen')
    form_columns = ('id', 'username', 'balance', 'role')
    can_create = False
    column_labels = {
        'id': 'Telegram ID',
        'total_deposits': 'Total Deposits',
        'purchase_count': 'Purchases',
        'last_seen': 'Last Seen'
    }
    column_formatters = {
        'username': lambda view, context, model, name: decrypt_data(model.username) if model.username else 'N/A',
        'total_deposits': lambda view, context, model, name: f"{db.session.query(func.sum(Deposit.amount)).filter(Deposit.user_id == model.id, Deposit.status == 'completed').scalar() or 0.0:.2f} credits",
        'purchase_count': lambda view, context, model, name: db.session.query(Sale).filter(Sale.user_id == model.id).count(),
        'last_seen': lambda view, context, model, name: db.session.query(Message.timestamp).filter(Message.user_id == model.id).order_by(Message.timestamp.desc()).first()[0] if db.session.query(Message).filter(Message.user_id == model.id).count() > 0 else 'Never'
    }

    @expose('/deposit/', methods=['GET', 'POST'])
    @tenacity.retry(stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_fixed(1))
    def deposit_view(self):
        msg = ''
        error = False
        new_user = False
        batch_price = Settings.query.first().batch_price if Settings.query.first() else 0.0
        if request.method == 'POST':
            try:
                uid = request.form.get('user_id', type=int)
                amt = request.form.get('amount', type=float)
                batch_price_input = request.form.get('batch_price', type=float)
                if batch_price_input is not None and batch_price_input >= 0:
                    settings = Settings.query.first()
                    if not settings:
                        settings = Settings(batch_price=batch_price_input)
                        db.session.add(settings)
                    else:
                        settings.batch_price = batch_price_input
                    db.session.commit()
                    batch_price = batch_price_input
                    msg = f'Batch price set to {batch_price:.2f} credits. '
                
                if uid and amt:
                    if uid <= 0:
                        msg += 'Invalid Telegram User ID. It must be a positive integer.'
                        error = True
                    elif amt <= 0:
                        msg += 'Invalid amount. It must be a positive number.'
                        error = True
                    else:
                        try:
                            user = User.query.get(uid)
                            if not user:
                                user = User(id=uid, balance=0.0, role='user', username=encrypt_data(f'User_{uid}'))
                                db.session.add(user)
                                db.session.commit()
                                new_user = True
                                msg += f'New user created with ID {uid}. '
                            
                            user.balance += amt
                            db.session.commit()

                            deposit = Deposit(
                                order_id=f'manual_{uid}_{int(time.time())}',
                                user_id=uid,
                                invoice_url='manual_deposit',
                                status='completed',
                                amount=amt,
                                timestamp=datetime.utcnow()
                            )
                            db.session.add(deposit)
                            db.session.commit()

                            msg += f'Successfully deposited {amt:.2f} credits to user ID {uid}.'
                            try:
                                send_message(uid, f'Your account has been credited with {amt:.2f} credits via manual deposit.')
                            except Exception as e:
                                app.logger.error(f"Failed to send Telegram message to {uid}: {e}")
                                msg += ' (Note: Failed to notify user via Telegram)'
                        except Exception as e:
                            db.session.rollback()
                            app.logger.error(f"Deposit processing failed for user {uid}: {e}")
                            msg = f'Failed to process deposit: {str(e)}'
                            error = True
                            raise e
            except ValueError as e:
                app.logger.error(f"Input validation failed: {e}")
                msg = 'Invalid input. Ensure User ID, amount, and batch price are valid numbers.'
                error = True
        return self.render('admin/deposit.html', message=msg, error=error, new_user=new_user, batch_price=batch_price)

class DataUploadView(BaseView):
    @expose('/', methods=['GET', 'POST'])
    def index(self):
        msg = ''
        error = False
        categories = ['Fullz', 'Fullz with CS', "CPN's"]
        batch_price = Settings.query.first().batch_price if Settings.query.first() else 0.0
        if request.method == 'POST':
            text = request.form.get('data_text', '').strip()
            cat = request.form.get('category', '')
            price = request.form.get('price', type=float, default=batch_price)
            if not text:
                msg = 'No data provided.'
                error = True
            elif cat not in categories:
                msg = 'Select a valid category.'
                error = True
            elif price <= 0:
                msg = 'Price must be a positive number.'
                error = True
            else:
                try:
                    count = 0
                    os.makedirs(FILE_DIR, exist_ok=True)
                    for idx, line in enumerate(text.splitlines(), 1):
                        parts = line.split(';')
                        if len(parts) != 10:
                            msg = f'Invalid format in line {idx}. Expected 10 fields.'
                            error = True
                            break
                        name = f'{cat}_{idx}'
                        filename = f'{cat.lower().replace(" ", "_")}_{idx}.txt'
                        file_path = os.path.join(FILE_DIR, filename)
                        # Encrypt file content
                        encrypted_content = encrypt_file_content(line)
                        with open(file_path, 'wb') as f:
                            f.write(encrypted_content)
                        # Encrypt filename
                        encrypted_filename = encrypt_data(filename)
                        prod = Product(
                            name=name,
                            filename=encrypted_filename,
                            price=price,
                            category=cat,
                            seller_id=ADMIN_ID
                        )
                        db.session.add(prod)
                        count += 1
                    if not error:
                        db.session.commit()
                        msg = f'Imported {count} products into {cat} at {price:.2f} credits each.'
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Data upload failed: {e}")
                    msg = 'Failed to import products. Please check the format and try again.'
                    error = True
        return self.render('admin/upload.html', message=msg, categories=categories, error=error, batch_price=batch_price)

class SalesReportView(BaseView):
    @expose('/')
    def index(self):
        try:
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            periods = {
                'Daily': now - timedelta(days=1),
                'Weekly': now - timedelta(weeks=1),
                'Monthly': now - timedelta(days=30),
                'Year-to-Date': now.replace(month=1, day=1)
            }
            stats = {label: db.session.query(Sale).filter(Sale.timestamp >= start).count()
                     for label, start in periods.items()}
            return self.render('admin/sales_report.html', stats=stats)
        except Exception as e:
            app.logger.error(f"Sales report rendering failed: {e}")
            return render_template('admin/error.html', error=str(e)), 500

admin.add_view(DashboardView(name='Dashboard', endpoint='dashboard'))
admin.add_view(UserAdmin(User, db.session, endpoint='useradmin'))
admin.add_view(ModelView(Product, db.session, endpoint='product'))
admin.add_view(ModelView(Sale, db.session, endpoint='sale'))
admin.add_view(ModelView(Deposit, db.session, endpoint='deposit'))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(DataUploadView(name='Data Upload', endpoint='upload'))

# --- Bot Logic ---
def get_balance(user_id):
    try:
        user = User.query.get(user_id)
        if not user:
            user = User(id=user_id, balance=0.0, role='user', username=encrypt_data(f'User_{user_id}'))
            db.session.add(user)
            db.session.commit()
        return user.balance
    except Exception as e:
        app.logger.error(f"Get balance failed for user {user_id}: {e}")
        return 0.0

def update_balance(user_id, amount):
    try:
        user = User.query.get(user_id)
        if not user:
            user = User(id=user_id, balance=0.0, role='user', username=encrypt_data(f'User_{user_id}'))
            db.session.add(user)
        user.balance += amount
        db.session.commit()
    except Exception as e:
        app.logger.error(f"Update balance failed for user {user_id}: {e}")

def get_products(category=None):
    try:
        if category:
            prods = Product.query.filter_by(category=category).all()
        else:
            prods = Product.query.all()
        for prod in prods:
            prod.decrypted_filename = decrypt_data(prod.filename)
        return prods
    except Exception as e:
        app.logger.error(f"Get products failed: {e}")
        return []

def get_purchase_history(user_id):
    try:
        return db.session.query(Product.name, Product.price, Sale.timestamp).join(Sale, Sale.product_id == Product.id).filter(Sale.user_id == user_id).order_by(Sale.timestamp.desc()).all()
    except Exception as e:
        app.logger.error(f"Get purchase history failed for user {user_id}: {e}")
        return []

def create_invoice(usd_amount, order_id):
    try:
        resp = requests.post(
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
        return resp.get('invoice_url'), resp
    except Exception as e:
        app.logger.error(f"Create invoice failed: {e}")
        return None, {}

def send_message(chat_id, text, buttons=None):
    try:
        payload = {'chat_id': chat_id, 'text': text}
        if buttons:
            payload['reply_markup'] = {'inline_keyboard': buttons}
        response = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json=payload)
        response.raise_for_status()
    except Exception as e:
        app.logger.error(f"Send message failed to chat {chat_id}: {e}")

def answer_callback(cid):
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery',
                     json={'callback_query_id': cid})
    except Exception as e:
        app.logger.error(f"Answer callback failed: {e}")

def send_document(chat_id, filename):
    try:
        decrypted_filename = decrypt_data(filename)
        path = os.path.join(FILE_DIR, decrypted_filename)
        with open(path, 'rb') as f:
            encrypted_content = f.read()
        decrypted_content = decrypt_file_content(encrypted_content)
        if not decrypted_content:
            raise ValueError("Failed to decrypt file content")
        files = {'document': (decrypted_filename, io.StringIO(decrypted_content))}
        data = {'chat_id': chat_id}
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument', files=files, data=data)
    except Exception as e:
        app.logger.error(f"Send document failed to chat {chat_id}: {e}")

@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if request.args.get('secret') != WEBHOOK_SECRET:
            abort(403)
        data = request.get_json(force=True) or {}
        update_id = str(data.get('update_id', ''))
        uid = None
        username = None
        if 'message' in data:
            uid = data['message']['from']['id']
            username = data['message']['from'].get('username', f'User_{uid}')
        elif 'callback_query' in data:
            uid = data['callback_query']['from']['id']
            username = data['callback_query']['from'].get('username', f'User_{uid}')

        if uid and username:
            try:
                user = User.query.get(uid)
                encrypted_username = encrypt_data(username)
                if not user:
                    user = User(id=uid, balance=0.0, role='user', username=encrypted_username)
                    db.session.add(user)
                else:
                    user.username = encrypted_username
                db.session.commit()
            except Exception as e:
                app.logger.error(f"Failed to create/update user {uid}: {e}")
                db.session.rollback()

            try:
                message = Message(
                    update_id=update_id,
                    user_id=uid,
                    raw_data=json.dumps(data, default=str),
                    timestamp=datetime.utcnow()
                )
                db.session.add(message)
                db.session.commit()
            except Exception as e:
                app.logger.error(f"Failed to record message for user {uid}: {e}")

        status = data.get('payment_status')
        if status in ('confirmed', 'partially_paid'):
            try:
                uid = int(str(data.get('order_id')).split('_')[0])
                btc_amt = float(data.get('pay_amount') or data.get('payment_amount') or 0)
                est = requests.get(
                    'https://api.nowpayments.io/v1/estimate',
                    params={'source_currency': 'BTC', 'target_currency': 'USD', 'source_amount': btc_amt},
                    headers={'x-api-key': NOWPAYMENTS_API_KEY}
                ).json()
                credits = float(est.get('estimated_amount', 0))
                update_balance(uid, credits)
                deposit = Deposit.query.get(data.get('order_id'))
                if deposit:
                    deposit.status = 'completed'
                    deposit.amount = credits
                    db.session.commit()
                send_message(uid, f'Your deposit has been credited as {credits:.2f} credits.')
            except Exception as e:
                app.logger.error(f"Payment processing failed for order {data.get('order_id')}: {e}")
            return '', 200

        if 'message' in data:
            msg = data['message']
            chat_id = msg['from']['id']
            username = msg['from'].get('username', f'User_{chat_id}')
            text = msg.get('text', '').strip()
            if deposit_requests.get(chat_id) == 'await_amount':
                try:
                    usd = float(text)
                    if usd < 25:
                        send_message(chat_id, 'Manual deposits are required for BTC load ups UNDER $25. Please contact @goatflow517.')
                        deposit_requests.pop(chat_id, None)
                        return '', 200
                    order_id = f'{chat_id}_{int(time.time())}'
                    inv, _ = create_invoice(usd, order_id)
                    if inv:
                        send_message(chat_id, f'Complete payment here:\n{inv}')
                    else:
                        send_message(chat_id, 'Failed to create invoice. Please try again.')
                except ValueError:
                    send_message(chat_id, 'Enter a valid number.')
                deposit_requests.pop(chat_id, None)
                return '', 200
            if text == '/start':
                welcome_message = f"HI @{username} Welcome To Nitro Bot, A Full Service shop for your FULLZ and CPN needs!\n" \
                                 f"We are steadily previewing new features and updates so be sure to check out our update channel https://t.me/+0DdVC1LxX5w2ZDVh\n\n" \
                                 f"If any assistance is needed please contact admin @goatflow517!\n\n" \
                                 f"Manual deposits are required for btc load ups UNDER 25$"
                buttons = [
                    [{'text': '💰 Deposit', 'callback_data': 'deposit'}],
                    [{'text': '📦 View Inventory', 'callback_data': 'buy_categories'}],
                    [{'text': '📊 Check Balance', 'callback_data': 'balance'}],
                    [{'text': '📰 Visit Update Channel', 'url': 'https://t.me/+0DdVC1LxX5w2ZDVh'}],
                    [{'text': '📞 Contact Admin', 'url': 'https://t.me/goatflow517'}],
                    [{'text': '🛒 Purchase History', 'callback_data': 'purchase_history'}],
                    [{'text': '🆔 View User ID', 'callback_data': 'view_user_id'}]
                ]
                if chat_id == ADMIN_ID:
                    buttons.append([{'text': '🔧 Admin', 'callback_data': 'admin'}])
                send_message(chat_id, welcome_message, buttons)
                return '', 200

        if 'callback_query' in data:
            cb = data['callback_query']
            chat_id = cb['from']['id']
            action = cb['data']
            answer_callback(cb['id'])
            if action == 'deposit':
                send_message(chat_id, 'Choose deposit method:', [[{'text': 'BTC', 'callback_data': 'deposit_btc'}],
                                                            [{'text': 'Manual Deposit', 'callback_data': 'deposit_manual'}]])
            elif action == 'deposit_btc':
                deposit_requests[chat_id] = 'await_amount'
                send_message(chat_id, 'Enter USD amount to deposit:')
            elif action == 'deposit_manual':
                send_message(chat_id, 'Please contact @goatflow517 for manual deposit.')
            elif action == 'admin':
                send_message(chat_id, f'Access the admin panel here:\n{BASE_URL}/admin')
            elif action == 'balance':
                bal = get_balance(chat_id)
                send_message(chat_id, f'Your balance: {bal:.2f} credits')
            elif action == 'view_user_id':
                send_message(chat_id, f'Your User ID: {chat_id}')
            elif action == 'purchase_history':
                history = get_purchase_history(chat_id)
                if not history:
                    send_message(chat_id, 'No purchase history found.')
                else:
                    msg = 'Your Purchase History:\n\n'
                    for name, price, timestamp in history:
                        msg += f'Product: {name}\nPrice: {price:.2f} credits\nDate: {timestamp}\n\n'
                    send_message(chat_id, msg)
            elif action.startswith('buy_categories'):
                send_message(chat_id, 'Choose category:', [[{'text': 'Fullz', 'callback_data': 'category_fullz'}],
                                                            [{'text': 'Fullz with CS', 'callback_data': 'category_fullz_cs'}],
                                                            [{'text': "CPN's", 'callback_data': 'category_cpn'}]])
            elif action.startswith('category_'):
                cats = {
                    'category_fullz': 'Fullz',
                    'category_fullz_cs': 'Fullz with CS',
                    'category_cpn': "CPN's"
                }
                cat = cats.get(action)
                prods = get_products(cat)
                if not prods:
                    send_message(chat_id, f'No products in {cat}.')
                else:
                    btns = [[{'text': f'{p.name} - {p.price:.2f} credits', 'callback_data': f'buy_{p.id}'}] for p in prods]
                    send_message(chat_id, f'Products in {cat}:', btns)
            elif action.startswith('buy_'):
                pid = int(action.split('_', 1)[1])
                bal = get_balance(chat_id)
                product = Product.query.get(pid)
                if not product:
                    send_message(chat_id, 'Product not found.')
                else:
                    if bal < product.price:
                        send_message(chat_id, 'Insufficient balance.')
                    else:
                        try:
                            update_balance(chat_id, -product.price)
                            sale = Sale(user_id=chat_id, product_id=pid, timestamp=datetime.utcnow())
                            db.session.add(sale)
                            db.session.commit()
                            send_document(chat_id, product.filename)
                            send_message(chat_id, f'You bought {product.name}!')
                        except Exception as e:
                            app.logger.error(f"Purchase failed for user {chat_id}, product {pid}: {e}")
                            db.session.rollback()
                            send_message(chat_id, 'Purchase failed. Please try again.')

        return '', 200
    except Exception as e:
        app.logger.error(f"Webhook processing failed: {e}")
        return '', 500

if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)