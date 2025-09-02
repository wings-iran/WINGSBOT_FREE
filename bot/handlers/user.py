from datetime import datetime
import requests, base64
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from ..db import query_db, execute_db
from ..panel import VpnPanelAPI
from ..utils import bytes_to_gb
from ..states import WALLET_AWAIT_AMOUNT_GATEWAY, WALLET_AWAIT_AMOUNT_CARD, WALLET_AWAIT_CARD_SCREENSHOT, WALLET_AWAIT_AMOUNT_CRYPTO, WALLET_AWAIT_CRYPTO_SCREENSHOT, RESELLER_AWAIT_UPLOAD
from ..states import SUPPORT_AWAIT_TICKET
from ..config import ADMIN_ID
from ..helpers.tg import ltr_code, notify_admins
from ..helpers.flow import set_flow, clear_flow
import io
try:
    import qrcode
except Exception:
    qrcode = None
import time

# Normalize Persian/Arabic digits to ASCII
_DIGIT_MAP = str.maketrans({
    '۰':'0','۱':'1','۲':'2','۳':'3','۴':'4','۵':'5','۶':'6','۷':'7','۸':'8','۹':'9',
    '٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'
})

def _normalize_amount_text(text: str) -> str:
    if not text:
        return ''
    t = text.translate(_DIGIT_MAP).strip()
    if t.startswith('/'):
        t = t[1:]
    return t


def _fetch_subscription_configs(sub_url: str, timeout_seconds: int = 15) -> list[str]:
    try:
        headers = {
            'Accept': 'text/plain, application/octet-stream, */*',
            'User-Agent': 'Mozilla/5.0',
        }
        r = requests.get(sub_url, headers=headers, timeout=timeout_seconds)
        r.raise_for_status()
        raw = (r.text or '').strip()
        if any(proto in raw for proto in ("vmess://","vless://","trojan://","ss://","hy2://")):
            text = raw
        else:
            compact = "".join(raw.split())
            pad = len(compact) % 4
            if pad:
                compact += "=" * (4 - pad)
            try:
                decoded = base64.b64decode(compact, validate=False)
                text = decoded.decode('utf-8', errors='ignore')
            except Exception:
                text = raw
        lines = [ln.strip() for ln in (text or '').splitlines()]
        return [ln for ln in lines if ln and (ln.startswith('vmess://') or ln.startswith('vless://') or ln.startswith('trojan://') or ln.startswith('ss://') or ln.startswith('hy2://'))]
    except Exception:
        return []


async def get_free_config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query_db("SELECT 1 FROM free_trials WHERE user_id = ?", (user_id,), one=True):
        await context.bot.answer_callback_query(query.id, "شما قبلاً کانفیگ تست خود را دریافت کرده‌اید.", show_alert=True)
        return

    # Use admin-selected panel for free trials if set; fallback to first
    cfg = query_db("SELECT value FROM settings WHERE key = 'free_trial_panel_id'", one=True)
    sel_id = (cfg.get('value') if cfg else '') or ''
    first_panel = None
    if sel_id.isdigit():
        first_panel = query_db("SELECT id FROM panels WHERE id = ?", (int(sel_id),), one=True)
    if not first_panel:
        first_panel = query_db("SELECT id FROM panels ORDER BY id LIMIT 1", one=True)
    if not first_panel:
        await query.message.edit_text(
            "❌ متاسفانه هیچ پنلی برای ارائه سرویس تنظیم نشده است.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
        )
        return

    try:
        await query.message.edit_text("لطفا کمی صبر کنید... \U0001F552")
    except Exception:
        pass

    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings WHERE key LIKE 'free_trial_%'")}
    trial_plan = {'traffic_gb': settings.get('free_trial_gb', '0.2'), 'duration_days': settings.get('free_trial_days', '1')}

    panel_api = VpnPanelAPI(panel_id=first_panel['id'])

    # Quick precheck: ensure at least one inbound is visible to API (best-effort)
    try:
        pre_inb = getattr(panel_api, 'list_inbounds', lambda: (None, 'NA'))()
        if isinstance(pre_inb, tuple):
            pre_list, _ = pre_inb
        else:
            pre_list = pre_inb
        if pre_list is None:
            # continue; maybe API requires create to login first
            pass
    except Exception:
        pass

    try:
        # For XUI-like panels, if a trial inbound is set, create on that inbound directly
        prow = query_db("SELECT panel_type FROM panels WHERE id = ?", (first_panel['id'],), one=True) or {}
        ptype = (prow.get('panel_type') or '').lower()
        trial_inb_row = query_db("SELECT value FROM settings WHERE key='free_trial_inbound_id'", one=True)
        trial_inb = int(trial_inb_row.get('value')) if (trial_inb_row and str(trial_inb_row.get('value') or '').isdigit()) else None
        if ptype in ('xui','x-ui','3xui','3x-ui','alireza','txui','tx-ui','tx ui') and trial_inb is not None and hasattr(panel_api, 'create_user_on_inbound'):
            username_created, sub_link, _msg = panel_api.create_user_on_inbound(trial_inb, user_id, trial_plan)
            marzban_username, config_link, message = username_created, sub_link, _msg
        else:
            marzban_username, config_link, message = await panel_api.create_user(user_id, trial_plan)
    except Exception as e:
        await query.message.edit_text(
            f"❌ ایجاد کاربر تست ناموفق بود.\nجزئیات: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
        )
        return

    if config_link:
        plan_id_row = query_db("SELECT id FROM plans LIMIT 1", one=True)
        plan_id = plan_id_row['id'] if plan_id_row else -1

        # Persist order; for XUI-like with selected inbound, save xui_inbound_id too
        xui_inb = None
        try:
            prow = query_db("SELECT panel_type FROM panels WHERE id = ?", (first_panel['id'],), one=True) or {}
            ptype = (prow.get('panel_type') or '').lower()
            if ptype in ('xui','x-ui','3xui','3x-ui','alireza','txui','tx-ui','tx ui'):
                trial_inb_row = query_db("SELECT value FROM settings WHERE key='free_trial_inbound_id'", one=True)
                if trial_inb_row and str(trial_inb_row.get('value') or '').isdigit():
                    xui_inb = int(trial_inb_row.get('value'))
        except Exception:
            xui_inb = None
        if xui_inb is not None:
            execute_db(
                "INSERT INTO orders (user_id, plan_id, panel_id, status, marzban_username, timestamp, xui_inbound_id, panel_type) VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT panel_type FROM panels WHERE id=?))",
                (user_id, plan_id, first_panel['id'], 'approved', marzban_username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), xui_inb, first_panel['id']),
            )
        else:
            execute_db(
                "INSERT INTO orders (user_id, plan_id, panel_id, status, marzban_username, timestamp, panel_type) VALUES (?, ?, ?, ?, ?, ?, (SELECT panel_type FROM panels WHERE id=?))",
                (user_id, plan_id, first_panel['id'], 'approved', marzban_username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), first_panel['id']),
            )
        execute_db("INSERT INTO free_trials (user_id, timestamp) VALUES (?, ?)", (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        # If panel is XUI-like, send direct configs instead of subscription link
        try:
            ptype_row = query_db("SELECT panel_type FROM panels WHERE id = ?", (first_panel['id'],), one=True) or {}
            ptype = (ptype_row.get('panel_type') or '').lower()
        except Exception:
            ptype = ''
        if ptype in ('xui','x-ui','3xui','3x-ui','alireza','txui','tx-ui','tx ui'):
            confs = []
            ib_id = None
            # Prefer selected trial inbound
            if xui_inb is not None:
                ib_id = xui_inb
            else:
                # Fallback: first inbound
                try:
                    inbs, _m = getattr(panel_api, 'list_inbounds', lambda: (None,'NA'))()
                    if inbs:
                        ib_id = inbs[0].get('id')
                except Exception:
                    ib_id = None
            if ib_id is not None and hasattr(panel_api, 'get_configs_for_user_on_inbound'):
                try:
                    confs = panel_api.get_configs_for_user_on_inbound(int(ib_id), marzban_username) or []
                except Exception:
                    confs = []
            if not confs and isinstance(config_link, str) and config_link.startswith('http'):
                # Decode subscription content as a fallback
                try:
                    confs = _fetch_subscription_configs(config_link)
                except Exception:
                    confs = []
            if confs:
                cfg_text = "\n".join(f"<code>{c}</code>" for c in confs)
                footer = ((query_db("SELECT value FROM settings WHERE key = 'config_footer_text'", one=True) or {}).get('value') or '')
                text = (
                    f"✅ کانفیگ تست رایگان شما با موفقیت ساخته شد!\n\n"
                    f"<b>حجم:</b> {trial_plan['traffic_gb']} گیگابایت\n"
                    f"<b>مدت اعتبار:</b> {trial_plan['duration_days']} روز\n\n"
                    f"<b>کانفیگ شما:</b>\n{cfg_text}\n\n" + footer
                )
                await query.message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
                )
            else:
                # As a last resort, mention link but mark as fallback
                text = (
                    f"✅ کانفیگ تست رایگان شما با موفقیت ساخته شد!\n\n"
                    f"<b>حجم:</b> {trial_plan['traffic_gb']} گیگابایت\n"
                    f"<b>مدت اعتبار:</b> {trial_plan['duration_days']} روز\n\n"
                    f"<b>لینک اشتراک (فقط درصورت نیاز):</b>\n<code>{config_link}</code>\n\n"
                    f"<b>آموزش اتصال :</b>\nhttps://t.me/madeingod_tm"
                )
                await query.message.edit_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
                )
        else:
            # Default: marzban-like, send subscription link
            text = (
                f"✅ کانفیگ تست رایگان شما با موفقیت ساخته شد!\n\n"
                f"<b>حجم:</b> {trial_plan['traffic_gb']} گیگابایت\n"
                f"<b>مدت اعتبار:</b> {trial_plan['duration_days']} روز\n\n"
                f"لینک کانفیگ شما:\n<code>{config_link}</code>\n\n"
                f"<b>آموزش اتصال :</b>\n"
                f"https://t.me/madeingod_tm"
            )
            await query.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
            )
    else:
        # If message is empty, give a generic hint
        reason = message or "اطلاعات کافی از پنل دریافت نشد."
        await query.message.edit_text(
            f"❌ متاسفانه در حال حاضر امکان ارائه کانفیگ تست وجود ندارد.\nخطا: {reason}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت به منو", callback_data='start_main')]]),
        )


