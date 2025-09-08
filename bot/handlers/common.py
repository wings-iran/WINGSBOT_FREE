from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ApplicationHandlerStop

from ..config import ADMIN_ID, CHANNEL_ID, CHANNEL_USERNAME, logger
from ..db import query_db
from ..utils import register_new_user
from ..helpers.flow import get_flow


async def force_join_checker(update: Update, context: ContextTypes.DEFAULT_TYPE):
	user = update.effective_user
	if not user:
		return
	# Bypass channel join for any admin (primary or additional)
	if user.id == ADMIN_ID:
		logger.debug(f"force_join_checker: admin {user.id} bypassed")
		return
	try:
		extra_admin = query_db("SELECT 1 FROM admins WHERE user_id = ?", (user.id,), one=True)
		if extra_admin:
			logger.debug(f"force_join_checker: extra admin {user.id} bypassed")
			return
	except Exception:
		pass
	# Capture referral payload from /start before blocking join
	try:
		if update.message and update.message.text:
			parts = update.message.text.strip().split()
			if len(parts) == 2 and parts[0].lower() == '/start':
				ref_id = int(parts[1])
				if ref_id != user.id:
					context.user_data['referrer_id'] = ref_id
	except Exception:
		pass
	# Skip join check during active flows to not block message inputs
	ud = context.user_data or {}
	if ud.get('awaiting') or ud.get('awaiting_admin') or ud.get('awaiting_ticket') or get_flow(context):
		logger.debug(f"force_join_checker: skip join check for user {user.id} due to active flow flags: {list(k for k,v in ud.items() if v)}")
		return
	from ..config import CHANNEL_CHAT as _CHAT
	chat_id = _CHAT if _CHAT is not None else (CHANNEL_ID or CHANNEL_USERNAME)
	try:
		member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user.id)
		if member.status in ['member', 'administrator', 'creator']:
			return
	except TelegramError as e:
		# If we cannot verify, keep user blocked and show join info instead of allowing silently
		logger.warning(f"Could not check channel membership for {user.id}: {e}")

	# Build a visible channel hint and a reliable join link if possible
	join_url = None
	channel_hint = ""
	try:
		chat_obj = await context.bot.get_chat(chat_id=chat_id)
		uname = getattr(chat_obj, 'username', None)
		inv = getattr(chat_obj, 'invite_link', None)
		if uname:
			handle = f"@{str(uname).replace('@','')}"
			join_url = f"https://t.me/{str(uname).replace('@','')}"
			channel_hint = f"\n\nکانال: {handle}"
		elif inv:
			join_url = inv
			channel_hint = "\n\nلینک دعوت کانال در دکمه زیر موجود است."
	except Exception:
		if (CHANNEL_USERNAME or '').strip():
			handle = (CHANNEL_USERNAME or '').strip()
			if not handle.startswith('@'):
				handle = f"@{handle}"
			join_url = f"https://t.me/{handle.replace('@','')}"
			channel_hint = f"\n\nکانال: {handle}"
		elif CHANNEL_ID:
			channel_hint = f"\n\nشناسه کانال: `{CHANNEL_ID}`"

	keyboard = []
	if join_url:
		keyboard.append([InlineKeyboardButton("\U0001F195 عضویت در کانال", url=join_url)])
	keyboard.append([InlineKeyboardButton("\u2705 عضو شدم", callback_data="check_join")])
	text = (
		f"\u26A0\uFE0F **قفل عضویت**\n\nبرای استفاده از ربات، ابتدا در کانال ما عضو شوید و سپس دکمه «عضو شدم» را بزنید." + channel_hint
	)
	logger.info(f"force_join_checker: blocking user {user.id} with join gate")
	if update.callback_query:
		await update.callback_query.message.edit_text(
			text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
		)
		await update.callback_query.answer("شما هنوز در کانال عضو نیستید!", show_alert=True)
	elif update.message:
		await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
	raise ApplicationHandlerStop


