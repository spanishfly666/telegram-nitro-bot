import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("BASE_URL") + "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

def set_telegram_webhook():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": WEBHOOK_URL,
        "secret_token": WEBHOOK_SECRET
    }
    response = requests.post(url, json=payload)
    print(response.json())
    if response.json().get("ok"):
        print("Webhook set successfully")
    else:
        print("Failed to set webhook")

if __name__ == "__main__":
    set_telegram_webhook()