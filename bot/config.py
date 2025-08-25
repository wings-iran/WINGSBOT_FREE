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
BOT_TOKEN = os.getenv("BOT_TOKEN", "7910215097:AAH-Zalti5nDFPTS8Dokw0Tgcgb3EpibGEc")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6839887159"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@wings_iran")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001553094061"))
DB_NAME = os.getenv("DB_NAME", "bot.db")
NOBITEX_TOKEN = os.getenv("NOBITEX_TOKEN", "79df9500f80eff5520a73f1414594028ca91daa6")

# Job schedule hour for daily tasks
DAILY_JOB_HOUR = int(os.getenv("DAILY_JOB_HOUR", "9"))