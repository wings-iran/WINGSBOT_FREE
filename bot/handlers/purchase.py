from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from ..db import query_db, execute_db
from ..handlers.common import start_command
from ..states import SELECT_PLAN, AWAIT_DISCOUNT_CODE, AWAIT_PAYMENT_SCREENSHOT, RENEW_AWAIT_PAYMENT, SELECT_PAYMENT_METHOD
from ..config import NOBITEX_TOKEN, logger
import requests
from ..helpers.tg import safe_edit_text as _safe_edit, ltr_code, notify_admins
from ..helpers.flow import set_flow, clear_flow


async def start_purchase_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    plans = query_db("SELECT id, name, price FROM plans ORDER BY price")
    if not plans:
        await _safe_edit(
            query.message,
            "در حال حاضر هیچ پلن فعالی برای فروش وجود ندارد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')]]),
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"{plan['name']} - {plan['price']:,} تومان", callback_data=f"select_plan_{plan['id']}")] for plan in plans]
    keyboard.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='start_main')])

    message_data = query_db("SELECT text FROM messages WHERE message_name = 'buy_config_main'", one=True)
    text = message_data.get('text') if message_data else "لطفا پلن خود را انتخاب کنید:"

    await _safe_edit(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return SELECT_PLAN


async def show_plan_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    plan_id = int(query.data.replace('select_plan_', ''))
    await query.answer()

    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    if not plan:
        await _safe_edit(
            query.message,
            "خطا: پلن یافت نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='buy_config_main')]]),
        )
        return SELECT_PLAN

    context.user_data['selected_plan_id'] = plan_id
    context.user_data['original_price'] = plan['price']
    context.user_data['final_price'] = plan['price']
    context.user_data['discount_code'] = None

    traffic_display = "نامحدود" if float(plan['traffic_gb']) == 0 else f"{plan['traffic_gb']} گیگابایت"

    text = (
        f"شما پلن زیر را انتخاب کرده‌اید:\n\n"
        f"**نام پلن:** {plan['name']}\n"
        f"**توضیحات:** {plan['description']}\n"
        f"**مدت زمان:** {plan['duration_days']} روز\n"
        f"**حجم:** {traffic_display}\n"
        f"**قیمت:** {plan['price']:,} تومان\n\n"
        f"آیا تایید می‌کنید؟"
    )
    keyboard = [
        [InlineKeyboardButton("\u2705 تایید و پرداخت", callback_data="confirm_purchase")],
        [InlineKeyboardButton("\U0001F381 کد تخفیف دارم", callback_data="apply_discount_start")],
        [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='buy_config_main')],
    ]
    await _safe_edit(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return SELECT_PLAN


async def apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.message.edit_text("لطفا کد تخفیف خود را وارد کنید:")
    return AWAIT_DISCOUNT_CODE


async def receive_and_validate_discount_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_code = update.message.text.strip().upper()
    original_price = context.user_data.get('original_price')

    if original_price is None:
        await update.message.reply_text("خطا! لطفا فرآیند را از ابتدا شروع کنید.")
        await start_command(update, context)
        return ConversationHandler.END

    code_data = query_db("SELECT * FROM discount_codes WHERE code = ?", (user_code,), one=True)
    error_message = None
    from datetime import datetime as _dt
    if not code_data:
        error_message = "کد تخفیف یافت نشد."
    elif code_data['expiry_date'] and _dt.strptime(code_data['expiry_date'], "%Y-%m-%d %H:%M:%S") < _dt.now():
        error_message = "این کد تخفیف منقضی شده است."
    elif code_data['usage_limit'] > 0 and code_data['times_used'] >= code_data['usage_limit']:
        error_message = "ظرفیت استفاده از این کد تخفیف به پایان رسیده است."

    if error_message:
        await update.message.reply_text(f"\u274C {error_message} لطفا کد دیگری وارد کنید یا برای لغو /cancel را بفرستید.")
        return AWAIT_DISCOUNT_CODE

    discount_percent = code_data['percentage']
    new_price = int(original_price * (100 - discount_percent) / 100)
    context.user_data['final_price'] = new_price
    context.user_data['discount_code'] = user_code

    await update.message.reply_text(
        f"✅ تخفیف {discount_percent}% اعمال شد.\n"
        f"قیمت اصلی: {original_price:,} تومان\n"
        f"**قیمت جدید: {new_price:,} تومان**"
    )

    return await show_payment_info(update, context)


def _best_mid_from_orderbook(bids, asks) -> float:
    try:
        best_bid = float(bids[0][0]) if bids and bids[0] else 0.0
        best_ask = float(asks[0][0]) if asks and asks[0] else 0.0
        if best_bid > 0 and best_ask > 0:
            return (best_bid + best_ask) / 2.0
        return best_ask or best_bid or 0.0
    except Exception:
        return 0.0


def _fetch_from_wallex() -> float:
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0',
    }
    endpoints = [
        ('GET', 'https://api.wallex.ir/v1/markets/orderbook', {'symbol': 'usdt-irt'}),
        ('GET', 'https://api.wallex.ir/v1/depth', {'symbol': 'usdt-irt'}),
    ]
    for method, url, params in endpoints:
        try:
            r = requests.request(method, url, headers=headers, params=params, timeout=10)
            if not r.ok:
                continue
            data = r.json() or {}
            # common shapes: {'result': {'orderbook': {'bids': [...], 'asks': [...]}}}
            res = data.get('result') or data
            ob = res.get('orderbook') or res.get('depth') or res
            bids = ob.get('bids') or []
            asks = ob.get('asks') or []
            price = _best_mid_from_orderbook(bids, asks)
            if price > 0:
                return price
        except Exception:
            continue
    return 0.0


