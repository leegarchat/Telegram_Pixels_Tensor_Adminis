from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config_manager import config

REQUEST_PUBLICATION_BUTTON = "📝 Request a publication"


def get_user_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=REQUEST_PUBLICATION_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_families_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, val in config.data.get("families", {}).items():
        if (
            val.get("topic_id") is not None
            and val.get("channel_id") is not None
            and config.list_samples(key)
        ):
            builder.button(text=val["name"], callback_data=f"fam:{key}")
    builder.adjust(2)
    return builder.as_markup()


def get_samples_kb(family_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, val in config.list_samples(family_key).items():
        builder.button(text=val.get("name", key), callback_data=f"sample:{key}")
    builder.adjust(2)
    return builder.as_markup()


def get_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Skip ➡️", callback_data="field:skip")]
    ])


def get_user_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Send to Moderation", callback_data="submit:send"),
            InlineKeyboardButton(text="🔄 Restart", callback_data="submit:restart")
        ]
    ])


def get_restart_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Start over", callback_data="submit:restart")]
    ])


def get_mod_decision_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data="mod:approve"),
            InlineKeyboardButton(text="❌ Reject", callback_data="mod:reject")
        ]
    ])
