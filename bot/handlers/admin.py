import asyncio
import io
import csv
import sqlite3
from datetime import datetime
import base64
import requests
import json as _json
from urllib.parse import urlsplit, quote as _urlquote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError, Forbidden, BadRequest
from telegram.ext import ContextTypes, ConversationHandler, ApplicationHandlerStop, MessageHandler
from telegram.ext import filters
from html import escape as html_escape
import re

from ..config import ADMIN_ID, logger
from ..db import query_db, execute_db
from ..panel import VpnPanelAPI
from ..utils import register_new_user
from ..states import *
from .renewal import process_renewal_for_order
from ..helpers.tg import safe_edit_text as _safe_edit_text, safe_edit_caption as _safe_edit_caption

# Normalize Persian/Arabic digits to ASCII
_DIGIT_MAP = str.maketrans({
    '۰':'0','۱':'1','۲':'2','۳':'3','۴':'4','۵':'5','۶':'6','۷':'7','۸':'8','۹':'9',
    '٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'
})

def _normalize_digits(text: str) -> str:
    return (text or '').translate(_DIGIT_MAP)

def _md_escape(text: str) -> str:
    if not text:
        return ''
    # Escape Telegram Markdown V1 special characters
    return (
        text.replace('\\', r'\\')
            .replace('_', r'\_')
            .replace('*', r'\*')
            .replace('`', r'\`')
            .replace('[', r'\[')
            .replace(']', r'\]')
            .replace('(', r'\(')
            .replace(')', r'\)')
    )

def _is_admin(user_id: int) -> bool:
    if int(user_id) == int(ADMIN_ID):
        return True
    row = query_db("SELECT 1 FROM admins WHERE user_id = ?", (user_id,), one=True)
    return bool(row)


async def admin_set_trial_inbound_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # Explain feature and list inbounds for XUI-like panels only
    msg = (
        "انتخاب اینباند کانفیگ تست\n\n"
        "این گزینه فقط برای پنل‌های XUI/3xUI/Alireza/TX-UI کاربرد دارد.\n"
        "اینباندی را انتخاب کنید تا کانفیگ‌های تست روی همان اینباند ساخته شوند."
    )
    # Choose panel first: use selected free_trial_panel_id or ask user to pick if not set
    sel = query_db("SELECT value FROM settings WHERE key='free_trial_panel_id'", one=True)
    panel_id = int((sel.get('value') or 0)) if sel and str(sel.get('value') or '').isdigit() else None
    if not panel_id:
        await _safe_edit_text(query.message, "ابتدا از گزینه 'انتخاب پنل ساخت تست' یک پنل انتخاب کنید.")
        return SETTINGS_MENU
    p = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True)
    if not p:
        await _safe_edit_text(query.message, "پنل انتخاب‌شده یافت نشد. دوباره انتخاب کنید.")
        return SETTINGS_MENU
    ptype = (p.get('panel_type') or '').lower()
    if ptype not in ('xui','x-ui','3xui','3x-ui','alireza','txui','tx-ui','tx ui'):
        await _safe_edit_text(query.message, "این تنظیم فقط برای پنل‌های XUI/3xUI/Alireza/TX-UI است.")
        return SETTINGS_MENU
    api = VpnPanelAPI(panel_id=panel_id)
    inbounds, msg_err = getattr(api, 'list_inbounds', lambda: (None,'NA'))()
    if not inbounds:
        await _safe_edit_text(query.message, f"لیست اینباندها دریافت نشد: {msg_err}")
        return SETTINGS_MENU
    kb = []
    for ib in inbounds[:60]:
        title = f"{ib.get('remark') or ib.get('tag') or ib.get('protocol','inbound')}:{ib.get('port','')}"
        kb.append([InlineKeyboardButton(title, callback_data=f"set_trial_inbound_{ib.get('id')}")])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_settings_manage")])
    await _safe_edit_text(query.message, msg, reply_markup=InlineKeyboardMarkup(kb))
    return SETTINGS_MENU


async def admin_set_trial_inbound_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    inbound_id = query.data.split('_')[-1]
    if not inbound_id.isdigit():
        await query.answer("شناسه نامعتبر", show_alert=True)
        return SETTINGS_MENU
    # Persist setting
    execute_db("INSERT OR REPLACE INTO settings (key, value) VALUES ('free_trial_inbound_id', ?)", (inbound_id,))
    await query.answer("اینباند تست ذخیره شد", show_alert=True)
    return await admin_settings_manage(update, context)

def _fetch_subscription_configs(sub_url: str, timeout_seconds: int = 15) -> list[str]:
    """Fetch subscription content and return a list of config URIs.

    Supports plain-text lists and base64-encoded payloads. Filters known URI schemes.
    """
    try:
        headers = {
            'Accept': 'text/plain, application/octet-stream, */*',
            'User-Agent': 'Mozilla/5.0',
        }
        resp = requests.get(sub_url, headers=headers, timeout=timeout_seconds)
        resp.raise_for_status()
        raw_text = (resp.text or '').strip()
        if any(proto in raw_text for proto in ("vmess://", "vless://", "trojan://", "ss://", "hy2://")):
            text = raw_text
        else:
            # Try base64 decode when content does not directly contain URIs
            b = raw_text.strip()
            # Remove whitespace and fix padding
            compact = "".join(b.split())
            missing = len(compact) % 4
            if missing:
                compact += "=" * (4 - missing)
            try:
                decoded = base64.b64decode(compact, validate=False)
                text = decoded.decode('utf-8', errors='ignore')
            except Exception:
                text = raw_text
        lines = [ln.strip() for ln in (text or '').splitlines()]
        configs = [ln for ln in lines if ln and ln.split(':', 1)[0] in ("vmess", "vless", "trojan", "ss", "hy2") and 
                   (ln.startswith("vmess://") or ln.startswith("vless://") or ln.startswith("trojan://") or ln.startswith("ss://") or ln.startswith("hy2://"))]
        return configs
    except Exception as e:
        logger.error(f"Failed to fetch/parse subscription from {sub_url}: {e}")
        return []

def _infer_origin_host(panel_row: dict) -> str:
    try:
        base = (panel_row.get('sub_base') or panel_row.get('url') or '').strip()
        if not base:
            return ''
        # If scheme-less, prepend http:// to allow urlsplit to parse host for IPs/domains
        if '://' not in base:
            base = f"http://{base}"
        parts = urlsplit(base)
        return parts.hostname or ''
    except Exception:
        return ''

def _build_configs_from_inbound(inbound: dict, username: str, panel_row: dict) -> list[str]:
    """Construct one or more config URIs (vless/vmess) for the created client, using inbound settings.

    This avoids relying on subscription fetches for X-UI-like panels.
    """
    try:
        settings_str = inbound.get('settings')
        try:
            settings = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
        except Exception:
            settings = {}
        clients = settings.get('clients') or []
        if not isinstance(clients, list):
            return []
        client = None
        for c in clients:
            if c.get('email') == username:
                client = c
                break
        if not client:
            return []

        protocol = (inbound.get('protocol') or '').lower()
        port = inbound.get('port') or inbound.get('listen_port') or 0
        remark = inbound.get('remark') or inbound.get('tag') or username

        stream = {}
        try:
            stream = _json.loads(inbound.get('streamSettings')) if isinstance(inbound.get('streamSettings'), str) else (inbound.get('streamSettings') or {})
        except Exception:
            stream = inbound.get('streamSettings') or {}

        network = (stream.get('network') or 'tcp').lower()
        security = (stream.get('security') or 'none').lower()
        tls_obj = stream.get('tlsSettings') or {}
        reality_obj = stream.get('realitySettings') or {}
        ws_obj = stream.get('wsSettings') or {}
        grpc_obj = stream.get('grpcSettings') or {}
        tcp_obj = stream.get('tcpSettings') or {}

        host = _infer_origin_host(panel_row) or (urlsplit(panel_row.get('url','')).hostname or '')
        if not host:
            return []

        def _build_vless() -> str:
            uuid = client.get('id') or client.get('uuid') or ''
            if not uuid:
                return ''
            params = ["encryption=none"]
            # stream params
            if network == 'ws':
                path = ws_obj.get('path') or '/'
                host_header = (ws_obj.get('headers') or {}).get('Host') or host
                params += [f"type=ws", f"path={_urlquote(path)}", f"host={_urlquote(host_header)}"]
            elif network == 'grpc':
                service = grpc_obj.get('serviceName') or ''
                if service:
                    params += ["type=grpc", f"serviceName={_urlquote(service)}", "mode=gun"]
            else:
                # tcp: support HTTP header with host/path if present
                params += [f"type={network}"]
                try:
                    header = (tcp_obj.get('header') or {})
                    htype = (header.get('type') or '').lower()
                    if htype == 'http':
                        # path
                        req = header.get('request') or {}
                        paths = req.get('path') or ['/']
                        if isinstance(paths, list) and paths:
                            params.append(f"path={_urlquote(str(paths[0]) or '/')}")
                        # host header may be list
                        hdrs = req.get('headers') or {}
                        hh = hdrs.get('Host') or hdrs.get('host') or []
                        if isinstance(hh, list) and hh:
                            params.append(f"host={_urlquote(str(hh[0]))}")
                        elif isinstance(hh, str) and hh:
                            params.append(f"host={_urlquote(hh)}")
                        params.append("headerType=http")
                except Exception:
                    pass
            # security
            if security in ('tls', 'xtls'):
                sni = tls_obj.get('serverName') or host
                alpn = tls_obj.get('alpn')
                params += ["security=tls", f"sni={_urlquote(sni)}"]
                if isinstance(alpn, list) and alpn:
                    params.append(f"alpn={_urlquote(','.join(alpn))}")
                params.append("fp=chrome")
            elif security == 'reality':
                sni = (reality_obj.get('serverNames') or [host])[0]
                pbk = reality_obj.get('publicKey') or ''
                sid = (reality_obj.get('shortId') or '')
                params += ["security=reality", f"sni={_urlquote(sni)}"]
                if pbk:
                    params.append(f"pbk={_urlquote(pbk)}")
                if sid:
                    params.append(f"sid={_urlquote(sid)}")
                params.append("fp=chrome")
            else:
                params.append("security=none")
            # assemble
            query = '&'.join(params)
            return f"vless://{uuid}@{host}:{int(port)}?{query}#{_urlquote(str(remark))}"

        def _build_vmess() -> str:
            uuid = client.get('id') or client.get('uuid') or ''
            if not uuid:
                return ''
            vmess_obj = {
                'v': '2',
                'ps': str(remark),
                'add': host,
                'port': str(int(port)),
                'id': uuid,
                'aid': '0',
                'net': network,
                'type': 'none',
                'host': '',
                'path': '',
                'tls': 'tls' if security in ('tls','xtls') else '',
                'sni': tls_obj.get('serverName') or '',
            }
            if network == 'ws':
                vmess_obj['path'] = ws_obj.get('path') or '/'
                vmess_obj['host'] = (ws_obj.get('headers') or {}).get('Host') or host
            data = _json.dumps(vmess_obj, separators=(',',':'), ensure_ascii=False).encode('utf-8')
            b64 = base64.b64encode(data).decode('utf-8')
            return f"vmess://{b64}"

        configs: list[str] = []
        if protocol == 'vless':
            c = _build_vless()
            if c:
                configs.append(c)
        elif protocol == 'vmess':
            c = _build_vmess()
            if c:
                configs.append(c)
        # Could extend to trojan if needed
        return configs
    except Exception as e:
        logger.error(f"Failed to build configs from inbound: {e}")
        return []

def _reset_pending_flows(context: ContextTypes.DEFAULT_TYPE):
    # Safely cancel any pending flows to avoid handler conflicts
    try:
        for key in ['awaiting', 'awaiting_admin', 'awaiting_ticket', 'wallet_prompt_msg_id', 'admin_add_prompt_msg_id']:
            context.user_data.pop(key, None)
    except Exception:
        pass

async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("\U0001F4CB مدیریت پلن‌ها", callback_data='admin_plan_manage'), InlineKeyboardButton("\u2699\uFE0F تنظیمات", callback_data='admin_settings_manage')],
        [InlineKeyboardButton("\U0001F4C8 آمار ربات", callback_data='admin_stats'), InlineKeyboardButton("\U0001F4E4 ارسال همگانی", callback_data='admin_broadcast_menu')],
        [InlineKeyboardButton("\U0001F4DD مدیریت پیام‌ها", callback_data='admin_messages_menu'), InlineKeyboardButton("\U0001F4E8 ارسال پیام با آیدی", callback_data='admin_send_by_id_start')],
        [InlineKeyboardButton("\U0001F381 مدیریت تخفیف‌ها", callback_data='admin_discount_menu'), InlineKeyboardButton("\U0001F4BB مدیریت پنل‌ها", callback_data='admin_panels_menu')],
        [InlineKeyboardButton("\U0001F4BE دریافت بکاپ", callback_data='backup_start'), InlineKeyboardButton("\U0001F514 تست پیام یادآوری", callback_data='admin_test_reminder')],
        [InlineKeyboardButton("\U0001F4AC تیکت‌ها", callback_data='admin_tickets_menu'), InlineKeyboardButton("\U0001F4D6 مدیریت آموزش‌ها", callback_data='admin_tutorials_menu')],
        [InlineKeyboardButton("👑 افزودن ادمین", callback_data='admin_admins_menu')],
        [InlineKeyboardButton("\u274C خروج", callback_data='admin_exit')],
    ]
    text = "\U0001F5A5\uFE0F پنل مدیریت ربات."

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except TelegramError:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    return ADMIN_MAIN_MENU


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    # Optional channel notice
    try:
        chat_id = update.effective_chat.id if update.effective_chat else (update.callback_query.message.chat_id if update.callback_query and update.callback_query.message else None)
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text="کانال اطلاع رسانی\n@wingsbotcr")
    except Exception:
        pass

    # Show admin menu
    return await send_admin_panel(update, context)