def _fetch_from_bitpin() -> float:
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0',
    }
    endpoints = [
        ('GET', 'https://api.bitpin.ir/v1/mth/orderbook/USDTIRT', None),
        ('GET', 'https://api.bitpin.ir/v1/orderbook/USDTIRT', None),
        ('GET', 'https://api.bitpin.ir/v2/orderbook/USDTIRT', None),
    ]
    for method, url, params in endpoints:
        try:
            r = requests.request(method, url, headers=headers, params=params, timeout=10)
            if not r.ok:
                continue
            data = r.json() or {}
            # common shapes: {'result': {'bids': [...], 'asks': [...]}} or flat
            res = data.get('result') or data
            bids = res.get('bids') or []
            asks = res.get('asks') or []
            price = _best_mid_from_orderbook(bids, asks)
            if price > 0:
                return price
        except Exception:
            continue
    return 0.0


def _fetch_nobitex_usd_irt() -> float:
    try:
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': 'Mozilla/5.0',
        }
        if NOBITEX_TOKEN:
            headers['Authorization'] = f"Token {NOBITEX_TOKEN}"
        # Try orderbook variants (prices in Toman)
        endpoints = [
            ('GET', 'https://api.nobitex.ir/v2/orderbook/USDTIRT', None),
            ('GET', 'https://api.nobitex.ir/v2/orderbook/USDT-IRT', None),
            ('GET', 'https://api.nobitex.ir/v2/orderbook/USDT_IRT', None),
            ('GET', 'https://api.nobitex.ir/v2/orderbook', {'symbol': 'USDTIRT'}),
        ]
        for method, url, params in endpoints:
            try:
                r = requests.request(method, url, headers=headers, params=params, timeout=10)
                if not r.ok:
                    continue
                data = r.json() or {}
                ob = data.get('orderbook') if isinstance(data, dict) else None
                bids = (ob or data).get('bids') or []
                asks = (ob or data).get('asks') or []
                best_bid = float(bids[0][0]) if bids and bids[0] else 0.0
                best_ask = float(asks[0][0]) if asks and asks[0] else 0.0
                if best_bid > 0 and best_ask > 0:
                    return (best_bid + best_ask) / 2.0
                if best_ask > 0 or best_bid > 0:
                    return best_ask or best_bid
            except Exception:
                continue
        # Fallback to stats (Toman)
        rs = requests.get('https://api.nobitex.ir/v2/stats', headers=headers, timeout=10)
        if rs.ok:
            d = rs.json() or {}
            stats = d.get('stats') or {}
            pair = stats.get('USDTIRT') or stats.get('USDT-IRT') or {}
            p = pair.get('latest') or pair.get('bestSell') or pair.get('average')
            if p:
                return float(p)
        # Legacy market/stats (Rial)
        rl = requests.post('https://api.nobitex.ir/market/stats', json={'srcCurrency': 'usdt', 'dstCurrency': 'rls'}, headers={'Content-Type': 'application/json', **({'Authorization': f'Token {NOBITEX_TOKEN}'} if NOBITEX_TOKEN else {})}, timeout=10)
        if rl.ok:
            d2 = rl.json() or {}
            s2 = d2.get('stats') or {}
            usdt = s2.get('usdt-rls') or s2.get('USDT-IRT') or {}
            p2 = usdt.get('latest') or usdt.get('bestSell') or usdt.get('average')
            if p2:
                return float(p2) / 10.0
    except Exception as e:
        logger.error(f"Nobitex fetch error: {e}")
    return 0.0


