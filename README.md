# ربات فروش و مدیریت سرویس 

سلام! این ربات رو برای مدیریت فروش کانفیگ و کارهای روزمره ساختم. ساده، مرتب و بدون حاشیه.

پشتیبانی از پنل‌های مرزبان / مرزنشین / x_ui / 3x_ui / tx_ui / علیرضا

- خرید پلن، اعمال تخفیف، پرداخت (درگاه/کارت/کریپتو)، ارسال رسید برای تایید ادمین
- تمدید سرویس، نمایش جزئیات سرویس، گرفتن لینک ساب
- کیف‌پول کاربر (شارژ کردن و مدیریت تراکنش)
- تیکت پشتیبانی، آموزش‌ها، معرفی دوستان
- پنل ادمین کامل برای مدیریت همه‌چی

چیزی که قفل شده تو نسخه رایگان
- بکاپ گرفتن
- تست پیام یادآوری تمدید
- هدیه ثبت‌نام و تعیین مبلغش
- افزودن ادمین
- ارسال پیام همگانی

برای نسخه ویژه از ربات زیر اقدام کنید:
@wingscrbot

پشتیبانی:
@wings_sup

راه‌اندازی سریع
1) پایتون 3.10+ داشته باشید
2) تو فایل env یا محیط، اینا رو ست کنید:
   - `BOT_TOKEN`
   - `ADMIN_ID`
   - `CHANNEL_USERNAME`
   - `CHANNEL_ID`
   - `DB_NAME` (اختیاری، پیشفرض همونه که تو پروژه هست)
3) اجرا: `python main.py`

ساختار پروژه
- همه‌ی کدها زیر `bot/`هست
- هندلرهای کاربر و ادمین جدا شدن، کارها تمیزتر شده
- هر چی لازم داشته باشید داخل همون پوشه‌ها معلومه

نکته
- اگه چیزی دیدی جای بهتر شدن داره، دست به کد شو. تمیز نگه داریمش.

## راه‌اندازی روی سرور (Polling)
- پیش‌نیاز: پایتون 3.10+
- متغیرهای محیطی را ست کنید: `BOT_TOKEN`, `ADMIN_ID`, `CHANNEL_USERNAME`, `CHANNEL_ID`
- اجرای مستقیم:
```bash
python3 main.py
```
- اجرای دائمی با systemd (اختیاری):
```ini
[Unit]
Description=Seller Bot
After=network.target

[Service]
WorkingDirectory=/opt/sellerbot
Environment=BOT_TOKEN=xxx
Environment=ADMIN_ID=123
Environment=CHANNEL_USERNAME=@your_channel
Environment=CHANNEL_ID=-1001234567890
ExecStart=/usr/bin/python3 /opt/sellerbot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## راه‌اندازی روی هاست اشتراکی (Webhook)
- نیازمند دامنه HTTPS و پورت باز روی هاست (یا روتینگ از طریق هاست)
- متغیرها:
  - `USE_WEBHOOK=1`
  - `WEBHOOK_URL=https://example.com/bot` (آدرس عمومی)
  - `WEBHOOK_PATH=<token یا مسیر دلخواه>` (مسیر داخلی سرویس)
  - `WEBHOOK_PORT=8080` (پورت گوش دادن برنامه)
  - `WEBHOOK_LISTEN=0.0.0.0` (آدرس bind)
  - `WEBHOOK_SECRET=<اختیاری>`
- اجرای برنامه:
```bash
USE_WEBHOOK=1 WEBHOOK_URL=https://example.com/bot WEBHOOK_PATH=hook \
WEBHOOK_PORT=8080 BOT_TOKEN=xxx ADMIN_ID=123 CHANNEL_USERNAME=@ch CHANNEL_ID=-100...
python3 main.py
```
- اگر هاست شما Reverse Proxy (مثل Nginx) دارد، یک پراکسی ساده تنظیم کنید:
```nginx
location /bot/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
}
```
- نکته: اگر `WEBHOOK_URL` معتبر نباشد، ربات به صورت خودکار به Polling برمی‌گردد تا از کار نیفتد.

## دریافت سورس (Clone از GitHub)
برای شروع، سورس را از گیت‌هاب کلون کنید:
```bash
git clone https://github.com/wings-iran/WINGSBOT_FREE.git
cd WINGSBOT_FREE
```

## کجا متغیرها را تنظیم کنیم؟ (ENV یا ویرایش فایل)
دو روش دارید:
- روش پیشنهادی: تنظیم به‌صورت متغیر محیطی (ENV) یا فایل `.env`/`sellerbot.env` که در سرویس systemd هم استفاده می‌شود.
- روش جایگزین: ویرایش مستقیم فایل `bot/config.py`.

