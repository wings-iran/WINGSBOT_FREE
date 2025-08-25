from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from ..premium import send_premium_notice


async def admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END


async def admin_broadcast_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END


async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END

