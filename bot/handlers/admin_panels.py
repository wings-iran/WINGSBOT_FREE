from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

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
    ADMIN_PANEL_INBOUNDS_MENU,
    ADMIN_PANEL_INBOUNDS_AWAIT_PROTOCOL,
    ADMIN_PANEL_INBOUNDS_AWAIT_TAG,
)
from ..helpers.tg import safe_edit_text as _safe_edit_text


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
    await _safe_edit_text(query.message, "آدرس کامل (URL) پنل را وارد کنید (مثال: https://panel.example.com):")
    return ADMIN_PANEL_AWAIT_URL


async def admin_panel_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['url'] = update.message.text
    ptype = context.user_data['new_panel'].get('type')
    if ptype in ('xui', '3xui', 'txui'):
        example = "مثال: http://example.com:2096 یا https://vpn.example.com:8443/app"
        await update.message.reply_text(
            "آدرس پایه ساب‌ لینک (subscription base) را وارد کنید.\n"
            "- می‌تواند دامنه/پورت متفاوت با URL ورود داشته باشد.\n"
            "- اگر مسیر (path) دارد، همان را هم وارد کنید.\n"
            f"{example}\n\n"
            "نکته: ربات به‌صورت خودکار /sub/{subId} یا /sub/{subId}?name={subId} را با توجه به نوع پنل اضافه می‌کند.")
        return ADMIN_PANEL_AWAIT_SUB_BASE
    if ptype == 'marzneshin':
        await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
        return ADMIN_PANEL_AWAIT_USER
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_receive_sub_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sub_base = update.message.text.strip().rstrip('/')
    context.user_data['new_panel']['sub_base'] = sub_base
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['user'] = update.message.text
    await update.message.reply_text("رمز عبور (password) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_PASS


async def admin_panel_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['token'] = update.message.text.strip()
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['pass'] = update.message.text
    p = context.user_data['new_panel']
    try:
        execute_db(
            "INSERT INTO panels (name, panel_type, url, username, password, sub_base, token) VALUES (?,?,?,?,?,?,?)",
            (p['name'], p.get('type', 'marzban'), p['url'], p.get('user',''), p.get('pass',''), p.get('sub_base'), p.get('token')),
        )
        await update.message.reply_text("\u2705 پنل با موفقیت اضافه شد.")
        context.user_data.clear()
        return await admin_panels_menu(update, context)
    except Exception as e:
        await update.message.reply_text(f"خطا در ذخیره‌سازی: {e}")
        context.user_data.clear()
        return await admin_panels_menu(update, context)


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

    panel = query_db("SELECT name FROM panels WHERE id = ?", (panel_id,), one=True)
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

    keyboard.append([InlineKeyboardButton("\u2795 افزودن اینباند جدید", callback_data="inbound_add_start")])
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