WINGSBOT_FREE – راهنمای نصب و مدیریت ربات فروش

این پروژه یک ربات تلگرامی برای مدیریت فروش سرویس‌های اینترنتی و اشتراک‌هاست.
با نصب این ربات روی سرور لینوکسی (Ubuntu) می‌توانید به‌صورت خودکار فرآیند فروش، پرداخت، مدیریت کاربر و پشتیبانی را انجام دهید.

✨ امکانات ربات

پشتیبانی از پنل‌های مرزبان، مرزنشین، x_ui, 3x_ui, tx_ui و علیرضا

فرآیند کامل خرید پلن، تخفیف و پرداخت:

درگاه بانکی

کارت به کارت

کریپتو

ارسال رسید و تایید توسط ادمین

تمدید سرویس و دریافت آنی لینک سابسکریپشن

کیف پول داخلی (شارژ و مشاهده تراکنش‌ها)

سیستم تیکت و پشتیبانی

بخش آموزش‌ها و معرفی به دوستان

پنل مدیریت کامل برای ادمین

📋 پیش‌نیازها

سیستم عامل: Ubuntu 20.04 یا Ubuntu 22.04

دسترسی: کاربر root یا کاربری با دسترسی sudo

توکن ربات تلگرام: از ربات @BotFather

شناسه عددی ادمین: از ربات @userinfobot

🚀 مراحل نصب
۱. بروزرسانی سرور و نصب پیش‌نیازها
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-venv python3-pip git -y
```
۲. دریافت سورس ربات
```bash
git clone https://github.com/wings-iran/WINGSBOT_FREE.git
cd WINGSBOT_FREE
```

۳. ساخت محیط مجازی و نصب وابستگی‌ها
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```


وقتی محیط مجازی فعال است، در ابتدای خط فرمان (.venv) دیده می‌شود.

۴. تنظیم متغیرهای محیطی (ENV)

یک فایل به نام sellerbot.env ایجاد کنید:
```bash
nano sellerbot.env
```


محتوا:
```bash

BOT_TOKEN=123456:ABC-DEF1234567890
ADMIN_ID=1122334455
CHANNEL_USERNAME=@YourChannel
CHANNEL_ID=-1001234567890
DB_NAME=bot.db
```


ذخیره: Ctrl + X → Y → Enter

۵. انتقال پروژه به مسیر استاندارد
```bash
deactivate
cd ..
sudo mv WINGSBOT_FREE /opt/sellerbot
```

۶. ساخت سرویس systemd
```bash
sudo nano /etc/systemd/system/sellerbot.service
```


محتوا:
```bash
[Unit]
Description=Seller Bot Service (WINGSBOT_FREE)
After=network.target

[Service]
User=root
WorkingDirectory=/opt/sellerbot
EnvironmentFile=/opt/sellerbot/sellerbot.env
ExecStart=/opt/sellerbot/.venv/bin/python3 /opt/sellerbot/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```


ذخیره و خروج.

۷. فعال‌سازی و اجرای ربات
```bash
sudo systemctl daemon-reload
sudo systemctl enable sellerbot.service
sudo systemctl start sellerbot.service
```

✅ بررسی وضعیت
```bash
sudo systemctl status sellerbot.service
```

باید حالت active (running) نمایش داده شود.
اکنون در تلگرام دستور /admin را به ربات بفرستید.

🛠 عیب‌یابی

برای دیدن لاگ‌های زنده:
```bash
sudo journalctl -u sellerbot.service -f --no-pager
```

مقادیر داخل sellerbot.env باید بدون فاصله یا علامت " باشند.

مطمئن شوید همه مراحل را درست اجرا کرده‌اید.

🗑 حذف کامل ربات
# توقف و غیرفعال‌سازی سرویس
```bash
sudo systemctl stop sellerbot.service
sudo systemctl disable sellerbot.service
```
# حذف فایل سرویس
sudo rm /etc/systemd/system/sellerbot.service
sudo systemctl daemon-reload

# حذف پروژه
sudo rm -rf /opt/sellerbot
