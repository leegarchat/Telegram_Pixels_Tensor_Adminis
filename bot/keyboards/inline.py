"""Inline and reply keyboard factories for the submission and moderation flows."""
# aiogram keyboard types: InlineKeyboard* for buttons under a message,
# ReplyKeyboard* for the persistent bottom keyboard in private chats.
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
# Helper that auto-arranges inline buttons into rows.
from aiogram.utils.keyboard import InlineKeyboardBuilder
# Global config: families/samples lists are read live so keyboards always match config.json.
from bot.config_manager import config

# Text of the persistent reply button shown in DMs; handled in user_submit.cmd_request_publication.
REQUEST_PUBLICATION_BUTTON = "📝 Request a publication"


def get_user_menu_kb() -> ReplyKeyboardMarkup:
    """Return the persistent DM keyboard with a single 'Request a publication' button."""
    return ReplyKeyboardMarkup(
        # One row, one button; tapping it re-enters the submission wizard.
        keyboard=[[KeyboardButton(text=REQUEST_PUBLICATION_BUTTON)]],
        resize_keyboard=True,  # shrink the keyboard to fit the button
        is_persistent=True,    # keep it visible even when the system keyboard appears
    )


def get_families_kb() -> InlineKeyboardMarkup:
    """Build the family picker: one button per linked family that has templates.

    Only families with both `topic_id` and `channel_id` set AND at least one
    sample are shown; callback data is `fam:<family_key>`.
    """
    builder = InlineKeyboardBuilder()
    for key, val in config.data.get("families", {}).items():
        if (
            val.get("topic_id") is not None
            and val.get("channel_id") is not None
            and config.list_samples(key)
        ):
            # Button label is the human-readable family/tag name.
            builder.button(text=val["name"], callback_data=f"fam:{key}")
    builder.adjust(2)  # two buttons per row for compactness
    return builder.as_markup()


def get_samples_kb(family_key: str) -> InlineKeyboardMarkup:
    """Build the template picker for one family; callback data is `sample:<sample_key>`."""
    builder = InlineKeyboardBuilder()
    for key, val in config.list_samples(family_key).items():
        builder.button(text=val.get("name", key), callback_data=f"sample:{key}")
    builder.adjust(2)
    return builder.as_markup()


def get_skip_kb() -> InlineKeyboardMarkup:
    """Return a single 'Skip' button for optional (non-bool) fields; callback `field:skip`."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Skip ➡️", callback_data="field:skip")]
    ])


def get_bool_kb(optional: bool = False) -> InlineKeyboardMarkup:
    """Return Yes/No buttons for `bool` fields, plus Skip when the field is optional.

    Args:
        optional: When True, append a second row with the Skip button.
    """
    keyboard = [[
        InlineKeyboardButton(text="✅ Yes", callback_data="field:bool:yes"),
        InlineKeyboardButton(text="❌ No", callback_data="field:bool:no"),
    ]]
    if optional:
        # Optional bool: user may leave the field empty (renders as nothing).
        keyboard.append([
            InlineKeyboardButton(text="Skip ➡️", callback_data="field:skip")
        ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_user_confirm_kb() -> InlineKeyboardMarkup:
    """Return the preview-stage buttons: send to moderation or restart the wizard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Send to Moderation", callback_data="submit:send"),
            InlineKeyboardButton(text="🔄 Restart", callback_data="submit:restart")
        ]
    ])


def get_restart_kb() -> InlineKeyboardMarkup:
    """Return a single 'Start over' button used when the post exceeds length limits."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Start over", callback_data="submit:restart")]
    ])


def get_mod_decision_kb() -> InlineKeyboardMarkup:
    """Return the Approve/Reject buttons attached to every submission in the mod group."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data="mod:approve"),
            InlineKeyboardButton(text="❌ Reject", callback_data="mod:reject")
        ]
    ])
