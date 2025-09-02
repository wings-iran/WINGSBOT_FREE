from telegram.ext import ContextTypes

FLOW_KEY = 'active_flow'


def set_flow(context: ContextTypes.DEFAULT_TYPE, flow_name: str) -> None:
    context.user_data[FLOW_KEY] = flow_name


def get_flow(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.user_data.get(FLOW_KEY)


def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(FLOW_KEY, None)