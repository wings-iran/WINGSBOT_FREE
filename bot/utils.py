from datetime import datetime
from telegram import User, Update
from .db import query_db, execute_db
from .config import logger
from telegram.constants import ParseMode


async def register_new_user(user: User, update: Update = None, referrer_hint: int | None = None):
	if not user:
		return
	existing = query_db("SELECT referrer_id FROM users WHERE user_id = ?", (user.id,), one=True)
	if not existing:
		referrer_id = None
		if referrer_hint is not None:
			referrer_id = referrer_hint
		elif update and update.message and update.message.text:
			# Parse /start <ref>
			parts = update.message.text.strip().split()
			if len(parts) == 2 and parts[0].lower() == '/start':
				try:
					referrer_id = int(parts[1])
				except Exception:
					referrer_id = None
		execute_db(
			"INSERT INTO users (user_id, first_name, join_date, referrer_id) VALUES (?, ?, ?, ?)",
			(user.id, user.first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), referrer_id),
		)
		if referrer_id and referrer_id != user.id:
			execute_db(
				"INSERT OR IGNORE INTO referrals (referrer_id, referee_id, created_at) VALUES (?, ?, ?)",
				(referrer_id, user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
			)
		logger.info(f"Registered new user {user.id} ({user.first_name}), ref={referrer_id}")
		# Signup bonus: credit wallet once for first-time users
		settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
		if settings.get('signup_bonus_enabled', '0') == '1':
			try:
				amount = int((settings.get('signup_bonus_amount') or '0') or 0)
			except Exception:
				amount = 0
			if amount > 0:
				# ensure wallet row
				execute_db("INSERT OR IGNORE INTO user_wallets (user_id, balance) VALUES (?, 0)", (user.id,))
				# credit
				execute_db("UPDATE user_wallets SET balance = COALESCE(balance,0) + ? WHERE user_id = ?", (amount, user.id))
				execute_db(
					"INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at, reference, meta) VALUES (?, ?, 'credit', 'bonus', 'approved', ?, ?, ?)",
					(user.id, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'signup_bonus', None)
				)
				# notify user
				if update and update.effective_chat:
					try:
						await update.effective_chat.send_message(
							f"\u2728 هدیه خوش‌آمدگویی: `{amount:,}` تومان به کیف پول شما افزوده شد.",
							parse_mode=ParseMode.MARKDOWN,
						)
					except Exception:
						pass
	else:
		# Backfill referrer if missing and hint exists
		current_ref = existing.get('referrer_id')
		if (current_ref is None or current_ref == '' ) and referrer_hint and referrer_hint != user.id:
			execute_db("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_hint, user.id))
			execute_db(
				"INSERT OR IGNORE INTO referrals (referrer_id, referee_id, created_at) VALUES (?, ?, ?)",
				(referrer_hint, user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
			)
			logger.info(f"Backfilled referrer for user {user.id} => {referrer_hint}")


def bytes_to_gb(byte_val):
	if not byte_val or byte_val == 0:
		return 0
	return round(byte_val / (1024 ** 3), 2)