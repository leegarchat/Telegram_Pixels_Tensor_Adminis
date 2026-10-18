"""Superadmin configuration commands (/cfg_*) for runtime bot settings.

All commands require the sender to be listed in `config.json -> admins`
(IsSuperAdmin). cfg_set_modgroup works in any chat; the rest additionally
require the moderation group context (InModGroup).
"""
# json: pretty-printing config for /cfg_show; html: escaping config dump and names.
import html
import json
# Router: handler registration; Message: updates; Command/CommandObject: slash parsing.
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

# config: live settings store; filters: group-membership and admin gates.
from bot.config_manager import config
from bot.filters import InModGroup, IsSuperAdmin

router = Router()


@router.message(InModGroup(), IsSuperAdmin(), Command("cfg_show"))
async def cmd_cfg_show(message: Message):
    """Dump the current config.json with the bot token masked, wrapped in HTML <pre>."""
    safe_copy = dict(config.data)
    safe_copy["bot_token"] = "***HIDDEN***"
    formatted = html.escape(json.dumps(safe_copy, indent=2, ensure_ascii=False))
    await message.answer(f"<b>Current config.json:</b>\n<pre>{formatted}</pre>", parse_mode="HTML")


@router.message(IsSuperAdmin(), Command("cfg_set_modgroup"))
async def cmd_set_modgroup(message: Message, command: CommandObject):
    """Point the bot at a new moderation supergroup: `/cfg_set_modgroup -100123...`.

    Works outside the mod group too (useful for the initial setup or recovery).
    """
    if not command.args or not command.args.lstrip("-").isdigit():
        return await message.answer("Usage: <code>/cfg_set_modgroup -100123456789</code>", parse_mode="HTML")
    config.data["mod_group_id"] = int(command.args)
    config.save()
    await message.answer(f"✅ <code>mod_group_id</code> updated to <code>{command.args}</code>", parse_mode="HTML")


@router.message(InModGroup(), IsSuperAdmin(), Command("cfg_set_family"))
async def cmd_set_family(message: Message, command: CommandObject):
    """Create/update a family binding (legacy path): `/cfg_set_family key topic_id channel_id Name`.

    Preserves existing templates for the key; prefer /link for topic-based setup.
    """
    # Format: /cfg_set_family p8 14 -1003333333 Pixel 8 Series
    if not command.args:
        return await message.answer(
            "Usage: <code>/cfg_set_family [key] [topic_id] [channel_id] [Name]</code>\n"
            "Example: <code>/cfg_set_family p8 14 -1003333333 Pixel 8 Series</code>",
            parse_mode="HTML"
        )
    parts = command.args.split(maxsplit=3)
    if len(parts) < 4:
        return await message.answer("⚠️ Not enough arguments.")

    if not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        return await message.answer("⚠️ <code>topic_id</code> and <code>channel_id</code> must be integers.", parse_mode="HTML")
    key, topic_id, channel_id, name = parts[0].lower(), int(parts[1]), int(parts[2]), parts[3]
    config.set_family_data(key, name, topic_id, channel_id)
    await message.answer(f"✅ Family <b>{html.escape(name)}</b> (<code>{html.escape(key)}</code>) saved to config.json.", parse_mode="HTML")


@router.message(InModGroup(), IsSuperAdmin(), Command("cfg_add_admin"))
async def cmd_add_admin(message: Message, command: CommandObject):
    """Grant superadmin rights: `/cfg_add_admin <telegram_user_id>` (idempotent)."""
    if not command.args or not command.args.isdigit():
        return await message.answer("Usage: <code>/cfg_add_admin [user_id]</code>", parse_mode="HTML")
    new_id = int(command.args)
    if new_id not in config.data.setdefault("admins", []):
        config.data["admins"].append(new_id)
        config.save()
    await message.answer(f"✅ User <code>{new_id}</code> added to admins.", parse_mode="HTML")


@router.message(InModGroup(), IsSuperAdmin(), Command("cfg_del_admin"))
async def cmd_del_admin(message: Message, command: CommandObject):
    """Revoke superadmin rights: `/cfg_del_admin <telegram_user_id>` (idempotent).

    Note: removing the last admin (including yourself) locks all /cfg_* commands.
    """
    if not command.args or not command.args.isdigit():
        return await message.answer("Usage: <code>/cfg_del_admin [user_id]</code>", parse_mode="HTML")
    rem_id = int(command.args)
    if rem_id in config.data.setdefault("admins", []):
        config.data["admins"].remove(rem_id)
        config.save()
    await message.answer(f"🗑 User <code>{rem_id}</code> removed from admins.", parse_mode="HTML")
