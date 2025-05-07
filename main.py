import os
import sqlite3
import time
import json
import logging
from datetime import datetime
from functools import wraps
from flask import Flask, request, abort, g
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
import requests
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, OWNER_ID

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Optional Rate Limiting ---
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    RATE_LIMITING = True
except ImportError:
    RATE_LIMITING = False
    logger.warning("flask_limiter not installed. Rate limiting disabled.")

# --- Flask & Database Setup ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.sqlite'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'change-me')
db = SQLAlchemy(app)

# Initialize rate limiter if available
if RATE_LIMITING:
    limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])
else:
    def limiter_limit(limit):
        def decorator(f):
            return f
        return decorator
    app.route = lambda path, **options: limiter_limit("100 per minute") if options.get('methods') == ['POST'] else limiter_limit("10 per minute")

db_path = 'database.sqlite'
FILE_DIR = 'files'

# Thread-safe deposit requests
from threading import Lock
deposit_requests = {}
deposit_lock = Lock()

# --- Models ---
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    balance = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(10), default='user')
    password_hash = db.Column(db.String(128), nullable=True)

class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    price = db.Column(db.Float)
    stock = db.Column(db.Integer, default=0)
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer)
    timestamp = db.Column(db.DateTime, default=db.func.now())

class Deposit(db.Model):
    order_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer)
    invoice_url = db.Column(db.String(512))
    status = db.Column(db.String(32), default='pending')
    timestamp = db.Column(db.DateTime, default=db.func.now())

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(64), unique=True)
    user_id = db.Column(db.Integer)
    raw_data = db.Column(db.Text)

# Database initialization
with app.app_context():
    try:
        db.create_all()
        default_categories = ['Fullz', 'Fullz with CS', "CPN's"]
        for cat in default_categories:
            if not Category.query.filter_by(name=cat).first():
                db.session.add(Category(name=cat))
        db.session.commit()
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

# --- Admin Panel ---
admin = Admin(app, name='Nitro Panel', template_mode='bootstrap4')

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not User.query.filter_by(id=auth.username, role='admin').first():
            return abort(401)
        return f(*args, **kwargs)
    return decorated

class UserAdmin(ModelView):
    column_list = ('id', 'balance', 'role')
    form_columns = ('id', 'balance', 'role', 'password_hash')
    can_create = False

    def on_model_change(self, form, model, is_created):
        if form.password_hash.data:
            model.password_hash = generate_password_hash(form.password_hash.data)

    @expose('/add_credits/', methods=('POST',))
    @admin_required
    def add_credits(self):
        try:
            user_id = request.form.get('user_id', type=int)
            amount = request.form.get('amount', type=float)
            with db.session.begin():
                user = User.query.get(user_id)
                if user:
                    user.balance += amount
                    db.session.commit()
                    logger.info(f"Added {amount} credits to user {user_id}")
                return self.redirect(self.get_url('useradmin.index_view'))
        except Exception as e:
            logger.error(f"Error adding credits: {e}")
            return abort(500)

class SalesReportView(BaseView):
    @expose('/')
    @admin_required
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
            stats = {label: db.session.query(Sale).filter(Sale.timestamp>=start).count()
                    for label, start in periods.items()}
            return self.render('admin/sales_report.html', stats=stats)
        except Exception as e:
            logger.error(f"Error generating sales report: {e}")
            return abort(500)

class BulkUploadView(BaseView):
    @expose('/', methods=('GET','POST'))
    @admin_required
    def index(self):
        msg = ''
        try:
            if request.method=='POST':
                text = request.form.get('bulk_text','').strip()
                count=0
                with db.session.begin():
                    for line in text.splitlines():
                        parts=line.split('|')
                        if len(parts)==4:
                            name,fn,price,cat_name=parts
                            category = Category.query.filter_by(name=cat_name.strip()).first()
                            if category:
                                prod=Product(
                                    name=name.strip(),
                                    filename=fn.strip(),
                                    price=float(price),
                                    category_id=category.id,
                                    stock=1
                                )
                                db.session.add(prod)
                                count+=1
                    db.session.commit()
                    msg=f'Imported {count} products.'
        except Exception as e:
            logger.error(f"Error in bulk upload: {e}")
            msg = 'Error importing products.'
        return self.render('admin/bulk_upload.html', message=msg)

