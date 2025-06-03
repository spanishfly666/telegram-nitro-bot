import os
     import requests
     from dotenv import load_dotenv

     # Load environment variables (skip .env on Heroku)
     if not os.getenv("HEROKU"):
       load_dotenv()

     TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
     WEBHOOK_URL = os.getenv("BASE_URL") + "/webhook"
     WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

     def set_telegram_webhook():
       if not TELEGRAM_TOKEN or not WEBHOOK_URL or not WEBHOOK_SECRET:
         print("Error: Required environment variables missing")
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