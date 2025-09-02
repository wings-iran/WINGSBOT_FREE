from telegram.error import BadRequest, TelegramError
from ..db import query_db
from ..config import ADMIN_ID, logger


async def safe_edit_text(message, text, reply_markup=None, parse_mode=None):
    # Log outgoing request details for troubleshooting
    try:
        kb_summary = None
        try:
            if reply_markup and hasattr(reply_markup, 'inline_keyboard'):
                rows = reply_markup.inline_keyboard or []
                kb_summary = f"rows={len(rows)} cols={[len(r) for r in rows]}"
            elif reply_markup and hasattr(reply_markup, 'to_dict'):
                d = reply_markup.to_dict()
                rows = (d.get('inline_keyboard') or []) if isinstance(d, dict) else []
                kb_summary = f"rows={len(rows)}"
        except Exception:
            kb_summary = "unknown"
        logger.info(
            f"TG API -> editMessageText chat_id={getattr(message, 'chat_id', None)} message_id={getattr(message, 'message_id', None)} parse_mode={parse_mode} text_len={len(text or '')} {kb_summary or ''}"
        )
    except Exception:
        pass
    try:
        resp = await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        try:
            logger.info(
                f"TG API <- editMessageText OK chat_id={getattr(message, 'chat_id', None)} message_id={getattr(message, 'message_id', None)}"
            )
        except Exception:
            pass
        return resp
    except BadRequest as e:
        try:
            logger.error(
                f"TG API <- editMessageText 400 BadRequest: {str(e)} | text_preview={(text or '')[:200]!r}")
        except Exception:
            pass
        if 'Message is not modified' in str(e):
            return None
        raise
    except TelegramError:
        # Best-effort: ignore other transient editing errors
        try:
            logger.error("TG API <- editMessageText TelegramError (non-400)")
        except Exception:
            pass
        return None


async def safe_edit_caption(message, caption, reply_markup=None, parse_mode=None):
    try:
        try:
            kb_summary = None
            if reply_markup and hasattr(reply_markup, 'inline_keyboard'):
                rows = reply_markup.inline_keyboard or []
                kb_summary = f"rows={len(rows)} cols={[len(r) for r in rows]}"
            logger.info(
                f"TG API -> editMessageCaption chat_id={getattr(message, 'chat_id', None)} message_id={getattr(message, 'message_id', None)} parse_mode={parse_mode} caption_len={len(caption or '')} {kb_summary or ''}"
            )
        except Exception:
            pass
        resp = await message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
        try:
            logger.info(
                f"TG API <- editMessageCaption OK chat_id={getattr(message, 'chat_id', None)} message_id={getattr(message, 'message_id', None)}"
            )
        except Exception:
            pass
        return resp
    except BadRequest as e:
        try:
            logger.error(
                f"TG API <- editMessageCaption 400 BadRequest: {str(e)} | caption_preview={(caption or '')[:200]!r}")
        except Exception:
            pass
        if 'Message is not modified' in str(e):
            return None
        raise
    except TelegramError:
        try:
            logger.error("TG API <- editMessageCaption TelegramError (non-400)")
        except Exception:
            pass
        return None


def ltr_code(text: str) -> str:
    t = (text or '').replace(' ', '').replace('-', '')
    return f"<code>\u2066{t}\u2069</code>"


async def answer_safely(query, text: str | None = None, show_alert: bool = False):
    try:
        await query.answer(text or '', show_alert=show_alert)
    except Exception:
        pass


def get_all_admin_ids() -> list[int]:
    try:
        rows = query_db("SELECT user_id FROM admins") or []
    except Exception:
        rows = []
    admin_ids: list[int] = []
    try:
        admin_ids.append(int(ADMIN_ID))
    except Exception:
        pass
    for r in rows:
        try:
            uid = int(r.get('user_id'))
            if uid not in admin_ids:
                admin_ids.append(uid)
        except Exception:
            continue
    return admin_ids


async def notify_admins(bot, *, text: str | None = None, parse_mode=None, reply_markup=None, photo: str | None = None, document: str | None = None, caption: str | None = None):
    for admin_id in get_all_admin_ids():
        try:
            if photo:
                await bot.send_photo(chat_id=admin_id, photo=photo, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
            elif document:
                await bot.send_document(chat_id=admin_id, document=document, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
            elif text:
                await bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception:
            continue