### روش ۱: متغیرهای محیطی (پیشنهادی)
نمونه فایل محیطی (در ریشه پروژه بسازید: `./sellerbot.env`):
```bash
BOT_TOKEN=xxxx:yyyy
ADMIN_ID=123456789
CHANNEL_USERNAME=@your_channel
CHANNEL_ID=-1001234567890
DB_NAME=bot.db
# برای وبهوک در هاست اشتراکی (اختیاری):
# USE_WEBHOOK=1
# WEBHOOK_URL=https://example.com/bot
# WEBHOOK_PATH=hook
# WEBHOOK_PORT=8080
# WEBHOOK_LISTEN=0.0.0.0
# WEBHOOK_SECRET=some-strong-secret
```

### روش ۲: ویرایش مستقیم فایل تنظیمات
این مقادیر را می‌توانید داخل `bot/config.py` تغییر دهید:
```12:19:bot/config.py
# --- Basic Settings (env-overridable) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "7910215097:AAH-Zalti5nDFPTS8Dokw0Tgcgb3EpibGEc")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6839887159"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@wings_iran")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001553094061"))
DB_NAME = os.getenv("DB_NAME", "bot.db")
NOBITEX_TOKEN = os.getenv("NOBITEX_TOKEN", "79df9500f80eff5520a73f1414594028ca91daa6")
```
- توجه: مقادیر بالا با ENV override می‌شوند. توصیه می‌شود مقداردهی واقعی را از طریق ENV انجام دهید.

## چطور آیدی عددی‌ها (IDs) را پیدا کنیم؟
برای راه‌اندازی به چند شناسه نیاز دارید: آیدی عددی کاربر (ADMIN_ID) و آیدی عددی کانال (CHANNEL_ID). همچنین یوزرنیم کانال (CHANNEL_USERNAME) را با @ می‌گذارید.

### آیدی عددی کاربر (ADMIN_ID)
ساده‌ترین روش‌ها:
- ربات‌های اطلاعات کاربر: در تلگرام به یکی از این ربات‌ها پیام بدهید و Start کنید: `@userinfobot` یا `@getmyid_bot`. آیدی عددی شما را نشان می‌دهند.
- روش جایگزین با Bot API: به ربات خودتان `/start` بفرستید، سپس در مرورگر باز کنید:
  - `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
  - در پاسخ JSON، دنبال `"from":{"id": ... }` بگردید (همان آیدی عددی شماست).

### آیدی عددی کانال (CHANNEL_ID)
بسته به نوع کانال:
- کانال عمومی (Public):
  1) BOT_TOKEN خود را داشته باشید.
  2) در مرورگر باز کنید: `https://api.telegram.org/bot<BOT_TOKEN>/getChat?chat_id=@your_channel`
  3) در پاسخ JSON، مقدار `result.id` را بردارید (معمولاً با `-100` شروع می‌شود).
- کانال خصوصی (Private):
  1) رباتتان را به کانال اضافه و Admin کنید.
  2) داخل کانال یک پیام جدید ارسال کنید.
  3) در مرورگر باز کنید: `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
  4) در پاسخ JSON، دنبال آبجکت نوع `"chat":{"type":"channel" ... }` بگردید و مقدار `id` آن را بردارید (همان CHANNEL_ID با پیشوند `-100`).
  - روش جایگزین: یک پیام از کانال را به ربات‌هایی مثل `@getidsbot` فوروارد کنید تا آیدی عددی کانال را بدهند.

نکات مهم:
- `CHANNEL_USERNAME` باید به‌صورت `@channel_username` باشد.
- `CHANNEL_ID` همیشه عددی است و معمولاً با `-100` شروع می‌شود.
- برای اینکه ربات بتواند عضوگیری/بررسی عضویت کانال را انجام دهد، بهتر است ربات را Admin کانال کنید.

## نصب از گیت و راه‌اندازی (خلاصه)
```bash
# دریافت سورس
git clone https://github.com/wings-iran/WINGSBOT_FREE.git
cd WINGSBOT_FREE

# ساخت محیط مجازی و نصب وابستگی
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# تنظیم متغیرها (یکی از دو روش بالا)
# اجرای سریع (Polling روی سرور)
source ./sellerbot.env 2>/dev/null || true
python3 main.py
```

برای راه‌اندازی کامل سرور (Polling با systemd) و هاست (Webhook)، دستورالعمل‌های همین فایل را در بخش‌های «راه‌اندازی روی سرور (Polling)» و «راه‌اندازی روی هاست اشتراکی (Webhook)» دنبال کنید.