admin.add_view(UserAdmin(User, db.session, endpoint='useradmin'))
admin.add_view(ModelView(Product, db.session, endpoint='product'))
admin.add_view(ModelView(Category, db.session, endpoint='category'))
admin.add_view(ModelView(Sale, db.session, endpoint='sale'))
admin.add_view(ModelView(Deposit, db.session, endpoint='deposit'))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(BulkUploadView(name='Bulk Upload', endpoint='bulk'))

# === Bot Logic ===
def safe_db_operation(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute('BEGIN')
                result = func(conn, *args, **kwargs)
                conn.commit()
                return result
        except Exception as e:
            logger.error(f"Database error in {func.__name__}: {e}")
            conn.rollback()
            raise
    return wrapper

@safe_db_operation
def get_balance(conn, user_id):
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (id,balance) VALUES (?,0)', (user_id,))
    c.execute('SELECT balance FROM users WHERE id=?', (user_id,))
    return c.fetchone()[0]

@safe_db_operation
def update_balance(conn, user_id, amount):
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (id,balance) VALUES (?,0)', (user_id,))
    c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))

@safe_db_operation
def get_products(conn, category_id=None):
    c = conn.cursor()
    query = 'SELECT id,name,price,stock FROM products WHERE stock > 0'
    params = ""
    if category_id:
        query += ' AND category_id=?'
        params = (category_id,)
    c.execute(query, params)
    return c.fetchall()

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
        logger.error(f"Error creating invoice: {e}")
        return None, None

def send_message(chat_id, text, buttons=None):
    try:
        payload = {'chat_id': chat_id, 'text': text}
        if buttons:
            payload['reply_markup'] = {'inline_keyboard': buttons}
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json=payload)
    except Exception as e:
        logger.error(f"Error sending message: {e}")

def answer_callback(cid):
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery', json={'callback_query_id': cid})
    except Exception as e:
        logger.error(f"Error answering callback: {e}")

def send_document(chat_id, filename):
    try:
        path = os.path.join(FILE_DIR, filename)
        with open(path, 'rb') as doc:
            files = {'document': doc}
            data = {'chat_id': chat_id}
            requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument', files=files, data=data)
    except Exception as e:
        logger.error(f"Error sending document: {e}")

