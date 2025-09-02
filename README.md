## نصب و راه‌اندازی سریع (فارسی)

این راهنما طوری نوشته شده که اگر هیچ تجربه‌ای هم نداشته باشید، بتوانید ربات را راه‌اندازی کنید.

### روش ۱: نصب خودکار با اسکریپت

1) وارد سرور لینوکسی خود شوید (Ubuntu 20.04/22.04 پیشنهاد می‌شود).

2) دستورهای زیر را اجرا کنید:

```bash
sudo apt update && sudo apt install -y git curl python3 python3-venv python3-pip
git clone https://github.com/wings-iran/WINGSBOT_FREE
cd WINGSBOT_FREE
bash install.sh
```

3) هنگام اجرای install.sh از شما سوال می‌شود:
- BOT_TOKEN: توکن ربات از BotFather
- ADMIN_ID: آیدی عددی ادمین (از @userinfobot)
- CHANNEL_ID: آیدی کانال یا @نام‌کاربری (اختیاری)

4) اجرای ربات:

```bash
source .venv/bin/activate
python -m bot.run
```

5) اجرای دائمی (اختیاری): فایل wingsbot.service ساخته می‌شود. می‌توانید آن را به systemd بدهید:

```bash
sudo cp wingsbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wingsbot
```

برای مشاهده وضعیت:

```bash
sudo systemctl status wingsbot
```

برای دیدن لاگ زنده:

```bash
sudo journalctl -u wingsbot -f --no-pager
```

### روش ۲: اجرای ساده با Docker

1) مخزن را دریافت کنید و فایل محیط را بسازید:

```bash
git clone https://github.com/wings-iran/WINGSBOT_FREE
cd WINGSBOT_FREE
cp .env.example .env
# سپس فایل .env را با مقادیر BOT_TOKEN و ADMIN_ID ویرایش کنید
```

2) اجرای کانتینر:

```bash
docker compose up -d --build
```

مشاهده لاگ‌ها:

```bash
docker compose logs -f
```

### نکات مهم پیکربندی

- BOT_TOKEN: توکن ربات از BotFather (الزامی)
- ADMIN_ID: آیدی عددی ادمین اصلی (الزامی)
- CHANNEL_ID: آیدی/نام کانال برای اجباری‌کردن عضویت (اختیاری)
- USE_WEBHOOK و سایر مقادیر وبهوک فقط زمانی نیاز است که بخواهید با وبهوک اجرا کنید.

### بروزرسانی ربات

```bash
git pull
source .venv/bin/activate && pip install -r requirements.txt
systemctl restart wingsbot  # اگر با systemd اجرا می‌کنید
```

### رفع اشکال متداول

- اگر ربات بالا نمی‌آید، ابتدا لاگ را بررسی کنید:
```bash
sudo journalctl -u wingsbot -f --no-pager
```
- از درست‌بودن توکن و ADMIN_ID در فایل .env مطمئن شوید.
- اگر با Docker اجرا می‌کنید، `docker compose logs -f` را بررسی کنید.

### حذف کامل (systemd)

```bash
sudo systemctl stop wingsbot
sudo systemctl disable wingsbot
sudo rm /etc/systemd/system/wingsbot.service
sudo systemctl daemon-reload
rm -rf ~/WINGSBOT_FREE
```
