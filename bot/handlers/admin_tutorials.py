from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..db import query_db, execute_db
from ..states import ADMIN_MAIN_MENU
from ..helpers.tg import safe_edit_text as _safe_edit_text

# Local state keys under user_data
KEY_FLOW = 'tutorial_flow'           # values: idle|add_title|add_media|view
KEY_TUTORIAL_ID = 'tutorial_edit_id'
KEY_PAGE = 'tutorial_media_page'
PAGE_SIZE = 5


def _reset_flow(context: ContextTypes.DEFAULT_TYPE):
    for k in (KEY_FLOW, KEY_TUTORIAL_ID):
        context.user_data.pop(k, None)


async def admin_tutorials_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _reset_flow(context)
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
    if update.callback_query:
        await update.callback_query.answer()
        # await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        await _safe_edit_text(update.callback_query.message, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_tutorial_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data[KEY_FLOW] = 'add_title'
    try:
        await q.message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id=q.message.chat_id, text="عنوان آموزش را وارد کنید:")
    return ADMIN_MAIN_MENU


async def admin_tutorial_receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    flow = context.user_data.get(KEY_FLOW)
    if flow not in ('add_title', 'edit_title'):
        return ADMIN_MAIN_MENU
    title = (update.message.text or '').strip()
    if not title:
        await update.message.reply_text("عنوان نامعتبر است. دوباره تلاش کنید:")
        return ADMIN_MAIN_MENU
    if flow == 'add_title':
        tid = execute_db("INSERT INTO tutorials (title, sort_order, created_at) VALUES (?, 0, ?)", (title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))) or 0
        context.user_data[KEY_TUTORIAL_ID] = tid
        context.user_data[KEY_FLOW] = 'add_media'
        kb = [[InlineKeyboardButton("پایان", callback_data="tutorial_finish")]]
        await update.message.reply_text("عنوان ثبت شد. رسانه‌ها (عکس/ویدیو/سند/صدا/متن) را یکی‌یکی ارسال کنید. برای پایان دکمه 'پایان' را بزنید.", reply_markup=InlineKeyboardMarkup(kb))
        return ADMIN_MAIN_MENU
    else:
        tid = int(context.user_data.get(KEY_TUTORIAL_ID) or 0)
        if tid == 0:
            await update.message.reply_text("شناسه آموزش نامعتبر است.")
            return ADMIN_MAIN_MENU
        execute_db("UPDATE tutorials SET title = ? WHERE id = ?", (title, tid))
        await update.message.reply_text("✅ عنوان بروزرسانی شد.")
        # refresh view
        fake_q = type('obj', (object,), {'data': f"tutorial_view_{tid}", 'message': update.message, 'answer': (lambda *args, **kwargs: None)})
        fake_update = type('obj', (object,), {'callback_query': fake_q})
        return await admin_tutorial_view(fake_update, context)


