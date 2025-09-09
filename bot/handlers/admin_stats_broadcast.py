from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..db import query_db, execute_db
from ..helpers.tg import safe_edit_text as _safe_edit_text
from ..states import BROADCAST_SELECT_AUDIENCE, BROADCAST_SELECT_MODE, BROADCAST_AWAIT_MESSAGE, ADMIN_MAIN_MENU
from ..states import ADMIN_STATS_MENU


async def admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("ارسال به همه", callback_data="broadcast_all")],
        [InlineKeyboardButton("ارسال به خریداران", callback_data="broadcast_buyers")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    await _safe_edit_text(query.message, "📣 ارسال همگانی:", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_SELECT_AUDIENCE


async def admin_broadcast_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['broadcast_audience'] = query.data.split('_')[-1]
    keyboard = [
        [InlineKeyboardButton("ارسال به صورت کپی", callback_data="broadcast_mode_copy")],
        [InlineKeyboardButton("ارسال به صورت فوروارد", callback_data="broadcast_mode_forward")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    await _safe_edit_text(query.message, "لطفا نوع ارسال را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_SELECT_MODE


async def admin_broadcast_set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['broadcast_mode'] = query.data.replace('broadcast_mode_', '')
    await _safe_edit_text(query.message, "لطفا پیام خود را برای ارسال، در قالب متن یا عکس ارسال کنید. (برای لغو /cancel را بفرستید)")
    return BROADCAST_AWAIT_MESSAGE


async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    audience = context.user_data.get('broadcast_audience')
    mode = context.user_data.get('broadcast_mode', 'copy')
    if not audience:
        await update.message.reply_text("ابتدا مخاطب ارسال را انتخاب کنید.")
        return ADMIN_MAIN_MENU
    users = []
    if audience == 'buyers':
        users = query_db("SELECT DISTINCT user_id FROM orders WHERE status='approved'")
    else:
        users = query_db("SELECT user_id FROM users")
    sent = 0
    for u in users or []:
        uid = u['user_id']
        try:
            if mode == 'forward':
                await context.bot.forward_message(chat_id=uid, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            else:
                await context.bot.copy_message(chat_id=uid, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ ارسال انجام شد. ({sent} نفر)")
    context.user_data.pop('broadcast_audience', None)
    return ADMIN_MAIN_MENU


async def admin_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    total_users = (query_db("SELECT COUNT(*) AS c FROM users", one=True) or {}).get('c', 0)
    buyers = (query_db("SELECT COUNT(DISTINCT user_id) AS c FROM orders WHERE status='approved'", one=True) or {}).get('c', 0)
    daily_rev = (query_db(
        """
        SELECT COALESCE(SUM(CASE WHEN o.final_price IS NOT NULL THEN o.final_price ELSE p.price END),0) AS rev
        FROM orders o
        JOIN plans p ON p.id = o.plan_id
        WHERE o.status='approved' AND date(o.timestamp) = date('now','localtime')
        """,
        one=True,
    ) or {}).get('rev', 0)
    monthly_rev = (query_db(
        """
        SELECT COALESCE(SUM(CASE WHEN o.final_price IS NOT NULL THEN o.final_price ELSE p.price END),0) AS rev
        FROM orders o
        JOIN plans p ON p.id = o.plan_id
        WHERE o.status='approved' AND strftime('%Y-%m', o.timestamp) = strftime('%Y-%m', 'now','localtime')
        """,
        one=True,
    ) or {}).get('rev', 0)

    text = (
        "📊 آمار ربات:\n\n"
        f"کاربران: {int(total_users)}\n"
        f"خریداران: {int(buyers)}\n\n"
        f"درآمد امروز: {int(daily_rev):,} تومان\n"
        f"درآمد این ماه: {int(monthly_rev):,} تومان"
    )
    keyboard = [
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="stats_refresh")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_STATS_MENU


async def admin_stats_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("در حال بروزرسانی...")
    return await admin_stats_menu(update, context)