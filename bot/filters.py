"""Custom aiogram filters that gate handlers by chat and by admin rights."""
# Filter is the base class for aiogram v3 filters; Message/CallbackQuery are update types.
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

# Global JSON config singleton; holds `mod_group_id` and the `admins` list.
from bot.config_manager import config


class InModGroup(Filter):
    """Accept only updates that originate from the private moderation group.

    Works for both Message updates (checks `event.chat`) and CallbackQuery
    updates (checks the chat of the attached message). Any other chat,
    including user DMs and channels, is rejected.
    """

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        """Return True when the update belongs to the configured mod group."""
        # Plain messages carry `.chat` directly; callbacks carry it on `.message.chat`.
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat
        if chat is None:
            # No chat context (e.g. inline queries) — never a mod-group event.
            return False
        # Compare against the single configured moderation supergroup id.
        mod_group_id = config.data.get("mod_group_id")
        return mod_group_id is not None and chat.id == mod_group_id


class IsSuperAdmin(Filter):
    """Accept only messages sent by a user listed in `config.json -> admins`.

    Note: this filter inspects Message only (it reads `from_user`). Applying it
    to CallbackQuery updates would always return False — callbacks in this
    project are gated by InModGroup instead (see PROBLEMS.md).
    """

    async def __call__(self, message: Message) -> bool:
        """Return True when the sender id is present in the admin list."""
        return bool(message.from_user and config.is_admin(message.from_user.id))
