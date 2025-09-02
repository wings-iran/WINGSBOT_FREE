from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from ..premium import send_premium_notice


async def backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END


async def admin_generate_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END


async def admin_run_reminder_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END


async def admin_toggle_signup_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_settings_manage')
    return ConversationHandler.END


async def admin_set_signup_bonus_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_settings_manage')
    return ConversationHandler.END


async def admin_set_signup_bonus_amount_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_settings_manage')
    return ConversationHandler.END


async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='admin_main')
    return ConversationHandler.END

