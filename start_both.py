#!/usr/bin/env python3

import subprocess
import sys
import time
import signal
import os

def start_bot():
    """Start the Telegram bot"""
    print("🤖 Starting Telegram bot...")
    return subprocess.Popen([sys.executable, "main.py"])

def start_web():
    """Start the web admin panel"""
    print("🌐 Starting web admin panel...")
    return subprocess.Popen([sys.executable, "web_admin.py"])

def main():
    bot_process = None
    web_process = None
    
    try:
        # Start both processes
        bot_process = start_bot()
        time.sleep(2)  # Give bot time to start
        web_process = start_web()
        
        print("\n✅ Both services started!")
        print("🤖 Bot: Running with polling")
        print("🌐 Web Admin: http://localhost:5000/admin")
        print("\nPress Ctrl+C to stop both services...")
        
        # Wait for processes
        while True:
            # Check if processes are still running
            if bot_process.poll() is not None:
                print("❌ Bot process died, restarting...")
                bot_process = start_bot()
            
            if web_process.poll() is not None:
                print("❌ Web process died, restarting...")
                web_process = start_web()
            
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\n🛑 Stopping services...")
        
        if bot_process:
            bot_process.terminate()
            bot_process.wait()
            print("🤖 Bot stopped")
        
        if web_process:
            web_process.terminate()
            web_process.wait()
            print("🌐 Web admin stopped")
        
        print("✅ All services stopped")

if __name__ == "__main__":
    main()