async def my_services_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    orders = query_db(
        "SELECT id, marzban_username, plan_id FROM orders WHERE user_id = ? AND status = 'approved' AND marzban_username IS NOT NULL ORDER BY id DESC",
        (user_id,),
    )

    if not orders:
        await query.message.edit_text(
            "شما در حال حاضر هیچ سرویس فعالی ندارید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]),
        )
        return

    keyboard = []
    text = "شما چندین سرویس فعال دارید. لطفاً یکی را برای مشاهده جزئیات و تمدید انتخاب کنید:\n"
    if len(orders) == 1:
        text = "سرویس فعال شما:"

    for order in orders:
        plan = query_db("SELECT name FROM plans WHERE id = ?", (order['plan_id'],), one=True)
        plan_name = plan['name'] if plan else "سرویس تست/ویژه"
        button_text = f"{plan_name} ({order['marzban_username']})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_service_{order['id']}")])

    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به منوی اصلی", callback_data='start_main')])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_specific_service_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = int(query.data.split('_')[-1])
    await query.answer()

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['user_id'] != query.from_user.id:
        await query.message.edit_text(
            "خطا: این سرویس یافت نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]),
        )
        return

    if not order.get('panel_id'):
        await query.message.edit_text(
            "خطا: اطلاعات پنل برای این سرویس یافت نشد. لطفا با پشتیبانی تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='my_services')]]),
        )
        return

    try:
        await query.message.edit_text("در حال دریافت اطلاعات سرویس شما... لطفا صبر کنید \U0001F552")
    except TelegramError:
        pass

    marzban_username = order['marzban_username']
    panel_api = VpnPanelAPI(panel_id=order['panel_id'])
    user_info, message = await panel_api.get_user(marzban_username)

    if not user_info:
        await query.message.edit_text(
            f"خطا در دریافت اطلاعات از پنل: {message}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='my_services')]]),
        )
        return

    # Compute traffic usage and expiry display
    total_bytes = int(user_info.get('data_limit', 0) or 0)
    used_bytes = int(user_info.get('used_traffic', 0) or 0)
    # If total is zero (unlimited), still show used in GB
    data_limit_gb = "نامحدود" if total_bytes == 0 else f"{bytes_to_gb(total_bytes)} گیگابایت"
    data_used_gb = bytes_to_gb(used_bytes)
    # Days remaining
    exp_ts = int(user_info.get('expire', 0) or 0)
    if exp_ts and exp_ts > 0:
        try:
            now_ts = int(datetime.now().timestamp())
            days_left = max(0, int((exp_ts - now_ts) / 86400))
            expire_display = f"{days_left} روز مانده"
        except Exception:
            expire_display = "نامحدود"
    else:
        expire_display = "نامحدود"
    sub_link = (
        f"{panel_api.base_url}{user_info['subscription_url']}"
        if user_info.get('subscription_url') and isinstance(user_info.get('subscription_url'), str) and not user_info['subscription_url'].startswith('http')
        else user_info.get('subscription_url', 'لینک یافت نشد')
    )

    # For 3x-UI/X-UI panels, try to show direct configs instead of sub link
    panel_type = (order.get('panel_type') or '').lower()
    if not panel_type and order.get('panel_id'):
        prow = query_db("SELECT panel_type FROM panels WHERE id = ?", (order['panel_id'],), one=True)
        if prow:
            panel_type = (prow.get('panel_type') or '').lower()
    link_label = "\U0001F517 لینک اشتراک:"
    link_value = f"<code>{sub_link}</code>"
    if panel_type in ('3xui','3x-ui','3x ui','xui','x-ui','sanaei','alireza','txui','tx-ui','tx ui'):
        link_label = "\U0001F517 کانفیگ‌ها:"
        link_value = "کانفیگی یافت نشد. دکمه ‘دریافت لینک مجدد’ را بزنید تا ساخته شود."
        try:
            confs = []
            if hasattr(panel_api, 'list_inbounds') and hasattr(panel_api, 'get_configs_for_user_on_inbound'):
                ib_id = None
                if order.get('xui_inbound_id'):
                    ib_id = int(order['xui_inbound_id'])
                else:
                    inbounds, _m = panel_api.list_inbounds()
                    if inbounds:
                        ib_id = inbounds[0].get('id')
                if ib_id is not None:
                    confs = panel_api.get_configs_for_user_on_inbound(ib_id, marzban_username) or []
            if not confs and sub_link and isinstance(sub_link, str) and sub_link.startswith('http'):
                confs = _fetch_subscription_configs(sub_link)
            if confs:
                link_value = "\n".join(f"<code>{c}</code>" for c in confs[:1])
        except Exception:
            pass
    try:
        execute_db("UPDATE orders SET last_link = ? WHERE id = ?", (sub_link or '', order_id))
    except Exception:
        pass

    text = (
        f"<b>\U0001F4E6 مشخصات سرویس (<code>{marzban_username}</code>)</b>\n\n"
        f"<b>\U0001F4CA حجم کل:</b> {data_limit_gb}\n"
        f"<b>\U0001F4C8 حجم مصرفی:</b> {data_used_gb} گیگابایت\n"
        f"<b>\U0001F4C5 تاریخ انقضا:</b> {expire_display}\n\n"
        f"<b>{link_label}</b>\n{link_value}"
    )

    keyboard = [
        [InlineKeyboardButton("\U0001F504 تمدید این سرویس", callback_data=f"renew_service_{order_id}")],
        [InlineKeyboardButton("\U0001F517 دریافت لینک مجدد", callback_data=f"refresh_service_link_{order_id}")],
        [InlineKeyboardButton("\U0001F511 تغییر کلید اتصال", callback_data=f"revoke_key_{order_id}")],
        [InlineKeyboardButton("\U0001F519 بازگشت به لیست سرویس‌ها", callback_data='my_services')],
    ]
    await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def refresh_service_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        order_id = int(query.data.split('_')[-1])
    except Exception:
        await query.answer("خطا در شناسه سرویس", show_alert=True)
        return ConversationHandler.END
    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['user_id'] != query.from_user.id:
        await query.answer("سرویس یافت نشد", show_alert=True)
        return ConversationHandler.END
    if not order.get('panel_id') or not order.get('marzban_username'):
        await query.answer("اطلاعات سرویس ناقص است", show_alert=True)
        return ConversationHandler.END
    panel_api = VpnPanelAPI(panel_id=order['panel_id'])
    # Determine panel type
    panel_type = (order.get('panel_type') or '').lower()
    if not panel_type and order.get('panel_id'):
        prow = query_db("SELECT panel_type FROM panels WHERE id = ?", (order['panel_id'],), one=True)
        if prow:
            panel_type = (prow.get('panel_type') or '').lower()
    # For 3x-UI/X-UI/TX-UI: build configs instead of sub link
    if panel_type in ('3xui','3x-ui','3x ui','xui','x-ui','sanaei','alireza','txui','tx-ui','tx ui'):
        try:
            # ensure login for 3x-UI
            if hasattr(panel_api, 'get_token'):
                try:
                    panel_api.get_token()
                except Exception:
                    pass
            ib_id = None
            if order.get('xui_inbound_id'):
                ib_id = int(order['xui_inbound_id'])
            else:
                if hasattr(panel_api, 'list_inbounds'):
                    inbounds, _m = panel_api.list_inbounds()
                    if inbounds:
                        ib_id = inbounds[0].get('id')
            if ib_id is None:
                try:
                    await context.bot.send_message(chat_id=query.message.chat_id, text="اینباندی یافت نشد.")
                except Exception:
                    pass
                return ConversationHandler.END
            # try multiple times to account for propagation
            confs = []
            if hasattr(panel_api, 'get_configs_for_user_on_inbound'):
                for _ in range(4):
                    pref_id = (order.get('xui_client_id') or None)
                    confs = panel_api.get_configs_for_user_on_inbound(ib_id, order['marzban_username'], preferred_id=pref_id) or []
                    if confs:
                        break
                    time.sleep(1.0)
            if not confs:
                # decode subscription as fallback for display
                user_info, message = await panel_api.get_user(order['marzban_username'])
                if user_info:
                    sub = (
                        f"{panel_api.base_url}{user_info['subscription_url']}" if user_info.get('subscription_url') and not user_info['subscription_url'].startswith('http') else user_info.get('subscription_url', '')
                    )
                    if sub:
                        confs = _fetch_subscription_configs(sub)
            if not confs:
                try:
                    await context.bot.send_message(chat_id=query.message.chat_id, text="ساخت کانفیگ ناموفق بود - کمی بعد دوباره تلاش کنید.")
                except Exception:
                    pass
                return ConversationHandler.END
            cfg_text = "\n".join(f"<code>{c}</code>" for c in confs)
            sent = False
            if qrcode:
                try:
                    buf = io.BytesIO()
                    qrcode.make(confs[0]).save(buf, format='PNG')
                    buf.seek(0)
                    await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf, caption=("\U0001F517 کانفیگ‌های جدید:\n" + cfg_text), parse_mode=ParseMode.HTML)
                    sent = True
                except Exception:
                    sent = False
            if not sent:
                await context.bot.send_message(chat_id=query.message.chat_id, text=("\U0001F517 کانفیگ‌های جدید:\n" + cfg_text), parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await context.bot.send_message(chat_id=query.message.chat_id, text="خطا در ساخت کانفیگ")
            except Exception:
                pass
        return ConversationHandler.END
    # Default: fetch fresh link from panel
    user_info, message = await panel_api.get_user(order['marzban_username'])
    if not user_info:
        await query.answer("دریافت لینک از پنل ناموفق بود", show_alert=True)
        return ConversationHandler.END
    sub_link = (
        f"{panel_api.base_url}{user_info['subscription_url']}"
        if user_info.get('subscription_url') and not user_info['subscription_url'].startswith('http')
        else user_info.get('subscription_url', 'لینک یافت نشد')
    )
    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"\U0001F517 لینک اشتراک به‌روز شده:\n<code>{sub_link}</code>", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return ConversationHandler.END


async def revoke_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        order_id = int(query.data.split('_')[-1])
    except Exception:
        await query.answer("خطا در شناسه سرویس", show_alert=True)
        return ConversationHandler.END
    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['user_id'] != query.from_user.id:
        await query.answer("سرویس یافت نشد", show_alert=True)
        return ConversationHandler.END
    if not order.get('panel_id') or not order.get('marzban_username'):
        await query.answer("اطلاعات سرویس ناقص است", show_alert=True)
        return ConversationHandler.END
    panel_api = VpnPanelAPI(panel_id=order['panel_id'])
    # Marzneshin or Marzban or 3x-UI
    try:
        import requests as _rq
        # Try to ensure token if available
        if hasattr(panel_api, '_ensure_token'):
            try:
                panel_api._ensure_token()
            except Exception:
                pass
        ok = False
        # Marzneshin endpoint
        try:
            url = f"{panel_api.base_url}/api/users/{order['marzban_username']}/revoke_sub"
            headers = {"Accept": "application/json"}
            if getattr(panel_api, 'token', None):
                headers["Authorization"] = f"Bearer {panel_api.token}"
            r = panel_api.session.post(url, headers=headers, timeout=12)
            ok = (r.status_code in (200, 201, 202, 204))
        except Exception:
            ok = False
        # 3x-UI rotate on specific inbound id first (ensure login)
        if not ok and (order.get('xui_inbound_id') and hasattr(panel_api, 'rotate_user_key_on_inbound')):
            if hasattr(panel_api, 'get_token'):
                try:
                    panel_api.get_token()
                except Exception:
                    pass
            try:
                updated = panel_api.rotate_user_key_on_inbound(int(order['xui_inbound_id']), order['marzban_username'])
                ok = bool(updated)
            except Exception:
                ok = False
        # 3x-UI rotate across inbounds as fallback
        if not ok and hasattr(panel_api, 'rotate_user_key'):
            try:
                ok = bool(panel_api.rotate_user_key(order['marzban_username']))
            except Exception:
                ok = False
        # Marzban fallback
        if not ok and hasattr(panel_api, 'revoke_subscription'):
            try:
                ok, _msg = panel_api.revoke_subscription(order['marzban_username'])
            except Exception:
                ok = False
        if not ok:
            await query.answer("خطا در تغییر کلید", show_alert=True)
            return ConversationHandler.END
        # For 3x-UI: send configs instead of sub link
        panel_type = (order.get('panel_type') or '').lower()
        if not panel_type and order.get('panel_id'):
            prow = query_db("SELECT panel_type FROM panels WHERE id = ?", (order['panel_id'],), one=True)
            if prow:
                panel_type = (prow.get('panel_type') or '').lower()
        if panel_type in ('3xui','3x-ui','3x ui'):
            try:
                await context.bot.send_message(chat_id=query.message.chat_id, text=("\U0001F511 کلید جدید صادر شد، چند لحظه بعد ‘دریافت لینک مجدد’ را بزنید."), parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return ConversationHandler.END
        # X-UI: recreate client to force new UUID and delete old
        if panel_type in ('xui','x-ui','sanaei','alireza') and hasattr(panel_api, 'recreate_user_key_on_inbound'):
            ib_id = None
            if order.get('xui_inbound_id'):
                ib_id = int(order['xui_inbound_id'])
            else:
                try:
                    inbounds, _m = panel_api.list_inbounds()
                    if inbounds:
                        ib_id = inbounds[0].get('id')
                except Exception:
                    ib_id = None
            if ib_id is None:
                await query.answer("اینباندی یافت نشد", show_alert=True)
                return ConversationHandler.END
            new_client = panel_api.recreate_user_key_on_inbound(ib_id, order['marzban_username'])
            if not new_client:
                await query.answer("خطا در تغییر کلید", show_alert=True)
                return ConversationHandler.END
            try:
                # Update username to new email if changed (X-UI path)
                new_username = new_client.get('email') or order['marzban_username']
                execute_db("UPDATE orders SET marzban_username = ?, xui_client_id = ? WHERE id = ?", (new_username, (new_client.get('id') or new_client.get('uuid')), order_id))
            except Exception:
                pass
            # Build and send new config (3x-UI path builder differs; for X-UI we may send sub link or raw config if available)
            try:
                # Try to reuse X-UI/3x-UI config builder with preferred new id
                if hasattr(panel_api, 'get_configs_for_user_on_inbound'):
                    confs = panel_api.get_configs_for_user_on_inbound(ib_id, order['marzban_username'], preferred_id=(new_client.get('id') or new_client.get('uuid'))) or []
                if confs:
                    cfg_text = "\n".join(f"<code>{c}</code>" for c in confs)
                    if qrcode:
                        try:
                            buf = io.BytesIO(); qrcode.make(confs[0]).save(buf, format='PNG'); buf.seek(0)
                            await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf, caption=("\U0001F511 کلید جدید صادر شد:\n" + cfg_text), parse_mode=ParseMode.HTML)
                        except Exception:
                            await context.bot.send_message(chat_id=query.message.chat_id, text=("\U0001F511 کلید جدید صادر شد:\n" + cfg_text), parse_mode=ParseMode.HTML)
                    else:
                        await context.bot.send_message(chat_id=query.message.chat_id, text=("\U0001F511 کلید جدید صادر شد:\n" + cfg_text), parse_mode=ParseMode.HTML)
                    return ConversationHandler.END
                # Fallback to user info/sub link
                info, _m = await panel_api.get_user(order['marzban_username'])
                sub = (info.get('subscription_url') if info else '') or ''
                if sub and not sub.startswith('http'):
                    sub = f"{panel_api.base_url}{sub}"
                caption = f"\U0001F511 کلید جدید صادر شد:\n<code>{sub or 'لینک یافت نشد'}</code>"
                if qrcode and sub:
                    try:
                        buf = io.BytesIO(); qrcode.make(sub).save(buf, format='PNG'); buf.seek(0)
                        await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf, caption=caption, parse_mode=ParseMode.HTML)
                    except Exception:
                        await context.bot.send_message(chat_id=query.message.chat_id, text=caption, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_message(chat_id=query.message.chat_id, text=caption, parse_mode=ParseMode.HTML)
            except Exception:
                await query.answer("خطا در ارسال کانفیگ جدید", show_alert=True)
            return ConversationHandler.END
        # Default: fetch fresh link and send
        user_info, message = await panel_api.get_user(order['marzban_username'])
        if not user_info:
            await query.answer("لینک جدید دریافت نشد", show_alert=True)
            return ConversationHandler.END
        sub_link = (
            f"{panel_api.base_url}{user_info['subscription_url']}"
            if user_info.get('subscription_url') and not user_info['subscription_url'].startswith('http')
            else user_info.get('subscription_url', 'لینک یافت نشد')
        )
        try:
            execute_db("UPDATE orders SET last_link = ? WHERE id = ?", (sub_link or '', order_id))
        except Exception:
            pass
        caption = f"\U0001F511 کلید جدید صادر شد:\n<code>{sub_link}</code>"
        if qrcode:
            try:
                buf = io.BytesIO()
                qrcode.make(sub_link).save(buf, format='PNG')
                buf.seek(0)
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf, caption=caption, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(chat_id=query.message.chat_id, text=caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=caption, parse_mode=ParseMode.HTML)
    except Exception:
        await query.answer("خطا در تغییر کلید", show_alert=True)
    return ConversationHandler.END


async def wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    w = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,), one=True)
    balance = (w['balance'] if w else 0)
    kb = [
        [InlineKeyboardButton("افزایش موجودی (درگاه)", callback_data='wallet_topup_gateway')],
        [InlineKeyboardButton("افزایش موجودی (رمزارز)", callback_data='wallet_topup_crypto')],
        [InlineKeyboardButton("افزایش موجودی (کارت به کارت)", callback_data='wallet_topup_card')],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')],
    ]
    await query.message.edit_text(f"\U0001F4B3 کیف پول شما\n\nموجودی: {balance:,} تومان", reply_markup=InlineKeyboardMarkup(kb))


