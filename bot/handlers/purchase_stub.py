from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
# Forward to real purchase handlers
from .purchase import (
    start_purchase_flow as real_start_purchase_flow,
    show_plan_confirmation as real_show_plan_confirmation,
    apply_discount_start as real_apply_discount_start,
    receive_and_validate_discount_code as real_receive_and_validate_discount_code,
    show_payment_info as real_show_payment_info,
    show_payment_method_selection as real_show_payment_method_selection,
    show_payment_info_card as real_show_payment_info_card,
    show_payment_info_crypto as real_show_payment_info_crypto,
    show_payment_info_gateway as real_show_payment_info_gateway,
    receive_payment_screenshot as real_receive_payment_screenshot,
    gateway_verify_purchase as real_gateway_verify_purchase,
    gateway_verify_renewal as real_gateway_verify_renewal,
    cancel_and_start_purchase as real_cancel_and_start_purchase,
    pay_method_wallet as real_pay_method_wallet,
)


async def start_purchase_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_start_purchase_flow(update, context)


async def show_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_plan_confirmation(update, context)


async def apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_apply_discount_start(update, context)


async def receive_and_validate_discount_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_receive_and_validate_discount_code(update, context)


async def show_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_payment_info(update, context)


async def show_payment_method_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_payment_method_selection(update, context)


async def show_payment_info_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_payment_info_card(update, context)


async def show_payment_info_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_payment_info_crypto(update, context)


async def show_payment_info_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_show_payment_info_gateway(update, context)


async def receive_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_receive_payment_screenshot(update, context)


async def gateway_verify_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_gateway_verify_purchase(update, context)


async def gateway_verify_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_gateway_verify_renewal(update, context)


async def cancel_and_start_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_cancel_and_start_purchase(update, context)


async def pay_method_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_pay_method_wallet(update, context)

