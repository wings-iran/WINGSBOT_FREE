🤖 ربات فروش و مدیریت سرویس

این ربات برای مدیریت فروش و سرویس‌ها طراحی شده است. سعی شده مراحل نصب و راه‌اندازی ساده و شفاف توضیح داده شود تا هر کسی بتواند بدون پیچیدگی از آن استفاده کند.


---

✨ امکانات

پشتیبانی از پنل‌های مرزبان / مرزنشین / x_ui / 3x_ui / tx_ui / علیرضا

خرید پلن، تخفیف، پرداخت (درگاه / کارت / کریپتو)

ارسال رسید برای تایید ادمین

تمدید سرویس + دریافت لینک ساب

کیف‌پول کاربر (شارژ و مدیریت تراکنش‌ها)

تیکت پشتیبانی + آموزش‌ها + معرفی دوستان

پنل ادمین کامل


🔒 امکانات ویژه نسخه غیررایگان:

بکاپ‌گیری

ارسال پیام یادآوری تمدید

هدیه ثبت‌نام

افزودن ادمین جدید

ارسال پیام همگانی


نسخه ویژه 👉 @wingscrbot
پشتیبانی 👉 @wings_sup


---

⚙️ تنظیم متغیرها

برای اجرای صحیح ربات باید اطلاعات اصلی در قالب متغیرها مشخص شوند. دو روش برای این کار وجود دارد:

🔹 روش ۱: استفاده از فایل env (پیشنهادی)

در ریشه پروژه فایلی به نام sellerbot.env ایجاد کنید و مقادیر زیر را وارد نمایید:

BOT_TOKEN=xxxx:yyyy
ADMIN_ID=123456789
CHANNEL_USERNAME=@your_channel
CHANNEL_ID=-1001234567890
DB_NAME=bot.db

🔹 روش ۲: تغییر مستقیم در فایل تنظیمات

به پوشه bot/ بروید و فایل config.py را باز کنید:

cd bot
nano config.py

مقادیر زیر را مطابق نیاز تغییر دهید:

BOT_TOKEN = "xxxx:yyyy"
ADMIN_ID = 123456789
CHANNEL_USERNAME = "@your_channel"
CHANNEL_ID = -1001234567890
DB_NAME = "bot.db"

📌 پیشنهاد می‌شود از روش اول استفاده کنید تا مدیریت راحت‌تر باشد.


---

🚀 نصب روی سرور (Polling + اجرای دائمی)

۱. دریافت سورس و نصب وابستگی‌ها

git clone https://github.com/wings-iran/WINGSBOT_FREE.git
cd WINGSBOT_FREE

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

۲. ایجاد سرویس systemd

sudo nano /etc/systemd/system/sellerbot.service

محتوا:

[Unit]
Description=Seller Bot
After=network.target

[Service]
WorkingDirectory=/opt/sellerbot
EnvironmentFile=/opt/sellerbot/sellerbot.env
ExecStart=/usr/bin/python3 /opt/sellerbot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target

۳. فعال‌سازی و اجرا

sudo systemctl daemon-reload
sudo systemctl enable sellerbot
sudo systemctl start sellerbot

۴. بررسی وضعیت سرویس

sudo systemctl status sellerbot

تا این مرحله، نصب روی سرور به پایان رسیده است ✅


---

🌐 نصب روی هاست (Webhook)

در هاست‌های اشتراکی یا مواردی که دسترسی کامل به سیستم ندارید، باید از وبهوک استفاده کنید.

۱. اضافه کردن متغیرهای وبهوک

در فایل sellerbot.env:

USE_WEBHOOK=1
WEBHOOK_URL=https://example.com/bot
WEBHOOK_PATH=hook
WEBHOOK_PORT=8080
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_SECRET=some-strong-secret

۲. اجرای برنامه با وبهوک

USE_WEBHOOK=1 WEBHOOK_URL=https://example.com/bot WEBHOOK_PATH=hook \
WEBHOOK_PORT=8080 BOT_TOKEN=xxx ADMIN_ID=123 CHANNEL_USERNAME=@ch CHANNEL_ID=-100... \
python3 main.py

۳. تنظیم Nginx یا Apache (در صورت نیاز)

نمونه پیکربندی برای Nginx:

location /bot/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}

📌 در صورتی که وبهوک معتبر نباشد، ربات به صورت خودکار روی Polling برمی‌گردد تا از کار نیفتد.