def _amount_keyboard(method: str) -> InlineKeyboardMarkup:
    amounts = [50000, 100000, 250000, 350000, 500000, 1000000, 2000000]
    rows = []
    row = []
    for i, amt in enumerate(amounts, 1):
        row.append(InlineKeyboardButton(f"{amt:,}", callback_data=f"wallet_amt_{method}_{amt}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='wallet_menu')])
    return InlineKeyboardMarkup(rows)


async def wallet_topup_gateway_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # present preset amounts
    context.user_data['awaiting'] = 'wallet_gateway_amount'
    try:
        last = context.user_data.pop('wallet_prompt_msg_id', None)
        if last:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=last)
    except Exception:
        pass
    sent = await context.bot.send_message(chat_id=query.message.chat_id, text="مبلغ را انتخاب کنید:", reply_markup=_amount_keyboard('gateway'))
    context.user_data['wallet_prompt_msg_id'] = sent.message_id
    return WALLET_AWAIT_AMOUNT_GATEWAY


async def wallet_topup_gateway_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # disabled: only via buttons
    return ConversationHandler.END


async def _wallet_show_gateway_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = context.user_data.get('wallet_topup_amount')
    if not amount:
        await update.message.reply_text("خطا: مبلغ یافت نشد.")
        return ConversationHandler.END
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    gateway_type = (settings.get('gateway_type') or 'zarinpal').lower()
    callback_url = (settings.get('gateway_callback_url') or '').strip()
    amount_rial = int(amount) * 10
    description = "شارژ کیف پول"
    cancel_text = "\U0001F519 لغو"
    cancel_cb = 'wallet_menu'
    if gateway_type == 'zarinpal':
        mid = (settings.get('zarinpal_merchant_id') or '').strip()
        if not mid:
            await update.message.reply_text("MerchantID تنظیم نشده است.")
            return ConversationHandler.END
        from .purchase import _zarinpal_request
        authority, start_url = _zarinpal_request(mid, amount_rial, description, callback_url or 'https://example.com/callback')
        if not (authority and start_url):
            await update.message.reply_text("خطا در ایجاد لینک زرین‌پال.")
            return ConversationHandler.END
        context.user_data['wallet_gateway'] = {'type': 'zarinpal', 'authority': authority, 'amount_rial': amount_rial}
        kb = [
            [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=start_url)],
            [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='wallet_verify_gateway')],
            [InlineKeyboardButton(cancel_text, callback_data=cancel_cb)],
        ]
        await update.message.reply_text(f"\U0001F6E0\uFE0F پرداخت آنلاین\n\nمبلغ: {amount:,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data.pop('awaiting', None)
        context.user_data.pop('wallet_prompt_msg_id', None)
        return ConversationHandler.END
    else:
        pin = (settings.get('aghapay_pin') or '').strip()
        if not pin or not callback_url:
            await update.message.reply_text("PIN یا Callback آقای پرداخت تنظیم نشده است.")
            return ConversationHandler.END
        from .purchase import _aghapay_create
        order_id_str = f"WAL-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        pay_url = _aghapay_create(pin, int(amount), callback_url, order_id_str, description)
        if not pay_url:
            await update.message.reply_text("خطا در ایجاد لینک آقای پرداخت.")
            return ConversationHandler.END
        context.user_data['wallet_gateway'] = {'type': 'aghapay', 'amount_rial': amount_rial, 'transid': pay_url.split('/')[-1]}
        kb = [
            [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=pay_url)],
            [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='wallet_verify_gateway')],
            [InlineKeyboardButton(cancel_text, callback_data=cancel_cb)],
        ]
        await update.message.reply_text(f"\U0001F6E0\uFE0F پرداخت آنلاین\n\nمبلغ: {amount:,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data.pop('awaiting', None)
        context.user_data.pop('wallet_prompt_msg_id', None)
        return ConversationHandler.END


async def wallet_verify_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gw = context.user_data.get('wallet_gateway') or {}
    if not gw:
        await query.message.edit_text("اطلاعات پرداخت یافت نشد.")
        return ConversationHandler.END
    ok = False
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    if gw.get('type') == 'zarinpal':
        from .purchase import _zarinpal_verify
        ok, _ = _zarinpal_verify(settings.get('zarinpal_merchant_id') or '', gw.get('amount_rial', 0), gw.get('authority',''))
    else:
        from .purchase import _aghapay_verify
        ok = _aghapay_verify(settings.get('aghapay_pin') or '', int(context.user_data.get('wallet_topup_amount',0)), gw.get('transid',''))
    if not ok:
        await query.message.edit_text("پرداخت تایید نشد. دوباره امتحان کنید.")
        return ConversationHandler.END
    user_id = query.from_user.id
    amount = context.user_data.get('wallet_topup_amount')
    tx_id = execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at, reference) VALUES (?, ?, 'credit', 'gateway', 'pending', ?, ?)", (user_id, int(amount), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), gw.get('transid','')))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 تایید", callback_data=f"wallet_tx_approve_{tx_id}"), InlineKeyboardButton("\u274C رد", callback_data=f"wallet_tx_reject_{tx_id}")],
        [InlineKeyboardButton("\U0001F4B8 منوی درخواست‌ها", callback_data="admin_wallet_tx_menu")],
    ])
    await notify_admins(
        context.bot,
        text=(f"\U0001F4B8 درخواست شارژ کیف پول (Gateway)\n\n"
              f"کاربر: `{user_id}`\n"
              f"مبلغ: {int(amount):,} تومان\n"
              f"TransID: {gw.get('transid','-')}"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await query.message.edit_text("درخواست شارژ شما ثبت شد و پس از تایید ادمین به موجودی افزوده می‌شود.")
    context.user_data.pop('wallet_gateway', None)
    context.user_data.pop('wallet_topup_amount', None)
    return ConversationHandler.END


async def wallet_topup_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'wallet_card_amount'
    set_flow(context, 'wallet')
    try:
        last = context.user_data.pop('wallet_prompt_msg_id', None)
        if last:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=last)
    except Exception:
        pass
    sent = await context.bot.send_message(chat_id=query.message.chat_id, text="مبلغ را انتخاب کنید:", reply_markup=_amount_keyboard('card'))
    context.user_data['wallet_prompt_msg_id'] = sent.message_id
    return WALLET_AWAIT_AMOUNT_CARD


async def wallet_topup_card_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # disabled: only via buttons
    return ConversationHandler.END


async def wallet_topup_card_receive_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    awaiting = context.user_data.get('awaiting')
    method_hint = context.user_data.get('wallet_method')
    if awaiting not in ('wallet_card_screenshot','wallet_crypto_screenshot'):
        # allow if method + amount present (fallback)
        if method_hint not in ('card','crypto') or not context.user_data.get('wallet_topup_amount'):
            return ConversationHandler.END
        # set awaiting based on method for downstream
        awaiting = 'wallet_card_screenshot' if method_hint == 'card' else 'wallet_crypto_screenshot'
        context.user_data['awaiting'] = awaiting
    # accept photo or document
    photo_file_id = None
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.document:
        photo_file_id = update.message.document.file_id
    if not photo_file_id:
        await update.message.reply_text("لطفا اسکرین‌شات معتبر ارسال کنید.")
        return WALLET_AWAIT_CARD_SCREENSHOT if awaiting == 'wallet_card_screenshot' else WALLET_AWAIT_CRYPTO_SCREENSHOT
    user_id = update.effective_user.id
    amount = context.user_data.get('wallet_topup_amount')
    if not amount:
        await update.message.reply_text("مبلغ یافت نشد. دوباره شروع کنید.")
        return ConversationHandler.END
    method = method_hint or ('card' if awaiting == 'wallet_card_screenshot' else 'crypto')
    tx_id = execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at, screenshot_file_id) VALUES (?, ?, 'credit', ?, 'pending', ?, ?)", (user_id, int(amount), method, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), photo_file_id))
    # Notify admins with photo
    caption = (f"\U0001F4B8 درخواست شارژ کیف پول ({'Card' if method=='card' else 'Crypto'})\n\n"
               f"کاربر: `{user_id}`\n"
               f"مبلغ: {int(amount):,} تومان")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 تایید", callback_data=f"wallet_tx_approve_{tx_id}"), InlineKeyboardButton("\u274C رد", callback_data=f"wallet_tx_reject_{tx_id}")],
        [InlineKeyboardButton("\U0001F4B8 منوی درخواست‌ها", callback_data="admin_wallet_tx_menu")],
    ])
    await notify_admins(context.bot, photo=photo_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await update.message.reply_text("درخواست شارژ ثبت شد و پس از تایید ادمین اعمال می‌شود.")
    context.user_data.pop('wallet_topup_amount', None)
    context.user_data.pop('wallet_method', None)
    context.user_data.pop('awaiting', None)
    return ConversationHandler.END


async def wallet_topup_crypto_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'wallet_crypto_amount'
    set_flow(context, 'wallet')
    try:
        last = context.user_data.pop('wallet_prompt_msg_id', None)
        if last:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=last)
    except Exception:
        pass
    sent = await context.bot.send_message(chat_id=query.message.chat_id, text="مبلغ را انتخاب کنید:", reply_markup=_amount_keyboard('crypto'))
    context.user_data['wallet_prompt_msg_id'] = sent.message_id
    return WALLET_AWAIT_AMOUNT_CRYPTO