# --- Order Review / Approval ---
async def admin_ask_panel_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split('_')[-1])

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['status'] != 'pending':
        is_media = bool(query.message.photo or query.message.video or query.message.document)
        base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')
        new_text = base_text + "\n\n\u26A0\uFE0F این سفارش قبلاً بررسی شده است."
        if is_media:
            await _safe_edit_caption(query.message, new_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, new_text, parse_mode=ParseMode.HTML, reply_markup=None)
        return

    panels = query_db("SELECT id, name, panel_type FROM panels ORDER BY id")
    if not panels:
        await query.message.edit_caption(caption=query.message.caption_html + "\n\n\u274C **خطا:** هیچ پنلی برای ساخت کاربر تعریف نشده است.", parse_mode=ParseMode.HTML, reply_markup=None)
        return

    keyboard = []
    for p in panels:
        label = f"ساخت در: {p['name']} ({p['panel_type']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"approve_on_panel_{order_id}_{p['id']}")])
    await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_approve_on_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("در حال ساخت کانفیگ...")
    *_, order_id, panel_id = query.data.split('_')
    order_id, panel_id = int(order_id), int(panel_id)

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    plan = query_db("SELECT * FROM plans WHERE id = ?", (order['plan_id'],), one=True)
    panel_row = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True)

    is_media = bool(query.message.photo or query.message.video or query.message.document)
    base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')
    progress_text = base_text + "\n\n\u23F3 در حال ساخت کانفیگ..."
    if is_media:
        await _safe_edit_caption(query.message, progress_text, parse_mode=ParseMode.HTML, reply_markup=None)
    else:
        await _safe_edit_text(query.message, progress_text, parse_mode=ParseMode.HTML, reply_markup=None)

    # Branch based on panel type
    ptype = (panel_row.get('panel_type') or 'marzban').lower()
    api = VpnPanelAPI(panel_id=panel_id)

    if ptype in ('xui', 'x-ui', 'sanaei', 'alireza', '3xui', '3x-ui', 'txui', 'tx-ui', 'sui', 's-ui'):
        # Step 1: show inbound list to admin
        inbounds, msg = api.list_inbounds() if hasattr(api, 'list_inbounds') else (None, 'Not supported')
        if not inbounds:
            safe = html_escape(str(msg))
            err_text = base_text + f"\n\n<b>خطای پنل:</b>\n<code>{safe}</code>"
            if is_media:
                await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
            else:
                await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
            return
        # keep context
        context.user_data['pending_xui'] = {'order_id': order_id, 'panel_id': panel_id}
        kb = []
        for ib in inbounds[:50]:
            title = f"{ib.get('remark','') or ib.get('protocol','inbound')}:{ib.get('port', '')}"
            kb.append([InlineKeyboardButton(title, callback_data=f"xui_inbound_{order_id}_{panel_id}_{ib['id']}")])
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        return

    # Default Marzban/Marzneshin flow: only send subscription link
    marzban_username, config_link, message = await api.create_user(order['user_id'], plan)
    if config_link and marzban_username:
        execute_db("UPDATE orders SET status = 'approved', marzban_username = ?, panel_id = ?, panel_type = ? WHERE id = ?", (marzban_username, panel_id, (panel_row.get('panel_type') or 'marzban').lower(), order_id))
        if order.get('discount_code'):
            execute_db("UPDATE discount_codes SET times_used = times_used + 1 WHERE code = ?", (order['discount_code'],))
        # Apply referral bonus
        await _apply_referral_bonus(order_id, context)
        cfg = query_db("SELECT value FROM settings WHERE key = 'config_footer_text'", one=True)
        footer = (cfg.get('value') if cfg else '') or ''
        # Always send ONLY subscription link for Marzban/Marzneshin
        final_message = (
            f"✅ سفارش شما تایید شد!\n\n"
            f"<b>پلن:</b> {plan['name']}\n"
            f"<b>لینک اشتراک شما:</b>\n<code>{config_link}</code>\n\n" + footer
        )
        try:
            await context.bot.send_message(order['user_id'], final_message, parse_mode=ParseMode.HTML)
            done_text = base_text + f"\n\n\u2705 **ارسال خودکار موفق بود.**"
            if is_media:
                await _safe_edit_caption(query.message, done_text, parse_mode=ParseMode.HTML, reply_markup=None)
            else:
                await _safe_edit_text(query.message, done_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except TelegramError as e:
            err_text = base_text + f"\n\n\u26A0\uFE0F **خطا:** ارسال به کاربر ناموفق بود. {e}\nکانفیگ: <code>{config_link}</code>"
            if is_media:
                await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
            else:
                await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
    else:
        fail_text = base_text + f"\n\n\u274C **خطای پنل:** `{message}`"
        if is_media:
            await _safe_edit_caption(query.message, fail_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, fail_text, parse_mode=ParseMode.HTML, reply_markup=None)


async def admin_xui_choose_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("در حال ساخت کلاینت روی اینباند انتخابی...")
    _, _, order_id, panel_id, inbound_id = query.data.split('_', 4)
    order_id, panel_id = int(order_id), int(panel_id)
    # Capture current message meta for later edits
    is_media = bool(query.message.photo or query.message.video or query.message.document)
    base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order:
        err_text = base_text + "\n\n\u274C سفارش یافت نشد."
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    plan = query_db("SELECT * FROM plans WHERE id = ?", (order['plan_id'],), one=True)

    api = VpnPanelAPI(panel_id=panel_id)
    if not hasattr(api, 'create_user_on_inbound'):
        err_text = base_text + "\n\n\u274C این نوع پنل از ساخت بر اساس اینباند پشتیبانی نمی‌کند."
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    username, sub_link, msg = api.create_user_on_inbound(inbound_id, order['user_id'], plan)
    if not sub_link or not username:
        safe = html_escape(str(msg))
        err_text = base_text + f"\n\n<b>خطای پنل:</b>\n<code>{safe}</code>"
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # Build direct configs from inbound where possible; fallback to fetching sub content
    panel_row = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True)
    execute_db("UPDATE orders SET status = 'approved', marzban_username = ?, panel_id = ?, panel_type = ?, xui_inbound_id = ? WHERE id = ?", (username, panel_id, (panel_row.get('panel_type') or 'marzban').lower(), int(inbound_id), order_id))
    if order.get('discount_code'):
        execute_db("UPDATE discount_codes SET times_used = times_used + 1 WHERE code = ?", (order['discount_code'],))

    inbound_detail = getattr(api, '_fetch_inbound_detail', lambda _id: None)(int(inbound_id))
    built_confs = []
    if inbound_detail:
        try:
            built_confs = _build_configs_from_inbound(inbound_detail, username, panel_row) or []
        except Exception:
            built_confs = []
    # If none, try decoding subscription
    if not built_confs:
        built_confs = _fetch_subscription_configs(sub_link)
    # As an extra attempt (but still ensure single output), try API helper only if still empty
    api_confs = []
    if not built_confs and hasattr(api, 'get_configs_for_user_on_inbound'):
        try:
            api_confs = api.get_configs_for_user_on_inbound(int(inbound_id), username) or []
        except Exception:
            api_confs = []
    display_confs = built_confs or api_confs

    footer = ((query_db("SELECT value FROM settings WHERE key = 'config_footer_text'", one=True) or {}).get('value') or '')
    ptype_lower = (panel_row.get('panel_type') or '').lower()
    if display_confs:
        preview = display_confs[:1]  # send only the first config
        configs_text = "\n".join(preview)
        # Build subscription link if available
        sub_line = ""
        try:
            sub_abs = sub_link or ''
            if sub_abs and not sub_abs.startswith('http'):
                sub_abs = f"{api.base_url}{sub_abs}"
            if sub_abs:
                sub_line = f"\n<b>لینک ساب:</b>\n<code>{sub_abs}</code>\n"
        except Exception:
            sub_line = ""
        user_message = (
            f"✅ سفارش شما تایید شد!\n\n"
            f"<b>پلن:</b> {plan['name']}\n"
            f"<b>کانفیگ شما:</b>\n<code>{configs_text}</code>{sub_line}\n" + footer
        )
    else:
        if ptype_lower in ('txui','tx-ui','tx ui'):
            user_message = (
                f"✅ سفارش شما تایید شد!\n\n"
                f"<b>پلن:</b> {plan['name']}\n"
                f"⛔ ساخت مستقیم کانفیگ از اینباند/ساب ناموفق بود. لطفا مجدد تلاش کنید یا تنظیمات اینباند را بررسی کنید."\
                f"\n\n" + footer
            )
        else:
            user_message = (
                f"✅ سفارش شما تایید شد!\n\n"
                f"<b>پلن:</b> {plan['name']}\n"
                f"<b>لینک اشتراک:</b>\n<code>{sub_link}</code>\n\n" + footer
            )
    try:
        await context.bot.send_message(order['user_id'], user_message, parse_mode=ParseMode.HTML)
        # Exit selection mode: clear pending and collapse keyboard
        context.user_data.pop('pending_xui', None)
        ok_text = base_text + f"\n\n\u2705 **ارسال با موفقیت انجام شد.**"
        if is_media:
            await _safe_edit_caption(query.message, ok_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_main')]]))
        else:
            await _safe_edit_text(query.message, ok_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_main')]]))
        # Ensure keyboard is cleared from selection state
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except TelegramError as e:
        err_text = base_text + f"\n\n\u26A0\uFE0F **خطا در ارسال به کاربر:** {e}"
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


async def admin_review_order_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split('_')[-1])
    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['status'] != 'pending':
        is_media = bool(query.message.photo or query.message.video or query.message.document)
        base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')
        new_text = base_text + "\n\n\u26A0\uFE0F این سفارش قبلاً بررسی شده است."
        if is_media:
            await _safe_edit_caption(query.message, new_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, new_text, parse_mode=ParseMode.HTML, reply_markup=None)
        return
    execute_db("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
    try:
        await context.bot.send_message(order['user_id'], "\u274C متاسفانه پرداخت شما تایید نشد. لطفا با پشتیبانی در تماس باشید.")
    except TelegramError:
        pass
    rej_text = base_text + "\n\n\u274C **درخواست رد شد.**"
    if is_media:
        await _safe_edit_caption(query.message, rej_text, parse_mode=ParseMode.HTML, reply_markup=None)
    else:
        await _safe_edit_text(query.message, rej_text, parse_mode=ParseMode.HTML, reply_markup=None)


async def admin_approve_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    *_, order_id, plan_id = query.data.split('_')
    order_id, plan_id = int(order_id), int(plan_id)

    await query.answer("در حال پردازش تمدید...")

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)

    is_media = bool(query.message.photo or query.message.video or query.message.document)
    base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')

    if not order or not plan:
        err_text = base_text + "\n\n\u274C **خطا:** سفارش یا پلن یافت نشد."
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        return

    if not order.get('panel_id'):
        err_text = base_text + "\n\n\u274C **خطا:** پنل این کاربر مشخص نیست."
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        return

    marzban_username = order['marzban_username']
    if not marzban_username:
        is_media = bool(query.message.photo or query.message.video or query.message.document)
        base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')
        err_text = base_text + "\n\n\u274C **خطا:** نام کاربری مرزبان برای این سفارش ثبت نشده است."
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        return

    progress_text = base_text + "\n\n\u23F3 در حال اتصال به پنل و تمدید..."
    if is_media:
        await _safe_edit_caption(query.message, progress_text, parse_mode=ParseMode.HTML, reply_markup=None)
    else:
        await _safe_edit_text(query.message, progress_text, parse_mode=ParseMode.HTML, reply_markup=None)

    from .renewal import process_renewal_for_order
    ok, msg = await process_renewal_for_order(order_id, plan_id, context)

    if ok:
        execute_db("UPDATE orders SET last_reminder_date = NULL WHERE id = ?", (order_id,))
        try:
            await context.bot.send_message(order['user_id'], f"✅ سرویس شما با موفقیت تمدید شد!")
            ok_text = base_text + "\n\n\u2705 **تمدید با موفقیت انجام شد.**"
            if is_media:
                await _safe_edit_caption(query.message, ok_text, parse_mode=ParseMode.HTML, reply_markup=None)
            else:
                await _safe_edit_text(query.message, ok_text, parse_mode=ParseMode.HTML, reply_markup=None)
        except TelegramError as e:
            warn_text = base_text + f"\n\n\u26A0\uFE0F **تمدید انجام شد اما پیام به کاربر ارسال نشد:** {e}"
            if is_media:
                await _safe_edit_caption(query.message, warn_text, parse_mode=ParseMode.HTML, reply_markup=None)
            else:
                await _safe_edit_text(query.message, warn_text, parse_mode=ParseMode.HTML, reply_markup=None)
        # Referral bonus on renewal
        await _apply_referral_bonus(order_id, context)
    else:
        from html import escape as html_escape
        safe = html_escape(str(msg))
        err_text = base_text + f"\n\n\u274C **خطای پنل هنگام تمدید:**\n<code>{safe}</code>"
        if is_media:
            await _safe_edit_caption(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await _safe_edit_text(query.message, err_text, parse_mode=ParseMode.HTML, reply_markup=None)


# --- Discount Code Management ---
async def admin_discount_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    codes = query_db(
        "SELECT id, code, percentage, usage_limit, times_used, strftime('%Y-%m-%d', expiry_date) as expiry FROM discount_codes ORDER BY id DESC"
    )

    text = "\U0001F381 **مدیریت کدهای تخفیف**\n\n"
    keyboard = []

    if not codes:
        text += "در حال حاضر هیچ کد تخفیفی ثبت نشده است."
    else:
        text += "کدهای تخفیف:\n"
        for code in codes:
            limit_str = f"{code['times_used']}/{code['usage_limit']}" if code['usage_limit'] > 0 else f"{code['times_used']}/\u221E"
            expiry_str = f"تا {code['expiry']}" if code['expiry'] else "بی‌نهایت"
            info_str = f"{code['code']} ({code['percentage']}%) - {limit_str} - {expiry_str}"
            keyboard.append([
                InlineKeyboardButton(info_str, callback_data=f"noop_{code['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"delete_discount_{code['id']}")
            ])

    keyboard.insert(0, [InlineKeyboardButton("\u2795 افزودن کد جدید", callback_data="add_discount_code")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به پنل اصلی", callback_data="admin_main")])

    sender = query.message.edit_text if query else update.message.reply_text
    await _safe_edit_text(sender, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_MENU


async def admin_discount_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    code_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM discount_codes WHERE id = ?", (code_id,))
    await query.answer("کد تخفیف با موفقیت حذف شد.", show_alert=True)
    return await admin_discount_menu(update, context)


async def admin_discount_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_discount'] = {}
    await query.message.edit_text("لطفا **کد تخفیف** را وارد کنید (مثال: `OFF20`):", parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_AWAIT_CODE


async def admin_discount_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().upper()
    if query_db("SELECT 1 FROM discount_codes WHERE code = ?", (code,), one=True):
        await update.message.reply_text("این کد تخفیف قبلا ثبت شده. لطفا یک کد دیگر وارد کنید.")
        return DISCOUNT_AWAIT_CODE
    context.user_data['new_discount']['code'] = code
    await update.message.reply_text("لطفا **درصد تخفیف** را به صورت عدد وارد کنید (مثال: `20`):", parse_mode=ParseMode.MARKDOWN)
    return DISCOUNT_AWAIT_PERCENT


async def admin_discount_receive_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        percent = int(update.message.text)
        if not 1 <= percent <= 100:
            raise ValueError()
        context.user_data['new_discount']['percent'] = percent
        await update.message.reply_text("**محدودیت تعداد استفاده** را وارد کنید (برای نامحدود عدد `0` را وارد کنید):", parse_mode=ParseMode.MARKDOWN)
        return DISCOUNT_AWAIT_LIMIT
    except ValueError:
        await update.message.reply_text("ورودی نامعتبر. لطفا فقط یک عدد بین ۱ تا ۱۰۰ وارد کنید.")
        return DISCOUNT_AWAIT_PERCENT


async def admin_discount_receive_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_discount']['limit'] = int(update.message.text)
        await update.message.reply_text("کد تخفیف تا **چند روز دیگر** معتبر باشد؟ (برای نامحدود عدد `0` را وارد کنید):", parse_mode=ParseMode.MARKDOWN)
        return DISCOUNT_AWAIT_EXPIRY
    except ValueError:
        await update.message.reply_text("ورودی نامعتبر. لطفا یک عدد وارد کنید.")
        return DISCOUNT_AWAIT_LIMIT


async def admin_discount_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int(update.message.text)
        expiry_date = (datetime.now() + __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days > 0 else None
        d = context.user_data['new_discount']
        execute_db(
            "INSERT INTO discount_codes (code, percentage, usage_limit, expiry_date, times_used) VALUES (?, ?, ?, ?, 0)",
            (d['code'], d['percent'], d.get('limit', 0), expiry_date),
        )
        await update.message.reply_text(f"\u2705 کد تخفیف `{d['code']}` با موفقیت ساخته شد.")
    except Exception as e:
        await update.message.reply_text(f"\u274C خطا در ذخیره کد تخفیف: {e}")

    context.user_data.clear()
    return await admin_discount_menu(update, context)


# --- Manual reminder check ---
async def admin_run_reminder_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("درحال اجرای دستی بررسی تمدیدها...")
    await query.message.edit_text("⏳ در حال اجرای دستی بررسی تمدیدها... این کار ممکن است کمی طول بکشد. پس از اتمام، به پنل اصلی باز خواهید گشت.")

    from ..jobs import check_expirations
    await check_expirations(context)

    await context.bot.send_message(ADMIN_ID, "✅ بررسی دستی تمدیدها با موفقیت انجام شد.")
    return await send_admin_panel(update, context)


# --- Stateless Admin Actions (Manual Send, Send by ID) ---
async def master_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_admin(update.effective_user.id):
        return

    action = context.user_data.get('next_action')
    # Do not intercept if an admin-specific await flow is active
    if context.user_data.get('awaiting_admin'):
        logger.debug(f"master_message_handler: awaiting_admin active for {update.effective_user.id}; skip intercept")
        return
    if not action:
        return

    logger.debug(f"master_message_handler: intercept action={action} for admin {update.effective_user.id}")
    if action == 'awaiting_manual_order_message':
        await process_manual_order_message(update, context)
    elif action == 'awaiting_user_id_for_send':
        await process_send_by_id_get_id(update, context)
    elif action == 'awaiting_message_for_user_id':
        await process_send_by_id_get_message(update, context)

    raise ApplicationHandlerStop


async def admin_manual_send_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = int(query.data.split('_')[-1])
    await query.answer()

    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order or order['status'] != 'pending':
        try:
            await query.message.edit_caption(caption=(query.message.caption_html or '') + "\n\n\u26A0\uFE0F این سفارش قبلاً بررسی شده است.", parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception:
            await query.message.edit_text((query.message.text_html or 'این درخواست') + "\n\n\u26A0\uFE0F این سفارش قبلاً بررسی شده است.", parse_mode=ParseMode.HTML, reply_markup=None)
        return

    # Detect media vs text
    is_media = bool(query.message.photo or query.message.video or query.message.document)
    base_text = query.message.caption_html if is_media else (query.message.text_html or query.message.text or '')

    context.user_data['next_action'] = 'awaiting_manual_order_message'
    context.user_data['action_data'] = {
        'order_id': order_id,
        'user_id': order['user_id'],
        'original_text': base_text,
        'is_media': is_media,
        'message_id': query.message.message_id,
    }

    prompt = (base_text + f"\n\n\U0001F4DD **ارسال دستی برای سفارش #{order_id}**\n"
              f"لطفا پیامی که میخواهید برای کاربر با آیدی `{order['user_id']}` ارسال شود را بفرستید.\n"
              f"پیام شما با تمام فرمت‌ها و لینک‌ها ارسال خواهد شد.")

    try:
        if is_media:
            await query.message.edit_caption(caption=prompt, parse_mode=ParseMode.HTML)
        else:
            await query.message.edit_text(prompt, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def process_manual_order_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action_data = context.user_data.get('action_data')
    if not action_data:
        return

    target_user_id = action_data['user_id']
    order_id = action_data['order_id']
    original_text = action_data.get('original_text') or ''
    is_media = action_data.get('is_media', False)
    admin_message_id = action_data['message_id']

    try:
        await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id,
        )
        execute_db("UPDATE orders SET status = 'approved', panel_type = (SELECT panel_type FROM panels WHERE id = (SELECT panel_id FROM orders WHERE id = ?)) WHERE id = ?", (order_id, order_id))
        order_row = query_db("SELECT discount_code FROM orders WHERE id = ?", (order_id,), one=True)
        if order_row and order_row.get('discount_code'):
            execute_db("UPDATE discount_codes SET times_used = times_used + 1 WHERE code = ?", (order_row['discount_code'],))

        await update.message.reply_text(f"\u2705 پیام با موفقیت به کاربر `{target_user_id}` ارسال شد.")
        success_text = original_text + f"\n\n\u2705 **ارسال دستی با موفقیت انجام شد.**"
        try:
            if is_media:
                await context.bot.edit_message_caption(
                    chat_id=ADMIN_ID,
                    message_id=admin_message_id,
                    caption=success_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=ADMIN_ID,
                    message_id=admin_message_id,
                    text=success_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
        except TelegramError:
            pass
    except TelegramError as e:
        await update.message.reply_text(f"\u274C خطا در ارسال پیام به `{target_user_id}`: {e}")

    context.user_data.pop('next_action', None)
    context.user_data.pop('action_data', None)


async def admin_send_by_id_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['next_action'] = 'awaiting_user_id_for_send'
    await query.message.edit_text("لطفا آیدی عددی کاربر مورد نظر را ارسال کنید:")


async def process_send_by_id_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text)
        context.user_data['next_action'] = 'awaiting_message_for_user_id'
        context.user_data['action_data'] = {'target_id': user_id}
        await update.message.reply_text(f"آیدی `{user_id}` دریافت شد. اکنون پیام خود را برای ارسال، بفرستید.")
    except ValueError:
        await update.message.reply_text("آیدی نامعتبر است. لطفا فقط عدد وارد کنید.")


async def process_send_by_id_get_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action_data = context.user_data.get('action_data')
    if not action_data:
        return

    user_id = action_data['target_id']
    try:
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id,
        )
        await update.message.reply_text(f"\u2705 پیام با موفقیت به کاربر `{user_id}` ارسال شد.")
    except TelegramError as e:
        await update.message.reply_text(f"\u274C خطا در ارسال پیام به `{user_id}`: {e}")

    context.user_data.pop('next_action', None)
    context.user_data.pop('action_data', None)


# --- Plan Management ---
async def admin_plan_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_sender = None
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message_sender = query.message.edit_text
    elif update.message:
        message_sender = update.message.reply_text
    if not message_sender:
        return ADMIN_PLAN_MENU

    plans = query_db("SELECT id, name, price FROM plans ORDER BY id")
    keyboard = []
    for p in plans:
        keyboard.append([
            InlineKeyboardButton(f"{p['name']} ({p['price']:,} ت)", callback_data=f"noop_{p['id']}"),
            InlineKeyboardButton("\u270F\uFE0F ویرایش", callback_data=f"plan_edit_{p['id']}"),
            InlineKeyboardButton("\u274C حذف", callback_data=f"plan_delete_{p['id']}")
        ])
    keyboard.append([InlineKeyboardButton("\u2795 افزودن پلن جدید", callback_data="plan_add")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")])
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, "مدیریت پلن‌های فروش:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("مدیریت پلن‌های فروش:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PLAN_MENU


async def admin_plan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    plan_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM plans WHERE id=?", (plan_id,))
    await query.answer("پلن حذف شد.", show_alert=True)
    return await admin_plan_manage(update, context)


async def admin_plan_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_plan'] = {}
    await update.callback_query.message.edit_text("نام پلن جدید را وارد کنید (مثال: یک ماهه - ۳۰ گیگ):")
    return ADMIN_PLAN_AWAIT_NAME


async def admin_plan_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_plan']['name'] = update.message.text
    await update.message.reply_text("توضیحات پلن را وارد کنید (مثال: مناسب ترید و وبگردی):")
    return ADMIN_PLAN_AWAIT_DESC


async def admin_plan_receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_plan']['desc'] = update.message.text
    await update.message.reply_text("قیمت پلن به تومان را وارد کنید (فقط عدد):")
    return ADMIN_PLAN_AWAIT_PRICE


async def admin_plan_receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_plan']['price'] = int(update.message.text)
        await update.message.reply_text("مدت اعتبار به روز را وارد کنید (عدد):")
        return ADMIN_PLAN_AWAIT_DAYS
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد وارد کنید.")
        return ADMIN_PLAN_AWAIT_PRICE


async def admin_plan_receive_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_plan']['days'] = int(update.message.text)
        await update.message.reply_text("حجم به گیگابایت را وارد کنید (برای حجم نامحدود، کلمه `نامحدود` را ارسال کنید):")
        return ADMIN_PLAN_AWAIT_GIGABYTES
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد وارد کنید.")
        return ADMIN_PLAN_AWAIT_DAYS


async def admin_plan_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    traffic_input = update.message.text.strip().lower()
    try:
        gb = 0.0 if traffic_input == "نامحدود" else float(traffic_input)
        context.user_data['new_plan']['gb'] = gb
        p = context.user_data['new_plan']

        execute_db(
            "INSERT INTO plans (name, description, price, duration_days, traffic_gb) VALUES (?,?,?,?,?)",
            (p['name'], p['desc'], p['price'], p['days'], p['gb']),
        )

        await update.message.reply_text("\u2705 پلن با موفقیت اضافه شد.")
        context.user_data.clear()
        return await admin_plan_manage(update, context)
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد (مثلا 0.5) یا کلمه `نامحدود` را وارد کنید.")
        return ADMIN_PLAN_AWAIT_GIGABYTES
    except Exception as e:
        logger.error(f"Error saving plan: {e}")
        await update.message.reply_text(f"خطا در ذخیره پلن: {e}")
        context.user_data.clear()
        return await send_admin_panel(update, context)


async def admin_plan_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    plan_id = int(query.data.split('_')[-1])
    context.user_data['editing_plan_id'] = plan_id

    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    if not plan:
        await query.answer("این پلن یافت نشد!", show_alert=True)
        return ADMIN_PLAN_MENU

    traffic_display = "نامحدود" if float(plan['traffic_gb']) == 0 else f"{plan['traffic_gb']} گیگابایت"
    text = (
        f"در حال ویرایش پلن **{plan['name']}**\n\n"
        f"۱. **نام:** {plan['name']}\n"
        f"۲. **توضیحات:** {plan['description']}\n"
        f"۳. **قیمت:** {plan['price']:,} تومان\n"
        f"۴. **مدت:** {plan['duration_days']} روز\n"
        f"۵. **حجم:** {traffic_display}\n\n"
        "کدام مورد را میخواهید ویرایش کنید؟"
    )

    keyboard = [
        [InlineKeyboardButton("نام", callback_data="edit_plan_name"), InlineKeyboardButton("توضیحات", callback_data="edit_plan_description")],
        [InlineKeyboardButton("قیمت", callback_data="edit_plan_price"), InlineKeyboardButton("مدت", callback_data="edit_plan_duration_days")],
        [InlineKeyboardButton("حجم", callback_data="edit_plan_traffic_gb")],
        [InlineKeyboardButton("\U0001F519 بازگشت به لیست پلن‌ها", callback_data="admin_plan_manage")],
    ]
    await _safe_edit_text(query.message, text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PLAN_EDIT_MENU


async def admin_plan_edit_ask_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    field = query.data.replace('edit_plan_', '')
    context.user_data['editing_plan_field'] = field

    prompts = {
        'name': "نام جدید پلن را وارد کنید:",
        'description': "توضیحات جدید را وارد کنید:",
        'price': "قیمت جدید به تومان را وارد کنید (فقط عدد):",
        'duration_days': "مدت اعتبار جدید به روز را وارد کنید (فقط عدد):",
        'traffic_gb': "حجم جدید به گیگابایت را وارد کنید (یا `نامحدود`):",
    }
    await _safe_edit_text(query.message, prompts[field])
    return ADMIN_PLAN_EDIT_AWAIT_VALUE


async def admin_plan_edit_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.get('editing_plan_field')
    plan_id = context.user_data.get('editing_plan_id')
    new_value = update.message.text.strip()

    if not field or not plan_id:
        await update.message.reply_text("خطا! لطفا از ابتدا شروع کنید.")
        return await send_admin_panel(update, context)

    try:
        if field in ['price', 'duration_days']:
            new_value = int(new_value)
        elif field == 'traffic_gb':
            new_value = 0.0 if new_value.lower() == 'نامحدود' else float(new_value)
    except ValueError:
        await update.message.reply_text("مقدار وارد شده نامعتبر است. لطفا مجددا تلاش کنید.")
        return ADMIN_PLAN_EDIT_AWAIT_VALUE

    execute_db(f"UPDATE plans SET {field} = ? WHERE id = ?", (new_value, plan_id))
    await update.message.reply_text("\u2705 پلن با موفقیت بروزرسانی شد.")

    context.user_data.pop('editing_plan_field', None)
    fake_query = type('obj', (object,), {
        'data': f'plan_edit_{plan_id}',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_plan_edit_start(fake_update, context)


# --- Settings, Cards & Panel Management ---
async def admin_settings_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    trial_status = settings.get('free_trial_status', '0')
    trial_button_text = "\u274C غیرفعال کردن تست" if trial_status == '1' else "\u2705 فعال کردن تست"
    trial_button_callback = "set_trial_status_0" if trial_status == '1' else "set_trial_status_1"

    usd_manual = settings.get('usd_irt_manual') or 'تنظیم نشده'
    usd_cached = settings.get('usd_irt_cached') or '-'
    usd_mode = (settings.get('usd_irt_mode') or 'manual').lower()
    mode_title = 'API' if usd_mode == 'api' else 'دستی'
    next_mode = 'manual' if usd_mode == 'api' else 'api'

    pay_card = settings.get('pay_card_enabled', '1') == '1'
    pay_crypto = settings.get('pay_crypto_enabled', '1') == '1'
    pay_gateway = settings.get('pay_gateway_enabled', '0') == '1'
    gateway_type = (settings.get('gateway_type') or 'zarinpal').lower()
    sb_enabled = settings.get('signup_bonus_enabled', '0') == '1'
    sb_amount = int((settings.get('signup_bonus_amount') or '0') or 0)
    trial_panel_id = (settings.get('free_trial_panel_id') or '').strip()
    panels = query_db("SELECT id, name FROM panels ORDER BY id") or []
    trial_panel_name = next((p['name'] for p in panels if str(p['id']) == trial_panel_id), 'پیش‌فرض')
    ref_percent = int((settings.get('referral_commission_percent') or '10') or 10)

    text = (
        f"\u2699\uFE0F **تنظیمات کلی ربات**\n\n"
        f"**وضعیت تست:** {'فعال' if trial_status == '1' else 'غیرفعال'}\n"
        f"**روز تست:** `{settings.get('free_trial_days', '1')}` | **حجم تست:** `{settings.get('free_trial_gb', '0.2')} GB`\n\n"
        f"**پنل ساخت تست:** `{trial_panel_name}`\n\n"
        f"**درصد کمیسیون معرفی:** `{ref_percent}%`\n\n"
        f"**نرخ دلار:** `{usd_manual}`\n"
        f"**آخرین نرخ کش‌شده:** `{usd_cached}`\n"
        f"**حالت نرخ دلار:** `{mode_title}`\n\n"
        f"**پرداخت‌ها:**\n"
        f"- کارت به کارت: {'فعال' if pay_card else 'غیرفعال'}\n"
        f"- رمزارز: {'فعال' if pay_crypto else 'غیرفعال'}\n"
        f"- درگاه پرداخت: {'فعال' if pay_gateway else 'غیرفعال'} ({'زرین‌پال' if gateway_type=='zarinpal' else 'آقای پرداخت'})\n"
        f"\n**موجودی اولیه هدیه:** {'فعال' if sb_enabled else 'غیرفعال'} | مبلغ: `{sb_amount:,}` تومان\n"
        f"\n**متن زیر کانفیگ:**\n{_md_escape((settings.get('config_footer_text') or '').strip()) or '-'}\n"
        f"برای تغییر:\n`/setms`\n`متن_جدید`\n"
    )
    keyboard = [
        [InlineKeyboardButton(trial_button_text, callback_data=trial_button_callback)],
        [InlineKeyboardButton("روز/حجم تست", callback_data="set_trial_days"), InlineKeyboardButton("ویرایش متن پرداخت", callback_data="set_payment_text")],
        [InlineKeyboardButton("انتخاب پنل ساخت تست", callback_data="set_trial_panel_start")],
        [InlineKeyboardButton("اینباند کانفیگ تست", callback_data="set_trial_inbound_start")],
        [InlineKeyboardButton("تنظیم درصد کمیسیون معرفی", callback_data="set_ref_percent_start")],
        [InlineKeyboardButton("\U0001F4B3 مدیریت کارت‌ها", callback_data="admin_cards_menu"), InlineKeyboardButton("\U0001F4B0 مدیریت ولت‌ها", callback_data="admin_wallets_menu")],
        [InlineKeyboardButton("\U0001F4B8 درخواست‌های شارژ کیف پول", callback_data="admin_wallet_tx_menu")],
        [InlineKeyboardButton("\U0001F4B5 تنظیمات نمایندگی", callback_data="admin_reseller_menu")],
        [InlineKeyboardButton("\U0001F4B1 تنظیم نرخ دلار", callback_data="set_usd_rate_start"), InlineKeyboardButton("\U0001F504 تغییر حالت نرخ: " + ("به دستی" if next_mode=='manual' else "به API"), callback_data=f"toggle_usd_mode_{next_mode}")],
        [InlineKeyboardButton(("غیرفعال کردن کارت" if pay_card else "فعال کردن کارت"), callback_data=f"toggle_pay_card_{0 if pay_card else 1}"), InlineKeyboardButton(("غیرفعال کردن رمزارز" if pay_crypto else "فعال کردن رمزارز"), callback_data=f"toggle_pay_crypto_{0 if pay_crypto else 1}")],
        [InlineKeyboardButton(("غیرفعال کردن درگاه" if pay_gateway else "فعال کردن درگاه"), callback_data=f"toggle_pay_gateway_{0 if pay_gateway else 1}"), InlineKeyboardButton(("زرین‌پال" if gateway_type!='zarinpal' else "آقای پرداخت"), callback_data=f"toggle_gateway_type_{'zarinpal' if gateway_type!='zarinpal' else 'aghapay'}")],
        [InlineKeyboardButton(("غیرفعال کردن هدیه ثبت‌نام" if sb_enabled else "فعال کردن هدیه ثبت‌نام"), callback_data=f"toggle_signup_bonus_{0 if sb_enabled else 1}"), InlineKeyboardButton("تنظیم مبلغ هدیه ثبت‌نام", callback_data="set_signup_bonus_amount")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    if query:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return SETTINGS_MENU


async def admin_toggle_trial_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    new_status = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'free_trial_status'", (new_status,))
    await query.answer(f"وضعیت تست رایگان {'فعال' if new_status == '1' else 'غیرفعال'} شد.", show_alert=True)
    return await admin_settings_manage(update, context)


# --- Reseller Settings & Requests ---
async def admin_reseller_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    enabled = settings.get('reseller_enabled', '1') == '1'
    fee = int((settings.get('reseller_fee_toman') or '200000') or 200000)
    percent = int((settings.get('reseller_discount_percent') or '50') or 50)
    days = int((settings.get('reseller_duration_days') or '30') or 30)
    cap = int((settings.get('reseller_max_purchases') or '10') or 10)
    text = (
        "\U0001F4B5 تنظیمات نمایندگی\n\n"
        f"وضعیت: {'فعال' if enabled else 'غیرفعال'}\n"
        f"هزینه: {fee:,} تومان\n"
        f"درصد تخفیف: {percent}%\n"
        f"مدت: {days} روز\n"
        f"سقف خرید: {cap} عدد\n"
    )
    kb = [
        [InlineKeyboardButton(("غیرفعال کردن" if enabled else "فعال کردن"), callback_data=f"toggle_reseller_{0 if enabled else 1}")],
        [InlineKeyboardButton("تنظیم هزینه", callback_data="set_reseller_fee"), InlineKeyboardButton("تنظیم درصد", callback_data="set_reseller_percent")],
        [InlineKeyboardButton("تنظیم مدت", callback_data="set_reseller_days"), InlineKeyboardButton("تنظیم سقف خرید", callback_data="set_reseller_cap")],
        [InlineKeyboardButton("حذف نمایندگی", callback_data="admin_reseller_delete_start")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_settings_manage")],
    ]
    await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    return SETTINGS_MENU


async def admin_toggle_reseller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = query.data.split('_')[-1]
    execute_db("INSERT OR REPLACE INTO settings (key, value) VALUES ('reseller_enabled', ?)", (val,))
    return await admin_reseller_menu(update, context)


async def admin_reseller_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rows = query_db("SELECT id, user_id, amount, method, status, created_at FROM reseller_requests ORDER BY id DESC LIMIT 50") or []
    text = "\U0001F4B5 درخواست‌های نمایندگی\n\n"
    kb = []
    if not rows:
        text += "درخواستی یافت نشد."
    for r in rows:
        line = f"#{r['id']} | کاربر {r['user_id']} | {r['amount']:,} | {r['method']} | {r['status']} | {r['created_at']}"
        kb.append([InlineKeyboardButton(line, callback_data=f"noop_{r['id']}")])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_reseller_menu")])
    await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    return SETTINGS_MENU


async def admin_reseller_set_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mapping = {
        'set_reseller_fee': ('reseller_fee_toman', 'مبلغ جدید (تومان) را وارد کنید:'),
        'set_reseller_percent': ('reseller_discount_percent', 'درصد تخفیف را وارد کنید:'),
        'set_reseller_days': ('reseller_duration_days', 'مدت (روز) را وارد کنید:'),
        'set_reseller_cap': ('reseller_max_purchases', 'سقف تعداد خرید را وارد کنید:'),
    }
    key = mapping[query.data][0]
    prompt = mapping[query.data][1]
    context.user_data['reseller_edit_key'] = key
    await query.message.edit_text(prompt)
    from ..states import ADMIN_RESELLER_AWAIT_VALUE
    return ADMIN_RESELLER_AWAIT_VALUE


async def admin_reseller_set_value_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.user_data.get('reseller_edit_key')
    if not key:
        await update.message.reply_text("جلسه منقضی شده است.")
        return await admin_reseller_menu(update, context)
    val = _normalize_digits(update.message.text.strip())
    execute_db("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    context.user_data.pop('reseller_edit_key', None)
    await update.message.reply_text("ذخیره شد.")
    # Return to reseller menu
    fake_query = type('obj', (object,), {
        'data': 'admin_reseller_menu',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_reseller_menu(fake_update, context)


async def admin_reseller_delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['reseller_delete'] = True
    await _safe_edit_text(query.message, "آیدی عددی کاربر را وارد کنید تا نمایندگی او غیرفعال شود:")
    return SETTINGS_MENU


async def admin_reseller_delete_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get('reseller_delete'):
        return ConversationHandler.END
    txt = (update.message.text or '').strip()
    # Normalize digits
    uid_str = _normalize_digits(txt)
    try:
        uid = int(uid_str)
    except Exception:
        await update.message.reply_text("آیدی نامعتبر است. فقط عدد وارد کنید.")
        return ConversationHandler.END
    # Deactivate reseller
    row = query_db("SELECT 1 FROM resellers WHERE user_id = ?", (uid,), one=True)
    if not row:
        await update.message.reply_text("نمایندگی برای این آیدی یافت نشد.")
        context.user_data.pop('reseller_delete', None)
        return ConversationHandler.END
    execute_db("UPDATE resellers SET status='inactive' WHERE user_id = ?", (uid,))
    try:
        await update.message.reply_text("نمایندگی کاربر غیرفعال شد.")
    except Exception:
        pass
    try:
        await update.get_bot().send_message(chat_id=uid, text="نمایندگی شما توسط ادمین غیرفعال شد.")
    except Exception:
        pass
    context.user_data.pop('reseller_delete', None)
    # Return to reseller menu
    fake_query = type('obj', (object,), {
        'data': 'admin_reseller_menu',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_reseller_menu(fake_update, context)


async def admin_reseller_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rr_id = int(query.data.split('_')[-1])
    rr = query_db("SELECT * FROM reseller_requests WHERE id = ?", (rr_id,), one=True)
    if not rr or rr.get('status') != 'pending':
        await query.answer("این درخواست قبلا بررسی شده است.", show_alert=True)
        return SETTINGS_MENU
    # Activate reseller for user
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")} or {}
    percent = int((settings.get('reseller_discount_percent') or '50') or 50)
    days = int((settings.get('reseller_duration_days') or '30') or 30)
    cap = int((settings.get('reseller_max_purchases') or '10') or 10)
    from datetime import datetime, timedelta
    now = datetime.now()
    exp = now + timedelta(days=days)
    execute_db("INSERT OR REPLACE INTO resellers (user_id, status, activated_at, expires_at, discount_percent, max_purchases, used_purchases) VALUES (?, 'active', ?, ?, ?, ?, COALESCE((SELECT used_purchases FROM resellers WHERE user_id = ?), 0))",
               (rr['user_id'], now.strftime("%Y-%m-%d %H:%M:%S"), exp.strftime("%Y-%m-%d %H:%M:%S"), percent, cap, rr['user_id']))
    execute_db("UPDATE reseller_requests SET status='approved' WHERE id=?", (rr_id,))
    try:
        await context.bot.send_message(rr['user_id'], "\u2705 نمایندگی شما فعال شد. از این پس پلن‌ها با تخفیف برای شما نمایش داده می‌شوند.")
    except Exception:
        pass
    await query.message.edit_reply_markup(reply_markup=None)
    return SETTINGS_MENU


async def admin_reseller_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rr_id = int(query.data.split('_')[-1])
    rr = query_db("SELECT * FROM reseller_requests WHERE id = ?", (rr_id,), one=True)
    if not rr or rr.get('status') != 'pending':
        await query.answer("این درخواست قبلا بررسی شده است.", show_alert=True)
        return SETTINGS_MENU
    execute_db("UPDATE reseller_requests SET status='rejected' WHERE id=?", (rr_id,))
    try:
        await context.bot.send_message(rr['user_id'], "\u274C درخواست نمایندگی شما رد شد. برای اطلاعات بیشتر با پشتیبانی در تماس باشید.")
    except Exception:
        pass
    await query.message.edit_reply_markup(reply_markup=None)
    return SETTINGS_MENU


async def admin_toggle_usd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    target = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'usd_irt_mode'", (target,))
    return await admin_settings_manage(update, context)


async def admin_cards_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_sender = None
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message_sender = query.message.edit_text
    elif update.message:
        message_sender = update.message.reply_text
    if not message_sender:
        return ADMIN_CARDS_MENU

    cards = query_db("SELECT id, card_number, holder_name FROM cards")
    keyboard = []
    text = "\U0001F4B3 **مدیریت کارت‌های بانکی**\n\n"
    if cards:
        text += "لیست کارت‌های فعلی:"
        for card in cards:
            keyboard.append([
                InlineKeyboardButton(f"{card['card_number']}", callback_data=f"noop_{card['id']}"),
                InlineKeyboardButton("\u270F\uFE0F ویرایش", callback_data=f"card_edit_{card['id']}"),
                InlineKeyboardButton("\u274C حذف", callback_data=f"card_delete_{card['id']}")
            ])
    else:
        text += "هیچ کارتی ثبت نشده است."
    keyboard.append([InlineKeyboardButton("\u2795 افزودن کارت جدید", callback_data="card_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به تنظیمات", callback_data="admin_settings_manage")])
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_CARDS_MENU


async def admin_card_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    card_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM cards WHERE id = ?", (card_id,))
    await query.answer("کارت با موفقیت حذف شد.", show_alert=True)
    return await admin_cards_menu(update, context)


async def admin_card_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    card_id = int(query.data.split('_')[-1])
    card = query_db("SELECT id, card_number, holder_name FROM cards WHERE id = ?", (card_id,), one=True)
    if not card:
        await query.answer("کارت یافت نشد", show_alert=True)
        return ADMIN_CARDS_MENU
    context.user_data['editing_card_id'] = card_id
    text = (
        f"ویرایش کارت:\n\n"
        f"شماره فعلی: {card['card_number']}\n"
        f"نام دارنده: {card['holder_name']}\n\n"
        f"کدام مورد را می‌خواهید تغییر دهید؟"
    )
    kb = [
        [InlineKeyboardButton("شماره کارت", callback_data="card_edit_field_number"), InlineKeyboardButton("نام دارنده", callback_data="card_edit_field_holder")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_cards_menu")],
    ]
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_CARDS_MENU


async def admin_card_edit_ask_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split('_')[-1]  # number|holder
    if 'editing_card_id' not in context.user_data:
        await query.answer("جلسه ویرایش منقضی شده است.", show_alert=True)
        return ADMIN_CARDS_MENU
    context.user_data['editing_card_field'] = field
    if field == 'number':
        await query.message.edit_text("شماره کارت جدید (۱۶ رقمی) را وارد کنید:")
        return ADMIN_CARDS_AWAIT_NUMBER
    else:
        await query.message.edit_text("نام و نام خانوادگی جدید صاحب کارت را وارد کنید:")
        return ADMIN_CARDS_AWAIT_HOLDER


async def admin_card_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_card'] = {}
    await query.message.edit_text("لطفا **شماره کارت** ۱۶ رقمی را وارد کنید:")
    return ADMIN_CARDS_AWAIT_NUMBER


async def admin_card_add_receive_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # If editing number
    editing_id = context.user_data.get('editing_card_id')
    editing_field = context.user_data.get('editing_card_field')
    if editing_id and editing_field == 'number':
        new_number = update.message.text.strip()
        execute_db("UPDATE cards SET card_number = ? WHERE id = ?", (new_number, editing_id))
        await update.message.reply_text("شماره کارت بروزرسانی شد.")
        context.user_data.pop('editing_card_id', None)
        context.user_data.pop('editing_card_field', None)
        return await admin_cards_menu(update, context)
    # Else creation flow
    context.user_data['new_card'] = context.user_data.get('new_card') or {}
    context.user_data['new_card']['number'] = update.message.text
    await update.message.reply_text("لطفا **نام و نام خانوادگی** صاحب کارت را وارد کنید:")
    return ADMIN_CARDS_AWAIT_HOLDER


async def admin_card_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # If editing holder name
    editing_id = context.user_data.get('editing_card_id')
    editing_field = context.user_data.get('editing_card_field')
    if editing_id and editing_field == 'holder':
        holder_name = (update.message.text or '').strip()
        execute_db("UPDATE cards SET holder_name = ? WHERE id = ?", (holder_name, editing_id))
        await update.message.reply_text("نام دارنده بروزرسانی شد.")
        context.user_data.pop('editing_card_id', None)
        context.user_data.pop('editing_card_field', None)
        return await admin_cards_menu(update, context)
    # Else creation flow
    card_number = context.user_data['new_card']['number']
    holder_name = update.message.text
    execute_db("INSERT INTO cards (card_number, holder_name) VALUES (?, ?)", (card_number, holder_name))
    await update.message.reply_text("\u2705 کارت جدید با موفقیت ثبت شد.")
    context.user_data.clear()
    return await admin_cards_menu(update, context)


async def admin_settings_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    action = query.data
    prompts = {
        'set_trial_days': "تعداد روزهای تست را وارد کنید:",
        'set_payment_text': "متن پرداخت را ارسال کنید:",
    }
    states = {
        'set_trial_days': SETTINGS_AWAIT_TRIAL_DAYS,
        'set_payment_text': SETTINGS_AWAIT_PAYMENT_TEXT,
    }
    await _safe_edit_text(query.message, prompts[action])
    return states[action]


async def admin_settings_save_trial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days, gb = update.message.text.split('-')
        execute_db("UPDATE settings SET value = ? WHERE key = 'free_trial_days'", (days.strip(),))
        execute_db("UPDATE settings SET value = ? WHERE key = 'free_trial_gb'", (gb.strip(),))
        await update.message.reply_text("\u2705 تنظیمات تست رایگان با موفقیت ذخیره شد.")
    except Exception:
        await update.message.reply_text("فرمت نامعتبر است. لطفا با فرمت `روز-حجم` وارد کنید.")
        return SETTINGS_AWAIT_TRIAL_DAYS
    return await send_admin_panel(update, context)


async def admin_settings_save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Support both conversation state and global awaiting_admin flag
    awaiting = context.user_data.get('awaiting_admin')
    if awaiting and awaiting != 'set_payment_text':
        return ConversationHandler.END
    new_text = (update.message.text or '').strip()
    if not new_text:
        await update.message.reply_text("ورودی نامعتبر است. متن خالی ارسال نکنید.")
        return ConversationHandler.END
    execute_db("UPDATE messages SET text = ? WHERE message_name = ?", (new_text, 'payment_info_text'))
    context.user_data.pop('awaiting_admin', None)
    await update.message.reply_text("\u2705 متن پرداخت با موفقیت ذخیره شد.")
    # If invoked globally, refresh settings view
    fake_query = type('obj', (object,), {
        'data': 'admin_settings_manage',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_settings_manage(fake_update, context)


async def admin_wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_sender = None
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message_sender = query.message.edit_text
    elif update.message:
        message_sender = update.message.reply_text
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
    if message_sender is query.message.edit_text:
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
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_WALLETS_MENU


async def admin_wallet_edit_ask_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split('_')[-1]  # asset|chain|address|memo
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
    # Reuse add flow states
    return {
        'asset': ADMIN_WALLETS_AWAIT_ASSET,
        'chain': ADMIN_WALLETS_AWAIT_CHAIN,
        'address': ADMIN_WALLETS_AWAIT_ADDRESS,
        'memo': ADMIN_WALLETS_AWAIT_MEMO,
    }[field]


async def admin_wallet_add_receive_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Edit-mode override
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
    # Save memo in edit-mode or create mode
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


async def admin_wallet_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_wallet'] = {}
    await query.message.edit_text("لطفا نام دارایی را وارد کنید (مثال: USDT, BTC):")
    return ADMIN_WALLETS_AWAIT_ASSET


async def admin_wallet_add_receive_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_wallet']['asset'] = update.message.text.strip().upper()
    await update.message.reply_text("شبکه/چین را وارد کنید (مثال: TRC20, ERC20, BSC):")
    return ADMIN_WALLETS_AWAIT_CHAIN


async def admin_wallet_add_receive_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_wallet']['chain'] = update.message.text.strip().upper()
    await update.message.reply_text("آدرس ولت را وارد کنید:")
    return ADMIN_WALLETS_AWAIT_ADDRESS


async def admin_wallet_add_receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_wallet']['address'] = update.message.text.strip()
    await update.message.reply_text("در صورت نیاز، ممو/تگ را وارد کنید (در غیر اینصورت - یا خالی):")
    return ADMIN_WALLETS_AWAIT_MEMO


async def admin_wallet_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    memo = update.message.text.strip()
    memo = None if memo in ('', '-', 'none', 'null', 'None') else memo
    w = context.user_data['new_wallet']
    execute_db("INSERT INTO wallets (asset, chain, address, memo) VALUES (?, ?, ?, ?)", (w['asset'], w['chain'], w['address'], memo))
    await update.message.reply_text("\u2705 ولت جدید با موفقیت ثبت شد.")
    context.user_data.clear()
    return await admin_wallets_menu(update, context)


# --- Panel Management (with Inbound Editor) ---
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

    sender = query.message.edit_text if query else update.message.reply_text
    await _safe_edit_text(sender, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_PANELS_MENU


async def admin_panel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    panel_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM panels WHERE id=?", (panel_id,))
    await query.answer("پنل و اینباندهای مرتبط با آن حذف شدند.", show_alert=True)
    return await admin_panels_menu(update, context)


async def admin_panel_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel'] = {}
    await update.callback_query.message.edit_text("نام پنل را وارد کنید (مثال: پنل آلمان):")
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
    if p_type == 'marzban':
        context.user_data['new_panel']['type'] = 'marzban'
    elif p_type == 'xui':
        context.user_data['new_panel']['type'] = 'xui'
    elif p_type == '3xui':
        context.user_data['new_panel']['type'] = '3xui'
    elif p_type == 'txui':
        context.user_data['new_panel']['type'] = 'txui'
    elif p_type == 'marzneshin':
        context.user_data['new_panel']['type'] = 'marzneshin'
    else:
        context.user_data['new_panel']['type'] = 'xui'
    await query.message.edit_text("آدرس کامل (URL) پنل را وارد کنید (مثال: https://panel.example.com):")
    return ADMIN_PANEL_AWAIT_URL


async def admin_panel_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['url'] = update.message.text
    ptype = context.user_data['new_panel'].get('type')
    # If X-UI-like panels, ask for subscription base URL
    if ptype in ('xui', '3xui', 'txui'):
        example = "مثال: http://example.com:2096 یا https://vpn.example.com:8443/app"
        await update.message.reply_text(
            "آدرس پایه ساب‌ لینک (subscription base) را وارد کنید.\n"
            "- می‌تواند دامنه/پورت متفاوت با URL ورود داشته باشد.\n"
            "- اگر مسیر (path) دارد، همان را هم وارد کنید.\n"
            f"{example}\n\n"
            "نکته: ربات به‌صورت خودکار /sub/{subId} یا /sub/{subId}?name={subId} را با توجه به نوع پنل اضافه می‌کند.")
        return ADMIN_PANEL_AWAIT_SUB_BASE
    # For Marzneshin, do NOT ask for API token. We will obtain it automatically via username/password.
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


async def admin_panel_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # For S-UI, this step may be skipped if token-only; still ask for completeness
    context.user_data['new_panel']['user'] = update.message.text
    await update.message.reply_text("رمز عبور (password) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_PASS


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
    except sqlite3.IntegrityError:
        await update.message.reply_text("خطا: نام پنل تکراری است. لطفا نام دیگری انتخاب کنید.")
        context.user_data.clear()
        return ADMIN_PANEL_AWAIT_NAME
    except Exception as e:
        await update.message.reply_text(f"خطا در ذخیره‌سازی: {e}")
        context.user_data.clear()
        return await send_admin_panel(update, context)


async def admin_panel_receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_panel']['token'] = update.message.text.strip()
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


# --- New handler: receive sub_base for X-UI panels ---
async def admin_panel_receive_sub_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sub_base = update.message.text.strip().rstrip('/')
    context.user_data['new_panel']['sub_base'] = sub_base
    # For Marzneshin/S-UI-like panels we skip token prompt and rely on automatic token retrieval.
    await update.message.reply_text("نام کاربری (username) ادمین پنل را وارد کنید:")
    return ADMIN_PANEL_AWAIT_USER


# --- Inbound Management Handlers ---
async def admin_panel_inbounds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if 'panel_inbounds_' in query.data:
        panel_id = int(query.data.split('_')[-1])
        context.user_data['editing_panel_id_for_inbounds'] = panel_id
    else:
        panel_id = context.user_data.get('editing_panel_id_for_inbounds')

    if not panel_id:
        await query.message.edit_text("خطا: آیدی پنل یافت نشد. لطفا دوباره تلاش کنید.")
        return ADMIN_PANELS_MENU

    await query.answer()

    panel = query_db("SELECT name, panel_type FROM panels WHERE id = ?", (panel_id,), one=True)
    inbounds = query_db("SELECT id, protocol, tag FROM panel_inbounds WHERE panel_id = ? ORDER BY id", (panel_id,))

    # Auto-discover inbounds for Marzban/MARZneshin panels and insert if DB empty
    if (not inbounds) or len(inbounds) == 0:
        try:
            prow = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True)
            if prow and (prow.get('panel_type') or 'marzban').lower() in ('marzban', 'marzneshin'):
                api = VpnPanelAPI(panel_id=panel_id)
                found, msg = getattr(api, 'list_inbounds', lambda: (None, 'NA'))()
                # Fallback: try Marzneshin API style if Marzban paths returned 404/empty
                if not found:
                    try:
                        from ..panel import MarzneshinAPI as _MZ
                        alt = _MZ(prow)
                        found, msg = alt.list_inbounds()
                        logger.info(f"Auto-discover fallback (apiv2) used for panel {panel_id}: {bool(found)}")
                    except Exception as _e:
                        logger.error(f"Auto-discover apiv2 fallback failed: {_e}")
                if found:
                    for ib in found[:100]:
                        proto = (ib.get('protocol') or '').lower()
                        tag = ib.get('tag') or ib.get('remark') or str(ib.get('id') or '')
                        if proto and tag:
                            execute_db("INSERT OR IGNORE INTO panel_inbounds (panel_id, protocol, tag) VALUES (?, ?, ?)", (panel_id, proto, tag))
                    inbounds = query_db("SELECT id, protocol, tag FROM panel_inbounds WHERE panel_id = ? ORDER BY id", (panel_id,))
        except Exception as e:
            logger.error(f"Auto-discover inbounds failed for panel {panel_id}: {e}")

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

    # Add refresh button only for marzban/marzneshin panels
    ptype = (panel.get('panel_type') or 'marzban').lower()
    if ptype in ('marzban', 'marzneshin'):
        keyboard.append([InlineKeyboardButton("\U0001F504 بروزرسانی اینباندها", callback_data="inbound_refresh")])
    keyboard.append([InlineKeyboardButton("\u2795 افزودن اینباند جدید", callback_data="inbound_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به لیست پنل‌ها", callback_data="admin_panels_menu")])

    try:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        try:
            logger.error(f"admin_panel_inbounds_menu edit failed: {e} | text_preview={(text or '')[:180]!r}")
        except Exception:
            pass
    return ADMIN_PANEL_INBOUNDS_MENU


async def admin_panel_inbounds_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panel_id = context.user_data.get('editing_panel_id_for_inbounds')
    if not panel_id:
        try:
            await query.answer("شناسه پنل نامعتبر است", show_alert=True)
        except Exception as e:
            try:
                logger.error(f"query.answer failed (invalid panel_id): {e}")
            except Exception:
                pass
        return ADMIN_PANELS_MENU
    prow = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True) or {}
    ptype = (prow.get('panel_type') or 'marzban').lower()
    if ptype not in ('marzban', 'marzneshin'):
        try:
            await query.answer("رفرش برای این نوع پنل پشتیبانی نمی‌شود.", show_alert=True)
        except Exception:
            pass
        return await admin_panel_inbounds_menu(update, context)
    # Try to fetch inbounds
    try:
        api = VpnPanelAPI(panel_id=panel_id)
        found, msg = getattr(api, 'list_inbounds', lambda: (None, 'NA'))()
        if not found:
            try:
                await query.answer(f"ناموفق: {msg}", show_alert=True)
            except Exception:
                pass
            return await admin_panel_inbounds_menu(update, context)
        count_before = query_db("SELECT COUNT(1) AS c FROM panel_inbounds WHERE panel_id = ?", (panel_id,), one=True) or {'c': 0}
        for ib in found[:200]:
            proto = (ib.get('protocol') or '').lower()
            tag = ib.get('tag') or ib.get('remark') or str(ib.get('id') or '')
            if proto and tag:
                execute_db("INSERT OR IGNORE INTO panel_inbounds (panel_id, protocol, tag) VALUES (?, ?, ?)", (panel_id, proto, tag))
        count_after = query_db("SELECT COUNT(1) AS c FROM panel_inbounds WHERE panel_id = ?", (panel_id,), one=True) or {'c': 0}
        diff = int(count_after.get('c') or 0) - int(count_before.get('c') or 0)
        try:
            await query.answer(f"به‌روزرسانی شد (+{max(diff,0)} مورد)", show_alert=True)
        except Exception as e:
            try:
                logger.error(f"query.answer failed on refresh: {e}")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"inbounds refresh failed for panel {panel_id}: {e}")
        try:
            await query.answer("خطا در بروزرسانی اینباندها", show_alert=True)
        except Exception:
            pass
    return await admin_panel_inbounds_menu(update, context)


async def admin_panel_inbound_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    inbound_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM panel_inbounds WHERE id = ?", (inbound_id,))
    await query.answer("اینباند با موفقیت حذف شد.", show_alert=True)
    return await admin_panel_inbounds_menu(update, context)


async def admin_panel_inbound_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_inbound'] = {}
    await query.message.edit_text("لطفا **پروتکل** اینباند را وارد کنید (مثلا `vless`, `vmess`, `trojan`):")
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
        await update.message.reply_text("✅ اینباند با موفقیت اضافه شد.")
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ خطا: این تگ قبلا برای این پنل ثبت شده است.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ذخیره‌سازی: {e}")

    context.user_data.pop('new_inbound', None)

    # Fake update to show the menu again
    fake_query = type('obj', (object,), {'data': f"panel_inbounds_{panel_id}", 'message': update.message, 'answer': lambda: asyncio.sleep(0)})
    fake_update = type('obj', (object,), {'callback_query': fake_query})
    return await admin_panel_inbounds_menu(fake_update, context)


# --- Messages & Buttons Editor ---
async def admin_messages_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    messages = query_db("SELECT message_name FROM messages")
    keyboard = [[InlineKeyboardButton(m['message_name'], callback_data=f"msg_select_{m['message_name']}")] for m in messages]
    keyboard.append([InlineKeyboardButton("\u2795 افزودن پیام جدید", callback_data="msg_add_start")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")])
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, "مدیریت پیام‌ها و صفحات:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("مدیریت پیام‌ها و صفحات:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MESSAGES_MENU


async def msg_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.message.edit_text("نام انگلیسی و منحصر به فرد برای پیام جدید وارد کنید (مثال: `about_us`):")
    return ADMIN_MESSAGES_ADD_AWAIT_NAME


async def msg_add_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_name = update.message.text.strip()
    if not message_name.isascii() or ' ' in message_name:
        await update.message.reply_text("خطا: نام باید انگلیسی و بدون فاصله باشد.")
        return ADMIN_MESSAGES_ADD_AWAIT_NAME
    if query_db("SELECT 1 FROM messages WHERE message_name = ?", (message_name,), one=True):
        await update.message.reply_text("خطا: این نام قبلا استفاده شده است.")
        return ADMIN_MESSAGES_ADD_AWAIT_NAME
    context.user_data['new_message_name'] = message_name
    await update.message.reply_text("محتوای این پیام (متن یا عکس) را ارسال کنید.")
    return ADMIN_MESSAGES_ADD_AWAIT_CONTENT


async def msg_add_receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_name = context.user_data.get('new_message_name')
    if not message_name:
        return await send_admin_panel(update, context)
    text = update.message.text or update.message.caption
    file_id, file_type = None, None
    if update.message.photo:
        file_id, file_type = update.message.photo[-1].file_id, 'photo'
    elif update.message.video:
        file_id, file_type = update.message.video.file_id, 'video'
    elif update.message.document:
        file_id, file_type = update.message.document.file_id, 'document'
    execute_db(
        "INSERT INTO messages (message_name, text, file_id, file_type) VALUES (?, ?, ?, ?)",
        (message_name, text, file_id, file_type),
    )
    await update.message.reply_text(f"\u2705 پیام جدید با نام `{message_name}` ساخته شد.")
    context.user_data.clear()
    return await send_admin_panel(update, context)


async def admin_messages_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    message_name = query.data.replace("msg_select_", "")
    context.user_data['editing_message_name'] = message_name
    await query.answer()
    message_data = query_db("SELECT text FROM messages WHERE message_name = ?", (message_name,), one=True)
    text_preview = (
        (message_data['text'][:200] + '...')
        if message_data and message_data.get('text') and len(message_data['text']) > 200
        else (message_data.get('text') if message_data else 'متن خالی')
    )
    text = f"**در حال ویرایش:** `{message_name}`\n\n**پیش‌نمایش متن:**\n{text_preview}\n\nچه کاری می‌خواهید انجام دهید؟"
    keyboard = [
        [InlineKeyboardButton("\U0001F4DD ویرایش متن", callback_data="msg_action_edit_text")],
        [InlineKeyboardButton("\U0001F518 ویرایش دکمه‌ها", callback_data="msg_action_edit_buttons")],
        [InlineKeyboardButton("\U0001F519 بازگشت به لیست", callback_data="admin_messages_menu")],
    ]
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_MESSAGES_MENU


async def admin_messages_edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    message_name = context.user_data['editing_message_name']
    await query.message.edit_text(f"لطفا متن جدید برای پیام `{message_name}` را ارسال کنید.", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_MESSAGES_EDIT_TEXT


async def admin_messages_edit_text_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_name = context.user_data['editing_message_name']
    new_text = update.message.text
    execute_db("UPDATE messages SET text = ? WHERE message_name = ?", (new_text, message_name))
    await update.message.reply_text("\u2705 متن با موفقیت بروزرسانی شد.")
    context.user_data.clear()
    return await send_admin_panel(update, context)


async def admin_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    message_name = context.user_data.get('editing_message_name')
    if not message_name:
        return ADMIN_MAIN_MENU
    buttons = query_db("SELECT id, text FROM buttons WHERE menu_name = ? ORDER BY row, col", (message_name,))
    keyboard = []
    if buttons:
        for b in buttons:
            keyboard.append([InlineKeyboardButton(f"{b['text']}", callback_data=f"noop_{b['id']}"), InlineKeyboardButton("\u274C حذف", callback_data=f"btn_delete_{b['id']}")])
    keyboard.append([InlineKeyboardButton("\u2795 افزودن دکمه جدید", callback_data="btn_add_new")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data=f"msg_select_{message_name}")])
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, f"ویرایش دکمه‌های پیام `{message_name}`:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"ویرایش دکمه‌های پیام `{message_name}`:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_MESSAGES_SELECT


async def admin_button_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    button_id = int(query.data.replace("btn_delete_", ""))
    execute_db("DELETE FROM buttons WHERE id = ?", (button_id,))
    await query.answer("دکمه حذف شد.", show_alert=True)
    return await admin_buttons_menu(update, context)


async def admin_button_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_button'] = {'menu_name': context.user_data['editing_message_name']}
    await query.message.edit_text("لطفا متن دکمه جدید را وارد کنید:")
    return ADMIN_BUTTON_ADD_AWAIT_TEXT


async def admin_button_add_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_button']['text'] = update.message.text
    await update.message.reply_text("لطفا **دیتای بازگشتی** (callback_data) یا **لینک URL** را وارد کنید:")
    return ADMIN_BUTTON_ADD_AWAIT_TARGET


async def admin_button_add_receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_button']['target'] = update.message.text
    await update.message.reply_text(
        "آیا این یک لینک URL است؟",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("بله، URL است", callback_data="btn_isurl_1")],
            [InlineKeyboardButton("خیر، دیتا است", callback_data="btn_isurl_0")],
        ]),
    )
    return ADMIN_BUTTON_ADD_AWAIT_URL


async def admin_button_add_receive_is_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['new_button']['is_url'] = int(query.data.replace("btn_isurl_", ""))
    await query.message.edit_text("لطفا شماره **سطر** (row) را وارد کنید (شروع از 1):")
    return ADMIN_BUTTON_ADD_AWAIT_ROW


async def admin_button_add_receive_row(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_button']['row'] = int(update.message.text)
        await update.message.reply_text("لطفا شماره **ستون** (column) را وارد کنید (شروع از 1):")
        return ADMIN_BUTTON_ADD_AWAIT_COL
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد وارد کنید.")
        return ADMIN_BUTTON_ADD_AWAIT_ROW


async def admin_button_add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['new_button']['col'] = int(update.message.text)
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد وارد کنید.")
        return ADMIN_BUTTON_ADD_AWAIT_COL
    b = context.user_data['new_button']
    execute_db(
        "INSERT INTO buttons (menu_name, text, target, is_url, row, col) VALUES (?, ?, ?, ?, ?, ?)",
        (b['menu_name'], b['text'], b['target'], b['is_url'], b['row'], b['col']),
    )
    await update.message.reply_text("\u2705 دکمه با موفقیت اضافه شد.")
    return await admin_buttons_menu(update, context)


# --- Broadcast ---
async def admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("ارسال به همه کاربران", callback_data="broadcast_all")],
        [InlineKeyboardButton("ارسال به خریداران", callback_data="broadcast_buyers")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, "پیام خود را به کدام گروه از کاربران می‌خواهید ارسال کنید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("پیام خود را به کدام گروه از کاربران می‌خواهید ارسال کنید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_SELECT_AUDIENCE


async def admin_broadcast_ask_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['broadcast_audience'] = query.data.split('_')[-1]
    await query.message.edit_text("لطفا پیام خود را برای ارسال، در قالب متن یا عکس ارسال کنید. (برای لغو /cancel را بفرستید)")
    return BROADCAST_AWAIT_MESSAGE


async def admin_broadcast_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    audience = context.user_data.get('broadcast_audience')
    if not audience:
        return await send_admin_panel(update, context)

    await update.message.reply_text("درحال آماده سازی برای ارسال...")
    if audience == 'all':
        users = query_db("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,))
    else:
        users = query_db("SELECT DISTINCT user_id FROM orders WHERE status = 'approved' AND user_id != ?", (ADMIN_ID,))
    if not users:
        await update.message.reply_text("هیچ کاربری در گروه هدف یافت نشد.")
        return await send_admin_panel(update, context)

    user_ids = [user['user_id'] for user in users]
    successful_sends, failed_sends = 0, 0
    await context.bot.send_message(ADMIN_ID, f"شروع ارسال پیام همگانی به {len(user_ids)} کاربر...")
    for user_id in user_ids:
        try:
            await context.bot.copy_message(chat_id=user_id, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
            successful_sends += 1
        except (Forbidden, BadRequest):
            failed_sends += 1
        except Exception:
            failed_sends += 1
        await asyncio.sleep(0.1)
    report_text = f"\u2705 **گزارش ارسال همگانی** \u2705\n\nتعداد کل هدف: {len(user_ids)}\nارسال موفق: {successful_sends}\nارسال ناموفق: {failed_sends}"
    await context.bot.send_message(ADMIN_ID, report_text)
    context.user_data.clear()
    return await send_admin_panel(update, context)


# --- Stats ---
async def admin_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    total_users = query_db("SELECT COUNT(user_id) as c FROM users", one=True)['c']
    trial_users = query_db("SELECT COUNT(user_id) as c FROM free_trials", one=True)['c']
    purchased_users = query_db("SELECT COUNT(DISTINCT user_id) as c FROM orders WHERE status = 'approved'", one=True)['c']
    # Revenue: sum final_price if present else plan price for approved orders
    daily_rev = query_db(
        """
        SELECT COALESCE(SUM(CASE WHEN o.final_price IS NOT NULL THEN o.final_price ELSE p.price END),0) AS rev
        FROM orders o
        JOIN plans p ON p.id = o.plan_id
        WHERE o.status='approved' AND date(o.timestamp) = date('now','localtime')
        """,
        one=True,
    )['rev']
    monthly_rev = query_db(
        """
        SELECT COALESCE(SUM(CASE WHEN o.final_price IS NOT NULL THEN o.final_price ELSE p.price END),0) AS rev
        FROM orders o
        JOIN plans p ON p.id = o.plan_id
        WHERE o.status='approved' AND strftime('%Y-%m', o.timestamp) = strftime('%Y-%m', 'now','localtime')
        """,
        one=True,
    )['rev']
    text = (
        f"\U0001F4C8 **آمار ربات**\n\n"
        f"\U0001F465 **کل کاربران:** {total_users} نفر\n"
        f"\U0001F4B8 **تعداد خریداران:** {purchased_users} نفر\n"
        f"\U0001F3AB **دریافت کنندگان تست:** {trial_users} نفر\n\n"
        f"\U0001F4B0 **درآمد امروز:** {int(daily_rev):,} تومان\n"
        f"\U0001F4B0 **درآمد این ماه:** {int(monthly_rev):,} تومان"
    )
    keyboard = [
        [InlineKeyboardButton("\U0001F504 بررسی کاربران فعال", callback_data="stats_refresh")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    if message_sender is query.message.edit_text:
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ADMIN_STATS_MENU


async def admin_stats_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.message.edit_text("\U0001F55C در حال بررسی کاربران... این عملیات ممکن است کمی طول بکشد.")

    all_users = query_db("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,))
    if not all_users:
        return await admin_stats_menu(update, context)

    inactive_count, inactive_ids = 0, []
    for user in all_users:
        user_id = user['user_id']
        try:
            await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        except (Forbidden, BadRequest):
            inactive_count += 1
            inactive_ids.append(user_id)
        await asyncio.sleep(0.1)

    if inactive_ids:
        placeholders = ','.join('?' for _ in inactive_ids)
        execute_db(f"DELETE FROM users WHERE user_id IN ({placeholders})", inactive_ids)
        execute_db(f"DELETE FROM free_trials WHERE user_id IN ({placeholders})", inactive_ids)
        logger.info(f"Removed {inactive_count} inactive users.")

    await query.answer(f"{inactive_count} کاربر غیرفعال حذف شدند.", show_alert=True)
    return await admin_stats_menu(update, context)


# --- Backup ---
async def backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panels = query_db("SELECT id, name FROM panels")
    if not panels:
        await query.message.edit_text("هیچ پنلی برای بکاپ‌گیری وجود ندارد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")]]))
        return ADMIN_MAIN_MENU

    keyboard = [[InlineKeyboardButton(f"بکاپ از پنل: {p['name']}", callback_data=f"backup_panel_{p['id']}")] for p in panels]
    if len(panels) > 1:
        keyboard.insert(0, [InlineKeyboardButton("بکاپ از همه پنل‌ها (ZIP)", callback_data="backup_panel_all")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")])
    # Always edit current message for consistency
    await _safe_edit_text(query.message, "لطفا پنل مورد نظر برای تهیه بکاپ (فایل ZIP) را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return BACKUP_CHOOSE_PANEL


async def admin_generate_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.message.edit_text("در حال آماده‌سازی فایل ZIP بکاپ... لطفا صبر کنید.")

    target = query.data.split('_')[-1]
    panel_ids = [p['id'] for p in query_db("SELECT id FROM panels")] if target == 'all' else [int(target)]

    if not panel_ids:
        await query.message.edit_text("خطا: پنلی برای بکاپ‌گیری یافت نشد.")
        return await send_admin_panel(update, context)

    import io as _io
    import json as _json
    import zipfile as _zipfile
    from ..config import DB_NAME

    zip_buffer = _io.BytesIO()
    total_users_count = 0
    with _zipfile.ZipFile(zip_buffer, mode='w', compression=_zipfile.ZIP_DEFLATED) as zf:
        # Include bot database
        try:
            with open(DB_NAME, 'rb') as fdb:
                zf.writestr('bot_db.sqlite', fdb.read())
        except Exception as e:
            logger.error(f"Could not include bot DB in backup: {e}")
        # Add per-panel snapshots
        for panel_id in panel_ids:
            try:
                panel_row = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True) or {}
                base_dir = f"panel_{panel_id}"
                # Panel info (mask password minimally)
                safe_info = dict(panel_row)
                if safe_info.get('password'):
                    safe_info['password'] = '***'
                zf.writestr(f"{base_dir}/panel_info.json", _json.dumps(safe_info, ensure_ascii=False, indent=2))

                # Inbounds from our DB
                inbounds = query_db("SELECT id, protocol, tag FROM panel_inbounds WHERE panel_id = ? ORDER BY id", (panel_id,)) or []
                zf.writestr(f"{base_dir}/panel_inbounds.json", _json.dumps(inbounds, ensure_ascii=False, indent=2))

                # Clients/users snapshot via panel API when possible
                api = VpnPanelAPI(panel_id=panel_id)
                users_payload = []
                # Marzban supports get_all_users
                try:
                    users, msg = await api.get_all_users()
                except Exception as e:
                    users, msg = None, str(e)
                if users:
                    users_payload = users
                    total_users_count += len(users)
                else:
                    # Try to enumerate clients from inbounds for X-UI-like panels
                    list_inb = None
                    try:
                        list_inb, _ = api.list_inbounds()
                    except Exception:
                        list_inb = None
                    if list_inb:
                        for ib in list_inb:
                            inbound_id = ib.get('id')
                            fetch = getattr(api, '_fetch_inbound_detail', None)
                            detail = None
                            if callable(fetch):
                                try:
                                    detail = fetch(inbound_id)
                                except Exception:
                                    detail = None
                            if not detail:
                                continue
                            settings_str = detail.get('settings')
                            try:
                                settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else {}
                            except Exception:
                                settings_obj = {}
                            clients = settings_obj.get('clients') or []
                            if isinstance(clients, list):
                                for c in clients:
                                    users_payload.append({
                                        'email': c.get('email'),
                                        'totalGB': c.get('totalGB'),
                                        'expiryTime': c.get('expiryTime'),
                                        'enable': c.get('enable'),
                                        'subId': c.get('subId'),
                                        'inbound_id': inbound_id,
                                    })
                        total_users_count += len(users_payload)
                zf.writestr(f"{base_dir}/clients_or_users.json", _json.dumps(users_payload, ensure_ascii=False, indent=2))
            except Exception as e:
                logger.error(f"Error adding panel {panel_id} to backup ZIP: {e}")

    zip_buffer.seek(0)
    filename = f"panel_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    file_to_send = InputFile(zip_buffer, filename=filename)
    try:
        await context.bot.send_document(chat_id=query.message.chat_id, document=file_to_send, caption=f"✅ فایل بکاپ آماده شد. مجموع کاربران: {total_users_count}")
    except TelegramError:
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_to_send, caption=f"✅ فایل بکاپ آماده شد. مجموع کاربران: {total_users_count}")
    try:
        await query.message.delete()
    except Exception:
        pass
    return await send_admin_panel(update, context)


# --- Admin fallback ---
async def cancel_admin_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("عملیات لغو شد.")
    return await send_admin_panel(update, context)


async def exit_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("از پنل خارج شدید.")
    return ConversationHandler.END


async def admin_set_usd_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("نرخ دلار به تومان را وارد کنید (مثال: 65000). برای پاک کردن '-' بفرستید.")
    return SETTINGS_AWAIT_USD_RATE


async def admin_set_usd_rate_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        # If invoked via global flow
        if context.user_data.get('awaiting_admin') == 'set_usd_rate':
            val = (update.message.text or '').strip()
        else:
            val = update.message.text.strip()
        if val == '-' or val == '' or val.lower() == 'clear':
            execute_db("UPDATE settings SET value = NULL WHERE key = 'usd_irt_manual'", ())
            await update.message.reply_text("نرخ دلار پاک شد؛ از نرخ API استفاده خواهد شد.")
        else:
            rate = int(float(val))
            if rate <= 0:
                raise ValueError()
            execute_db("UPDATE settings SET value = ? WHERE key = 'usd_irt_manual'", (str(rate),))
            await update.message.reply_text("نرخ دلار ذخیره شد.")
    except Exception:
        await update.message.reply_text("ورودی نامعتبر است. یک عدد صحیح تومان وارد کنید یا '-' برای پاک کردن.")
        # If we are in conversation state, keep waiting; otherwise just end
        if context.user_data.get('awaiting_admin') == 'set_usd_rate':
            return ConversationHandler.END
        return SETTINGS_AWAIT_USD_RATE
    # Clear global flag if used and refresh settings
    if context.user_data.get('awaiting_admin') == 'set_usd_rate':
        context.user_data.pop('awaiting_admin', None)
        fake_query = type('obj', (object,), {
            'data': 'admin_settings_manage',
            'message': update.message,
            'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
            'from_user': update.effective_user,
        })
        fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
        return await admin_settings_manage(fake_update, context)
    return await admin_settings_manage(update, context)


async def admin_clear_usd_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    execute_db("UPDATE settings SET value = '' WHERE key IN ('usd_irt_cached','usd_irt_cached_ts')")
    await query.answer("کش دلار پاک شد.", show_alert=True)
    return await admin_settings_manage(update, context)


async def admin_toggle_pay_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    val = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'pay_card_enabled'", (val,))
    return await admin_settings_manage(update, context)


async def admin_toggle_pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    val = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'pay_crypto_enabled'", (val,))
    return await admin_settings_manage(update, context)


async def admin_toggle_pay_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    val = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'pay_gateway_enabled'", (val,))
    return await admin_settings_manage(update, context)


async def admin_toggle_gateway_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    t = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'gateway_type'", (t,))
    return await admin_settings_manage(update, context)


async def admin_set_gateway_api_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    gateway_type = (settings.get('gateway_type') or 'zarinpal').lower()
    context.user_data['gateway_setup'] = {'step': 1, 'type': gateway_type}
    if gateway_type == 'zarinpal':
        await query.message.edit_text("مرحله 1/2: MerchantID زرین‌پال را وارد کنید:")
    else:
        await query.message.edit_text("مرحله 1/2: PIN آقای پرداخت (کد درگاه) را وارد کنید:")
    return SETTINGS_AWAIT_GATEWAY_API


async def admin_set_gateway_api_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.get('gateway_setup') or {'step': 1, 'type': 'zarinpal'}
    step = data.get('step', 1)
    gtype = data.get('type', 'zarinpal')
    txt = (update.message.text or '').strip()

    if gtype == 'zarinpal':
        if step == 1:
            if len(txt) < 5:
                await update.message.reply_text("MerchantID نامعتبر است. دوباره وارد کنید:")
                return SETTINGS_AWAIT_GATEWAY_API
            execute_db("UPDATE settings SET value = ? WHERE key = 'zarinpal_merchant_id'", (txt,))
            context.user_data['gateway_setup']['step'] = 2
            await update.message.reply_text("مرحله 2/2: Callback URL را وارد کنید (مثال: https://site.com/pay/callback):")
            return SETTINGS_AWAIT_GATEWAY_API
        else:
            if not (txt.startswith('http://') or txt.startswith('https://')):
                await update.message.reply_text("Callback URL نامعتبر است. با http(s) شروع شود:")
                return SETTINGS_AWAIT_GATEWAY_API
            execute_db("UPDATE settings SET value = ? WHERE key = 'gateway_callback_url'", (txt,))
            await update.message.reply_text("اطلاعات زرین‌پال ذخیره شد.")
            context.user_data.pop('gateway_setup', None)
            return await admin_settings_manage(update, context)
    else:
        if step == 1:
            if len(txt) < 4:
                await update.message.reply_text("PIN نامعتبر است. دوباره وارد کنید:")
                return SETTINGS_AWAIT_GATEWAY_API
            execute_db("UPDATE settings SET value = ? WHERE key = 'aghapay_pin'", (txt,))
            context.user_data['gateway_setup']['step'] = 2
            await update.message.reply_text("مرحله 2/2: Callback URL را وارد کنید (اختیاری، برای رد این مرحله '-' بزنید):")
            return SETTINGS_AWAIT_GATEWAY_API
        else:
            if txt != '-' and txt:
                if not (txt.startswith('http://') or txt.startswith('https://')):
                    await update.message.reply_text("Callback URL نامعتبر است. با http(s) شروع شود یا '-' برای رد:")
                    return SETTINGS_AWAIT_GATEWAY_API
                execute_db("UPDATE settings SET value = ? WHERE key = 'gateway_callback_url'", (txt,))
            await update.message.reply_text("اطلاعات آقای پرداخت ذخیره شد.")
            context.user_data.pop('gateway_setup', None)
            return await admin_settings_manage(update, context)


async def admin_wallet_tx_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    rows = query_db("SELECT id, user_id, amount, direction, method, status, created_at FROM wallet_transactions WHERE status = 'pending' ORDER BY id DESC LIMIT 30")
    text = "\U0001F4B8 درخواست‌های شارژ کیف پول (در انتظار تایید)\n\n"
    keyboard = []
    if not rows:
        text += "درخواستی برای بررسی وجود ندارد."
    else:
        for r in rows:
            line = f"#{r['id']} | user:{r['user_id']} | {r['amount']:,} تومان | {r['method']}"
            keyboard.append([
                InlineKeyboardButton(line, callback_data=f"wallet_tx_view_{r['id']}")
            ])
            keyboard.append([
                InlineKeyboardButton("\u2705 تایید", callback_data=f"wallet_tx_approve_{r['id']}"),
                InlineKeyboardButton("\u274C رد", callback_data=f"wallet_tx_reject_{r['id']}")
            ])
    keyboard.append([InlineKeyboardButton("\u2795 افزایش دستی", callback_data="wallet_adjust_start_credit"), InlineKeyboardButton("\u2796 کاهش دستی", callback_data="wallet_adjust_start_debit")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت به تنظیمات", callback_data="admin_settings_manage")])

    markup = InlineKeyboardMarkup(keyboard)
    chat_id = (query.message.chat_id if query else update.message.chat_id)
    # prevent duplicate menu messages: delete last menu
    try:
        last_menu_id = context.user_data.pop('wallet_tx_menu_msg', None)
        if last_menu_id:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_menu_id)
    except Exception:
        pass
    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    context.user_data['wallet_tx_menu_msg'] = sent.message_id
    return ADMIN_WALLET_MENU


async def admin_wallet_tx_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split('_')[-1])
    r = query_db("SELECT * FROM wallet_transactions WHERE id = ?", (tx_id,), one=True)
    if not r:
        await query.answer("یافت نشد", show_alert=True)
        return ADMIN_WALLET_MENU
    caption = (f"درخواست #{r['id']}\n"
               f"کاربر: {r['user_id']}\n"
               f"مبلغ: {r['amount']:,} تومان\n"
               f"روش: {r['method']}\n"
               f"وضعیت: {r['status']}\n"
               f"تاریخ: {r['created_at']}")
    if r.get('screenshot_file_id'):
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=r['screenshot_file_id'], caption=caption)
    else:
        await context.bot.send_message(chat_id=query.message.chat_id, text=caption, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_wallet_tx_menu")]]))
    return ADMIN_WALLET_MENU


def _wallet_apply_balance(user_id: int, amount: int, direction: str):
    delta = amount if direction == 'credit' else -amount
    execute_db("INSERT OR IGNORE INTO user_wallets (user_id, balance) VALUES (?, 0)", (user_id,))
    execute_db("UPDATE user_wallets SET balance = balance + ? WHERE user_id = ?", (delta, user_id))


async def admin_wallet_tx_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split('_')[-1])
    r = query_db("SELECT * FROM wallet_transactions WHERE id = ?", (tx_id,), one=True)
    if not r or r.get('status') != 'pending':
        await query.answer("نامعتبر", show_alert=True)
        return ADMIN_WALLET_MENU
    execute_db("UPDATE wallet_transactions SET status = 'approved' WHERE id = ?", (tx_id,))
    _wallet_apply_balance(r['user_id'], r['amount'], r['direction'])
    # Notify user on credit
    try:
        if (r.get('direction') or '') == 'credit':
            bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (r['user_id'],), one=True)
            balance = bal_row.get('balance') if bal_row else 0
            await context.bot.send_message(r['user_id'], f"✅ {r['amount']:,} تومان به کیف پول شما افزوده شد.\nموجودی فعلی: {balance:,} تومان")
    except Exception:
        pass
    await query.answer("تایید شد.", show_alert=True)
    return await admin_wallet_tx_menu(update, context)


async def admin_wallet_tx_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tx_id = int(query.data.split('_')[-1])
    r = query_db("SELECT * FROM wallet_transactions WHERE id = ?", (tx_id,), one=True)
    if not r or r.get('status') != 'pending':
        await query.answer("نامعتبر", show_alert=True)
        return ADMIN_WALLET_MENU
    execute_db("UPDATE wallet_transactions SET status = 'rejected' WHERE id = ?", (tx_id,))
    await query.answer("رد شد.", show_alert=True)
    return await admin_wallet_tx_menu(update, context)


async def admin_wallet_adjust_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    direc = 'credit' if query.data.endswith('credit') else 'debit'
    context.user_data['wallet_adjust_direction'] = direc
    # Two-step numeric: 1) ask user id, 2) ask amount
    context.user_data['awaiting_admin'] = 'wallet_adjust_user_id'
    try:
        from ..config import logger as _lg
        _lg.debug(f"wallet_adjust_start: dir={direc} awaiting={context.user_data.get('awaiting_admin')}")
    except Exception:
        pass
    hint = "افزایش" if direc == 'credit' else "کاهش"
    sent = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(f"{hint} دستی موجودی\n\n"
              f"1) آیدی عددی کاربر را ارسال کنید.\n"
              f"2) سپس مبلغ را وارد کنید."),
        parse_mode=ParseMode.HTML,
    )
    context.user_data['wallet_adjust_prompt_msg'] = sent.message_id
    return ADMIN_WALLET_MENU


async def admin_wallet_adjust_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # deprecated in new single-step flow; keep for compatibility but redirect
    context.user_data['awaiting_admin'] = 'wallet_adjust_inline'
    await update.message.reply_text("لطفا با فرمت <code>USER_ID AMOUNT</code> ارسال کنید. مثال: <code>123456789 50000</code>", parse_mode=ParseMode.HTML)
    return ADMIN_WALLET_MENU


async def admin_wallet_adjust_receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # deprecated in new single-step flow; keep for compatibility but redirect
    context.user_data['awaiting_admin'] = 'wallet_adjust_inline'
    await update.message.reply_text("لطفا با فرمت <code>USER_ID AMOUNT</code> ارسال کنید. مثال: <code>123456789 50000</code>", parse_mode=ParseMode.HTML)
    return ADMIN_WALLET_MENU


async def admin_wallet_adjust_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Allow manual adjust flow via global text handler for ADMIN
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    awaiting = context.user_data.get('awaiting_admin')
    # Hard-guard: if admin is in the middle of adding/editing a panel, do not intercept text
    # The panel add flow stores interim data in 'new_panel'
    if context.user_data.get('new_panel'):
        return ConversationHandler.END
    try:
        from ..config import logger as _lg
        _lg.debug(f"admin_wallet_adjust_text_router: awaiting={awaiting} text={(update.message.text or '')[:50]}")
    except Exception:
        pass
    text = _normalize_digits((update.message.text or '').strip())
    # Step 1: ask user id
    if awaiting == 'wallet_adjust_user_id':
        try:
            uid = int(re.findall(r"\d+", text)[0])
        except Exception:
            await update.message.reply_text("❌ آیدی عددی نامعتبر. دوباره ارسال کنید.")
            raise ApplicationHandlerStop
        context.user_data['wallet_adjust_user'] = uid
        context.user_data['awaiting_admin'] = 'wallet_adjust_amount_only'
        try:
            last_prompt_id = context.user_data.get('wallet_adjust_prompt_msg')
            if last_prompt_id:
                await context.bot.delete_message(chat_id=update.message.chat_id, message_id=last_prompt_id)
        except Exception:
            pass
        sent = await update.message.reply_text("مبلغ (تومان) را وارد کنید:")
        context.user_data['wallet_adjust_prompt_msg'] = sent.message_id
        raise ApplicationHandlerStop
    # Step 2: amount only
    if awaiting == 'wallet_adjust_amount_only':
        try:
            amount = int(re.findall(r"\d+", text)[0])
            if amount <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ مبلغ نامعتبر. فقط عدد ارسال کنید.")
            raise ApplicationHandlerStop
        uid = int(context.user_data.get('wallet_adjust_user') or 0)
        if not uid:
            await update.message.reply_text("❌ آیدی کاربر مشخص نیست. دوباره شروع کنید.")
            context.user_data.pop('awaiting_admin', None)
            raise ApplicationHandlerStop
        if not query_db("SELECT 1 FROM users WHERE user_id = ?", (uid,), one=True):
            await update.message.reply_text("❌ آیدی کاربر یافت نشد.")
            context.user_data.pop('awaiting_admin', None)
            raise ApplicationHandlerStop
        direc = context.user_data.get('wallet_adjust_direction', 'credit')
        if direc == 'debit':
            bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True) or {'balance': 0}
            if int(bal_row.get('balance') or 0) < amount:
                await update.message.reply_text("❌ موجودی کاربر کافی نیست برای کسر.")
                raise ApplicationHandlerStop
        execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at) VALUES (?, ?, ?, 'manual', 'approved', ?)", (uid, amount, direc, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        _wallet_apply_balance(uid, amount, direc)
        try:
            if direc == 'credit':
                bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True)
                balance = bal_row.get('balance') if bal_row else 0
                await context.bot.send_message(uid, f"✅ {amount:,} تومان به کیف پول شما افزوده شد.\nموجودی فعلی: {balance:,} تومان")
        except Exception:
            pass
        new_bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True) or {'balance': 0}
        await update.message.reply_text(f"✅ انجام شد. موجودی فعلی کاربر {int(new_bal_row.get('balance') or 0):,} تومان")
        # cleanup
        try:
            last_prompt_id = context.user_data.pop('wallet_adjust_prompt_msg', None)
            if last_prompt_id:
                await context.bot.delete_message(chat_id=update.message.chat_id, message_id=last_prompt_id)
        except Exception:
            pass
        context.user_data.pop('awaiting_admin', None)
        context.user_data.pop('wallet_adjust_direction', None)
        context.user_data.pop('wallet_adjust_user', None)
        await admin_wallet_tx_menu(update, context)
        raise ApplicationHandlerStop
    if awaiting == 'wallet_adjust_inline':
        # Parse "userId amount"
        nums = re.findall(r"\d+", text)
        if len(nums) < 2:
            await update.message.reply_text("ورودی نامعتبر. فرمت درست: <code>USER_ID AMOUNT</code>", parse_mode=ParseMode.HTML)
            raise ApplicationHandlerStop
        try:
            uid = int(nums[0])
            amount = int(nums[1])
            if amount <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("اعداد نامعتبر. مثال: <code>123456789 50000</code>", parse_mode=ParseMode.HTML)
            raise ApplicationHandlerStop
        # Validate user exists
        if not query_db("SELECT 1 FROM users WHERE user_id = ?", (uid,), one=True):
            await update.message.reply_text("❌ آیدی کاربر یافت نشد. لطفا آیدی عددی صحیح وارد کنید.")
            raise ApplicationHandlerStop
        direc = context.user_data.get('wallet_adjust_direction', 'credit')
        # If debit, ensure sufficient balance
        if direc == 'debit':
            bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True) or {'balance': 0}
            if int(bal_row.get('balance') or 0) < amount:
                await update.message.reply_text("❌ موجودی کاربر کافی نیست برای کسر.")
                raise ApplicationHandlerStop
        execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at) VALUES (?, ?, ?, 'manual', 'approved', ?)", (uid, amount, direc, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        _wallet_apply_balance(uid, amount, direc)
        try:
            if direc == 'credit':
                bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True)
                balance = bal_row.get('balance') if bal_row else 0
                await context.bot.send_message(uid, f"✅ {amount:,} تومان به کیف پول شما افزوده شد.\nموجودی فعلی: {balance:,} تومان")
        except Exception:
            pass
        new_bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True) or {'balance': 0}
        await update.message.reply_text(f"✅ انجام شد. موجودی فعلی کاربر {int(new_bal_row.get('balance') or 0):,} تومان")
        # Clean prompt if exists
        try:
            last_prompt_id = context.user_data.pop('wallet_adjust_prompt_msg', None)
            if last_prompt_id:
                await context.bot.delete_message(chat_id=update.message.chat_id, message_id=last_prompt_id)
        except Exception:
            pass
        context.user_data.pop('awaiting_admin', None)
        context.user_data.pop('wallet_adjust_direction', None)
        await admin_wallet_tx_menu(update, context)
        raise ApplicationHandlerStop
    # Fallback: allow quick "USER_ID AMOUNT" only if the message strictly matches two numbers separated by space
    # This avoids catching unrelated inputs like URLs/IPs that contain multiple numbers
    if (not awaiting) and re.match(r"^\s*\d+\s+\d+\s*$", text):
        try:
            nums = re.findall(r"\d+", text)
            uid = int(nums[0])
            amount = int(nums[1])
            if amount <= 0:
                raise ValueError()
        except Exception:
            return ConversationHandler.END
        if not query_db("SELECT 1 FROM users WHERE user_id = ?", (uid,), one=True):
            await update.message.reply_text("❌ آیدی کاربر یافت نشد. لطفا آیدی عددی صحیح وارد کنید.")
            raise ApplicationHandlerStop
        execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at) VALUES (?, ?, 'credit', 'manual', 'approved', ?)", (uid, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        _wallet_apply_balance(uid, amount, 'credit')
        try:
            bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,), one=True)
            balance = bal_row.get('balance') if bal_row else 0
            await context.bot.send_message(uid, f"✅ {amount:,} تومان به کیف پول شما افزوده شد.\nموجودی فعلی: {balance:,} تومان")
        except Exception:
            pass
        await update.message.reply_text(f"✅ افزایش انجام شد. موجودی فعلی کاربر {int(balance or 0):,} تومان")
        await admin_wallet_tx_menu(update, context)
        raise ApplicationHandlerStop
    return ConversationHandler.END


async def _safe_edit_text(message, text, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if 'Message is not modified' not in str(e):
            raise


async def _safe_edit_caption(message, caption, reply_markup=None, parse_mode=None):
    try:
        await message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if 'Message is not modified' not in str(e):
            raise


async def admin_tickets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Show only pending tickets
    rows = query_db("SELECT id, user_id, created_at FROM tickets WHERE status = 'pending' ORDER BY id DESC LIMIT 50")
    text = "\U0001F4AC تیکت‌های پاسخ‌داده‌نشده\n\n"
    kb = []
    if not rows:
        text += "درخواستی برای بررسی وجود ندارد."
    else:
        for r in rows:
            kb.append([InlineKeyboardButton(f"#{r['id']} از {r['user_id']} - {r['created_at']}", callback_data=f"ticket_view_{r['id']}")])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_main')])
    if message_sender is query.message.edit_text:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_ticket_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket_id = int(query.data.split('_')[-1])
    t = query_db("SELECT * FROM tickets WHERE id = ?", (ticket_id,), one=True)
    if not t:
        await query.answer("یافت نشد", show_alert=True)
        return ADMIN_MAIN_MENU
    kb = [[InlineKeyboardButton("✉️ پاسخ", callback_data=f"ticket_reply_{ticket_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"ticket_delete_{ticket_id}")], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_tickets_menu')]]
    if t.get('content_type') == 'photo' and t.get('file_id'):
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=t['file_id'], caption=t.get('text') or '', reply_markup=InlineKeyboardMarkup(kb))
    elif t.get('content_type') == 'document' and t.get('file_id'):
        await context.bot.send_document(chat_id=query.message.chat_id, document=t['file_id'], caption=t.get('text') or '', reply_markup=InlineKeyboardMarkup(kb))
    elif t.get('content_type') == 'video' and t.get('file_id'):
        await context.bot.send_video(chat_id=query.message.chat_id, video=t['file_id'], caption=t.get('text') or '', reply_markup=InlineKeyboardMarkup(kb))
    elif t.get('content_type') == 'voice' and t.get('file_id'):
        await context.bot.send_voice(chat_id=query.message.chat_id, voice=t['file_id'], reply_markup=InlineKeyboardMarkup(kb))
    elif t.get('content_type') == 'audio' and t.get('file_id'):
        await context.bot.send_audio(chat_id=query.message.chat_id, audio=t['file_id'], caption=t.get('text') or '', reply_markup=InlineKeyboardMarkup(kb))
    else:
        await context.bot.send_message(chat_id=query.message.chat_id, text=t.get('text') or '(بدون متن)', reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_ticket_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket_id = int(query.data.split('_')[-1])
    execute_db("UPDATE tickets SET status = 'deleted' WHERE id = ?", (ticket_id,))
    await query.answer("حذف شد", show_alert=True)
    await admin_tickets_menu(update, context)
    return ADMIN_MAIN_MENU


async def admin_ticket_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket_id = int(query.data.split('_')[-1])
    context.user_data['awaiting_admin'] = 'ticket_reply'
    context.user_data['ticket_reply_id'] = ticket_id
    # Clear any previous manual-send action to avoid interception
    context.user_data.pop('next_action', None)
    await context.bot.send_message(chat_id=query.message.chat_id, text=f"لطفاً پاسخ خود را برای تیکت #{ticket_id} ارسال کنید:")
    return ADMIN_MAIN_MENU


async def admin_ticket_receive_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if context.user_data.get('awaiting_admin') != 'ticket_reply':
        # still allow explicit admin reply with prefix 'reply:' followed by ticket id
        if update.message and update.message.text and update.message.text.startswith('reply:'):
            try:
                _, tid = update.message.text.split(':', 1)
                tid = int(tid.strip())
                context.user_data['awaiting_admin'] = 'ticket_reply'
                context.user_data['ticket_reply_id'] = tid
            except Exception:
                return ConversationHandler.END
        else:
            return ConversationHandler.END
    ticket_id = int(context.user_data.get('ticket_reply_id') or 0)
    t = query_db("SELECT user_id FROM tickets WHERE id = ?", (ticket_id,), one=True)
    if not t:
        await update.message.reply_text("تیکت یافت نشد.")
        context.user_data.pop('awaiting_admin', None)
        raise ApplicationHandlerStop
    target_chat_id = int(t['user_id'])
    # Try to copy full message; fallback to plain text
    try:
        if update.message:
            if update.message.text and update.message.text.startswith('reply:'):
                # strip reply:tid prefix
                body = update.message.text.split(':', 1)[1].strip()
                await context.bot.send_message(chat_id=target_chat_id, text=body or ' ')
            else:
                await context.bot.copy_message(chat_id=target_chat_id, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
        else:
            await context.bot.send_message(chat_id=target_chat_id, text=update.effective_message.text or '')
    except Forbidden:
        await update.message.reply_text("❌ کاربر هنوز استارت نکرده یا پیام‌های ربات را مسدود کرده است. از کاربر بخواهید /start را بزند.")
        raise ApplicationHandlerStop
    except Exception:
        try:
            await context.bot.send_message(chat_id=target_chat_id, text=(update.message.text or ''))
        except Forbidden:
            await update.message.reply_text("❌ کاربر هنوز استارت نکرده یا پیام‌های ربات را مسدود کرده است. از کاربر بخواهید /start را بزند.")
            raise ApplicationHandlerStop
        except Exception as e:
            await update.message.reply_text(f"❌ ارسال پیام ناموفق بود: {e}")
            raise ApplicationHandlerStop
    execute_db("UPDATE tickets SET status = 'answered' WHERE id = ?", (ticket_id,))
    await update.message.reply_text("✅ پاسخ ارسال شد و تیکت بسته شد.")
    context.user_data.pop('awaiting_admin', None)
    context.user_data.pop('ticket_reply_id', None)
    raise ApplicationHandlerStop


async def admin_tutorials_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rows = query_db("SELECT id, title FROM tutorials ORDER BY sort_order, id DESC")
    kb = []
    text = "\U0001F4D6 مدیریت آموزش‌ها\n\n"
    if not rows:
        text += "هیچ آموزشی ثبت نشده است."
    else:
        for r in rows:
            kb.append([InlineKeyboardButton(r['title'], callback_data=f"tutorial_view_{r['id']}"), InlineKeyboardButton("🗑 حذف", callback_data=f"tutorial_delete_{r['id']}")])
    kb.append([InlineKeyboardButton("➕ افزودن آموزش", callback_data='tutorial_add_start')])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_main')])
    if message_sender is query.message.edit_text:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_tutorial_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_admin'] = 'tutorial_add_title'
    await context.bot.send_message(chat_id=query.message.chat_id, text="عنوان آموزش را وارد کنید:")
    return ADMIN_MAIN_MENU


async def admin_tutorial_receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if context.user_data.get('awaiting_admin') != 'tutorial_add_title':
        logger.debug(f"admin_tutorial_receive_title: ignoring text from {update.effective_user.id}; awaiting_admin={context.user_data.get('awaiting_admin')}")
        return ConversationHandler.END
    title = (update.message.text or '').strip()
    if not title:
        await update.message.reply_text("عنوان نامعتبر است.")
        return ConversationHandler.END
    tid = execute_db("INSERT INTO tutorials (title, sort_order, created_at) VALUES (?, 0, ?)", (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    context.user_data['awaiting_admin'] = 'tutorial_add_media'
    context.user_data['tutorial_edit_id'] = tid
    await update.message.reply_text(f"آموزش '{title}' ایجاد شد. حالا رسانه‌ها را (عکس/ویدیو/سند/صدا) یکی‌یکی ارسال کنید. برای پایان، عبارت 'پایان' را بفرستید.")
    return ConversationHandler.END


async def admin_tutorial_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if context.user_data.get('awaiting_admin') != 'tutorial_add_media':
        logger.debug(f"admin_tutorial_receive_media: ignoring message from {update.effective_user.id}; awaiting_admin={context.user_data.get('awaiting_admin')}")
        return ConversationHandler.END
    if (update.message.text or '').strip().lower() in ('پایان', 'end', 'finish'):
        context.user_data.pop('awaiting_admin', None)
        await update.message.reply_text("✅ ثبت آموزش تمام شد.")
        return ConversationHandler.END
    tid = int(context.user_data.get('tutorial_edit_id') or 0)
    ctype = None
    file_id = None
    caption = update.message.caption or ''
    if update.message.photo:
        ctype = 'photo'
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        ctype = 'document'
        file_id = update.message.document.file_id
    elif update.message.video:
        ctype = 'video'
        file_id = update.message.video.file_id
    elif update.message.voice:
        ctype = 'voice'
        file_id = update.message.voice.file_id
    elif update.message.audio:
        ctype = 'audio'
        file_id = update.message.audio.file_id
    elif update.message.text:
        ctype = 'text'
        file_id = update.message.text
        caption = ''
    else:
        await update.message.reply_text("نوع پیام پشتیبانی نمی‌شود.")
        return ConversationHandler.END
    execute_db("INSERT INTO tutorial_media (tutorial_id, content_type, file_id, caption, sort_order, created_at) VALUES (?, ?, ?, ?, 0, ?)", (tid, ctype, file_id, caption, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    await update.message.reply_text("✅ ثبت شد. رسانه بعدی را ارسال کنید یا 'پایان' بفرستید.")
    return ConversationHandler.END


async def admin_tutorial_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tid = int(query.data.split('_')[-1])
    execute_db("DELETE FROM tutorials WHERE id = ?", (tid,))
    await query.answer("حذف شد", show_alert=True)
    await admin_tutorials_menu(update, context)
    return ADMIN_MAIN_MENU


async def admin_tutorial_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tid = int(query.data.split('_')[-1])
    t = query_db("SELECT title FROM tutorials WHERE id = ?", (tid,), one=True)
    if not t:
        await query.answer("یافت نشد", show_alert=True)
        return ADMIN_MAIN_MENU
    text = f"\U0001F4D6 {t['title']}\n\nارسال رسانه جدید (عکس/ویدیو/سند/صدا/متن) برای افزودن. برای حذف رسانه‌ها فعلاً باید کل آموزش حذف شود."
    kb = [[InlineKeyboardButton("🗑 حذف آموزش", callback_data=f"tutorial_delete_{tid}")], [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_tutorials_menu')]]
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['awaiting_admin'] = 'tutorial_add_media'
    context.user_data['tutorial_edit_id'] = tid
    return ADMIN_MAIN_MENU


async def admin_toggle_signup_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    val = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'signup_bonus_enabled'", (val,))
    await query.answer("ذخیره شد.", show_alert=False)
    return await admin_settings_manage(update, context)


async def admin_set_signup_bonus_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("مبلغ موجودی اولیه (تومان) را وارد کنید:")
    return SETTINGS_AWAIT_SIGNUP_BONUS


async def admin_set_signup_bonus_amount_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or '').strip()
    try:
        amount = int(float(txt))
        if amount < 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("مبلغ نامعتبر است. یک عدد صحیح وارد کنید:")
        return SETTINGS_AWAIT_SIGNUP_BONUS
    execute_db("UPDATE settings SET value = ? WHERE key = 'signup_bonus_amount'", (str(amount),))
    await update.message.reply_text("ذخیره شد.")
    fake_query = type('obj', (object,), {
        'data': 'admin_settings_manage',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_settings_manage(fake_update, context)


async def _apply_referral_bonus(order_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        order = query_db("SELECT id, user_id, plan_id, final_price FROM orders WHERE id = ?", (order_id,), one=True)
        if not order:
            return
        user_row = query_db("SELECT referrer_id FROM users WHERE user_id = ?", (order['user_id'],), one=True)
        ref_id = user_row.get('referrer_id') if user_row else None
        if not ref_id or int(ref_id) == int(order['user_id']):
            return
        # idempotency: skip if already credited for this order
        exists = query_db("SELECT 1 FROM wallet_transactions WHERE reference = ?", (f"ref_bonus_order_{order_id}",), one=True)
        if exists:
            return
        plan = query_db("SELECT price FROM plans WHERE id = ?", (order['plan_id'],), one=True)
        base_price = 0
        try:
            base_price = int(order.get('final_price') or plan.get('price') or 0)
        except Exception:
            base_price = 0
        if base_price <= 0:
            return
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
        pct = 10
        try:
            pct = int((settings.get('referral_commission_percent') or '10').strip())
        except Exception:
            pct = 10
        pct = max(0, min(100, pct))
        bonus = max(1, int(base_price * (pct / 100.0)))
        # ensure wallet row and credit
        execute_db("INSERT OR IGNORE INTO user_wallets (user_id, balance) VALUES (?, 0)", (ref_id,))
        execute_db("UPDATE user_wallets SET balance = COALESCE(balance,0) + ? WHERE user_id = ?", (bonus, ref_id))
        execute_db(
            "INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at, reference, meta) VALUES (?, ?, 'credit', 'referral', 'approved', ?, ?, ?)",
            (ref_id, bonus, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"ref_bonus_order_{order_id}", None)
        )
        # notify referrer
        try:
            await context.bot.send_message(chat_id=ref_id, text=f"\U0001F389 پاداش معرفی: `{bonus:,}` تومان")
        except Exception:
            pass
    except Exception:
        pass


async def admin_global_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    # Do not intercept admin_* here; let ConversationHandler route states properly
    return


async def admin_global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_admin(update.effective_user.id):
        return
    flag = context.user_data.get('awaiting_admin')
    if not flag:
        return
    dispatch = {
        'set_ref_percent': admin_set_ref_percent_save,
        'set_config_footer': admin_set_config_footer_save,
        'set_payment_text': admin_settings_save_payment_text,
        'set_usd_rate': admin_set_usd_rate_save,
    }
    handler = dispatch.get(flag)
    if handler:
        await handler(update, context)
        raise ApplicationHandlerStop


async def admin_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    admins = query_db("SELECT user_id FROM admins ORDER BY user_id") or []
    text = "👑 مدیریت ادمین‌ها\n\n" + ("لیست ادمین‌ها:\n" + "\n".join(f"- `{row['user_id']}`" for row in admins) if admins else "ادمین دیگری ثبت نشده است.")
    text += "\n\nافزودن: `/addadmin USER_ID`\nحذف: `/deladmin USER_ID`\n"
    kb = [[InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")]]
    sender = query.message.edit_text if query else update.message.reply_text
    try:
        await sender(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await (query.message.reply_text if query else update.message.reply_text)(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    parts = (update.message.text or '').strip().split(maxsplit=1)
    if len(parts) == 2:
        try:
            uid = int(parts[1])
            execute_db("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
            await update.message.reply_text(f"✅ کاربر `{uid}` به عنوان ادمین اضافه شد.", parse_mode=ParseMode.MARKDOWN)
            return
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
            return
    # If no arg, show usage
    await update.message.reply_text("استفاده: /addadmin USER_ID")


async def admin_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    parts = (update.message.text or '').strip().split(maxsplit=1)
    if len(parts) == 2:
        try:
            uid = int(parts[1])
            execute_db("DELETE FROM admins WHERE user_id = ?", (uid,))
            await update.message.reply_text(f"✅ کاربر `{uid}` از لیست ادمین‌ها حذف شد.", parse_mode=ParseMode.MARKDOWN)
            return
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
            return
    await update.message.reply_text("استفاده: /deladmin USER_ID")


async def admin_setms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return
    txt = (update.message.text or '').strip()
    arg = txt[len('/setms'):].strip() if txt.startswith('/setms') else ''
    if arg:
        execute_db("UPDATE settings SET value = ? WHERE key = 'config_footer_text'", (arg,))
        await update.message.reply_text("✅ متن زیر کانفیگ بروزرسانی شد.")
        return
    context.user_data['awaiting_admin'] = 'set_config_footer'
    await update.message.reply_text("متن جدید برای نمایش زیر کانفیگ‌ها را ارسال کنید:")


async def admin_set_payment_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data['awaiting_admin'] = 'set_payment_text'
    target = query.message if query else update.message
    await target.reply_text("متن پرداخت را ارسال کنید:")
    return SETTINGS_MENU if query else ConversationHandler.END


async def admin_set_usd_rate_start_global(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data['awaiting_admin'] = 'set_usd_rate'
    target = query.message if query else update.message
    await target.reply_text("نرخ دلار به تومان را ارسال کنید (برای پاک کردن '-').")
    return SETTINGS_AWAIT_USD_RATE if query else ConversationHandler.END


async def admin_set_config_footer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data['awaiting_admin'] = 'set_config_footer'
    target = query.message if query else update.message
    await target.reply_text("متن زیر کانفیگ را ارسال کنید:")
    return SETTINGS_MENU if query else ConversationHandler.END


async def admin_set_config_footer_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if context.user_data.get('awaiting_admin') != 'set_config_footer':
        return ConversationHandler.END
    new_text = (update.message.text or '').strip()
    execute_db("UPDATE settings SET value = ? WHERE key = 'config_footer_text'", (new_text,))
    context.user_data.pop('awaiting_admin', None)
    await update.message.reply_text("✅ متن زیر کانفیگ ذخیره شد.")
    # Refresh settings view
    fake_query = type('obj', (object,), {
        'data': 'admin_settings_manage',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_settings_manage(fake_update, context)


async def admin_set_ref_percent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data['awaiting_admin'] = 'set_ref_percent'
    target = query.message if query else update.message
    await target.reply_text("درصد کمیسیون معرفی را به صورت عددی (۰ تا ۱۰۰) ارسال کنید:")
    return SETTINGS_MENU if query else ConversationHandler.END


async def admin_set_ref_percent_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        return ConversationHandler.END
    if context.user_data.get('awaiting_admin') != 'set_ref_percent':
        return ConversationHandler.END
    txt = (update.message.text or '').strip()
    try:
        percent = int(float(txt))
        if percent < 0 or percent > 100:
            raise ValueError()
        execute_db("UPDATE settings SET value = ? WHERE key = 'referral_commission_percent'", (str(percent),))
        await update.message.reply_text("✅ درصد کمیسیون ذخیره شد.")
        context.user_data.pop('awaiting_admin', None)
    except Exception:
        await update.message.reply_text("ورودی نامعتبر است. یک عدد بین ۰ تا ۱۰۰ ارسال کنید.")
        return ConversationHandler.END
    # Refresh settings view
    fake_query = type('obj', (object,), {
        'data': 'admin_settings_manage',
        'message': update.message,
        'answer': (lambda *args, **kwargs: asyncio.sleep(0)),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_settings_manage(fake_update, context)


async def admin_set_trial_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    panels = query_db("SELECT id, name FROM panels ORDER BY id") or []
    keyboard = [[InlineKeyboardButton(p['name'], callback_data=f"set_trial_panel_{p['id']}")] for p in panels]
    keyboard.insert(0, [InlineKeyboardButton("پیش‌فرض", callback_data="set_trial_panel_0")])
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_settings_manage")])
    target = query.message if query else update.message
    await target.reply_text("پنل ساخت تست را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


async def admin_set_trial_panel_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panel_id = query.data.split('_')[-1]
    value = '' if panel_id == '0' else panel_id
    execute_db("UPDATE settings SET value = ? WHERE key = 'free_trial_panel_id'", (value,))
    await query.answer("ذخیره شد", show_alert=True)
    return await admin_settings_manage(update, context)