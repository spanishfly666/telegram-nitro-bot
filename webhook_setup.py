import os
import requests

# Only load .env locally, not on Heroku
if not os.getenv("DYNO"):  # DYNO is set on Heroku
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception as e:
        print(f"Warning: Failed to load .env file: {e}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("BASE_URL") + "/webhook" if os.getenv("BASE_URL") else None
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

def set_telegram_webhook():
    if not all([TELEGRAM_TOKEN, WEBHOOK_URL, WEBHOOK_SECRET]):
        print("Error: Missing required environment variables (TELEGRAM_TOKEN, BASE_URL, WEBHOOK_SECRET)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": WEBHOOK_URL,
        "secret_token": WEBHOOK_SECRET
    }
    try:
        response = requests.post(url, json=payload)
        print(response.json())
        if response.json().get("ok"):
            print("Webhook set successfully")
        else:
            print("Failed to set webhook")
    except Exception as e:
        print(f"Error setting webhook: {e}")

if __name__ == "__main__":
    set_telegram_webhook()