from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
# Forward to real user wallet handlers
from .user import (
    wallet_menu as real_wallet_menu,
    wallet_topup_gateway_start as real_wallet_topup_gateway_start,
    wallet_verify_gateway as real_wallet_verify_gateway,
    wallet_topup_card_start as real_wallet_topup_card_start,
    wallet_topup_crypto_start as real_wallet_topup_crypto_start,
    wallet_select_amount as real_wallet_select_amount,
    wallet_upload_start_card as real_wallet_upload_start_card,
    wallet_upload_start_crypto as real_wallet_upload_start_crypto,
    wallet_upload_router as real_wallet_upload_router,
)


async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await real_wallet_menu(update, context)


async def wallet_topup_gateway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_topup_gateway_start(update, context)


async def wallet_verify_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_verify_gateway(update, context)


async def wallet_topup_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_topup_card_start(update, context)


async def wallet_topup_crypto_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_topup_crypto_start(update, context)


async def wallet_select_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_select_amount(update, context)


async def wallet_upload_start_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_upload_start_card(update, context)


async def wallet_upload_start_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_upload_start_crypto(update, context)


async def wallet_upload_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await real_wallet_upload_router(update, context)

