from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from ..db import query_db, execute_db
from ..states import SETTINGS_MENU, SETTINGS_AWAIT_TRIAL_DAYS, SETTINGS_AWAIT_PAYMENT_TEXT, SETTINGS_AWAIT_USD_RATE, SETTINGS_AWAIT_GATEWAY_API, SETTINGS_AWAIT_SIGNUP_BONUS
from ..helpers.tg import safe_edit_text as _safe_edit_text
from ..config import ADMIN_ID, logger


def _md_escape(text: str) -> str:
    if not text:
        return ''
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
        [InlineKeyboardButton("تنظیمات نمایندگی", callback_data="admin_reseller_menu")],
        [InlineKeyboardButton("تنظیم درصد کمیسیون معرفی", callback_data="set_ref_percent_start")],
        [InlineKeyboardButton("\U0001F4B3 مدیریت کارت‌ها", callback_data="admin_cards_menu"), InlineKeyboardButton("\U0001F4B0 مدیریت ولت‌ها", callback_data="admin_wallets_menu")],
        [InlineKeyboardButton("\U0001F4B8 درخواست‌های شارژ کیف پول", callback_data="admin_wallet_tx_menu")],
        [InlineKeyboardButton("\U0001F4B1 تنظیم نرخ دلار", callback_data="set_usd_rate_start"), InlineKeyboardButton("\U0001F504 تغییر حالت نرخ: " + ("به دستی" if next_mode=='manual' else "به API"), callback_data=f"toggle_usd_mode_{next_mode}")],
        [InlineKeyboardButton(("غیرفعال کردن کارت" if pay_card else "فعال کردن کارت"), callback_data=f"toggle_pay_card_{0 if pay_card else 1}"), InlineKeyboardButton(("غیرفعال کردن رمزارز" if pay_crypto else "فعال کردن رمزارز"), callback_data=f"toggle_pay_crypto_{0 if pay_crypto else 1}")],
        [InlineKeyboardButton(("غیرفعال کردن درگاه" if pay_gateway else "فعال کردن درگاه"), callback_data=f"toggle_pay_gateway_{0 if pay_gateway else 1}"), InlineKeyboardButton(("زرین‌پال" if gateway_type!='zarinpal' else "آقای پرداخت"), callback_data=f"toggle_gateway_type_{'zarinpal' if gateway_type!='zarinpal' else 'aghapay'}")],
        [InlineKeyboardButton(("غیرفعال کردن هدیه ثبت‌نام" if sb_enabled else "فعال کردن هدیه ثبت‌نام"), callback_data=f"toggle_signup_bonus_{0 if sb_enabled else 1}"), InlineKeyboardButton("تنظیم مبلغ هدیه ثبت‌نام", callback_data="set_signup_bonus_amount")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data="admin_main")],
    ]
    await _safe_edit_text(query.message, text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


async def admin_toggle_trial_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    new_status = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'free_trial_status'", (new_status,))
    await query.answer(f"وضعیت تست رایگان {'فعال' if new_status == '1' else 'غیرفعال'} شد.", show_alert=True)
    return await admin_settings_manage(update, context)


async def admin_toggle_usd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    target = query.data.split('_')[-1]
    execute_db("UPDATE settings SET value = ? WHERE key = 'usd_irt_mode'", (target,))
    return await admin_settings_manage(update, context)


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
    return await admin_settings_manage(update, context)


async def admin_settings_save_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    fake_query = type('obj', (object,), {
        'data': 'admin_settings_manage',
        'message': update.message,
        'answer': (lambda *args, **kwargs: None),
        'from_user': update.effective_user,
    })
    fake_update = type('obj', (object,), {'callback_query': fake_query, 'effective_user': update.effective_user})
    return await admin_settings_manage(fake_update, context)