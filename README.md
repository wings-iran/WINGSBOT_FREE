## 🤖 ربات فروش و مدیریت سرویس

این ربات برای مدیریت فروش و سرویس‌ها طراحی شده است. سعی شده مراحل نصب و راه‌اندازی ساده و شفاف توضیح داده شود تا هر کسی بتواند بدون پیچیدگی از آن استفاده کند.

---

### ✨ امکانات

- پشتیبانی از پنل‌های مرزبان / مرزنشین / x_ui / 3x_ui / tx_ui / علیرضا
- خرید پلن، تخفیف، پرداخت (درگاه / کارت / کریپتو)
- ارسال رسید برای تایید ادمین
- تمدید سرویس + دریافت لینک ساب
- کیف‌پول کاربر (شارژ و مدیریت تراکنش‌ها)
- تیکت پشتیبانی + آموزش‌ها + معرفی دوستان
- پنل ادمین کامل




### 🚀 نصب روی سرور (Polling + اجرای دائمی)
قبل هر کاری پیشنیاز هارو نصب کنید 
```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip -y
```

- ۱) دریافت سورس و نصب وابستگی‌ها

```bash
git clone https://github.com/wings-iran/WINGSBOT_FREE.git
cd WINGSBOT_FREE
```
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
### ⚙️ تنظیم متغیرها

برای اجرای صحیح ربات باید اطلاعات اصلی در قالب متغیرها مشخص شوند. دو روش برای این کار وجود دارد:

 تغییر مستقیم در فایل تنظیمات
فایل sellerbot.env بسازید 
```bash
sudo nano /opt/sellerbot/sellerbot.env
```
این متغییر هارو وارد کنید 
BOT_TOKEN = "xxxx:yyyy"
ADMIN_ID = 123456789
CHANNEL_USERNAME = "@your_channel"
CHANNEL_ID = -1001234567890
DB_NAME = "bot.db" 
سپس سیو کن ctrl+x+y

به پوشه `bot/` بروید و فایل `config.py` را باز کنید:

```bash
cd bot
nano config.py
```

مقادیر زیر را مطابق نیاز تغییر دهید:

```python
BOT_TOKEN = "xxxx:yyyy"
ADMIN_ID = 123456789
CHANNEL_USERNAME = "@your_channel"
CHANNEL_ID = -1001234567890
DB_NAME = "bot.db"
```
- ۲) ایجاد سرویس systemd

فایل سرویس را ایجاد کنید:

```bash
sudo nano /etc/systemd/system/sellerbot.service
```

محتوا را قرار دهید:

```ini
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
```

- ۳) فعال‌سازی و اجرا

```bash
sudo systemctl daemon-reload
sudo systemctl enable sellerbot
sudo systemctl start sellerbot
```

- ۴) بررسی وضعیت سرویس

```bash
sudo systemctl status sellerbot
```

سپس وارد ربات در تلگرام شوید و این کامند را ارسال کنید:

```bash
/admin
```

تا این مرحله، نصب روی سرور به پایان رسیده است ✅

---

### 🌐 نصب روی هاست (Webhook)

در هاست‌های اشتراکی یا مواردی که دسترسی کامل به سیستم ندارید، باید از وبهوک استفاده کنید.

- ۱) اضافه کردن متغیرهای وبهوک

در فایل `sellerbot.env`:

```bash
USE_WEBHOOK=1
WEBHOOK_URL=https://example.com/bot
WEBHOOK_PATH=hook
WEBHOOK_PORT=8080
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_SECRET=some-strong-secret
```

- ۲) اجرای برنامه با وبهوک

```bash
USE_WEBHOOK=1 WEBHOOK_URL=https://example.com/bot WEBHOOK_PATH=hook \
WEBHOOK_PORT=8080 BOT_TOKEN=xxx ADMIN_ID=123 CHANNEL_USERNAME=@ch CHANNEL_ID=-100... \
python3 main.py
```

- ۳) تنظیم Nginx یا Apache (در صورت نیاز)

نمونه پیکربندی برای Nginx:

```nginx
location /bot/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}
```

📌 در صورتی که وبهوک معتبر نباشد، ربات به صورت خودکار روی Polling برمی‌گردد تا از کار نیفتد.
