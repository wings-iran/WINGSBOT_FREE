from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from ..db import query_db, execute_db
from ..states import DISCOUNT_MENU, DISCOUNT_AWAIT_CODE, DISCOUNT_AWAIT_PERCENT, DISCOUNT_AWAIT_LIMIT, DISCOUNT_AWAIT_EXPIRY
from ..helpers.tg import safe_edit_text as _safe_edit_text


async def admin_discount_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    codes = query_db(
        "SELECT id, code, percentage, usage_limit, times_used, strftime('%Y-%m-%d', expiry_date) as expiry FROM discount_codes ORDER BY id DESC"
    )

    text = "\U0001F381 **مدیریت کدهای تخفیف**\n\n"
    keyboard = []

    if not codes:
        text += "در حال حاضر هیچ کد تخفیفی ثبت نشده است."
    else:
        text += "کدهای تخفیف:\n"
        for code in codes:
            limit_str = f"{code['times_used']}/{code['usage_limit']}" if code['usage_limit'] > 0 else f"{code['times_used']}/\u221E"
            expiry_str = f"تا {code['expiry']}" if code['expiry'] else "بی‌نهایت"
            info_str = f"{code['code']} ({code['percentage']}%) - {limit_str} - {expiry_str}"
            keyboard.append([
                InlineKeyboardButton(info_str, callback_data=f"noop_{code['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"delete_discount_{code['id']}")
            ])

    keyboard.insert(0, [InlineKeyboardButton("\u2795 افزودن کد جدید", callback_data="add_discount_code")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به پنل اصلی", callback_data="admin_main")])

    if query:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_MENU


async def admin_discount_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    code_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM discount_codes WHERE id = ?", (code_id,))
    await query.answer("کد تخفیف با موفقیت حذف شد.", show_alert=True)
    return await admin_discount_menu(update, context)


async def admin_discount_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_discount'] = {}
    await _safe_edit_text(query.message, "لطفا **کد تخفیف** را وارد کنید (مثال: `OFF20`):", parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_AWAIT_CODE


async def admin_discount_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().upper()
    if query_db("SELECT 1 FROM discount_codes WHERE code = ?", (code,), one=True):
        await update.message.reply_text("این کد تخفیف قبلا ثبت شده. لطفا یک کد دیگر وارد کنید.")
        return DISCOUNT_AWAIT_CODE
    context.user_data['new_discount']['code'] = code
    await update.message.reply_text("لطفا **درصد تخفیف** را به صورت عدد وارد کنید (مثال: `20`):", parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_AWAIT_PERCENT


async def admin_discount_receive_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        percent = int(update.message.text)
        if not 1 <= percent <= 100:
            raise ValueError()
        context.user_data['new_discount']['percent'] = percent
        await update.message.reply_text("**محدودیت تعداد استفاده** را وارد کنید (برای نامحدود عدد `0` را وارد کنید):", parse_mode=ParseMode.MARKDOWN)
        return DISCOUNT_AWAIT_LIMIT
    except ValueError:
        await update.message.reply_text("ورودی نامعتبر. لطفا فقط یک عدد بین ۱ تا ۱۰۰ وارد کنید.")
        return DISCOUNT_AWAIT_PERCENT


async def admin_discount_receive_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_discount']['limit'] = int(update.message.text)
        await update.message.reply_text("کد تخفیف تا **چند روز دیگر** معتبر باشد؟ (برای نامحدود عدد `0` را وارد کنید):", parse_mode=ParseMode.MARKDOWN)
        return DISCOUNT_AWAIT_EXPIRY
    except ValueError:
        await update.message.reply_text("ورودی نامعتبر. لطفا یک عدد وارد کنید.")
        return DISCOUNT_AWAIT_LIMIT


async def admin_discount_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int(update.message.text)
        expiry_date = (datetime.now() + __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days > 0 else None
        d = context.user_data['new_discount']
        execute_db(
            "INSERT INTO discount_codes (code, percentage, usage_limit, expiry_date, times_used) VALUES (?, ?, ?, ?, 0)",
            (d['code'], d['percent'], d.get('limit', 0), expiry_date),
        )
        await update.message.reply_text(f"\u2705 کد تخفیف `{d['code']}` با موفقیت ساخته شد.")
    except Exception as e:
        await update.message.reply_text(f"\u274C خطا در ذخیره کد تخفیف: {e}")

    context.user_data.clear()
    return await admin_discount_menu(update, context)