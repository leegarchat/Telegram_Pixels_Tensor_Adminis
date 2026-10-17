from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config_manager import config


def get_families_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, val in config.data.get("families", {}).items():
        builder.button(text=val["name"], callback_data=f"fam:{key}")
    builder.adjust(2)
    return builder.as_markup()


def get_post_types_kb() -> InlineKeyboardMarkup:
    types = ["ROM", "Kernel", "Recovery", "News / Other"]
    builder = InlineKeyboardBuilder()
    for t in types:
        builder.button(text=t, callback_data=f"type:{t}")
    builder.adjust(2)
    return builder.as_markup()


def get_skip_banner_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Skip Banner ➡️", callback_data="banner:skip")]
    ])


def get_user_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Send to Moderation", callback_data="submit:send"),
            InlineKeyboardButton(text="🔄 Restart", callback_data="submit:restart")
        ]
    ])


def get_mod_decision_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data="mod:approve"),
            InlineKeyboardButton(text="❌ Reject", callback_data="mod:reject")
        ]
    ])