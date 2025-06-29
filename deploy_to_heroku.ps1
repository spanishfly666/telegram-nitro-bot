# Navigate to project directory
cd C:\Users\PLAIN\Desktop\telegram_nitro_bot_package

# Ensure LF line endings for all text files to avoid Heroku issues
Get-ChildItem -Recurse -Include *.html,*.css,*.py,*.txt | ForEach-Object {
    (Get-Content $_.FullName -Raw) -replace "`r`n", "`n" | Set-Content $_.FullName
}

# Stage all changes (add all modified, new, and deleted files)
git add .

# Commit changes with a message
git commit -m "Updated all local files to Heroku, including new admin/master.html and templates"

# Install Heroku builds plugin if not already installed
heroku plugins:install heroku-builds

# Clear Heroku build cache to prevent stale files
heroku builds:cache:purge -a telegram-nitro-bot

# Push to Heroku (force push to overwrite any cached files)
git push heroku master --force

# Monitor Heroku logs to confirm deployment
heroku logs --tail -a telegram-nitro-bot

# Reset Telegram webhook
$env:TELEGRAM_TOKEN = (Select-String -Path .env -Pattern "TELEGRAM_TOKEN").Line.Split("=")[1]
$env:WEBHOOK_URL = "https://telegram-nitro-bot-3b1ee249dc12.herokuapp.com/webhook?secret=1a2d3k4j5h6ntb596yjt"
Invoke-RestMethod -Uri "https://api.telegram.org/bot$env:TELEGRAM_TOKEN/setWebhook?url=$env:WEBHOOK_URL" -Method Post

# Verify deployed files on Heroku
heroku run bash -a telegram-nitro-botc d  
 C : \ U s e r s \ P L A I N \ D e s k t o p \ t e l e g r a m _ n i t r o _ b o t _ p a c k a g e  
 