async def send_dynamic_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_name: str, back_to: str = 'start_main'):
	query = update.callback_query

	message_data = query_db("SELECT text, file_id, file_type FROM messages WHERE message_name = ?", (message_name,), one=True)
	if not message_data:
		await query.answer(f"محتوای '{message_name}' یافت نشد!", show_alert=True)
		return

	text = message_data.get('text')
	file_id = message_data.get('file_id')
	file_type = message_data.get('file_type')

	buttons_data = query_db(
		"SELECT text, target, is_url, row, col FROM buttons WHERE menu_name = ? ORDER BY row, col",
		(message_name,),
	)

	if message_name == 'start_main':
		trial_status = query_db("SELECT value FROM settings WHERE key = 'free_trial_status'", one=True)
		if not trial_status or trial_status.get('value') != '1':
			buttons_data = [b for b in buttons_data if b.get('target') != 'get_free_config']

	keyboard = []
	if buttons_data:
		max_row = max((b['row'] for b in buttons_data), default=0) if buttons_data else 0
		keyboard_rows = [[] for _ in range(max_row + 1)]
		for b in buttons_data:
			btn = (
				InlineKeyboardButton(b['text'], url=b['target'])
				if b['is_url']
				else InlineKeyboardButton(b['text'], callback_data=b['target'])
			)
			if 0 < b['row'] <= len(keyboard_rows):
				keyboard_rows[b['row'] - 1].append(btn)
		keyboard = [row for row in keyboard_rows if row]

	# Fallback: ensure core user buttons arranged with top row [Buy, GetFree] and next row [My Services, ...]
	if message_name == 'start_main':
		missing = lambda target: not any((b.get('target') == target) for b in (buttons_data or []))
		# Top row: Buy + Get Free (if enabled and missing)
		top_row = []
		if missing('buy_config_main'):
			top_row.append(InlineKeyboardButton("\U0001F4E1 خرید کانفیگ", callback_data='buy_config_main'))
		trial_status = query_db("SELECT value FROM settings WHERE key = 'free_trial_status'", one=True)
		if trial_status and trial_status.get('value') == '1' and missing('get_free_config'):
			top_row.append(InlineKeyboardButton("\U0001F381 دریافت تست", callback_data='get_free_config'))
		if top_row:
			keyboard.append(top_row)
		# Second row starts with My Services if missing
		second_row = []
		if missing('my_services'):
			second_row.append(InlineKeyboardButton("\U0001F4DD سرویس‌های من", callback_data='my_services'))
		# Fill remaining core buttons in subsequent rows
		def add_button_if_missing(row_acc, target, text):
			if missing(target):
				row_acc.append(InlineKeyboardButton(text, callback_data=target))
				return True
			return False
		# Complete second row up to 2
		if add_button_if_missing(second_row, 'wallet_menu', "\U0001F4B3 کیف پول من") and len(second_row) == 2:
			keyboard.append(second_row); second_row = []
		if len(second_row) == 2:
			keyboard.append(second_row); second_row = []
		if second_row:
			keyboard.append(second_row); second_row = []
		# Remaining rows (pairs)
		remaining_targets = [
			('wallet_menu', "\U0001F4B3 کیف پول من"),
			('support_menu', "\U0001F4AC پشتیبانی"),
			('tutorials_menu', "\U0001F4D6 آموزش‌ها"),
			('referral_menu', "\U0001F517 معرفی به دوستان"),
			('reseller_menu', "\U0001F4B5 دریافت نمایندگی"),
		]
		row = []
		for tgt, label in remaining_targets:
			if missing(tgt):
				row.append(InlineKeyboardButton(label, callback_data=tgt))
				if len(row) == 2:
					keyboard.append(row); row = []
		if row:
			keyboard.append(row)

	if message_name != 'start_main':
		keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data=back_to)])

	reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

	try:
		if file_id or (query.message and (query.message.photo or query.message.video or query.message.document)):
			await query.message.delete()
			if file_id:
				sender = getattr(context.bot, f"send_{file_type}")
				await sender(
					chat_id=query.message.chat_id,
					file_id=file_id,
					caption=text,
					reply_markup=reply_markup,
					parse_mode=ParseMode.MARKDOWN,
				)
			else:
				await context.bot.send_message(
					chat_id=query.message.chat_id,
					text=text,
					reply_markup=reply_markup,
					parse_mode=ParseMode.MARKDOWN,
				)
		else:
			await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
	except TelegramError as e:
		if 'Message is not modified' not in str(e):
			logger.error(f"Error handling dynamic message: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
	logger.debug(f"start_command by user {update.effective_user.id}")
	await register_new_user(update.effective_user, update, referrer_hint=context.user_data.get('referrer_id'))
	context.user_data.clear()

	sender = None
	if update.callback_query:
		sender = None
	elif update.message:
		sender = update.message.reply_text

	if not sender:
		pass

	message_data = query_db("SELECT text FROM messages WHERE message_name = 'start_main'", one=True)
	text = message_data.get('text') if message_data else "خوش آمدید!"

	buttons_data = query_db(
		"SELECT text, target, is_url, row, col FROM buttons WHERE menu_name = 'start_main' ORDER BY row, col"
	)

	trial_status = query_db("SELECT value FROM settings WHERE key = 'free_trial_status'", one=True)
	if not trial_status or trial_status.get('value') != '1':
		buttons_data = [b for b in buttons_data if b.get('target') != 'get_free_config']

	keyboard = []
	if buttons_data:
		max_row = max((b['row'] for b in buttons_data), default=0)
		keyboard_rows = [[] for _ in range(max_row + 1)]
		for b in buttons_data:
			btn = (
				InlineKeyboardButton(b['text'], url=b['target'])
				if b['is_url']
				else InlineKeyboardButton(b['text'], callback_data=b['target'])
			)
			if 0 < b['row'] <= len(keyboard_rows):
				keyboard_rows[b['row'] - 1].append(btn)
		keyboard = [row for row in keyboard_rows if row]

	# Fallback: ensure core user buttons arranged neatly (rows of 2)
	# Fallback arrangement with top row [Buy, GetFree] and second row starting with My Services
	missing = lambda target: not any((b.get('target') == target) for b in (buttons_data or []))
	# Top row
	top_row = []
	if missing('buy_config_main'):
		top_row.append(InlineKeyboardButton("\U0001F4E1 خرید کانفیگ", callback_data='buy_config_main'))
	trial_status = query_db("SELECT value FROM settings WHERE key = 'free_trial_status'", one=True)
	if trial_status and trial_status.get('value') == '1' and missing('get_free_config'):
		top_row.append(InlineKeyboardButton("\U0001F381 دریافت تست", callback_data='get_free_config'))
	if top_row:
		keyboard.append(top_row)
	# Second row begins with My Services
	second_row = []
	if missing('my_services'):
		second_row.append(InlineKeyboardButton("\U0001F4DD سرویس‌های من", callback_data='my_services'))
	# Fill remaining in pairs
	remaining_targets = [
		('wallet_menu', "\U0001F4B3 کیف پول من"),
		('support_menu', "\U0001F4AC پشتیبانی"),
		('tutorials_menu', "\U0001F4D6 آموزش‌ها"),
		('referral_menu', "\U0001F517 معرفی به دوستان"),
		('reseller_menu', "\U0001F4B5 دریافت نمایندگی"),
	]
	if second_row:
		# Try to add one more to complete the row
		for tgt, label in list(remaining_targets):
			if missing(tgt) and len(second_row) < 2:
				second_row.append(InlineKeyboardButton(label, callback_data=tgt))
		keyboard.append(second_row)
	row = []
	for tgt, label in remaining_targets:
		if missing(tgt):
			# Skip ones already used in second_row
			if any(btn.text == label for btn in keyboard[-1]) if keyboard else False:
				continue
			row.append(InlineKeyboardButton(label, callback_data=tgt))
			if len(row) == 2:
				keyboard.append(row); row = []
	if row:
		keyboard.append(row)

	reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
	if update.callback_query:
		try:
			await update.callback_query.message.delete()
		except TelegramError:
			pass
		await context.bot.send_message(chat_id=update.callback_query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
	else:
		await sender(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def dynamic_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	query = update.callback_query

	reserved_prefixes = [
		'approve_', 'reject_', 'plan_', 'select_plan_', 'card_', 'msg_', 'edit_plan_',
		'btn_', 'noop_', 'renew_', 'set_', 'delete_discount_', 'add_discount_code',
		'panel_', 'backup_', 'admin_', 'apply_discount_start', 'confirm_purchase',
		'get_free_config', 'my_services', 'view_service_', 'check_join', 'buy_config_main',
		'inbound_', 'wallet_', 'pay_method_', 'wallet_tx_', 'ticket_', 'gateway_verify_', 'tutorial_', 'referral_'
	]

	if any(query.data.startswith(p) for p in reserved_prefixes):
		return

	await query.answer()
	message_name = query.data

	if query_db("SELECT 1 FROM messages WHERE message_name = ?", (message_name,), one=True):
		await send_dynamic_message(update, context, message_name=message_name, back_to='start_main')
	else:
		await query.answer("این دکمه در حال حاضر کار نمی‌کند.", show_alert=True)