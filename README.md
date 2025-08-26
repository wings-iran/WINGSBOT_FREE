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
