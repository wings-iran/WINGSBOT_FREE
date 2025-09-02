from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..db import query_db, execute_db
from ..states import ADMIN_WALLETS_MENU, ADMIN_WALLETS_AWAIT_ASSET, ADMIN_WALLETS_AWAIT_CHAIN, ADMIN_WALLETS_AWAIT_ADDRESS, ADMIN_WALLETS_AWAIT_MEMO
from ..helpers.tg import safe_edit_text as _safe_edit_text


async def admin_wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    message_sender = None
    if query:
        await query.answer()
        message_sender = 'edit'
    elif update.message:
        message_sender = 'reply'
    if not message_sender:
        return ADMIN_WALLETS_MENU

    wallets = query_db("SELECT id, asset, chain, address, COALESCE(memo,'') AS memo FROM wallets")
    keyboard = []
    text = "\U0001F4B0 **مدیریت ولت‌های رمزارز**\n\n"
    if wallets:
        text += "لیست ولت‌های فعلی:"
        for w in wallets:
            disp = f"{w['asset']} | {w['chain']}"
            keyboard.append([
                InlineKeyboardButton(disp, callback_data=f"noopw_{w['id']}"),
                InlineKeyboardButton("\u270F\uFE0F ویرایش", callback_data=f"wallet_edit_{w['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"wallet_delete_{w['id']}")
            ])
    else:
        text += "هیچ ولتی ثبت نشده است."
    keyboard.append([InlineKeyboardButton("\u2795 افزودن ولت جدید", callback_data="wallet_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به تنظیمات", callback_data="admin_settings_manage")])

    if message_sender == 'edit':
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_WALLETS_MENU


async def admin_wallet_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    wallet_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM wallets WHERE id = ?", (wallet_id,))
    await query.answer("ولت با موفقیت حذف شد.", show_alert=True)
    return await admin_wallets_menu(update, context)


async def admin_wallet_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_wallet'] = {}
    await _safe_edit_text(query.message, "لطفا نام دارایی را وارد کنید (مثال: USDT, BTC):")
    return ADMIN_WALLETS_AWAIT_ASSET


async def admin_wallet_add_receive_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get('editing_wallet_id') and context.user_data.get('editing_wallet_field') == 'asset':
        new_val = (update.message.text or '').strip().upper()
        execute_db("UPDATE wallets SET asset = ? WHERE id = ?", (new_val, context.user_data['editing_wallet_id']))
        await update.message.reply_text("دارایی بروزرسانی شد.")
        context.user_data.pop('editing_wallet_id', None)
        context.user_data.pop('editing_wallet_field', None)
        return await admin_wallets_menu(update, context)
    context.user_data['new_wallet'] = context.user_data.get('new_wallet') or {}
    context.user_data['new_wallet']['asset'] = update.message.text.strip().upper()
    await update.message.reply_text("شبکه/چین را وارد کنید (مثال: TRC20, ERC20, BSC):")
    return ADMIN_WALLETS_AWAIT_CHAIN


async def admin_wallet_add_receive_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get('editing_wallet_id') and context.user_data.get('editing_wallet_field') == 'chain':
        new_val = (update.message.text or '').strip().upper()
        execute_db("UPDATE wallets SET chain = ? WHERE id = ?", (new_val, context.user_data['editing_wallet_id']))
        await update.message.reply_text("شبکه بروزرسانی شد.")
        context.user_data.pop('editing_wallet_id', None)
        context.user_data.pop('editing_wallet_field', None)
        return await admin_wallets_menu(update, context)
    context.user_data['new_wallet']['chain'] = update.message.text.strip().upper()
    await update.message.reply_text("آدرس ولت را وارد کنید:")
    return ADMIN_WALLETS_AWAIT_ADDRESS


async def admin_wallet_add_receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get('editing_wallet_id') and context.user_data.get('editing_wallet_field') == 'address':
        new_val = (update.message.text or '').strip()
        execute_db("UPDATE wallets SET address = ? WHERE id = ?", (new_val, context.user_data['editing_wallet_id']))
        await update.message.reply_text("آدرس بروزرسانی شد.")
        context.user_data.pop('editing_wallet_id', None)
        context.user_data.pop('editing_wallet_field', None)
        return await admin_wallets_menu(update, context)
    context.user_data['new_wallet']['address'] = update.message.text.strip()
    await update.message.reply_text("در صورت نیاز، ممو/تگ را وارد کنید (در غیر اینصورت - یا خالی):")
    return ADMIN_WALLETS_AWAIT_MEMO


async def admin_wallet_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    memo_input = (update.message.text or '').strip()
    if context.user_data.get('editing_wallet_id') and context.user_data.get('editing_wallet_field') == 'memo':
        memo = None if memo_input in ('', '-', 'none', 'null', 'None') else memo_input
        execute_db("UPDATE wallets SET memo = ? WHERE id = ?", (memo, context.user_data['editing_wallet_id']))
        await update.message.reply_text("ممو/تگ بروزرسانی شد.")
        context.user_data.pop('editing_wallet_id', None)
        context.user_data.pop('editing_wallet_field', None)
        return await admin_wallets_menu(update, context)
    memo = None if memo_input in ('', '-', 'none', 'null', 'None') else memo_input
    w = context.user_data['new_wallet']
    execute_db("INSERT INTO wallets (asset, chain, address, memo) VALUES (?, ?, ?, ?)", (w['asset'], w['chain'], w['address'], memo))
    await update.message.reply_text("\u2705 ولت جدید با موفقیت ثبت شد.")
    context.user_data.clear()
    return await admin_wallets_menu(update, context)


async def admin_wallet_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[-1])
    w = query_db("SELECT * FROM wallets WHERE id = ?", (wallet_id,), one=True)
    if not w:
        await query.answer("ولت یافت نشد", show_alert=True)
        return ADMIN_WALLETS_MENU
    context.user_data['editing_wallet_id'] = wallet_id
    text = (
        f"ویرایش ولت:\n\n"
        f"دارایی: {w['asset']}\nشبکه: {w['chain']}\nآدرس: {w['address']}\n"
        f"ممو/تگ: {w.get('memo') or '-'}\n\nکدام مورد را تغییر می‌دهید؟"
    )
    kb = [
        [InlineKeyboardButton("دارایی", callback_data="wallet_edit_field_asset"), InlineKeyboardButton("شبکه", callback_data="wallet_edit_field_chain")],
        [InlineKeyboardButton("آدرس", callback_data="wallet_edit_field_address"), InlineKeyboardButton("ممو/تگ", callback_data="wallet_edit_field_memo")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_wallets_menu")],
    ]
    await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_WALLETS_MENU


async def admin_wallet_edit_ask_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split('_')[-1]
    if 'editing_wallet_id' not in context.user_data:
        await query.answer("جلسه ویرایش منقضی شده است.", show_alert=True)
        return ADMIN_WALLETS_MENU
    context.user_data['editing_wallet_field'] = field
    prompts = {
        'asset': "نام دارایی جدید (مثال: USDT):",
        'chain': "شبکه/چین جدید (مثال: TRC20):",
        'address': "آدرس ولت جدید را وارد کنید:",
        'memo': "ممو/تگ جدید را وارد کنید (برای حذف - بفرستید):",
    }
    await _safe_edit_text(query.message, prompts.get(field, 'مقدار جدید را وارد کنید:'))
    return {
        'asset': ADMIN_WALLETS_AWAIT_ASSET,
        'chain': ADMIN_WALLETS_AWAIT_CHAIN,
        'address': ADMIN_WALLETS_AWAIT_ADDRESS,
        'memo': ADMIN_WALLETS_AWAIT_MEMO,
    }[field]