@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.args.get('secret') != WEBHOOK_SECRET:
        logger.warning("Invalid webhook secret")
        abort(403)
    
    try:
        data = request.get_json(force=True) or {}
        update_id = data.get('update_id')
        uid = None
        if 'message' in data:
            uid = data['message']['from']['id']
        elif 'callback_query' in data:
            uid = data['callback_query']['from']['id']
        
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO messages(update_id,user_id,raw_data) VALUES(?,?,?)',
                     (str(update_id), uid, json.dumps(data)))
            conn.commit()

        status = data.get('payment_status')
        if status in ('confirmed', 'partially_paid'):
            uid = int(str(data.get('order_id')).split('_')[0])
            btc_amt = float(data.get('pay_amount') or data.get('payment_amount') or 0)
            est = requests.get('https://api.nowpayments.io/v1/estimate',
                             params={'source_currency': 'BTC', 'target_currency': 'USD', 'source_amount': btc_amt},
                             headers={'x-api-key': NOWPAYMENTS_API_KEY}).json()
            credits = float(est.get('estimated_amount', 0))
            update_balance(uid, credits)
            send_message(uid, f'Your deposit has been credited as {credits:.2f} credits.')
            return '', 200

        if 'message' in data:
            msg = data['message']
            chat_id = msg['from']['id']
            text = msg.get('text', '').strip()
            
            with deposit_lock:
                if deposit_requests.get(chat_id) == 'await_amount':
                    try:
                        usd = float(text)
                        if usd <= 0:
                            raise ValueError("Amount must be positive")
                        order_id = f'{chat_id}_{int(time.time())}'
                        inv, _ = create_invoice(usd, order_id)
                        if inv:
                            send_message(chat_id, f'Complete payment here:\n{inv}')
                        else:
                            send_message(chat_id, 'Error creating invoice. Please try again.')
                    except ValueError:
                        send_message(chat_id, 'Enter a valid positive number.')
                    deposit_requests.pop(chat_id, None)
                    return '', 200

            if text == '/start':
                buttons = [
                    [{'text': '💰 Deposit', 'callback_data': 'deposit'}],
                    [{'text': '📥 Buy Product', 'callback_data': 'buy_categories'}],
                    [{'text': '📊 Check Balance', 'callback_data': 'balance'}]
                ]
                if chat_id == OWNER_ID:
                    buttons.append([{'text': '🔧 Admin', 'callback_data': 'admin'}])
                send_message(chat_id, 'Welcome! Choose an option:', buttons)
                return '', 200

        if 'callback_query' in data:
            cb = data['callback_query']
            chat_id = cb['from']['id']
            action = cb['data']
            answer_callback(cb['id'])

            if action == 'deposit':
                send_message(chat_id, 'Choose deposit method:', [
                    [{'text': 'BTC', 'callback_data': 'deposit_btc'}],
                    [{'text': 'Manual Deposit', 'callback_data': 'deposit_manual'}]
                ])
            elif action == 'deposit_btc':
                with deposit_lock:
                    deposit_requests[chat_id] = 'await_amount'
                send_message(chat_id, 'Enter USD amount to deposit:')
            elif action == 'deposit_manual':
                send_message(chat_id, 'Please contact the admin @goatflow517 for manual deposits.')
            elif action == 'admin':
                send_message(chat_id, f'Access the admin panel here:\n{BASE_URL}/admin')
            elif action == 'balance':
                bal = get_balance(chat_id)
                send_message(chat_id, f'Your balance: {bal:.2f} credits')
            elif action == 'buy_categories':
                categories = Category.query.all()
                buttons = [[{'text': cat.name, 'callback_data': f'category_{cat.id}'}] for cat in categories]
                send_message(chat_id, 'Choose category:', buttons)
            elif action.startswith('category_'):
                cat_id = int(action.split('_')[1])
                prods = get_products(category_id=cat_id)
                buttons = [[{'text': f'{n} - {p:.2f} credits', 'callback_data': f'confirm_{i}'}]
                          for i, n, p, _ in prods]
                send_message(chat_id, 'Products:', buttons)
            elif action.startswith('confirm_'):
                pid = int(action.split('_')[1])
                with sqlite3.connect(db_path) as conn:
                    c = conn.cursor()
                    c.execute('SELECT name,filename,price,stock FROM products WHERE id=?', (pid,))
                    row = c.fetchone()
                if not row:
                    send_message(chat_id, 'Product not found.')
                    return '', 200
                name, fn, pr, stock = row
                if stock <= 0:
                    send_message(chat_id, 'Product out of stock.')
                    return '', 200
                bal = get_balance(chat_id)
                if bal < pr:
                    send_message(chat_id, 'Insufficient balance.')
                    return '', 200
                send_message(chat_id, f'Confirm purchase of {name} for {pr:.2f} credits?', [
                    [{'text': '✅ Confirm', 'callback_data': f'buy_{pid}'}],
                    [{'text': '❌ Cancel', 'callback_data': 'buy_categories'}]
                ])
            elif action.startswith('buy_'):
                pid = int(action.split('_')[1])
                with sqlite3.connect(db_path) as conn:
                    c = conn.cursor()
                    c.execute('SELECT name,filename,price,stock FROM products WHERE id=?', (pid,))
                    row = c.fetchone()
                    if not row:
                        send_message(chat_id, 'Product not found.')
                        return '', 200
                    name, fn, pr, stock = row
                    if stock <= 0:
                        send_message(chat_id, 'Product out of stock.')
                        return '', 200
                    bal = get_balance(chat_id)
                    if bal < pr:
                        send_message(chat_id, 'Insufficient balance.')
                        return '', 200
                    c.execute('UPDATE products SET stock=stock-1 WHERE id=?', (pid,))
                    c.execute('INSERT INTO sales (user_id,product_id) VALUES (?,?)', (chat_id, pid))
                    conn.commit()
                update_balance(chat_id, -pr)
                send_document(chat_id, fn)
                send_message(chat_id, f'You bought {name}!')
                logger.info(f"User {chat_id} purchased product {pid}")

        return '', 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return '', 500

if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))