async def wallet_topup_crypto_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # disabled: only via buttons
    return ConversationHandler.END


async def wallet_topup_amount_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # disabled
    return ConversationHandler.END


async def wallet_select_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')  # wallet_amt_<method>_<amount>
    if len(parts) != 4:
        return ConversationHandler.END
    method = parts[2]
    try:
        amount = int(parts[3])
    except Exception:
        await query.message.edit_text("مبلغ نامعتبر.")
        return ConversationHandler.END
    context.user_data['wallet_topup_amount'] = amount
    context.user_data['wallet_method'] = method
    if method == 'gateway':
        # call gateway flow using dummy update with message
        dummy = type('obj', (object,), {'message': query.message})
        return await _wallet_show_gateway_message(dummy, context)
    if method == 'card':
        # proceed to card list and then show upload button
        context.user_data['awaiting'] = 'wallet_upload'
        cards = query_db("SELECT card_number, holder_name FROM cards")
        if not cards:
            await query.message.edit_text("خطا: هیچ کارت بانکی در سیستم ثبت نشده است.")
            return ConversationHandler.END
        lines = [f"\U0001F4B0 مبلغ: {amount:,} تومان", "\nبه یکی از کارت‌های زیر واریز کنید و سپس روی دکمه زیر بزنید و رسید را ارسال کنید:"]
        for c in cards:
            lines.append(f"- {c['holder_name']}\n{ltr_code(c['card_number'])}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ارسال اسکرین‌شات", callback_data='wallet_upload_start_card')], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='wallet_menu')]])
        await query.message.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)
        context.user_data.pop('wallet_prompt_msg_id', None)
        return WALLET_AWAIT_CARD_SCREENSHOT
    if method == 'crypto':
        context.user_data['awaiting'] = 'wallet_upload'
        wallets = query_db("SELECT asset, chain, address, memo FROM wallets ORDER BY id DESC")
        if not wallets:
            await query.message.edit_text("هیچ ولتی ثبت نشده است. لطفا بعدا تلاش کنید.")
            return ConversationHandler.END
        lines = ["لطفا مبلغ معادل را به یکی از ولت‌های زیر واریز کرده و سپس روی دکمه زیر بزنید و رسید را ارسال کنید:"]
        for w in wallets:
            memo = f"\nMEMO: {w['memo']}" if w.get('memo') else ''
            lines.append(f"- {w['asset']} ({w['chain']}):\n{w['address']}{memo}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ارسال اسکرین‌شات", callback_data='wallet_upload_start_crypto')], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='wallet_menu')]])
        await query.message.edit_text("\n\n".join(lines), reply_markup=kb)
        context.user_data.pop('wallet_prompt_msg_id', None)
        return WALLET_AWAIT_CRYPTO_SCREENSHOT
    return ConversationHandler.END


