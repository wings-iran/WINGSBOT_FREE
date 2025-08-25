from datetime import datetime
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import ContextTypes

from .config import logger
from .db import query_db, execute_db
from .panel import VpnPanelAPI
from .utils import bytes_to_gb


async def check_expirations(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily expiration check job...")
    today_str = datetime.now().strftime('%Y-%m-%d')
    reminder_msg_data = query_db("SELECT text FROM messages WHERE message_name = 'renewal_reminder_text'", one=True)
    if not reminder_msg_data:
        logger.error("Renewal reminder message template not found in DB. Skipping job.")
        return
    reminder_msg_template = reminder_msg_data['text']

    active_orders = query_db(
        "SELECT id, user_id, marzban_username, panel_id, last_reminder_date FROM orders "
        "WHERE status = 'approved' AND marzban_username IS NOT NULL AND panel_id IS NOT NULL"
    )

    orders_map = {}
    for order in active_orders:
        if order['marzban_username'] not in orders_map:
            orders_map[order['marzban_username']] = []
        orders_map[order['marzban_username']].append(order)

    all_panels = query_db("SELECT id FROM panels")
    for panel_data in all_panels:
        try:
            panel_api = VpnPanelAPI(panel_id=panel_data['id'])
            all_users, msg = await panel_api.get_all_users()
            if not all_users:
                logger.warning(f"Skipping panel ID {panel_data['id']} due to get_all_users error: {msg}")
                continue

            for m_user in all_users:
                username = m_user.get('username')
                if username not in orders_map:
                    continue

                user_orders = orders_map[username]
                for order in user_orders:
                    if order['last_reminder_date'] == today_str:
                        continue

                    details_str = ""
                    # Time-based check
                    if m_user.get('expire'):
                        expire_dt = datetime.fromtimestamp(m_user['expire'])
                        days_left = (expire_dt - datetime.now()).days
                        if 0 <= days_left <= 3:
                            details_str = f"تنها **{days_left+1} روز** تا پایان اعتبار زمانی سرویس شما باقی مانده است."

                    # Usage-based check
                    if not details_str and m_user.get('data_limit', 0) > 0:
                        usage_percent = (m_user.get('used_traffic', 0) / m_user['data_limit']) * 100
                        if usage_percent >= 80:
                            details_str = f"بیش از **{int(usage_percent)} درصد** از حجم سرویس شما مصرف شده است."

                    if details_str:
                        try:
                            final_msg = reminder_msg_template.format(marzban_username=username, details=details_str)
                            await context.bot.send_message(order['user_id'], final_msg, parse_mode=ParseMode.MARKDOWN)
                            execute_db("UPDATE orders SET last_reminder_date = ? WHERE id = ?", (today_str, order['id']))
                            logger.info(f"Sent reminder to user {order['user_id']} for service {username}")
                        except (Forbidden, BadRequest):
                            logger.warning(f"Could not send reminder to blocked user {order['user_id']}")
                        except Exception as e:
                            logger.error(f"Error sending reminder to {order['user_id']}: {e}")
                        import asyncio as _asyncio
                        await _asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Failed to process reminders for panel ID {panel_data['id']}: {e}")