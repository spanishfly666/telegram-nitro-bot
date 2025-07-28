import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Your bot token from .env file
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

async def start(update: Update, context):
    chat_id = update.message.chat_id
    username = update.message.from_user.username or f"User_{chat_id}"
    welcome_message = (
        f"HI {username} Welcome To Nitro Bot, A Full Service shop for your FULLZ and CPN needs!\n"
        f"We are steadily previewing new features and updates so be sure to check out our update channel https://t.me/+0DdVC1LxX5w2ZDVh\n\n"
        f"If any assistance is needed please contact admin!\n\n"
        f"Manual deposits are required for BTC load ups UNDER 25$"
    )
    keyboard = [
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
        [InlineKeyboardButton("🛒 View Inventory", callback_data="buy_categories")],
        [InlineKeyboardButton("💵 Check Balance", callback_data="balance")],
        [InlineKeyboardButton("📜 Purchase History", callback_data="purchase_history")],
        [InlineKeyboardButton("🆔 View User ID", callback_data="view_user_id")],
        [InlineKeyboardButton("📢 Visit Update Channel", url="https://t.me/+0DdVC1LxX5w2ZDVh")],
        [InlineKeyboardButton("📞 Contact Admin", url="https://t.me/goatflow517")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_message, reply_markup=reply_markup)

async def handle_callback(update: Update, context):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        print(f"Error answering callback: {e}")
    
    chat_id = query.from_user.id
    action = query.data

    try:
        if action == "deposit":
            keyboard = [
                [InlineKeyboardButton("BTC", callback_data="deposit_btc")],
                [InlineKeyboardButton("Manual Deposit", callback_data="deposit_manual")]
            ]
            await query.message.reply_text("Choose deposit method:", reply_markup=InlineKeyboardMarkup(keyboard))
        elif action == "deposit_btc":
            await query.message.reply_text("Enter USD amount to deposit:")
        elif action == "deposit_manual":
            await query.message.reply_text("Please contact admin for manual deposit.")
        elif action == "balance":
            await query.message.reply_text("Your balance: 0.00 credits (demo mode)")
        elif action == "view_user_id":
            await query.message.reply_text(f"Your User ID: {chat_id}")
        elif action == "purchase_history":
            await query.message.reply_text("No purchase history found (demo mode).")
        elif action == "buy_categories":
            await query.message.reply_text("Inventory feature coming soon (demo mode).")
    except Exception as e:
        print(f"Error handling callback {action}: {e}")
        await query.message.reply_text("Sorry, there was an error processing your request.")

async def handle_message(update: Update, context):
    await update.message.reply_text("Please use /start to begin or use the menu buttons.")

def main():
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN not found in .env file!")
        print("Create a .env file with: TELEGRAM_TOKEN=your_bot_token_from_botfather")
        return
    
    # Create application
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Starting bot...")
    print("Press Ctrl+C to stop")
    
    # Run with polling
    app.run_polling()

if __name__ == "__main__":
    main() 