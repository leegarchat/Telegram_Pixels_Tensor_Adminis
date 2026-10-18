from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from bot.config_manager import config


class InModGroup(Filter):
    """Matches only messages/callbacks from the private moderation group."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat
        if chat is None:
            return False
        mod_group_id = config.data.get("mod_group_id")
        return mod_group_id is not None and chat.id == mod_group_id
