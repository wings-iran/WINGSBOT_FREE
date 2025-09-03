import logging
import os

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("bot")

def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default

def _load_env_file():
    # Try to read .env without python-dotenv dependency
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
    alt_env = os.path.join(os.getcwd(), '.env')
    for path in (env_path, alt_env):
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            k, v = line.split('=', 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

# Preload .env variables
_load_env_file()

# --- Basic Settings (env-overridable) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = _safe_int(os.getenv("ADMIN_ID", "0"), 0)
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
# CHANNEL_ID can be numeric or @username. Keep raw and also provide int fallback.
RAW_CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_ID = _safe_int(RAW_CHANNEL_ID, 0)
DB_NAME = os.getenv("DB_NAME", "bot.db")
NOBITEX_TOKEN = os.getenv("NOBITEX_TOKEN", "")

# Job schedule hour for daily tasks
DAILY_JOB_HOUR = _safe_int(os.getenv("DAILY_JOB_HOUR", "9"), 9)
