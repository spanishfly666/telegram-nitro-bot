import os
import sqlite3
import time
import json
import requests
from flask import Flask, request, abort
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from flask_sqlalchemy import SQLAlchemy
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID

# --- Flask & Database Setup ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.sqlite'
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

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128))
    filename = db.Column(db.String(256))
    price = db.Column(db.Float)
    category = db.Column(db.String(50))
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id'))

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

# --- Ensure Tables Exist ---
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        app.logger.error(f"db.create_all() failed: {e}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # users
    c.execute(
        '''CREATE TABLE IF NOT EXISTS users (
               id INTEGER PRIMARY KEY,
               balance REAL,
               role TEXT
           );'''
    )
    # products
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
    # messages
    c.execute(
        '''CREATE TABLE IF NOT EXISTS messages (
               id INTEGER PRIMARY KEY,
               update_id TEXT UNIQUE,
               user_id INTEGER,
               raw_data TEXT
           );'''
    )
    conn.commit()
    conn.close()

# --- Admin Panel ---
admin = Admin(app, name='Nitro Panel', template_mode='bootstrap4')

class UserAdmin(ModelView):
    column_list = ('id', 'balance', 'role')
    form_columns = ('id', 'balance', 'role')
    can_create = False

    @expose('/deposit/', methods=['GET', 'POST'])
    def deposit_view(self):
        msg = ''
        if request.method == 'POST':
            uid = request.form.get('user_id', type=int)
            amt = request.form.get('amount', type=float)
            user = User.query.get(uid)
            if user and amt and amt > 0:
                user.balance += amt
                db.session.commit()
                msg = f'Deposited {amt:.2f} credits to user {uid}.'
            else:
                msg = 'Invalid user or amount.'
        return self.render('admin/deposit.html', message=msg)

class SalesReportView(BaseView):
    @expose('/')
    def index(self):
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

class BulkUploadView(BaseView):
    @expose('/', methods=['GET', 'POST'])
    def index(self):
        msg = ''
        categories = ['Fullz', 'Fullz with CS', "CPN's"]
        if request.method == 'POST':
            text = request.form.get('bulk_text', '').strip()
            cat = request.form.get('category', '')
            if cat not in categories:
                msg = 'Select a valid category.'
            else:
                count = 0
                for line in text.splitlines():
                    parts = line.split('|')
                    if len(parts) == 3:
                        name, fn, price = parts
                        prod = Product(name=name.strip(), filename=fn.strip(), price=float(price), category=cat)
                        db.session.add(prod)
                        count += 1
                db.session.commit()
                msg = f'Imported {count} products into {cat}.'
        return self.render('admin/bulk_upload.html', message=msg, categories=categories)

admin.add_view(UserAdmin(User, db.session, endpoint='useradmin'))
admin.add_view(ModelView(Product, db.session, endpoint='product'))
admin.add_view(ModelView(Sale, db.session, endpoint='sale'))
admin.add_view(ModelView(Deposit, db.session, endpoint='deposit'))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(BulkUploadView(name='Bulk Upload', endpoint='bulk'))

# --- Bot Logic ---
def get_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users(id, balance) VALUES(?, 0)', (user_id,))
    c.execute('SELECT balance FROM users WHERE id=?', (user_id,))
    bal = c.fetchone()[0]
    conn.close()
    return bal

def update_balance(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users(id, balance) VALUES(?, 0)', (user_id,))
    c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
    conn.commit()
    conn.close()

def get_products(category=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if category:
        c.execute('SELECT id, name, price FROM products WHERE category=?', (category,))
    else:
        c.execute('SELECT id, name, price FROM products')
    items = c.fetchall()
    conn.close()
    return items

def create_invoice(usd_amount, order_id):
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

def send_message(chat_id, text, buttons=None):
    payload = {'chat_id': chat_id, 'text': text}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json=payload)

def answer_callback(cid):
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery',
                  json={'callback_query_id': cid})

def send_document(chat_id, filename):
    path = os.path.join(FILE_DIR, filename)
    with open(path, 'rb') as doc:
        files = {'document': doc}
        data = {'chat_id': chat_id}
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument', files=files, data=data)

@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.args.get('secret') != WEBHOOK_SECRET:
        abort(403)
    data = request.get_json(force=True) or {}
    update_id = data.get('update_id')
    uid = None
    if 'message' in data:
        uid = data['message']['from']['id']
    elif 'callback_query' in data:
        uid = data['callback_query']['from']['id']

    # log raw update
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT OR IGNORE INTO messages(update_id, user_id, raw_data) VALUES (?, ?, ?)',
        (str(update_id), uid, json.dumps(data))
    )
    conn.commit()
    conn.close()

    # handle IPN
    status = data.get('payment_status')
    if status in ('confirmed', 'partially_paid'):
        uid = int(str(data.get('order_id')).split('_')[0])
        btc_amt = float(data.get('pay_amount') or data.get('payment_amount') or 0)
        est = requests.get(
            'https://api.nowpayments.io/v1/estimate',
            params={'source_currency': 'BTC', 'target_currency': 'USD', 'source_amount': btc_amt},
            headers={'x-api-key': NOWPAYMENTS_API_KEY}
        ).json()
        credits = float(est.get('estimated_amount', 0))
        update_balance(uid, credits)
        send_message(uid, f'Your deposit has been credited as {credits:.2f} credits.')
        return '', 200

    # user messages
    if 'message' in data:
        msg = data['message']
        chat_id = msg['from']['id']
        text = msg.get('text', '').strip()
        if deposit_requests.get(chat_id) == 'await_amount':
            try:
                usd = float(text)
                order_id = f'{chat_id}_{int(time.time())}'
                inv, _ = create_invoice(usd, order_id)
                send_message(chat_id, f'Complete payment here:\n{inv}')
            except ValueError:
                send_message(chat_id, 'Enter a valid number.')
            deposit_requests.pop(chat_id, None)
            return '', 200
        if text == '/start':
            buttons = [
                [{'text': '💰 Deposit', 'callback_data': 'deposit'}],
                [{'text': '📥 Buy Product', 'callback_data': 'buy_categories'}],
                [{'text': '📊 Check Balance', 'callback_data': 'balance'}]
            ]
            if chat_id == ADMIN_ID:
                buttons.append([{'text': '🔧 Admin', 'callback_data': 'admin'}])
            send_message(chat_id, 'Welcome! Choose an option:', buttons)
            return '', 200

    # callback queries
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
            send_message(chat_id, 'Please contact the admin.')
        elif action == 'admin':
            send_message(chat_id, f'Access the admin panel here:\n{BASE_URL}/admin')
        elif action == 'balance':
            bal = get_balance(chat_id)
            send_message(chat_id, f'Your balance: {bal:.2f} credits')
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
                btns = [[{'text': f'{n} - {p:.2f} credits', 'callback_data': f'buy_{i}'}] for i, n, p in prods]
                send_message(chat_id, f'Products in {cat}:', btns)
        elif action.startswith('buy_'):
            pid = int(action.split('_', 1)[1])
            bal = get_balance(chat_id)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('SELECT name, filename, price FROM products WHERE id=?', (pid,))
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
    os.makedirs(FILE_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))