from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
# Forward to real renewal handlers
from .renewal import (
    start_renewal_flow as real_start_renewal_flow,
    show_renewal_plan_confirmation as real_show_renewal_plan_confirmation,
    renew_apply_discount_start as real_renew_apply_discount_start,
    receive_renewal_payment as real_receive_renewal_payment,
)


async def start_renewal_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_start_renewal_flow(update, context)


async def show_renewal_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_renewal_plan_confirmation(update, context)


async def renew_apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_renew_apply_discount_start(update, context)


async def receive_renewal_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_receive_renewal_payment(update, context)

