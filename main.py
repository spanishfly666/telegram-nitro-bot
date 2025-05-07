import os
import sqlite3
import time
import json
import requests
from flask import Flask, request, abort, render_template
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from flask_sqlalchemy import SQLAlchemy
from config import TELEGRAM_TOKEN, NOWPAYMENTS_API_KEY, WEBHOOK_SECRET, BASE_URL, ADMIN_ID, OWNER_ID

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
    username = db.Column(db.String(50), nullable=True)

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
               raw_data TEXT
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
            # Sales stats
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

            # User stats
            total_users = 0
            recent_uploads = []
            try:
                total_users = db.session.query(User).count()
                recent_uploads = db.session.query(Product).order_by(Product.id.desc()).limit(5).all()
            except Exception as e:
                app.logger.error(f"User stats query failed: {e}")

            # Product stats
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
    column_list = ('id', 'username', 'balance', 'role')
    form_columns = ('id', 'username', 'balance', 'role')
    can_create = False

    @expose('/deposit/', methods=['GET', 'POST'])
    def deposit_view(self):
        msg = ''
        error = False
        if request.method == 'POST':
            uid = request.form.get('user_id', type=int)
            amt = request.form.get('amount', type=float)
            user = User.query.get(uid)
            if user and amt and amt > 0:
                try:
                    user.balance += amt
                    db.session.commit()
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute(
                        'INSERT INTO deposits (order_id, user_id, invoice_url, status, timestamp) VALUES (?, ?, ?, ?, ?)',
                        (f'manual_{uid}_{int(time.time())}', uid, 'manual_deposit', 'completed', db.func.now())
                    )
                    conn.commit()
                    conn.close()
                    msg = f'Successfully deposited {amt:.2f} credits to user ID {uid}.'
                    try:
                        send_message(uid, f'Your account has been credited with {amt:.2f} credits via manual deposit.')
                    except Exception as e:
                        app.logger.error(f"Failed to send Telegram message: {e}")
                        msg += ' (Note: Failed to notify user via Telegram)'
                except Exception as e:
                    app.logger.error(f"Deposit processing failed: {e}")
                    msg = 'Failed to process deposit. Please try again.'
                    error = True
            else:
                msg = 'Invalid user ID or amount. Please ensure the user ID exists and the amount is positive.'
                error = True
        return self.render('admin/deposit.html', message=msg, error=error)

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

class BulkUploadView(BaseView):
    @expose('/', methods=['GET', 'POST'])
    def index(self):
        msg = ''
        error = False
        categories = ['Fullz', 'Fullz with CS', "CPN's"]
        if request.method == 'POST':
            text = request.form.get('bulk_text', '').strip()
            cat = request.form.get('category', '')
            if cat not in categories:
                msg = 'Select a valid category.'
                error = True
            else:
                try:
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
                except Exception as e:
                    app.logger.error(f"Bulk upload failed: {e}")
                    msg = 'Failed to import products. Please check the format and try again.'
                    error = True
        return self.render('admin/bulk_upload.html', message=msg, categories=categories, error=error)

admin.add_view(DashboardView(name='Dashboard', endpoint='dashboard'))
admin.add_view(UserAdmin(User, db.session, endpoint='useradmin'))
admin.add_view(ModelView(Product, db.session, endpoint='product'))
admin.add_view(ModelView(Sale, db.session, endpoint='sale'))
admin.add_view(ModelView(Deposit, db.session, endpoint='deposit'))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(BulkUploadView(name='Bulk Upload', endpoint='bulk'))

# --- Bot Logic ---
def get_balance(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users(id, balance, username) VALUES(?, 0, NULL)', (user_id,))
        c.execute('SELECT balance FROM users WHERE id=?', (user_id,))
        bal = c.fetchone()[0]
        conn.close()
        return bal
    except Exception as e:
        app.logger.error(f"Get balance failed for user {user_id}: {e}")
        return 0.0

def update_balance(user_id, amount):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users(id, balance, username) VALUES(?, 0, NULL)', (user_id,))
        c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        app.logger.error(f"Update balance failed for user {user_id}: {e}")

def get_products(category=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if category:
            c.execute('SELECT id, name, price FROM products WHERE category=?', (category,))
        else:
            c.execute('SELECT id, name, price FROM products')
        items = c.fetchall()
        conn.close()
        return items
    except Exception as e:
        app.logger.error(f"Get products failed: {e}")
        return []

def get_purchase_history(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT p.name, p.price, s.timestamp
            FROM sales s
            JOIN products p ON s.product_id = p.id
            WHERE s.user_id = ?
            ORDER BY s.timestamp DESC
        ''', (user_id,))
        history = c.fetchall()
        conn.close()
        return history
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
        path = os.path.join(FILE_DIR, filename)
        with open(path, 'rb') as doc:
            files = {'document': doc}
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
        update_id = data.get('update_id')
        uid = None
        username = None
        if 'message' in data:
            uid = data['message']['from']['id']
            username = data['message']['from'].get('username', 'User')
        elif 'callback_query' in data:
            uid = data['callback_query']['from']['id']
            username = data['callback_query']['from'].get('username', 'User')

        if uid and username:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO users(id, balance, username) VALUES(?, 0, ?)', (uid, username))
            c.execute('UPDATE users SET username=? WHERE id=?', (username, uid))
            conn.commit()
            conn.close()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            'INSERT OR IGNORE INTO messages(update_id, user_id, raw_data) VALUES (?, ?, ?)',
            (str(update_id), uid, json.dumps(data))
        )
        conn.commit()
        conn.close()

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

        if 'message' in data:
            msg = data['message']
            chat_id = msg['from']['id']
            username = msg['from'].get('username', 'User')
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
    except Exception as e:
        app.logger.error(f"Webhook processing failed: {e}")
        return '', 500

if __name__ == '__main__':
    os.makedirs(FILE_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)