def _fetch_usdt_irt_price() -> float:
    # Priority based on mode: manual or api; then cached
    from ..db import query_db as _q, execute_db as _x
    mode = ((_q("SELECT value FROM settings WHERE key = 'usd_irt_mode'", one=True) or {}).get('value') or 'manual').lower()
    if mode == 'manual':
        manual = (_q("SELECT value FROM settings WHERE key = 'usd_irt_manual'", one=True) or {}).get('value') or ''
        try:
            rate = float(manual.strip()) if manual.strip() else 0.0
            if rate > 0:
                return rate
        except Exception:
            pass
    else:
        price = _fetch_nobitex_usd_irt()
        if price > 0:
            try:
                _x("UPDATE settings SET value = ? WHERE key = 'usd_irt_cached'", (str(int(price)),))
                _x("UPDATE settings SET value = ? WHERE key = 'usd_irt_cached_ts'", (datetime.now().isoformat(timespec='seconds'),))
            except Exception:
                pass
            return price
    # Cached fallback
    cached = (_q("SELECT value FROM settings WHERE key = 'usd_irt_cached'", one=True) or {}).get('value') or ''
    try:
        c = float(cached.strip()) if cached.strip() else 0.0
        if c > 0:
            return c
    except Exception:
        pass
    return 0.0


async def show_payment_method_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    final_price = context.user_data.get('final_price')
    if final_price is None:
        await update.effective_message.reply_text("خطا! قیمت نهایی مشخص نیست. لطفا از ابتدا شروع کنید.")
        return await cancel_flow(update, context)

    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    pay_card = settings.get('pay_card_enabled', '1') == '1'
    pay_crypto = settings.get('pay_crypto_enabled', '1') == '1'
    pay_gateway = settings.get('pay_gateway_enabled', '0') == '1'

    # User wallet balance
    bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (update.effective_user.id,), one=True)
    balance = bal_row.get('balance') if bal_row else 0

    text = "روش پرداخت خود را انتخاب کنید:"
    kb = []
    # Wallet option always shown; validation happens on click
    kb.append([InlineKeyboardButton("\U0001F4B3 پرداخت با کیف پول", callback_data='pay_method_wallet')])
    if pay_card:
        kb.append([InlineKeyboardButton("\U0001F4B3 کارت به کارت", callback_data='pay_method_card')])
    if pay_crypto:
        kb.append([InlineKeyboardButton("\U0001F4B0 رمزارز (Crypto)", callback_data='pay_method_crypto')])
    if pay_gateway:
        kb.append([InlineKeyboardButton("\U0001F6E0\uFE0F درگاه پرداخت", callback_data='pay_method_gateway')])
    # Back button depends on flow: purchase vs renewal
    if context.user_data.get('renewing_order_id'):
        order_id = context.user_data.get('renewing_order_id')
        kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data=f"view_service_{order_id}")])
    else:
        kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='buy_config_main')])

    extra = f"\n\n\U0001F4B0 موجودی کیف پول شما: {balance:,} تومان"
    if query:
        await _safe_edit(query.message, text + extra, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text + extra, reply_markup=InlineKeyboardMarkup(kb))
    return SELECT_PAYMENT_METHOD


