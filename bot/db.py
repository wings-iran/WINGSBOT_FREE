import sqlite3
from datetime import datetime
from .config import DB_NAME, logger


def query_db(query: str, args=(), one: bool = False):
    try:
        with sqlite3.connect(DB_NAME, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, args)
            rows = cursor.fetchall()
            if one:
                return dict(rows[0]) if rows else None
            return [dict(row) for row in rows]
    except sqlite3.Error as e:
        logger.error(f"DB query error: {e}")
        return None if one else []


def execute_db(query: str, args=()):
    try:
        with sqlite3.connect(DB_NAME, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute(query, args)
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logger.error(f"DB execute error: {e}")
        return None


def initialize_default_content(cursor: sqlite3.Cursor, conn: sqlite3.Connection):
    default_messages = {
        'start_main': ('\U0001F44B سلام! به ربات فروش کانفیگ ما خوش آمدید.\nبرای شروع از دکمه‌های زیر استفاده کنید.', None, None),
        'admin_panel_main': ('\U0001F5A5\uFE0F پنل مدیریت ربات. لطفا یک گزینه را انتخاب کنید.', None, None),
        'buy_config_main': ('\U0001F4E1 **خرید کانفیگ**\n\nلطفا یکی از پلن‌های زیر را انتخاب کنید:', None, None),
        'payment_info_text': ('\U0001F4B3 **اطلاعات پرداخت** \U0001F4B3\n\nمبلغ پلن انتخابی را به یکی از کارت‌های زیر واریز کرده و سپس اسکرین‌شات رسید را در همین صفحه ارسال نمایید.', None, None),
        'renewal_reminder_text': ('\u26A0\uFE0F **یادآوری تمدید سرویس**\n\nکاربر گرامی، اعتبار سرویس شما با نام کاربری `{marzban_username}` رو به اتمام است.\n\n{details}\n\nبرای جلوگیری از قطع شدن سرویس، لطفاً از طریق دکمه "سرویس من" در منوی اصلی ربات اقدام به تمدید نمایید.', None, None)
    }

    for name, (text, f_id, f_type) in default_messages.items():
        cursor.execute(
            "INSERT OR IGNORE INTO messages (message_name, text, file_id, file_type) VALUES (?, ?, ?, ?)",
            (name, text, f_id, f_type),
        )

    conn.commit()

    from .db import query_db as _query_db, execute_db as _execute_db

    if not _query_db("SELECT 1 FROM panels", one=True):
        url_row = _query_db("SELECT value FROM settings WHERE key = 'panel_url'", one=True)
        user_row = _query_db("SELECT value FROM settings WHERE key = 'panel_user'", one=True)
        password_row = _query_db("SELECT value FROM settings WHERE key = 'panel_pass'", one=True)

        url = url_row.get('value') if url_row else 'https://your-panel.com'
        user = user_row.get('value') if user_row else 'admin'
        password = password_row.get('value') if password_row else 'password'

        _execute_db(
            "INSERT INTO panels (name, panel_type, url, username, password, sub_base) VALUES (?, ?, ?, ?, ?, ?)",
            ('پنل اصلی (پیش‌فرض)', 'marzban', url, user, password, None),
        )
        _execute_db("DELETE FROM settings WHERE key IN ('panel_url', 'panel_user', 'panel_pass')")

    default_settings = {'free_trial_days': '1', 'free_trial_gb': '0.2', 'free_trial_status': '1'}
    for key, value in default_settings.items():
        _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # Insert default cards
    if not _query_db("SELECT 1 FROM cards", one=True):
        _execute_db(
            "INSERT INTO cards (card_number, holder_name) VALUES (?, ?)",
            ("6037-0000-0000-0000", "نام دارنده کارت"),
        )

    # Ensure USD rate related settings exist
    _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('usd_irt_manual', ''))
    _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('usd_irt_cached', ''))
    _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('usd_irt_cached_ts', ''))
    _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('usd_irt_mode', 'manual'))

    # Payment method toggles and gateway config
    defaults = [
        ('pay_card_enabled', '1'),
        ('pay_crypto_enabled', '1'),
        ('pay_gateway_enabled', '0'),
        ('gateway_type', 'zarinpal'),
        ('zarinpal_merchant_id', ''),
        ('aghapay_pin', ''),
        ('aghapay_api_key', ''),
        ('gateway_callback_url', ''),
        # Signup bonus defaults
        ('signup_bonus_enabled', '0'),
        ('signup_bonus_amount', '0'),
        # Free trial: selected panel (optional)
        ('free_trial_panel_id', ''),
        # Referral commission percent (default 10)
        ('referral_commission_percent', '10'),
        # Config footer text (shown under config link)
        ('config_footer_text', 'آموزش اتصال :\nhttps://t.me/madeingod_tm'),
        # Reseller defaults
        ('reseller_enabled', '1'),
        ('reseller_fee_toman', '200000'),
        ('reseller_discount_percent', '50'),
        ('reseller_duration_days', '30'),
        ('reseller_max_purchases', '10'),
    ]
    for k, v in defaults:
        _execute_db("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def db_setup():
    with sqlite3.connect(DB_NAME, check_same_thread=False) as conn:
        cursor = conn.cursor()
        # --- Create Tables ---
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, join_date TEXT)"
        )
        # Referrals
        cursor.execute("PRAGMA table_info(users)")
        ucols = [col[1] for col in cursor.fetchall()]
        if 'referrer_id' not in ucols:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
            except sqlite3.Error:
                pass
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referee_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(referrer_id, referee_id)
            )
            """
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS messages (message_name TEXT PRIMARY KEY, text TEXT, file_id TEXT, file_type TEXT)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS buttons (id INTEGER PRIMARY KEY AUTOINCREMENT, menu_name TEXT, text TEXT, target TEXT, is_url BOOLEAN DEFAULT 0, row INTEGER, col INTEGER)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT, price INTEGER NOT NULL, duration_days INTEGER NOT NULL, traffic_gb REAL NOT NULL)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY AUTOINCREMENT, card_number TEXT NOT NULL, holder_name TEXT NOT NULL)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS free_trials (user_id INTEGER PRIMARY KEY, timestamp TEXT)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS discount_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, percentage INTEGER NOT NULL, usage_limit INTEGER NOT NULL, times_used INTEGER DEFAULT 0, expiry_date TEXT)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS panels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, panel_type TEXT NOT NULL DEFAULT 'marzban', url TEXT NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL, sub_base TEXT, token TEXT)"
        )
        # Migrations for existing tables
        cursor.execute("PRAGMA table_info(panels)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'panel_type' not in columns:
            try:
                cursor.execute("ALTER TABLE panels ADD COLUMN panel_type TEXT NOT NULL DEFAULT 'marzban'")
                cursor.execute("UPDATE panels SET panel_type = 'marzban' WHERE panel_type IS NULL")
            except sqlite3.Error as e:
                logger.error(f"Error adding panel_type to panels: {e}")
        cursor.execute("PRAGMA table_info(panels)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'sub_base' not in columns:
            try:
                cursor.execute("ALTER TABLE panels ADD COLUMN sub_base TEXT")
            except sqlite3.Error as e:
                logger.error(f"Error adding sub_base to panels: {e}")
        cursor.execute("PRAGMA table_info(panels)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'token' not in columns:
            try:
                cursor.execute("ALTER TABLE panels ADD COLUMN token TEXT")
            except sqlite3.Error as e:
                logger.error(f"Error adding token to panels: {e}")
        # --- NEW: Table for manually setting inbounds for each panel ---
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS panel_inbounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id INTEGER NOT NULL,
                protocol TEXT NOT NULL,
                tag TEXT NOT NULL,
                UNIQUE(panel_id, tag),
                FOREIGN KEY (panel_id) REFERENCES panels(id) ON DELETE CASCADE
            )
            """
        )

        # Orders table with conditional add columns
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(orders)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'panel_id' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN panel_id INTEGER")
            if 'discount_code' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN discount_code TEXT")
            if 'final_price' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN final_price INTEGER")
            if 'last_reminder_date' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN last_reminder_date TEXT")
            if 'panel_type' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN panel_type TEXT")
            if 'last_link' not in columns:
                try:
                    cursor.execute("ALTER TABLE orders ADD COLUMN last_link TEXT")
                except sqlite3.Error:
                    pass
            if 'xui_inbound_id' not in columns:
                try:
                    cursor.execute("ALTER TABLE orders ADD COLUMN xui_inbound_id INTEGER")
                except sqlite3.Error:
                    pass
            if 'xui_client_id' not in columns:
                try:
                    cursor.execute("ALTER TABLE orders ADD COLUMN xui_client_id TEXT")
                except sqlite3.Error:
                    pass
        else:
            cursor.execute(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending', marzban_username TEXT, screenshot_file_id TEXT, timestamp TEXT,
                    panel_id INTEGER, discount_code TEXT, final_price INTEGER, last_reminder_date TEXT, panel_type TEXT,
                    last_link TEXT, xui_inbound_id INTEGER, xui_client_id TEXT, reseller_applied INTEGER DEFAULT 0
                )
                """
            )
        # NEW: wallets and wallet_transactions
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                memo TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                direction TEXT NOT NULL, -- credit/debit
                method TEXT NOT NULL,    -- gateway/crypto/card/manual
                status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
                created_at TEXT NOT NULL,
                screenshot_file_id TEXT,
                reference TEXT,
                meta TEXT
            )
            """
        )
        # Reseller tables
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS resellers (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                activated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                discount_percent INTEGER NOT NULL,
                max_purchases INTEGER NOT NULL,
                used_purchases INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reseller_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                method TEXT NOT NULL, -- card/crypto/gateway
                status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
                created_at TEXT NOT NULL,
                screenshot_file_id TEXT,
                reference TEXT,
                meta TEXT
            )
            """
        )
        # Migration: add reseller_applied if missing
        cursor.execute("PRAGMA table_info(orders)")
        ocols = [col[1] for col in cursor.fetchall()]
        if 'reseller_applied' not in ocols:
            try:
                cursor.execute("ALTER TABLE orders ADD COLUMN reseller_applied INTEGER DEFAULT 0")
            except sqlite3.Error:
                pass
        # Tickets table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content_type TEXT,
                text TEXT,
                file_id TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        # Threaded ticket messages (new)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender TEXT NOT NULL, -- 'user' | 'admin'
                content_type TEXT,
                text TEXT,
                file_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
            )
            """
        )
        # Tutorials
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tutorials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tutorial_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tutorial_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                caption TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (tutorial_id) REFERENCES tutorials(id) ON DELETE CASCADE
            )
            """
        )
        # Admins table (additional admins besides primary ADMIN_ID)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.commit()
        initialize_default_content(cursor, conn)