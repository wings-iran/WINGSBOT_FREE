from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from ..premium import send_premium_notice


async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_premium_notice(update, context, back_to='start_main')


async def wallet_topup_gateway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_verify_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_topup_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_topup_crypto_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_select_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_upload_start_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_upload_start_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_premium_notice(update, context, back_to='start_main')
    return ConversationHandler.END


async def wallet_upload_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return ConversationHandler.END