async def pay_method_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    final_price = context.user_data.get('final_price')
    if final_price is None:
        await query.message.edit_text("خطا: قیمت نهایی یافت نشد. از ابتدا شروع کنید.")
        return ConversationHandler.END
    bal_row = query_db("SELECT balance FROM user_wallets WHERE user_id = ?", (user.id,), one=True)
    balance = bal_row.get('balance') if bal_row else 0
    if balance < int(final_price):
        kb = [
            [InlineKeyboardButton("\U0001F4B3 شارژ کیف پول", callback_data='wallet_menu')],
            [InlineKeyboardButton("\U0001F519 بازگشت", callback_data='buy_config_main')],
        ]
        await query.message.edit_text(f"\u26A0\uFE0F موجودی کیف پول کافی نیست.\nموجودی: {balance:,} تومان\nمبلغ موردنیاز: {int(final_price):,} تومان", reply_markup=InlineKeyboardMarkup(kb))
        return SELECT_PAYMENT_METHOD

    # Deduct and log transaction
    execute_db("INSERT OR IGNORE INTO user_wallets (user_id, balance) VALUES (?, 0)", (user.id,))
    execute_db("UPDATE user_wallets SET balance = balance - ? WHERE user_id = ?", (int(final_price), user.id))
    execute_db("INSERT INTO wallet_transactions (user_id, amount, direction, method, status, created_at) VALUES (?, ?, 'debit', 'wallet', 'approved', ?)", (user.id, int(final_price), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    is_renewal = context.user_data.get('renewing_order_id')
    if is_renewal:
        order_id = context.user_data.get('renewing_order_id')
        plan_id = context.user_data.get('selected_renewal_plan_id')
        if not order_id or not plan_id:
            await query.message.edit_text("خطا در فرآیند تمدید. دوباره تلاش کنید.")
            return ConversationHandler.END
        plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
        await notify_admins(context.bot,
            text=(f"\u2757 **درخواست تمدید** (برای سفارش #{order_id})\n\n**پلن تمدید:** {plan['name']}\n\U0001F4B0 **مبلغ:** {int(final_price):,} تومان\n\U0001F4B3 **روش:** کیف پول\n\nلطفا پس از بررسی، تمدید را تایید کنید:"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید و تمدید سرویس", callback_data=f"approve_renewal_{order_id}_{plan_id}")]]),
        )
        # Show remaining balance
        new_bal = (balance - int(final_price))
        await query.message.edit_text(f"\u2705 پرداخت از کیف پول ثبت شد و برای تایید به ادمین ارسال شد.\nموجودی فعلی: {new_bal:,} تومان")
        context.user_data.clear()
        await start_command(update, context)
        return ConversationHandler.END

    # Purchase flow
    plan_id = context.user_data.get('selected_plan_id')
    discount_code = context.user_data.get('discount_code')
    if not plan_id:
        await query.message.edit_text("خطا: پلن انتخابی یافت نشد.")
        return ConversationHandler.END
    order_id = execute_db(
        "INSERT INTO orders (user_id, plan_id, timestamp, final_price, discount_code) VALUES (?, ?, ?, ?, ?)",
        (user.id, plan_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(final_price), discount_code),
    )
    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    user_info = f"\U0001F464 **کاربر:** {user.mention_html()}\n\U0001F194 **آیدی:** `{user.id}`"
    plan_info = f"\U0001F4CB **پلن:** {plan['name']}"
    price_info = f"\U0001F4B0 **مبلغ پرداختی:** {int(final_price):,} تومان\n\U0001F4B3 **روش:** کیف پول"
    await notify_admins(context.bot,
        text=(f"\U0001F514 **درخواست خرید جدید** (سفارش #{order_id})\n\n{user_info}\n\n{plan_info}\n{price_info}\n\nلطفا نتیجه را اعلام کنید:"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 تأیید و ارسال خودکار", callback_data=f"approve_auto_{order_id}")],
            [InlineKeyboardButton("\U0001F4DD تأیید و ارسال دستی", callback_data=f"approve_manual_{order_id}")],
            [InlineKeyboardButton("\u274C رد درخواست", callback_data=f"reject_order_{order_id}")],
        ]),
    )
    # Apply referral bonus immediately on wallet payment
    try:
        from .admin import _apply_referral_bonus
        await _apply_referral_bonus(order_id, context)
    except Exception:
        pass
    new_bal = (balance - int(final_price))
    await query.message.edit_text(f"\u2705 پرداخت از کیف پول ثبت شد و برای تایید به ادمین ارسال شد.\nموجودی فعلی: {new_bal:,} تومان")
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END


async def show_payment_info_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    final_price = context.user_data.get('final_price')
    if final_price is None:
        await update.effective_message.reply_text("خطا! قیمت نهایی مشخص نیست. لطفا از ابتدا شروع کنید.")
        return await cancel_flow(update, context)

    cards = query_db("SELECT card_number, holder_name FROM cards")
    payment_message_data = query_db("SELECT text FROM messages WHERE message_name = 'payment_info_text'", one=True)

    is_renewal = context.user_data.get('renewing_order_id')
    if is_renewal:
        order_id = context.user_data['renewing_order_id']
        cancel_callback = f"view_service_{order_id}"
        cancel_text = "\U0001F519 لغو تمدید"
        next_state = RENEW_AWAIT_PAYMENT
    else:
        cancel_callback = 'buy_config_main'
        cancel_text = "\U0001F519 لغو و بازگشت"
        next_state = AWAIT_PAYMENT_SCREENSHOT

    if not cards:
        text_to_send = "خطا: هیچ کارت بانکی در سیستم ثبت نشده است."
    else:
        text_to_send = (payment_message_data['text'] or '') + "\n\n"
        text_to_send += f"\U0001F4B0 <b>مبلغ قابل پرداخت: {final_price:,} تومان</b>\n\n"
        text_to_send += "\u2500" * 15 + "\n\n"
        for card in cards:
            holder = card['holder_name']
            text_to_send += f"\U0001F464 <b>نام دارنده:</b> {holder}\n"
            text_to_send += f"\U0001F4B3 <b>شماره کارت:</b>\n{ltr_code(card['card_number'])}\n\n"
        text_to_send += "\u2500" * 15

    keyboard = [[InlineKeyboardButton(cancel_text, callback_data=cancel_callback)]]
    # Mark awaiting and set flow lock so join-gate won’t block screenshot messages
    context.user_data['awaiting'] = 'renewal_payment' if is_renewal else 'purchase_payment'
    set_flow(context, 'renewal' if is_renewal else 'purchase')

    if query:
        await _safe_edit(query.message, text_to_send, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text_to_send, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

    return next_state


async def show_payment_info_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    final_price = context.user_data.get('final_price')
    if final_price is None:
        await update.effective_message.reply_text("خطا! قیمت نهایی مشخص نیست. لطفا از ابتدا شروع کنید.")
        return await cancel_flow(update, context)

    wallets = query_db("SELECT asset, chain, address, COALESCE(memo,'') AS memo FROM wallets")
    if not wallets:
        text_to_send = "خطا: هیچ ولتی ثبت نشده است."
        kb = [[InlineKeyboardButton("\U0001F519 بازگشت", callback_data='buy_config_main')]]
        if query:
            await query.message.edit_text(text_to_send, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(text_to_send, reply_markup=InlineKeyboardMarkup(kb))
        return SELECT_PAYMENT_METHOD

    usdt_irt = _fetch_usdt_irt_price()
    usd_amount = (final_price / usdt_irt) if usdt_irt > 0 else 0

    is_renewal = context.user_data.get('renewing_order_id')
    if is_renewal:
        order_id = context.user_data['renewing_order_id']
        cancel_callback = f"view_service_{order_id}"
        cancel_text = "\U0001F519 لغو تمدید"
        next_state = RENEW_AWAIT_PAYMENT
    else:
        cancel_callback = 'buy_config_main'
        cancel_text = "\U0001F519 لغو و بازگشت"
        next_state = AWAIT_PAYMENT_SCREENSHOT

    rate_line = (
        f"\U0001F4C8 نرخ دلار: {int(usdt_irt):,} تومان" if usdt_irt > 0 else "\U0001F4C8 نرخ دلار: تنظیم نشده"
    )
    usd_line = f"\U0001F4B1 معادل تقریبی: {usd_amount:.2f} USD" if usdt_irt > 0 else "\U0001F4B1 معادل تقریبی: —"

    text_lines = [
        "\U0001F4B0 پرداخت رمزارزی",
        f"\U0001F4B5 مبلغ نهایی: {final_price:,} تومان",
        rate_line,
        usd_line,
        "\nولت‌های قابل پذیرش:",
    ]
    for w in wallets:
        memo_line = f"\nTag/Memo: `{w['memo']}`" if w['memo'] else ''
        text_lines.append(f"- **{w['asset']}** روی **{w['chain']}**\n`{w['address']}`{memo_line}")
    text_to_send = "\n".join(text_lines)

    keyboard = [[InlineKeyboardButton(cancel_text, callback_data=cancel_callback)]]
    # Mark awaiting and set flow lock so join-gate won’t block screenshot messages
    context.user_data['awaiting'] = 'renewal_payment' if is_renewal else 'purchase_payment'
    set_flow(context, 'renewal' if is_renewal else 'purchase')

    if query:
        await _safe_edit(query.message, text_to_send, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    return next_state


async def show_payment_info_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    final_price = context.user_data.get('final_price')
    if final_price is None:
        await update.effective_message.reply_text("خطا! قیمت نهایی مشخص نیست. لطفا از ابتدا شروع کنید.")
        return await cancel_flow(update, context)

    settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
    gateway_type = (settings.get('gateway_type') or 'zarinpal').lower()
    callback_url = (settings.get('gateway_callback_url') or '').strip()

    is_renewal = context.user_data.get('renewing_order_id')
    if is_renewal:
        order_id = context.user_data['renewing_order_id']
        cancel_callback = f"view_service_{order_id}"
        cancel_text = "\U0001F519 لغو تمدید"
        next_state = RENEW_AWAIT_PAYMENT
    else:
        cancel_callback = 'buy_config_main'
        cancel_text = "\U0001F519 لغو و بازگشت"
        next_state = AWAIT_PAYMENT_SCREENSHOT

    amount_rial = int(final_price) * 10
    description = "پرداخت ربات فروش کانفیگ"

    if gateway_type == 'zarinpal':
        merchant_id = (settings.get('zarinpal_merchant_id') or '').strip()
        if not merchant_id:
            text_to_send = "خطا: MerchantID زرین‌پال تنظیم نشده است."
        else:
            authority, start_url = _zarinpal_request(merchant_id, amount_rial, description, callback_url or 'https://example.com/callback')
            if authority and start_url:
                context.user_data['gateway'] = {'type': 'zarinpal', 'authority': authority, 'amount_rial': amount_rial}
                kb = [
                    [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=start_url)],
                    [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='gateway_verify_purchase' if not is_renewal else 'gateway_verify_renewal')],
                    [InlineKeyboardButton(cancel_text, callback_data=cancel_callback)],
                ]
                text_to_send = f"\U0001F6E0\uFE0F پرداخت آنلاین\n\n\U0001F4B0 مبلغ: {final_price:,} تومان\n\nروی دکمه زیر بزنید و پس از پرداخت، دکمه \"بررسی پرداخت\" را لمس کنید."
                if query:
                    await query.message.edit_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
                else:
                    await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
                return next_state
            else:
                text_to_send = "خطا: ایجاد درخواست پرداخت زرین‌پال ناموفق بود."
    else:
        # Aghayepardakht v2 with PIN
        pin = (settings.get('aghapay_pin') or '').strip()
        if not pin:
            text_to_send = "خطا: PIN آقای پرداخت تنظیم نشده است."
        elif not callback_url:
            text_to_send = "خطا: Callback URL آقای پرداخت تنظیم نشده است."
        else:
            order_id_str = f"ORD-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            payment_url = _aghapay_create(pin, int(final_price), callback_url, order_id_str, description)
            if payment_url:
                context.user_data['gateway'] = {'type': 'aghapay', 'amount_rial': amount_rial, 'transid': payment_url.split('/')[-1]}
                kb = [
                    [InlineKeyboardButton("\U0001F6D2 رفتن به صفحه پرداخت", url=payment_url)],
                    [InlineKeyboardButton("\U0001F50D بررسی پرداخت", callback_data='gateway_verify_purchase' if not is_renewal else 'gateway_verify_renewal')],
                    [InlineKeyboardButton(cancel_text, callback_data=cancel_callback)],
                ]
                text_to_send = f"\U0001F6E0\uFE0F پرداخت آنلاین\n\n\U0001F4B0 مبلغ: {final_price:,} تومان\n\nروی دکمه زیر بزنید و پس از پرداخت، دکمه \"بررسی پرداخت\" را لمس کنید."
                if query:
                    await query.message.edit_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
                else:
                    await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
                return next_state
            else:
                text_to_send = "خطا: ایجاد لینک پرداخت آقای پرداخت ناموفق بود. (v2)"

    kb = [[InlineKeyboardButton(cancel_text, callback_data=cancel_callback)]]
    if query:
        await _safe_edit(query.message, text_to_send, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text_to_send, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    return next_state


async def show_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # After confirming plan or discount, first ask for payment method
    return await show_payment_method_selection(update, context)


async def receive_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    photo_file_id = None
    document_file_id = None
    caption_extra = ''
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.document:
        document_file_id = update.message.document.file_id
    elif update.message.text:
        caption_extra = update.message.text

    plan_id = context.user_data.get('selected_plan_id')
    final_price = context.user_data.get('final_price')
    discount_code = context.user_data.get('discount_code')

    if not plan_id or final_price is None:
        await update.message.reply_text("خطا: اطلاعات خرید یافت نشد. لطفا مجددا خرید کنید.")
        await start_command(update, context)
        return ConversationHandler.END

    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    order_id = execute_db(
        "INSERT INTO orders (user_id, plan_id, screenshot_file_id, timestamp, final_price, discount_code) VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, plan_id, (photo_file_id or document_file_id or None), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), final_price, discount_code),
    )

    user_info = f"\U0001F464 **کاربر:** {user.mention_html()}\n\U0001F194 **آیدی:** `{user.id}`"
    plan_info = f"\U0001F4CB **پلن:** {plan['name']}"

    price_info = f"\U0001F4B0 **مبلغ پرداختی:** {final_price:,} تومان"
    if discount_code:
        price_info += f"\n\U0001F381 **کد تخفیف:** `{discount_code}`"

    caption = f"\U0001F514 **درخواست خرید جدید** (سفارش #{order_id})\n\n{user_info}\n\n{plan_info}\n{price_info}\n\nلطفا نتیجه را اعلام کنید:"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 تأیید و ارسال خودکار", callback_data=f"approve_auto_{order_id}")],
        [InlineKeyboardButton("\U0001F4DD تأیید و ارسال دستی", callback_data=f"approve_manual_{order_id}")],
        [InlineKeyboardButton("\u274C رد درخواست", callback_data=f"reject_order_{order_id}")],
    ])
    if photo_file_id:
        await notify_admins(context.bot, photo=photo_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif document_file_id:
        await notify_admins(context.bot, document=document_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await notify_admins(context.bot, text=f"{caption}\n\n{caption_extra}", parse_mode=ParseMode.HTML, reply_markup=kb)
    await update.message.reply_text("\u2705 رسید شما برای ادمین ارسال شد. لطفا تا زمان تایید و دریافت کانفیگ صبور باشید.")
    context.user_data.pop('awaiting', None)
    context.user_data.pop('renewing_order_id', None)
    context.user_data.pop('selected_plan_id', None)
    context.user_data.pop('final_price', None)
    context.user_data.pop('discount_code', None)
    clear_flow(context)
    await start_command(update, context)
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END


async def cancel_and_start_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clear any pending flow and jump to purchase list
    context.user_data.clear()
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    await start_purchase_flow(update, context)
    return ConversationHandler.END


def _zarinpal_request(merchant_id: str, amount_rial: int, description: str, callback_url: str) -> tuple[str, str]:
    try:
        payload = {
            "merchant_id": merchant_id,
            "amount": amount_rial,
            "description": description,
            "callback_url": callback_url,
        }
        r = requests.post('https://api.zarinpal.com/pg/v4/payment/request.json', json=payload, timeout=12)
        r.raise_for_status()
        data = r.json() or {}
        if isinstance(data, dict) and data.get('data') and data['data'].get('authority'):
            authority = data['data']['authority']
            start_url = f"https://payment.zarinpal.com/pg/StartPay/{authority}"
            return authority, start_url
        # Some responses may place authority differently
        if data.get('authority'):
            authority = data['authority']
            start_url = f"https://payment.zarinpal.com/pg/StartPay/{authority}"
            return authority, start_url
        return '', ''
    except Exception as e:
        logger.error(f"Zarinpal request error: {e}")
        return '', ''


def _zarinpal_verify(merchant_id: str, amount_rial: int, authority: str) -> tuple[bool, str]:
    try:
        payload = {
            "merchant_id": merchant_id,
            "amount": amount_rial,
            "authority": authority,
        }
        r = requests.post('https://api.zarinpal.com/pg/v4/payment/verify.json', json=payload, timeout=12)
        r.raise_for_status()
        data = r.json() or {}
        code = (data.get('data') or {}).get('code') if isinstance(data.get('data'), dict) else data.get('code')
        ref_id = (data.get('data') or {}).get('ref_id') if isinstance(data.get('data'), dict) else data.get('ref_id', '')
        ok = str(code) in ('100', '101')
        return ok, str(ref_id or '')
    except Exception as e:
        logger.error(f"Zarinpal verify error: {e}")
        return False, ''


def _aghapay_create(pin: str, amount_toman: int, callback_url: str, order_id: str, description: str) -> str:
    try:
        payload = {
            "pin": pin,
            "amount": amount_toman,
            "callback": callback_url,
            "invoice_id": order_id,
            "description": description,
        }
        r = requests.post('https://panel.aqayepardakht.ir/api/v2/create', json=payload, timeout=12)
        if not r.ok:
            logger.error(f"Aghayepardakht v2 create HTTP {r.status_code}: {r.text[:200]}")
            return ''
        data = r.json() or {}
        if data.get('status') == 'success' and data.get('transid'):
            transid = data['transid']
            return f"https://panel.aqayepardakht.ir/startpay/{transid}"
        logger.error(f"Aghayepardakht v2 create unexpected response: {data}")
        return ''
    except Exception as e:
        logger.error(f"Aghayepardakht v2 create error: {e}")
        return ''


def _aghapay_verify(pin: str, amount_toman: int, transid: str) -> bool:
    try:
        payload = {
            "pin": pin,
            "amount": amount_toman,
            "transid": transid,
        }
        r = requests.post('https://panel.aqayepardakht.ir/api/v2/verify', json=payload, timeout=12)
        if not r.ok:
            logger.error(f"Aghayepardakht v2 verify HTTP {r.status_code}: {r.text[:200]}")
            return False
        data = r.json() or {}
        return data.get('status') == 'success' and str(data.get('code')) == '1'
    except Exception as e:
        logger.error(f"Aghayepardakht v2 verify error: {e}")
        return False


async def gateway_verify_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gw = context.user_data.get('gateway') or {}
    if not gw:
        await query.message.edit_text("خطا: اطلاعات پرداخت یافت نشد.")
        return SELECT_PAYMENT_METHOD
    if gw.get('type') == 'zarinpal':
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
        merchant_id = settings.get('zarinpal_merchant_id') or ''
        ok, ref_id = _zarinpal_verify(merchant_id, gw.get('amount_rial', 0), gw.get('authority', ''))
        if not ok:
            await query.message.edit_text("پرداخت تایید نشد. اگر پرداخت کرده‌اید چند لحظه دیگر دوباره بررسی کنید یا از روش‌های دیگر استفاده کنید.")
            return SELECT_PAYMENT_METHOD
    elif gw.get('type') == 'aghapay':
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
        pin = settings.get('aghapay_pin') or ''
        ok = _aghapay_verify(pin, int(context.user_data.get('final_price', 0)), gw.get('transid', ''))
        if not ok:
            await query.message.edit_text("پرداخت تایید نشد. اگر پرداخت کرده‌اید چند لحظه دیگر دوباره بررسی کنید یا از روش‌های دیگر استفاده کنید.")
            return SELECT_PAYMENT_METHOD
    # For Aghayepardakht we cannot verify here without invoice id; fallback to manual review by admin
    # Create order and send to admin for approval
    user = query.from_user
    plan_id = context.user_data.get('selected_plan_id')
    final_price = context.user_data.get('final_price')
    discount_code = context.user_data.get('discount_code')
    if not plan_id or final_price is None:
        await query.message.edit_text("خطا: اطلاعات خرید یافت نشد. لطفا مجددا خرید کنید.")
        await start_command(update, context)
        return ConversationHandler.END
    order_id = execute_db(
        "INSERT INTO orders (user_id, plan_id, timestamp, final_price, discount_code) VALUES (?, ?, ?, ?, ?)",
        (user.id, plan_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), final_price, discount_code),
    )
    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    user_info = f"\U0001F464 **کاربر:** {user.mention_html()}\n\U0001F194 **آیدی:** `{user.id}`"
    plan_info = f"\U0001F4CB **پلن:** {plan['name']}"
    price_info = f"\U0001F4B0 **مبلغ پرداختی:** {final_price:,} تومان\n\U0001F6E0\uFE0F **روش:** درگاه پرداخت ({gw.get('type','')})"
    await notify_admins(context.bot,
        text=(f"\U0001F514 **درخواست خرید جدید** (سفارش #{order_id})\n\n{user_info}\n\n{plan_info}\n{price_info}\n\nلطفا نتیجه را اعلام کنید:"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2705 تأیید و ارسال خودکار", callback_data=f"approve_auto_{order_id}")],
            [InlineKeyboardButton("\U0001F4DD تأیید و ارسال دستی", callback_data=f"approve_manual_{order_id}")],
            [InlineKeyboardButton("\u274C رد درخواست", callback_data=f"reject_order_{order_id}")],
        ]),
    )
    try:
        from .admin import _apply_referral_bonus
        await _apply_referral_bonus(order_id, context)
    except Exception:
        pass
    await query.message.edit_text("\u2705 پرداخت شما ثبت شد و برای تایید به ادمین ارسال شد. لطفا منتظر بمانید.")
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END


async def gateway_verify_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    gw = context.user_data.get('gateway') or {}
    if not gw:
        await query.message.edit_text("خطا: اطلاعات پرداخت یافت نشد.")
        return RENEW_AWAIT_PAYMENT
    if gw.get('type') == 'zarinpal':
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
        merchant_id = settings.get('zarinpal_merchant_id') or ''
        ok, ref_id = _zarinpal_verify(merchant_id, gw.get('amount_rial', 0), gw.get('authority', ''))
        if not ok:
            await query.message.edit_text("پرداخت تایید نشد. اگر پرداخت کرده‌اید کمی بعد دوباره بررسی کنید.")
            return RENEW_AWAIT_PAYMENT
    elif gw.get('type') == 'aghapay':
        settings = {s['key']: s['value'] for s in query_db("SELECT key, value FROM settings")}
        pin = settings.get('aghapay_pin') or ''
        ok = _aghapay_verify(pin, int(context.user_data.get('final_price', 0)), gw.get('transid', ''))
        if not ok:
            await query.message.edit_text("پرداخت تایید نشد. اگر پرداخت کرده‌اید کمی بعد دوباره بررسی کنید.")
            return RENEW_AWAIT_PAYMENT
    # Send to admin for renewal approval
    order_id = context.user_data.get('renewing_order_id')
    plan_id = context.user_data.get('selected_renewal_plan_id')
    final_price = context.user_data.get('final_price')
    if not order_id or not plan_id or final_price is None:
        await query.message.edit_text("خطا در فرآیند تمدید. لطفا مجددا تلاش کنید.")
        return ConversationHandler.END
    plan = query_db("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
    await notify_admins(context.bot,
        text=(f"\u2757 **درخواست تمدید** (برای سفارش #{order_id})\n\n**پلن تمدید:** {plan['name']}\n\U0001F4B0 **مبلغ:** {final_price:,} تومان\n\U0001F6E0\uFE0F **روش:** درگاه پرداخت ({gw.get('type','')})\n\nلطفا پس از بررسی، تمدید را تایید کنید:"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u2705 تایید و تمدید سرویس", callback_data=f"approve_renewal_{order_id}_{plan_id}")]]),
    )
    await query.message.edit_text("\u2705 پرداخت تمدید ثبت شد و برای تایید به ادمین ارسال شد.")
    context.user_data.clear()
    await start_command(update, context)
    return ConversationHandler.END