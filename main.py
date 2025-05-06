# main.py

import os
import time
import json
import requests
from flask import Flask, request, abort, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from sqlalchemy.exc import IntegrityError
from config import (
    TELEGRAM_TOKEN,
    NOWPAYMENTS_API_KEY,
    WEBHOOK_SECRET,
    BASE_URL,
    ADMIN_ID,
    OWNER_ID,
)

# --- Flask & Database Setup ---
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.sqlite'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET', 'change-me')
db = SQLAlchemy(app)

# --- Models ---
class User(db.Model):
    __tablename__ = 'users'
    id      = db.Column(db.Integer, primary_key=True)
    balance = db.Column(db.Float,   default=0.0)
    role    = db.Column(db.String(10), default='user')  # owner, admin, seller, user

class Product(db.Model):
    __tablename__ = 'products'
    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(128))
    filename  = db.Column(db.String(256))
    price     = db.Column(db.Float)
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id'))

class Sale(db.Model):
    __tablename__ = 'sales'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer)
    product_id = db.Column(db.Integer)
    timestamp  = db.Column(db.DateTime, default=db.func.now())

class Deposit(db.Model):
    __tablename__ = 'deposits'
    order_id    = db.Column(db.String(64), primary_key=True)
    user_id     = db.Column(db.Integer)
    invoice_url = db.Column(db.String(512))
    status      = db.Column(db.String(32), default='pending')
    timestamp   = db.Column(db.DateTime, default=db.func.now())

class Message(db.Model):
    __tablename__ = 'messages'
    id        = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(64), unique=True)
    user_id   = db.Column(db.Integer)
    raw_data  = db.Column(db.Text)

# --- Create all tables ---
with app.app_context():
    db.create_all()

# --- Admin Interface ---
admin = Admin(app, name='Nitro Panel', template_mode='bootstrap4')

class UserAdmin(ModelView):
    column_list  = ('id','balance','role')
    form_columns = ('id','balance','role')
    can_create   = False

    @expose('/add_credits/', methods=('POST',))
    def add_credits(self):
        user_id = request.form.get('user_id', type=int)
        amount  = request.form.get('amount',  type=float)
        user = User.query.get(user_id)
        if user:
            user.balance += amount
            db.session.commit()
        return redirect(self.get_url('user.index'))

class SalesReportView(BaseView):
    @expose('/')
    def index(self):
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        periods = {
            'Daily':      now - timedelta(days=1),
            'Weekly':     now - timedelta(weeks=1),
            'Monthly':    now - timedelta(days=30),
            'Year-to-Date': now.replace(month=1, day=1),
        }
        stats = {
            label: Sale.query.filter(Sale.timestamp >= start).count()
            for label, start in periods.items()
        }
        return self.render('admin/sales_report.html', stats=stats)

class BulkUploadView(BaseView):
    @expose('/', methods=('GET','POST'))
    def index(self):
        msg = ''
        if request.method == 'POST':
            text  = request.form.get('bulk_text','')
            lines = text.strip().splitlines()
            count = 0
            for line in lines:
                parts = line.split('|')
                if len(parts)==3:
                    name, fn, price = parts
                    prod = Product(name=name.strip(),
                                   filename=fn.strip(),
                                   price=float(price))
                    db.session.add(prod)
                    count += 1
            db.session.commit()
            msg = f'Imported {count} products.'
        return self.render('admin/bulk_upload.html', message=msg)

admin.add_view(UserAdmin    (User,    db.session, endpoint='user'))
admin.add_view(ModelView    (Product, db.session, endpoint='product'))
admin.add_view(ModelView    (Sale,    db.session, endpoint='sale'))
admin.add_view(ModelView    (Deposit, db.session, endpoint='deposit'))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(BulkUploadView(name='Bulk Upload', endpoint='bulk'))

# === Telegram Bot Logic ===
deposit_requests = {}

def get_or_create_user(user_id):
    u = User.query.get(user_id)
    if not u:
        u = User(id=user_id, balance=0.0)
        db.session.add(u)
        db.session.commit()
    return u

def get_balance(user_id):
    return get_or_create_user(user_id).balance

def update_balance(user_id, amount):
    u = get_or_create_user(user_id)
    u.balance += amount
    db.session.commit()

def get_products():
    return Product.query.all()

def create_invoice(usd_amount, order_id):
    r = requests.post(
        'https://api.nowpayments.io/v1/invoice',
        json={
            'price_amount':   usd_amount,
            'price_currency': 'usd',
            'pay_currency':   'btc',
            'order_id':       order_id,
            'ipn_callback_url': f'{BASE_URL}/webhook?secret={WEBHOOK_SECRET}',
            'is_fixed_rate':  True
        },
        headers={'x-api-key': NOWPAYMENTS_API_KEY}
    ).json()
    return r.get('invoice_url'), r

