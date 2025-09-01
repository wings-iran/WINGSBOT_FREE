from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from ..db import query_db, execute_db
from ..states import (
    RENEW_SELECT_PLAN,
    RENEW_AWAIT_DISCOUNT_CODE,
    RENEW_AWAIT_PAYMENT,
)
from ..panel import VpnPanelAPI
from ..helpers.flow import set_flow, clear_flow
from ..helpers.tg import notify_admins


async def start_renewal_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    order_id = int(query.data.split('_')[-1])
    await query.answer()

    context.user_data['renewing_order_id'] = order_id

    plans = query_db("SELECT id, name, price FROM plans ORDER BY price")
    if not plans:
        await query.message.edit_text(
            "در حال حاضر هیچ پلن فعالی برای تمدید وجود ندارد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='my_services')]]),
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"{plan['name']} - {plan['price']:,} تومان", callback_data=f"renew_select_plan_{plan['id']}")] for plan in plans]
    keyboard.append([InlineKeyboardButton("\U0001F519 لغو تمدید", callback_data=f"view_service_{order_id}")])

    text = "\U0001F504 **تمدید سرویس**\n\nلطفا یکی از پلن‌های زیر را برای تمدید انتخاب کنید:"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

    return RENEW_SELECT_PLAN


async def show_renewal_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    plan_id = int(query.data.replace('renew_select_plan_', ''))
    await query.answer()

    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    order_id = context.user_data.get('renewing_order_id')

    if not plan or not order_id:
        await query.message.edit_text(
            "خطا: پلن یا سفارش یافت نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data=f"view_service_{order_id}")]]),
        )
        return ConversationHandler.END

    context.user_data['selected_renewal_plan_id'] = plan_id
    context.user_data['original_price'] = plan['price']
    context.user_data['final_price'] = plan['price']
    context.user_data['discount_code'] = None

    text = (
        f"شما پلن زیر را برای تمدید انتخاب کرده‌اید:\n\n"
        f"**نام پلن:** {plan['name']}\n"
        f"**قیمت:** {plan['price']:,} تومان\n\n"
        f"آیا تایید می‌کنید؟"
    )
    keyboard = [
        [InlineKeyboardButton("\u2705 تایید و پرداخت", callback_data="renew_confirm_purchase")],
        [InlineKeyboardButton("\U0001F381 کد تخفیف دارم", callback_data="renew_apply_discount_start")],
        [InlineKeyboardButton("\U0001F519 لغو", callback_data=f"view_service_{order_id}")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return RENEW_SELECT_PLAN


async def renew_apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.message.edit_text("لطفا کد تخفیف خود را برای تمدید وارد کنید:")
    return RENEW_AWAIT_DISCOUNT_CODE


async def receive_renewal_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    photo_file_id = update.message.photo[-1].file_id
    plan_id = context.user_data.get('selected_renewal_plan_id')
    order_id = context.user_data.get('renewing_order_id')
    final_price = context.user_data.get('final_price')
    discount_code = context.user_data.get('discount_code')

    if not all([plan_id, order_id, final_price is not None]):
        await update.message.reply_text("خطا در فرآیند تمدید. لطفا مجددا تلاش کنید.")
        from ..handlers.common import start_command
        await start_command(update, context)
        return ConversationHandler.END

    original_order = query_db("SELECT marzban_username FROM orders WHERE id = ?", (order_id,), one=True)
    if not original_order:
        await update.message.reply_text("خطا در یافتن سفارش اصلی. لطفا با پشتیبانی تماس بگیرید.")
        return ConversationHandler.END

    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)

    price_info = f"\U0001F4B0 **مبلغ پرداختی:** {final_price:,} تومان"
    if discount_code:
        price_info += f"\n\U0001F381 **کد تخفیف:** `{discount_code}`"
        # Increment discount code usage upon payment for renewal
        execute_db("UPDATE discount_codes SET times_used = times_used + 1 WHERE code = ?", (discount_code,))

    caption = (
        f"\u2757 **درخواست تمدید** (برای سفارش #{order_id})\n\n"
        f"**کاربر:** {user.mention_html()} (`{user.id}`)\n"
        f"**نام کاربری مرزبان:** `{original_order['marzban_username']}`\n"
        f"**پلن تمدید:** {plan['name']}\n"
        f"{price_info}\n\n"
        f"لطفا پس از بررسی، تمدید را تایید کنید:"
    )

    await notify_admins(context.bot, photo=photo_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید و تمدید سرویس", callback_data=f"approve_renewal_{order_id}_{plan_id}")]]))
    await update.message.reply_text("✅ رسید شما برای تمدید ارسال شد. لطفا تا زمان تایید نهایی صبور باشید.")
    context.user_data.pop('awaiting', None)
    clear_flow(context)
    from ..handlers.common import start_command
    await start_command(update, context)
    return ConversationHandler.END


async def process_renewal_for_order(order_id: int, plan_id: int, context: ContextTypes.DEFAULT_TYPE):
    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    if not order or not plan:
        return False, "سفارش یا پلن یافت نشد"
    if not order.get('panel_id'):
        return False, "پنل این سرویس مشخص نیست"
    api = VpnPanelAPI(panel_id=order['panel_id'])
    marz_username = order.get('marzban_username')
    if not marz_username:
        return False, "نام کاربری سرویس ثبت نشده است"
    # For 3x-UI, renew on the same inbound id used at creation
    panel_type = (query_db("SELECT panel_type FROM panels WHERE id = ?", (order['panel_id'],), one=True) or {}).get('panel_type', '').lower()
    if panel_type in ('3xui','3x-ui','3x ui'):
        inbound_id = int(order.get('xui_inbound_id') or 0)
        if inbound_id:
            add_gb = 0.0
            add_days = 0
            try:
                add_gb = float(plan.get('traffic_gb', 0))
            except Exception:
                add_gb = 0.0
            try:
                add_days = int(plan.get('duration_days', 0))
            except Exception:
                add_days = 0
            # Prefer recreate strategy first for 3x-UI for maximum compatibility
            if hasattr(api, 'renew_by_recreate_on_inbound'):
                renewed_user, message = api.renew_by_recreate_on_inbound(inbound_id, marz_username, add_gb, add_days)
                if not renewed_user and hasattr(api, 'renew_user_on_inbound'):
                    renewed_user, message = api.renew_user_on_inbound(inbound_id, marz_username, add_gb, add_days)
            elif hasattr(api, 'renew_user_on_inbound'):
                renewed_user, message = api.renew_user_on_inbound(inbound_id, marz_username, add_gb, add_days)
            else:
                renewed_user, message = await api.renew_user_in_panel(marz_username, plan)
        else:
            renewed_user, message = await api.renew_user_in_panel(marz_username, plan)
    elif panel_type in ('xui','x-ui','sanaei','alireza','txui','tx-ui','tx ui'):
        inbound_id = int(order.get('xui_inbound_id') or 0)
        if inbound_id:
            add_gb = 0.0
            add_days = 0
            try:
                add_gb = float(plan.get('traffic_gb', 0))
            except Exception:
                add_gb = 0.0
            try:
                add_days = int(plan.get('duration_days', 0))
            except Exception:
                add_days = 0
            # Try inbound-based renew first
            if hasattr(api, 'renew_user_on_inbound'):
                renewed_user, message = api.renew_user_on_inbound(inbound_id, marz_username, add_gb, add_days)
            # Fallback to recreate strategy
            if not renewed_user and hasattr(api, 'renew_by_recreate_on_inbound'):
                renewed_user, message = api.renew_by_recreate_on_inbound(inbound_id, marz_username, add_gb, add_days)
        else:
            renewed_user, message = await api.renew_user_in_panel(marz_username, plan)
    else:
        renewed_user, message = await api.renew_user_in_panel(marz_username, plan)
    if renewed_user:
        # Persist new client id if present (for 3x-UI/X-UI recreate paths)
        try:
            new_cid = renewed_user.get('id') or renewed_user.get('uuid')
            if new_cid:
                execute_db("UPDATE orders SET xui_client_id = ? WHERE id = ?", (new_cid, order_id))
        except Exception:
            pass
        return True, "Success"
    try:
        from ..config import logger as _logger
        _logger.error(f"Renew failed for order {order_id} (panel {order['panel_id']} type {panel_type}): {message}")
    except Exception:
        pass
    return False, message