from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

IS_FREE_BUILD = False

PREMIUM_NOTICE = ''


async def send_premium_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, back_to: str = 'start_main') -> None:
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.edit_text(
                PREMIUM_NOTICE,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data=back_to)]])
            )
            return
        msg = update.effective_message
        if msg:
            await msg.reply_text(
                PREMIUM_NOTICE,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data=back_to)]])
            )
    except Exception:
        # Best-effort only; never block flows
        pass

