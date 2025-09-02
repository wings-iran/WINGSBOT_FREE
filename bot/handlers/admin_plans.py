from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..db import query_db, execute_db
from ..states import (
    ADMIN_PLAN_MENU,
    ADMIN_PLAN_AWAIT_NAME,
    ADMIN_PLAN_AWAIT_DESC,
    ADMIN_PLAN_AWAIT_PRICE,
    ADMIN_PLAN_AWAIT_DAYS,
    ADMIN_PLAN_AWAIT_GIGABYTES,
    ADMIN_PLAN_EDIT_MENU,
    ADMIN_PLAN_EDIT_AWAIT_VALUE,
)
from ..helpers.tg import safe_edit_text as _safe_edit_text
from ..config import logger
import asyncio


async def admin_plan_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_sender = None
    query = update.callback_query
    if query:
        await query.answer()
        message_sender = 'edit'
    elif update.message:
        message_sender = 'reply'
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
    text = "مدیریت پلن‌های فروش:"

    if message_sender == 'edit':
        await _safe_edit_text(query.message, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_PLAN_MENU


async def admin_plan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    plan_id = int(query.data.split('_')[-1])
    execute_db("DELETE FROM plans WHERE id=?", (plan_id,))
    await query.answer("پلن حذف شد.", show_alert=True)
    return await admin_plan_manage(update, context)


async def admin_plan_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_plan'] = {}
    await _safe_edit_text(update.callback_query.message, "نام پلن جدید را وارد کنید (مثال: یک ماهه - ۳۰ گیگ):")
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
        return await admin_plan_manage(update, context)


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
        return await admin_plan_manage(update, context)

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