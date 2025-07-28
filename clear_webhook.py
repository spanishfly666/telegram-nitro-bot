import os
from telegram import Bot
from dotenv import load_dotenv
import asyncio

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

async def clear_webhook():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Webhook cleared and pending updates dropped!")

if __name__ == "__main__":
    asyncio.run(clear_webhook()) 