async def admin_tutorial_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get(KEY_FLOW) not in ('add_media', 'view'):
        return ADMIN_MAIN_MENU
    tid = int(context.user_data.get(KEY_TUTORIAL_ID) or 0)
    if tid == 0:
        await update.message.reply_text("خطا: شناسه آموزش مشخص نیست. از ابتدا تلاش کنید.")
        _reset_flow(context)
        return ADMIN_MAIN_MENU
    ctype, file_id, caption = None, None, (update.message.caption or '')
    if update.message.photo:
        ctype, file_id = 'photo', update.message.photo[-1].file_id
    elif update.message.document:
        ctype, file_id = 'document', update.message.document.file_id
    elif update.message.video:
        ctype, file_id = 'video', update.message.video.file_id
    elif update.message.voice:
        ctype, file_id = 'voice', update.message.voice.file_id
    elif update.message.audio:
        ctype, file_id = 'audio', update.message.audio.file_id
    elif update.message.text:
        ctype, file_id, caption = 'text', update.message.text, ''
    else:
        await update.message.reply_text("نوع پیام پشتیبانی نمی‌شود.")
        return ADMIN_MAIN_MENU
    execute_db("INSERT INTO tutorial_media (tutorial_id, content_type, file_id, caption, sort_order, created_at) VALUES (?, ?, ?, ?, 0, ?)", (tid, ctype, file_id, caption, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    await update.message.reply_text("✅ ثبت شد. رسانه بعدی را ارسال کنید یا 'پایان' را بزنید.")
    return ADMIN_MAIN_MENU


async def admin_tutorial_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer("تمام شد")
    _reset_flow(context)
    await admin_tutorials_menu(update, context)
    return ADMIN_MAIN_MENU


async def admin_tutorial_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split('_')[-1])
    execute_db("DELETE FROM tutorials WHERE id = ?", (tid,))
    await q.answer("حذف شد", show_alert=True)
    return await admin_tutorials_menu(update, context)


async def admin_tutorial_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split('_')[-1])
    t = query_db("SELECT title FROM tutorials WHERE id = ?", (tid,), one=True)
    if not t:
        await q.answer("یافت نشد", show_alert=True)
        return ADMIN_MAIN_MENU
    context.user_data[KEY_TUTORIAL_ID] = tid
    context.user_data[KEY_FLOW] = 'view'
    # Pagination
    page = 0
    if q.data.startswith('tutorial_view_') and KEY_PAGE in context.user_data:
        page = int(context.user_data.get(KEY_PAGE) or 0)
    context.user_data[KEY_PAGE] = page
    total_row = query_db("SELECT COUNT(*) AS c FROM tutorial_media WHERE tutorial_id = ?", (tid,), one=True) or {'c': 0}
    total = int(total_row.get('c') or 0)
    offset = page * PAGE_SIZE
    media_rows = query_db("SELECT id, content_type, caption FROM tutorial_media WHERE tutorial_id = ? ORDER BY sort_order, id LIMIT ? OFFSET ?", (tid, PAGE_SIZE, offset)) or []

    text = f"\U0001F4D6 {t['title']}\n\nبرای افزودن رسانه جدید، فایل/متن را ارسال کنید.\n\nرسانه‌ها:" + ("\n(خالی)" if not media_rows else "")
    kb = []
    for m in media_rows:
        label = f"{m['content_type']} - #{m['id']}"
        kb.append([
            InlineKeyboardButton(label, callback_data=f"noop_{m['id']}"),
            InlineKeyboardButton("⬆️", callback_data=f"tmedia_up_{m['id']}"),
            InlineKeyboardButton("⬇️", callback_data=f"tmedia_down_{m['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"tmedia_del_{m['id']}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ قبلی', callback_data='tutorial_media_page_prev'))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton('بعدی ➡️', callback_data='tutorial_media_page_next'))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("✏️ ویرایش عنوان", callback_data='tutorial_edit_title')])
    kb.append([InlineKeyboardButton("🗑 حذف آموزش", callback_data=f"tutorial_delete_{tid}")])
    kb.append([InlineKeyboardButton("\U0001F519 بازگشت", callback_data='admin_tutorials_menu')])
    await _safe_edit_text(q.message, text, reply_markup=InlineKeyboardMarkup(kb))
    return ADMIN_MAIN_MENU


async def admin_tutorial_media_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    page = int(context.user_data.get(KEY_PAGE) or 0)
    if q.data.endswith('_prev') and page > 0:
        page -= 1
    elif q.data.endswith('_next'):
        page += 1
    context.user_data[KEY_PAGE] = page
    tid = int(context.user_data.get(KEY_TUTORIAL_ID) or 0)
    if tid == 0:
        return ADMIN_MAIN_MENU
    fake_q = type('obj', (object,), {'data': f"tutorial_view_{tid}", 'message': q.message, 'answer': (lambda *a, **k: None)})
    fake_update = type('obj', (object,), {'callback_query': fake_q})
    return await admin_tutorial_view(fake_update, context)


async def admin_tutorial_edit_title_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not context.user_data.get(KEY_TUTORIAL_ID):
        return ADMIN_MAIN_MENU
    context.user_data[KEY_FLOW] = 'edit_title'
    await _safe_edit_text(q.message, "عنوان جدید را وارد کنید:")
    return ADMIN_MAIN_MENU


def _reindex_sort_orders(tutorial_id: int):
    rows = query_db("SELECT id FROM tutorial_media WHERE tutorial_id = ? ORDER BY sort_order, id", (tutorial_id,)) or []
    order = 0
    for r in rows:
        execute_db("UPDATE tutorial_media SET sort_order = ? WHERE id = ?", (order, r['id']))
        order += 1


async def admin_tutorial_media_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    mid = int(q.data.split('_')[-1])
    execute_db("DELETE FROM tutorial_media WHERE id = ?", (mid,))
    tid = int(context.user_data.get(KEY_TUTORIAL_ID) or 0)
    _reindex_sort_orders(tid)
    return await admin_tutorial_view(update, context)


async def admin_tutorial_media_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    parts = q.data.split('_')
    direction = parts[1]  # up/down
    mid = int(parts[-1])
    row = query_db("SELECT id, tutorial_id, sort_order FROM tutorial_media WHERE id = ?", (mid,), one=True)
    if not row:
        return ADMIN_MAIN_MENU
    tid = row['tutorial_id']
    if row.get('sort_order') is None:
        _reindex_sort_orders(tid)
        row = query_db("SELECT id, tutorial_id, sort_order FROM tutorial_media WHERE id = ?", (mid,), one=True)
    current = int(row['sort_order'] or 0)
    neighbor_order = current - 1 if direction == 'up' else current + 1
    neighbor = query_db("SELECT id, sort_order FROM tutorial_media WHERE tutorial_id = ? AND sort_order = ?", (tid, neighbor_order), one=True)
    if neighbor:
        execute_db("UPDATE tutorial_media SET sort_order = ? WHERE id = ?", (neighbor_order, mid))
        execute_db("UPDATE tutorial_media SET sort_order = ? WHERE id = ?", (current, neighbor['id']))
    return await admin_tutorial_view(update, context)