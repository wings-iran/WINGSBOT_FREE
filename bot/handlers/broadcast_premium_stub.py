from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
# Forward to real broadcast handlers
from .admin_stats_broadcast import (
    admin_broadcast_menu as real_admin_broadcast_menu,
    admin_broadcast_ask_message as real_admin_broadcast_ask_message,
    admin_broadcast_execute as real_admin_broadcast_execute,
)


async def admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_broadcast_menu(update, context)


async def admin_broadcast_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_broadcast_ask_message(update, context)


async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_broadcast_execute(update, context)

