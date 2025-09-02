from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ..premium import send_premium_notice


async def start_purchase_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def show_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def receive_and_validate_discount_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def show_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def show_payment_method_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def show_payment_info_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END


async def show_payment_info_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END


async def show_payment_info_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END


async def receive_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END


async def gateway_verify_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END


async def gateway_verify_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='my_services')
    return ConversationHandler.END


async def cancel_and_start_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_purchase_flow(update, context)


async def pay_method_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='buy_config_main')
    return ConversationHandler.END

