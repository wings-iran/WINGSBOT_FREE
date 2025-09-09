from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
# Forward to real implementations to avoid premium gating
from .admin import (
    backup_start as real_backup_start,
    admin_generate_backup as real_admin_generate_backup,
    admin_run_reminder_check as real_admin_run_reminder_check,
    admin_toggle_signup_bonus as real_admin_toggle_signup_bonus,
    admin_set_signup_bonus_amount_start as real_admin_set_signup_bonus_amount_start,
    admin_set_signup_bonus_amount_save as real_admin_set_signup_bonus_amount_save,
    admin_add_command as real_admin_add_command,
)


async def backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_backup_start(update, context)


async def admin_generate_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_generate_backup(update, context)


async def admin_run_reminder_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_run_reminder_check(update, context)


async def admin_toggle_signup_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_toggle_signup_bonus(update, context)


async def admin_set_signup_bonus_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_set_signup_bonus_amount_start(update, context)


async def admin_set_signup_bonus_amount_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_admin_set_signup_bonus_amount_save(update, context)


async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await real_admin_add_command(update, context)
    return ConversationHandler.END

