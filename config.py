import os

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
NOWPAYMENTS_API_KEY  = os.getenv("NOWPAYMENTS_API_KEY")
WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET")
BASE_URL             = os.getenv("BASE_URL")

# use the ADMIN_TELEGRAM_ID var you already configured
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "1217831346"))

# main.py only imports OWNER_ID, so point that at the same:
OWNER_ID = ADMIN_ID