async def support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [[InlineKeyboardButton("\U0001F4AC ثبت تیکت", callback_data='ticket_create_start')], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]
    await query.message.edit_text("پشتیبانی ربات\n\nبرای ثبت تیکت روی دکمه زیر بزنید.", reply_markup=InlineKeyboardMarkup(kb))


async def ticket_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("لطفاً پیام/مشکل خود را ارسال کنید. هر نوع پیامی پذیرفته می‌شود.")
    return SUPPORT_AWAIT_TICKET


async def ticket_receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    # State-driven: invoked only in SUPPORT_AWAIT_TICKET
    user_id = update.effective_user.id
    # Persist main ticket row if not exists
    ticket_id = execute_db("INSERT INTO tickets (user_id, content_type, text, file_id, created_at, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                           (user_id, 'meta', '', None, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    # Detect content
    content_type = 'text'
    text = update.message.text or ''
    file_id = None
    if update.message.photo:
        content_type = 'photo'
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        content_type = 'document'
        file_id = update.message.document.file_id
        text = update.message.caption or ''
    elif update.message.video:
        content_type = 'video'
        file_id = update.message.video.file_id
        text = update.message.caption or ''
    elif update.message.voice:
        content_type = 'voice'
        file_id = update.message.voice.file_id
    elif update.message.audio:
        content_type = 'audio'
        file_id = update.message.audio.file_id
    # Save threaded message
    execute_db("INSERT INTO ticket_messages (ticket_id, sender, content_type, text, file_id, created_at) VALUES (?, 'user', ?, ?, ?, ?)",
               (ticket_id, content_type, text, file_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    # Forward original message and controls to all admins
    admin_kb = [[InlineKeyboardButton("✉️ پاسخ", callback_data=f"ticket_reply_{ticket_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"ticket_delete_{ticket_id}")],[InlineKeyboardButton("📨 منوی تیکت‌ها", callback_data='admin_tickets_menu')]]
    summary = f"تیکت #{ticket_id}\nکاربر: `{user_id}`\nزمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    from ..helpers.tg import get_all_admin_ids
    for aid in get_all_admin_ids():
        try:
            await context.bot.forward_message(chat_id=aid, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=aid, text=summary, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(admin_kb))
        except Exception:
            pass
    await update.message.reply_text("✅ تیکت شما ثبت شد. پشتیبانی به زودی پاسخ می‌دهد.")
    return ConversationHandler.END


async def tutorials_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = query_db("SELECT id, title FROM tutorials ORDER BY sort_order, id DESC")
    kb = []
    if not rows:
        await query.message.edit_text("هنوز آموزشی ثبت نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]))
        return
    for r in rows:
        kb.append([InlineKeyboardButton(r['title'], callback_data=f"tutorial_show_{r['id']}")])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')])
    await query.message.edit_text("آموزش‌ها:", reply_markup=InlineKeyboardMarkup(kb))


async def tutorial_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid = int(query.data.split('_')[-1])
    items = query_db("SELECT content_type, file_id, COALESCE(caption,'') AS caption FROM tutorial_media WHERE tutorial_id = ? ORDER BY sort_order, id", (tid,))
    if not items:
        await query.message.edit_text("برای این آموزش محتوایی ثبت نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='tutorials_menu')]]))
        return
    # send sequentially
    for it in items[:20]:
        ct = it['content_type']; fid = it['file_id']; cap = it['caption']
        if ct == 'photo':
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=fid, caption=cap)
        elif ct == 'video':
            await context.bot.send_video(chat_id=query.message.chat_id, video=fid, caption=cap)
        elif ct == 'document':
            await context.bot.send_document(chat_id=query.message.chat_id, document=fid, caption=cap)
        elif ct == 'voice':
            await context.bot.send_voice(chat_id=query.message.chat_id, voice=fid, caption=cap)
        elif ct == 'audio':
            await context.bot.send_audio(chat_id=query.message.chat_id, audio=fid, caption=cap)
        elif ct == 'text':
            await context.bot.send_message(chat_id=query.message.chat_id, text=fid)
    await context.bot.send_message(chat_id=query.message.chat_id, text="پایان آموزش.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='tutorials_menu')]]))


async def referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    # generate deep-link
    link = f"https://t.me/{(await context.bot.get_me()).username}?start={uid}"
    total = query_db("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (uid,), one=True) or {'c': 0}
    buyers = query_db("SELECT COUNT(DISTINCT o.user_id) AS c FROM orders o JOIN referrals r ON r.referee_id = o.user_id WHERE r.referrer_id = ? AND o.status='approved'", (uid,), one=True) or {'c': 0}
    cfg = query_db("SELECT value FROM settings WHERE key = 'referral_commission_percent'", one=True)
    percent = int((cfg.get('value') if cfg else '10') or 10)
    text = (
        "معرفی به دوستان\n\n"
        f"لینک اختصاصی شما:\n{link}\n\n"
        f"تعداد زیرمجموعه‌ها: {int(total.get('c') or 0)}\n"
        f"تعداد خریداران: {int(buyers.get('c') or 0)}\n\n"
        f"در صورتی که افرادی که با لینک شما وارد ربات می‌شوند خرید انجام دهند، {percent}% مبلغ خریدشان به عنوان پاداش به کیف پول شما واریز می‌شود."
    )
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]))


async def reseller_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    # Mark intent so direct uploads are accepted even if button wasn't pressed
    context.user_data['reseller_intent'] = True
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    if settings.get('reseller_enabled', '1') != '1':
        await query.message.edit_text("قابلیت نمایندگی موقتا غیرفعال است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]))
        return ConversationHandler.END
    # If already active reseller and not expired
    rs = query_db("SELECT status, expires_at, used_purchases, max_purchases, discount_percent FROM resellers WHERE user_id = ?", (uid,), one=True)
    if rs:
        text = (
            f"\U0001F4B5 وضعیت نمایندگی شما\n\n"
            f"درصد تخفیف: {int(rs.get('discount_percent') or settings.get('reseller_discount_percent') or 50)}%\n"
            f"سقف خرید: {int(rs.get('used_purchases') or 0)}/{int(rs.get('max_purchases') or settings.get('reseller_max_purchases') or 10)}\n"
            f"انقضا: {rs.get('expires_at')}\n"
        )
        kb = [[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    # Show purchase offer
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    percent = int((settings.get('reseller_discount_percent') or '50') or 50)
    days = int((settings.get('reseller_duration_days') or '30') or 30)
    cap = int((settings.get('reseller_max_purchases') or '10') or 10)
    text = (
        "دریافت نمایندگی\n\n"
        f"با دریافت نمایندگی می‌توانید اشتراک‌ها را با {percent}% تخفیف تهیه کنید.\n"
        f"هزینه دریافت نمایندگی: {fee:,} تومان\n"
        f"سقف خرید اشتراک: {cap} عدد\n"
        f"مدت زمان استفاده: {days} روز\n\n"
        "برای ادامه، روی دکمه زیر بزنید:"
    )
    kb = [[InlineKeyboardButton("پرداخت و دریافت", callback_data='reseller_pay_start')], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def reseller_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['reseller_intent'] = True
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    text = (
        f"پرداخت هزینه نمایندگی ({fee:,} تومان)\n\nروش پرداخت خود را انتخاب کنید:"
    )
    kb = [
        [InlineKeyboardButton("\U0001F4B3 کارت به کارت", callback_data='reseller_pay_card')],
        [InlineKeyboardButton("\U0001F4B0 رمزارز", callback_data='reseller_pay_crypto')],
        [InlineKeyboardButton("\U0001F6E0\uFE0F درگاه پرداخت", callback_data='reseller_pay_gateway')],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_menu')],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def reseller_pay_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    cards = query_db("SELECT card_number, holder_name FROM cards") or []
    if not cards:
        await query.message.edit_text("هیچ کارت بانکی تنظیم نشده است.")
        return ConversationHandler.END
    lines = [f"\U0001F4B0 مبلغ: {fee:,} تومان", "\nبه یکی از کارت‌های زیر واریز کنید و سپس روی دکمه زیر بزنید و رسید را ارسال کنید:"]
    for c in cards:
        lines.append(f"- {c['holder_name']}\n{ltr_code(c['card_number'])}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ارسال اسکرین‌شات", callback_data='reseller_upload_start_card')],[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')]])
    await query.message.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)
    context.user_data['reseller_payment'] = {'method': 'card', 'amount': fee}
    context.user_data['awaiting'] = 'reseller_upload'
    return RESELLER_AWAIT_UPLOAD


async def reseller_pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    wallets = query_db("SELECT asset, chain, address, memo FROM wallets ORDER BY id DESC") or []
    if not wallets:
        await query.message.edit_text("هیچ ولتی ثبت نشده است. لطفا بعدا تلاش کنید.")
        return ConversationHandler.END
    lines = [f"\U0001F4B0 مبلغ: {fee:,} تومان", "لطفا مبلغ معادل را به یکی از ولت‌های زیر واریز کرده و سپس روی دکمه زیر بزنید و رسید را ارسال کنید:"]
    for w in wallets:
        memo = f"\nMEMO: {w['memo']}" if w.get('memo') else ''
        lines.append(f"- {w['asset']} ({w['chain']}):\n{w['address']}{memo}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ارسال اسکرین‌شات", callback_data='reseller_upload_start_crypto')],[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')]])
    await query.message.edit_text("\n\n".join(lines), reply_markup=kb)
    context.user_data['reseller_payment'] = {'method': 'crypto', 'amount': fee}
    context.user_data['awaiting'] = 'reseller_upload'
    return RESELLER_AWAIT_UPLOAD


async def reseller_pay_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    gateway_type = (settings.get('gateway_type') or 'zarinpal').lower()
    callback_url = (settings.get('gateway_callback_url') or '').strip()
    amount_rial = int(fee) * 10
    if gateway_type == 'zarinpal':
        from .purchase import _zarinpal_request
        mid = (settings.get('zarinpal_merchant_id') or '').strip()
        if not mid:
            await query.message.edit_text("MerchantID تنظیم نشده است.")
            return ConversationHandler.END
        authority, start_url = _zarinpal_request(mid, amount_rial, "پرداخت دریافت نمایندگی", callback_url or 'https://example.com/callback')
        if not (authority and start_url):
            await query.message.edit_text("خطا در ایجاد لینک زرین‌پال.")
            return ConversationHandler.END
        context.user_data['reseller_gateway'] = {'type': 'zarinpal', 'authority': authority, 'amount_rial': amount_rial}
        context.user_data['reseller_payment'] = {'method': 'gateway', 'amount': fee}
        context.user_data['awaiting'] = 'reseller_upload'
        kb = [
            [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=start_url)],
            [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='reseller_verify_gateway')],
            [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')],
        ]
        await query.message.edit_text(f"\U0001F6E0\uFE0F پرداخت آنلاین\n\nمبلغ: {fee:,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    else:
        from .purchase import _aghapay_create
        pin = (settings.get('aghapay_pin') or '').strip()
        if not pin or not callback_url:
            await query.message.edit_text("PIN یا Callback آقای پرداخت تنظیم نشده است.")
            return ConversationHandler.END
        order_id_str = f"RES-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        pay_url = _aghapay_create(pin, int(fee), callback_url, order_id_str, "پرداخت دریافت نمایندگی")
        if not pay_url:
            await query.message.edit_text("خطا در ایجاد لینک آقای پرداخت.")
            return ConversationHandler.END
        context.user_data['reseller_gateway'] = {'type': 'aghapay', 'amount_rial': amount_rial, 'transid': pay_url.split('/')[-1]}
        context.user_data['reseller_payment'] = {'method': 'gateway', 'amount': fee}
        context.user_data['awaiting'] = 'reseller_upload'
        kb = [
            [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=pay_url)],
            [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='reseller_verify_gateway')],
            [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')],
        ]
        await query.message.edit_text(f"\U0001F6E0\uFE0F پرداخت آنلاین\n\nمبلغ: {fee:,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END


async def reseller_verify_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gw = context.user_data.get('reseller_gateway') or {}
    if not gw:
        await query.message.edit_text("اطلاعات پرداخت یافت نشد.")
        return ConversationHandler.END
    ok = False
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    if gw.get('type') == 'zarinpal':
        from .purchase import _zarinpal_verify
        ok, ref_id = _zarinpal_verify(settings.get('zarinpal_merchant_id') or '', gw.get('amount_rial', 0), gw.get('authority',''))
        reference = ref_id
    else:
        from .purchase import _aghapay_verify
        fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
        ok = _aghapay_verify(settings.get('aghapay_pin') or '', fee, gw.get('transid',''))
        reference = gw.get('transid','')
    if not ok:
        await query.message.edit_text("پرداخت تایید نشد. دوباره امتحان کنید.")
        return ConversationHandler.END
    # Log request and notify admins
    user = query.from_user
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    rr_id = execute_db(
        "INSERT INTO reseller_requests (user_id, amount, method, status, created_at, reference) VALUES (?, ?, ?, 'pending', ?, ?)",
        (user.id, fee, gw.get('type','gateway'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reference)
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید نمایندگی", callback_data=f"reseller_approve_{rr_id}"), InlineKeyboardButton("\u274C رد", callback_data=f"reseller_reject_{rr_id}")]])
    await notify_admins(context.bot, text=(f"\U0001F4B5 درخواست دریافت نمایندگی\n\nکاربر: `{user.id}`\nمبلغ: {fee:,} تومان\nروش: {gw.get('type','gateway')}\nRef: {reference}"), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await query.message.edit_text("\u2705 پرداخت شما ثبت شد و برای تایید به ادمین ارسال شد. لطفا منتظر بمانید.")
    context.user_data.pop('reseller_gateway', None)
    return ConversationHandler.END


async def reseller_upload_start_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'reseller_upload'
    context.user_data['reseller_payment'] = context.user_data.get('reseller_payment') or {'method': 'card'}
    await query.message.edit_text("رسید/اسکرین‌شات پرداخت نمایندگی را ارسال کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')]]))
    return RESELLER_AWAIT_UPLOAD


async def reseller_upload_start_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'reseller_upload'
    context.user_data['reseller_payment'] = context.user_data.get('reseller_payment') or {'method': 'crypto'}
    await query.message.edit_text("رسید/اسکرین‌شات پرداخت نمایندگی را ارسال کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='reseller_pay_start')]]))
    return RESELLER_AWAIT_UPLOAD


async def reseller_upload_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Accept if explicitly awaiting OR if payment context exists (fallback when user skips the button)
    if context.user_data.get('awaiting') != 'reseller_upload' and not (context.user_data.get('reseller_payment') or context.user_data.get('reseller_intent')):
        return ConversationHandler.END
    user_id = update.effective_user.id
    pay = context.user_data.get('reseller_payment') or {}
    method = pay.get('method') or 'card'
    amount = int(pay.get('amount') or 0)
    if amount <= 0:
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
        amount = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    file_id = None
    caption_extra = ''
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    elif update.message.text:
        caption_extra = update.message.text
    rr_id = execute_db(
        "INSERT INTO reseller_requests (user_id, amount, method, status, created_at, screenshot_file_id, meta) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (user_id, int(amount), method, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), file_id, caption_extra[:500])
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید نمایندگی", callback_data=f"reseller_approve_{rr_id}"), InlineKeyboardButton("\u274C رد", callback_data=f"reseller_reject_{rr_id}")]])
    caption = (f"\U0001F4B5 درخواست دریافت نمایندگی ({'Card' if method=='card' else 'Crypto'})\n\nکاربر: `{user_id}`\nمبلغ: {int(amount):,} تومان")
    if file_id:
        await notify_admins(context.bot, photo=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await notify_admins(context.bot, text=f"{caption}\n\n{caption_extra}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await update.message.reply_text("درخواست نمایندگی ثبت شد و پس از تایید ادمین فعال می‌شود.")
    context.user_data.pop('awaiting', None)
    context.user_data.pop('reseller_payment', None)
    context.user_data.pop('reseller_intent', None)
    return ConversationHandler.END

async def wallet_upload_start_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'wallet_upload'
    context.user_data['wallet_method'] = 'card'
    await query.message.edit_text("رسید/اسکرین‌شات یا هر پیامی مرتبط با پرداخت را ارسال کنید تا برای ادمین ارسال شود.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='wallet_menu')]]))
    return WALLET_AWAIT_CARD_SCREENSHOT


async def wallet_upload_start_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting'] = 'wallet_upload'
    context.user_data['wallet_method'] = 'crypto'
    await query.message.edit_text("رسید/اسکرین‌شات یا هر پیامی مرتبط با پرداخت را ارسال کنید تا برای ادمین ارسال شود.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='wallet_menu')]]))
    return WALLET_AWAIT_CRYPTO_SCREENSHOT


async def wallet_upload_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get('awaiting') != 'wallet_upload':
        return ConversationHandler.END
    user_id = update.effective_user.id
    amount = context.user_data.get('wallet_topup_amount')
    method = context.user_data.get('wallet_method') or 'card'
    if not amount or method not in ('card','crypto'):
        return ConversationHandler.END
    file_id = None
    sent_as = 'text'
    caption_extra = ''
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        sent_as = 'photo'
    elif update.message.document:
        file_id = update.message.document.file_id
        sent_as = 'document'
    elif update.message.text:
        caption_extra = update.message.text
    tx_id = execute_db(
        "INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at, screenshot_file_id, meta) VALUES (?, ?, 'credit', ?, 'pending', ?, ?, ?)",
        (user_id, int(amount), method, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), file_id, caption_extra[:500])
    )
    # notify admin accordingly
    caption = (f"\U0001F4B8 درخواست شارژ کیف پول ({'Card' if method=='card' else 'Crypto'})\n\n"
               f"کاربر: `{user_id}`\n"
               f"مبلغ: {int(amount):,} تومان")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید", callback_data=f"wallet_tx_approve_{tx_id}"), InlineKeyboardButton("\u274C رد", callback_data=f"wallet_tx_reject_{tx_id}")],[InlineKeyboardButton("\U0001F4B8 منوی درخواست‌ها", callback_data="admin_wallet_tx_menu")]])
    if sent_as == 'photo' and file_id:
        await notify_admins(context.bot, photo=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif sent_as == 'document' and file_id:
        await notify_admins(context.bot, document=file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await notify_admins(context.bot, text=f"{caption}\n\n{caption_extra}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await update.message.reply_text("درخواست شارژ ثبت شد و پس از تایید ادمین اعمال می‌شود.")
    context.user_data.pop('awaiting', None)
    context.user_data.pop('wallet_method', None)
    context.user_data.pop('wallet_topup_amount', None)
    clear_flow(context)
    return ConversationHandler.END