import os
import io
import logging
from flask import Flask, request, Response, jsonify, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from sqlalchemy.sql import func
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from dotenv import load_dotenv
import requests

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask & Database setup
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "default-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "postgresql://localhost/nitro_bot").replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["FLASK_ADMIN_SWATCH"] = "cerulean"
db = SQLAlchemy(app)

# Environment variables
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID", "123456789")

# Encryption functions
def encrypt_data(data):
    if not ENCRYPTION_KEY:
        return data
    try:
        fernet = Fernet(ENCRYPTION_KEY.encode())
        return fernet.encrypt(data.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return data

def decrypt_data(encrypted_data):
    if not ENCRYPTION_KEY or not encrypted_data:
        return encrypted_data
    try:
        fernet = Fernet(ENCRYPTION_KEY.encode())
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return encrypted_data

# Database Models
class User(db.Model):
    id = db.Column(db.BigInteger, primary_key=True)
    balance = db.Column(db.Float, default=0.0)
    role = db.Column(db.String(50), default="user")
    username = db.Column(db.Text)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    file_path = db.Column(db.String(200))
    description = db.Column(db.Text)

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.BigInteger, nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.BigInteger, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default="pending")
    payment_id = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(db.String(100))
    user_id = db.Column(db.BigInteger)
    raw_data = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

# Admin Views
class UserModelView(ModelView):
    column_list = ['id', 'balance', 'role', 'username']
    column_searchable_list = ['id', 'role']
    column_filters = ['role', 'balance']
    
    def get_query(self):
        return self.session.query(self.model)
    
    def get_count_query(self):
        return self.session.query(func.count('*')).select_from(self.model)

class ProductModelView(ModelView):
    column_list = ['id', 'name', 'price', 'category', 'description']
    form_excluded_columns = ['file_path']

class PurchaseModelView(ModelView):
    column_list = ['id', 'user_id', 'product_id', 'price', 'timestamp']
    column_filters = ['user_id', 'product_id', 'timestamp']

class PaymentModelView(ModelView):
    column_list = ['id', 'user_id', 'amount', 'status', 'timestamp']
    column_filters = ['status', 'user_id', 'timestamp']

class MessageModelView(ModelView):
    column_list = ['id', 'user_id', 'timestamp']
    column_filters = ['user_id', 'timestamp']

# Custom Admin Views
class DashboardView(BaseView):
    @expose('/')
    def index(self):
        total_users = User.query.count()
        total_revenue = db.session.query(func.sum(Purchase.price)).scalar() or 0
        pending_payments = Payment.query.filter_by(status='pending').count()
        recent_purchases = Purchase.query.order_by(Purchase.timestamp.desc()).limit(10).all()
        
        return self.render('admin/dashboard.html',
                         total_users=total_users,
                         total_revenue=total_revenue,
                         pending_payments=pending_payments,
                         recent_purchases=recent_purchases)

class SalesReportView(BaseView):
    @expose('/')
    def index(self):
        # Daily sales for last 30 days
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        daily_sales = db.session.query(
            func.date(Purchase.timestamp),
            func.sum(Purchase.price),
            func.count(Purchase.id)
        ).filter(Purchase.timestamp >= thirty_days_ago).group_by(func.date(Purchase.timestamp)).all()
        
        return self.render('admin/sales_report.html', daily_sales=daily_sales)

class AddCreditsView(BaseView):
    @expose('/', methods=['GET', 'POST'])
    def index(self):
        if request.method == 'POST':
            user_id = request.form.get('user_id')
            amount = float(request.form.get('amount', 0))
            
            user = User.query.get(user_id)
            if user:
                user.balance += amount
                db.session.commit()
                flash(f'Added {amount} credits to user {user_id}', 'success')
            else:
                flash('User not found', 'error')
        
        return self.render('admin/add_credits.html')

# Initialize Flask-Admin
admin = Admin(app, name='Nitro Bot Admin', template_mode='bootstrap3')
admin.add_view(DashboardView(name='Dashboard', endpoint='dashboard'))
admin.add_view(UserModelView(User, db.session))
admin.add_view(ProductModelView(Product, db.session))
admin.add_view(PurchaseModelView(Purchase, db.session))
admin.add_view(PaymentModelView(Payment, db.session))
admin.add_view(MessageModelView(Message, db.session))
admin.add_view(SalesReportView(name='Sales Report', endpoint='sales'))
admin.add_view(AddCreditsView(name='Add Credits', endpoint='credits'))

# Routes
@app.route("/")
def index():
    return '''
    <h1>Nitro Bot Web Admin</h1>
    <p>Admin panel is running!</p>
    <p><a href="/admin">Go to Admin Panel</a></p>
    '''

@app.route("/webhook/payment", methods=["POST"])
def payment_webhook():
    data = request.get_json()
    if not data:
        logger.error("Invalid webhook data received")
        return Response("Invalid data", status=400)
    
    try:
        payment_id = data.get("payment_id")
        status = data.get("payment_status")
        
        payment = Payment.query.filter_by(payment_id=payment_id).first()
        if payment:
            payment.status = status
            if status == "finished":
                user = User.query.get(payment.user_id)
                if user:
                    user.balance += payment.amount
            db.session.commit()
            logger.info(f"Payment {payment_id} updated to {status}")
        
        return Response("OK", status=200)
    except Exception as e:
        logger.error(f"Payment webhook failed: {e}")
        return Response(str(e), status=500)

# Initialize database
def init_db():
    try:
        with app.app_context():
            db.create_all()
            if not Settings.query.first():
                default_setting = Settings(key="initialized", value="true")
                db.session.add(default_setting)
                db.session.commit()
                logger.info("Database initialized with default settings")
            logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("WEB_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)