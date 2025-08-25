from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ..premium import send_premium_notice


async def start_renewal_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='my_services')
    return ConversationHandler.END


async def show_renewal_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='my_services')
    return ConversationHandler.END


async def renew_apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='my_services')
    return ConversationHandler.END


async def receive_renewal_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='my_services')
    return ConversationHandler.END

