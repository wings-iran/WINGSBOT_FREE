import logging
import os

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("bot")

# --- Basic Settings (env-overridable) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "your bot token")
ADMIN_ID = int(os.getenv("ADMIN_ID", "your id"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "your channle id")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "channle id"))
DB_NAME = os.getenv("DB_NAME", "bot.db")
NOBITEX_TOKEN = os.getenv("NOBITEX_TOKEN", "nobitex token")

# Job schedule hour for daily tasks
DAILY_JOB_HOUR = int(os.getenv("DAILY_JOB_HOUR", "9"))