def send_message(chat_id, text, buttons=None):
    payload = {'chat_id': chat_id, 'text': text}
    if buttons:
        payload['reply_markup'] = {'inline_keyboard': buttons}
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                  json=payload)

def answer_callback(callback_id):
    requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery',
        json={'callback_query_id': callback_id}
    )

@app.route('/', methods=['GET'])
def index():
    return 'OK', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.args.get('secret') != WEBHOOK_SECRET:
        abort(403)

    data     = request.get_json(force=True) or {}
    update_id= data.get('update_id')
    uid      = None
    if 'message' in data:
        uid = data['message']['from']['id']
    elif 'callback_query' in data:
        uid = data['callback_query']['from']['id']

    # --- log every update, ignore duplicates ---
    if update_id is not None:
        try:
            msg = Message(
                update_id=str(update_id),
                user_id=uid,
                raw_data=json.dumps(data)
            )
            db.session.add(msg)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()

    # --- handle IPN ---
    status = data.get('payment_status')
    if status in ('confirmed','partially_paid'):
        # order_id is e.g. "1217831346_1746472986"
        order = data.get('order_id','').split('_')
        payer = int(order[0]) if order else None
        btc_amt = float(data.get('pay_amount') or data.get('payment_amount') or 0)
        # convert BTC→USD credits
        est = requests.get(
            'https://api.nowpayments.io/v1/estimate',
            params={'source_currency':'BTC','target_currency':'USD','source_amount':btc_amt},
            headers={'x-api-key': NOWPAYMENTS_API_KEY}
        ).json()
        credits = float(est.get('estimated_amount',0))
        update_balance(payer, credits)
        send_message(payer, f'Your deposit has been credited as {credits:.2f} credits.')
        return '', 200

    # --- user text messages ---
    if 'message' in data:
        m = data['message']
        cid = m['from']['id']
        txt = m.get('text','').strip()

        # in-flow deposit amount entry?
        if deposit_requests.get(cid) == 'await_amount':
            try:
                usd = float(txt)
                oid = f'{cid}_{int(time.time())}'
                inv_url, _ = create_invoice(usd, oid)
                send_message(cid, f'Complete payment here:\n{inv_url}')
            except ValueError:
                send_message(cid, 'Enter a valid number.')
            deposit_requests.pop(cid, None)
            return '', 200

        if txt == '/start':
            buttons = [
                [{'text':'💰 Deposit',    'callback_data':'deposit'}],
                [{'text':'📥 Buy Product','callback_data':'buy_categories'}],
                [{'text':'📊 Check Balance','callback_data':'balance'}],
            ]
            if cid == OWNER_ID:
                buttons.append([{'text':'🔧 Admin','callback_data':'admin'}])
            send_message(cid, 'Welcome! Choose an option:', buttons)
            return '', 200

    # --- callback queries ---
    if 'callback_query' in data:
        cb     = data['callback_query']
        cid    = cb['from']['id']
        action = cb['data']
        answer_callback(cb['id'])

        if action == 'deposit':
            btns = [
                [{'text':'BTC','callback_data':'deposit_btc'}],
                [{'text':'Manual Deposit','callback_data':'deposit_manual'}],
            ]
            send_message(cid, 'Choose deposit method:', btns)

        elif action == 'deposit_btc':
            deposit_requests[cid] = 'await_amount'
            send_message(cid, 'Enter USD amount to deposit:')

        elif action == 'deposit_manual':
            send_message(cid, 'Please contact the admin at @goatflow517 for manual deposits.')

        elif action == 'balance':
            bal = get_balance(cid)
            send_message(cid, f'Your balance: {bal:.2f} credits')

        elif action == 'admin':
            send_message(cid, f'Access the admin panel here:\n{BASE_URL}/admin')

        elif action == 'buy_categories':
            cats = [
                [{'text':'Fullz','callback_data':'category_fullz'}],
                [{'text':'Fullz with CS','callback_data':'category_fullz_cs'}],
                [{'text':"CPN's",'callback_data':'category_cpn'}],
            ]
            send_message(cid, 'Choose category:', cats)

        elif action.startswith('category_'):
            prods = get_products()
            btns = [[{'text':f'{p.name} — {p.price:.2f} credits','callback_data':f'buy_{p.id}'}]
                    for p in prods]
            send_message(cid, 'Products:', btns)

        elif action.startswith('buy_'):
            pid = int(action.split('_',1)[1])
            bal = get_balance(cid)
            p   = Product.query.get(pid)
            if not p:
                send_message(cid, 'Product not found.')
            elif bal < p.price:
                send_message(cid, 'Insufficient balance.')
            else:
                update_balance(cid, -p.price)
                # log sale
                s = Sale(user_id=cid, product_id=pid)
                db.session.add(s)
                db.session.commit()
                # deliver file
                with open(os.path.join('files', p.filename),'rb') as f:
                    requests.post(
                        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument',
                        files={'document': f},
                        data={'chat_id': cid}
                    )
                send_message(cid, f'You bought {p.name}!')

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
