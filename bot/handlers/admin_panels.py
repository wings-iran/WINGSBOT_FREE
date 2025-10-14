from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
import re

from ..db import query_db, execute_db
from ..states import (
    ADMIN_PANELS_MENU,
    ADMIN_PANEL_AWAIT_NAME,
    ADMIN_PANEL_AWAIT_TYPE,
    ADMIN_PANEL_AWAIT_URL,
    ADMIN_PANEL_AWAIT_SUB_BASE,
    ADMIN_PANEL_AWAIT_TOKEN,
    ADMIN_PANEL_AWAIT_USER,
    ADMIN_PANEL_AWAIT_PASS,
    ADMIN_PANEL_AWAIT_DEFAULT_INBOUND,
    ADMIN_PANEL_INBOUNDS_MENU,
    ADMIN_PANEL_INBOUNDS_AWAIT_PROTOCOL,
    ADMIN_PANEL_INBOUNDS_AWAIT_TAG,
)
from ..helpers.tg import safe_edit_text as _safe_edit_text
from ..panel import VpnPanelAPI as PanelAPI


async def admin_panels_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    panels = query_db("SELECT id, name, panel_type, url, COALESCE(sub_base, '') AS sub_base FROM panels ORDER BY id DESC")

    text = "\U0001F4BB **مدیریت پنل‌ها**\n\n"
    keyboard = []

    if not panels:
        text += "هیچ پنلی ثبت نشده است."
    else:
        for p in panels:
            ptype = p['panel_type']
            extra = ''
            if (ptype or '').lower() in ('xui', 'x-ui', 'sanaei'):
                extra = f"\n   \u27A4 sub base: {p.get('sub_base') or '-'}"
            text += f"- {p['name']} ({ptype})\n   URL: {p['url']}{extra}\n"
            keyboard.append([
                InlineKeyboardButton("مدیریت اینباندها", callback_data=f"panel_inbounds_{p['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"panel_delete_{p['id']}")
            ])

    keyboard.insert(0, [InlineKeyboardButton("\u2795 افزودن پنل جدید", callback_data="panel_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")])

    sender = query.message if query else update.message
    if query:
        await _safe_edit_text(sender, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_PANELS_MENU


async def admin_panel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    panel_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM panels WHERE id=?", (panel_id,))
    await query.answer("پنل و اینباندهای مرتبط با آن حذف شدند.", show_alert=True)
    return await admin_panels_menu(update, context)


async def admin_panel_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clear any conflicting flags from other flows to avoid misrouting inputs
    for key in [
        'awaiting', 'awaiting_admin', 'awaiting_ticket', 'next_action', 'action_data',
        'reseller_delete', 'wallet_adjust_direction', 'wallet_adjust_user', 'wallet_adjust_prompt_msg',
        'ticket_reply_id', 'tutorial_edit_id', 'admin_add_prompt_msg_id'
    ]:
        try:
            context.user_data.pop(key, None)
        except Exception:
            pass
    context.user_data['new_panel'] = {}
    await _safe_edit_text(update.callback_query.message, "نام پنل را وارد کنید (مثال: پنل آلمان):")
    return ADMIN_PANEL_AWAIT_NAME


async def admin_panel_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['name'] = update.message.text
    keyboard = [
        [InlineKeyboardButton("Marzban", callback_data="panel_type_marzban")],
        [InlineKeyboardButton("Alireza (X-UI)", callback_data="panel_type_xui")],
        [InlineKeyboardButton("3x-UI", callback_data="panel_type_3xui")],
        [InlineKeyboardButton("TX-UI", callback_data="panel_type_txui")],
        [InlineKeyboardButton("Marzneshin", callback_data="panel_type_marzneshin")],
    ]
    await update.message.reply_text("نوع پنل را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PANEL_AWAIT_TYPE


async def admin_panel_receive_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    p_type = query.data.replace("panel_type_", "").lower()
    mapping = {
        'marzban': 'marzban',
        'xui': 'xui',
        '3xui': '3xui',
        'txui': 'txui',
        'marzneshin': 'marzneshin',
    }
    context.user_data['new_panel']['type'] = mapping.get(p_type, 'xui')
    await _safe_edit_text(
        query.message,
        "آدرس کامل (URL) پنل را وارد کنید\n"
        "- مثال: http://1.2.3.4:2053 یا https://panel.example.com\n"
        "- اگر http/https ننویسید، به‌صورت خودکار http اضافه می‌شود",
    )
    return ADMIN_PANEL_AWAIT_URL


async def admin_panel_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_url = (update.message.text or '').strip().rstrip('/')
    if raw_url and not re.match(r'^[a-zA-Z][a-zA-Z0-9+\.-]*://', raw_url):
        raw_url = f"http://{raw_url}"
    context.user_data['new_panel']['url'] = raw_url
    ptype = context.user_data['new_panel'].get('type')
    if ptype in ('xui', '3xui', 'txui'):
        example = "مثال: http://1.2.3.4:2096 یا http://example.com:2096 یا https://vpn.example.com:8443/app"
        await update.message.reply_text(
            "آدرس پایه ساب‌ لینک (subscription base) را وارد کنید.\n"
            "- می‌تواند دامنه/پورت متفاوت با URL ورود داشته باشد.\n"
            "- اگر مسیر (path) دارد، همان را هم وارد کنید.\n"
            f"{example}\n\n"
            "نکته: اگر http/https ننویسید، به‌صورت خودکار http اضافه می‌شود.\n"
            "نکته: ربات به‌صورت خودکار /sub/{subId} یا /sub/{subId}?name={subId} را با توجه به نوع پنل اضافه می‌کند.")
        return ADMIN_PANEL_AWAIT_SUB_BASE
    # For Marzneshin, do NOT ask for API token here. We will auto-fetch token using username/password.
    # Proceed to ask for admin username directly.
    await update.message.reply_text(
        "نام کاربری (username) ادمین پنل را وارد کنید:\n"
        "- URL و sub base اکنون می‌توانند http و آی‌پی باشند."
    )
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_receive_sub_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sub_base = (update.message.text or '').strip().rstrip('/')
    if sub_base and not re.match(r'^[a-zA-Z][a-zA-Z0-9+\.-]*://', sub_base):
        sub_base = f"http://{sub_base}"
    context.user_data['new_panel']['sub_base'] = sub_base
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['user'] = update.message.text.strip()
    await update.message.reply_text("رمز عبور پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_PASS


async def admin_panel_receive_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives panel password, attempts to connect and fetch inbounds."""
    query = update.callback_query
    if query:
        await query.answer()
    
    context.user_data['new_panel']['pass'] = update.message.text.strip()
    panel_data = context.user_data['new_panel']

    # Construct a temporary panel_row object for the API class
    panel_row = {
        'id': -1,  # Temporary ID
        'panel_type': panel_data['type'],
        'url': panel_data['url'],
        'username': panel_data.get('user'),
        'password': panel_data.get('pass'),
        'token': panel_data.get('token'),
        'sub_base': panel_data.get('sub_base'),
    }

    # Use the main PanelAPI to connect, but we need to select the correct class
    # based on panel_type. The factory function is not available here, so we do it manually.
    ptype = panel_data['type'].lower()
    # For X-UI-like panels, we support direct inbound discovery
    if ptype in ('xui', 'x-ui', 'alireza', 'sanaei'):
        from ..panel import XuiAPI as PanelAPIType
    elif ptype in ('3xui', '3x-ui'):
        from ..panel import ThreeXuiAPI as PanelAPIType
    elif ptype in ('txui', 'tx-ui', 'tx ui'):
        from ..panel import TxUiAPI as PanelAPIType
    elif ptype in ('marzban', 'marzneshin'):
        # For Marzban/Marzneshin, do not attempt direct inbound listing here.
        # Save panel directly and let auto-discovery/refresh happen in the panels menu.
        panel_id = execute_db(
            "INSERT INTO panels (name, panel_type, url, sub_base, token, username, password) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                panel_data['name'],
                panel_data['type'],
                panel_data['url'],
                panel_data.get('sub_base') or '',
                panel_data.get('token') or '',
                panel_data.get('user'),
                panel_data.get('pass')
            )
        )
        if panel_id:
            await update.message.reply_text("✅ پنل Marzban با موفقیت ذخیره شد. از منوی پنل‌ها، گزینه بروزرسانی اینباندها را بزنید تا به‌صورت خودکار کشف شوند.")
        else:
            await update.message.reply_text("❌ خطا در ذخیره پنل در دیتابیس.")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        # Fallback or error for unsupported types
        await update.message.reply_text(f"نوع پنل {ptype} برای اتصال مستقیم پشتیبانی نمی‌شود.")
        return ConversationHandler.END

    api = PanelAPIType(panel_row)

    connecting_message = await update.message.reply_text("در حال اتصال به پنل و دریافت لیست اینباندها...")

    # The list_inbounds() method in the panel API returns a tuple: (inbounds_list, message)
    inbounds, msg = api.list_inbounds()

    if not inbounds:
        error_message = msg or "لیست اینباندها خالی است یا خطایی رخ داده است."
        await connecting_message.edit_text(f"خطا در دریافت اینباندها: {error_message}")
        return ConversationHandler.END
    
    context.user_data['new_panel_inbounds'] = inbounds
    
    text = "اتصال موفقیت‌آمیز بود. لطفاً اینباند پیش‌فرض برای ساخت سرویس را انتخاب کنید:\n\n"
    keyboard = []
    for inbound in inbounds:
        # Use remark as the primary name, fallback to protocol and port
        inbound_name = inbound.get('remark') or f"{inbound.get('protocol', '')}:{inbound.get('port', '')}"
        text += f"- {inbound_name}\n"
        keyboard.append([InlineKeyboardButton(inbound_name, callback_data=f"panel_inbound_{inbound['id']}")])
    
    keyboard.append([InlineKeyboardButton("انصراف", callback_data="cancel")])
    await connecting_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    return ADMIN_PANEL_AWAIT_DEFAULT_INBOUND


async def admin_panel_receive_default_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves the panel and the selected default inbound."""
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel':
        await query.edit_message_text("عملیات لغو شد.")
        context.user_data.clear()
        return ConversationHandler.END

    selected_inbound_id = int(query.data.split('_')[-1])
    inbounds = context.user_data.get('new_panel_inbounds', [])

    selected_inbound = None
    for inbound in inbounds:
        if inbound.get('id') == selected_inbound_id:
            selected_inbound = inbound
            break
    
    if not selected_inbound:
        await query.edit_message_text("خطا: اینباند انتخاب شده یافت نشد. لطفاً دوباره تلاش کنید.")
        return ConversationHandler.END

    # Save panel and the selected inbound
    panel_data = context.user_data['new_panel']
    panel_id = execute_db(
        "INSERT INTO panels (name, panel_type, url, sub_base, token, username, password) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            panel_data['name'],
            panel_data['type'],
            panel_data['url'],
            panel_data.get('sub_base') or '',  # Ensure empty string instead of None
            panel_data.get('token') or '',     # Ensure empty string instead of None
            panel_data.get('user'),
            panel_data.get('pass')
        )
    )
    
    if panel_id:
        # Save the default inbound
        protocol = selected_inbound.get('protocol', 'vless')
        tag = selected_inbound.get('remark') or selected_inbound.get('tag', '')
        inbound_id = selected_inbound.get('id') # Actual inbound ID from panel
        inbound_row_id = execute_db("INSERT INTO panel_inbounds (panel_id, protocol, tag, inbound_id) VALUES (?, ?, ?, ?)", (panel_id, protocol, tag, inbound_id))

        if inbound_row_id:
            await query.edit_message_text("پنل و اینباند پیش‌فرض با موفقیت ذخیره شدند.")
        else:
            # If inbound fails to save, delete the panel to avoid orphaned data
            execute_db("DELETE FROM panels WHERE id = ?", (panel_id,))
            await query.edit_message_text("خطا در ذخیره اینباند پیش‌فرض در دیتابیس. پنل حذف شد.")
    else:
        await query.edit_message_text("خطا در ذخیره پنل در دیتابیس.")

    context.user_data.clear()
    return ConversationHandler.END


async def admin_panel_save_no_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the panel directly without a default inbound."""
    panel_data = context.user_data['new_panel']
    execute_db(
        "INSERT INTO panels (name, panel_type, url, sub_base, token, username, password) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (panel_data['name'], panel_data['type'], panel_data['url'], panel_data.get('sub_base'), panel_data.get('token'), panel_data.get('user'), panel_data.get('pass'))
    )
    context.user_data.clear()


async def admin_panel_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['token'] = update.message.text.strip()
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_inbounds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if 'panel_inbounds_' in query.data:
        panel_id = int(query.data.split('_')[-1])
        context.user_data['editing_panel_id_for_inbounds'] = panel_id
    else:
        panel_id = context.user_data.get('editing_panel_id_for_inbounds')

    if not panel_id:
        await _safe_edit_text(query.message, "خطا: آیدی پنل یافت نشد. لطفا دوباره تلاش کنید.")
        return ADMIN_PANELS_MENU

    await query.answer()

    panel = query_db("SELECT name, panel_type FROM panels WHERE id = ?", (panel_id,), one=True)
    inbounds = query_db("SELECT id, protocol, tag FROM panel_inbounds WHERE panel_id = ? ORDER BY id", (panel_id,))

    text = f" **مدیریت اینباندهای پنل: {panel['name']}**\n\n"
    keyboard = []

    if not inbounds:
        text += "هیچ اینباندی برای این پنل تنظیم نشده است."
    else:
        text += "لیست اینباندها (پروتکل: تگ):\n"
        for i in inbounds:
            keyboard.append([
                InlineKeyboardButton(f"{i['protocol']}: {i['tag']}", callback_data=f"noop_{i['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"inbound_delete_{i['id']}")
            ])

    # Allow auto-refresh for Marzban/Marzneshin types
    ptype = (panel.get('panel_type') or 'marzban').lower() if panel else 'marzban'
    if ptype in ('marzban', 'marzneshin'):
        keyboard.append([InlineKeyboardButton("\U0001F504 بروزرسانی اینباندها", callback_data="inbound_refresh")])
    # Removed add inbound button globally
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به لیست پنل‌ها", callback_data="admin_panels_menu")])

    await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PANEL_INBOUNDS_MENU


# دریافت لیست اینباندها/بروزرسانی - بنا به درخواست کارفرما حذف شد (ورود دستی)


async def admin_panel_inbound_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    inbound_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM panel_inbounds WHERE id = ?", (inbound_id,))
    await query.answer("اینباند با موفقیت حذف شد.", show_alert=True)
    return await admin_panel_inbounds_menu(update, context)


async def admin_panel_inbound_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_inbound'] = {}
    await _safe_edit_text(query.message, "لطفا **پروتکل** اینباند را وارد کنید (مثلا `vless`, `vmess`, `trojan`):")
    return ADMIN_PANEL_INBOUNDS_AWAIT_PROTOCOL


async def admin_panel_inbound_receive_protocol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_inbound']['protocol'] = update.message.text.strip().lower()
    await update.message.reply_text("بسیار خب. حالا **تگ (tag)** دقیق اینباند را وارد کنید:")
    return ADMIN_PANEL_INBOUNDS_AWAIT_TAG


async def admin_panel_inbound_receive_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    panel_id = context.user_data.get('editing_panel_id_for_inbounds')
    if not panel_id:
        await update.message.reply_text("خطا: آیدی پنل یافت نشد. لطفا دوباره تلاش کنید.")
        return await admin_panels_menu(update, context)

    protocol = context.user_data['new_inbound']['protocol']
    tag = update.message.text.strip()

    try:
        execute_db("INSERT INTO panel_inbounds (panel_id, protocol, tag) VALUES (?, ?, ?)", (panel_id, protocol, tag))
        await update.message.reply_text("\u2705 اینباند با موفقیت اضافه شد.")
    except Exception as e:
        await update.message.reply_text(f"\u274C خطا در ذخیره‌سازی: {e}")

    context.user_data.pop('new_inbound', None)

    fake_query = type('obj', (object,), {'data': f"panel_inbounds_{panel_id}", 'message': update.message, 'answer': lambda: None})
    fake_update = type('obj', (object,), {'callback_query': fake_query})
    return await admin_panel_inbounds_menu(fake_update, context)