import json
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandObject, Filter

from bot.config_manager import config
from bot.filters import InModGroup

router = Router()
router.message.filter(InModGroup())


class IsSuperAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return config.is_admin(message.from_user.id)


@router.message(IsSuperAdmin(), Command("cfg_show"))
async def cmd_cfg_show(message: Message):
    safe_copy = dict(config.data)
    safe_copy["bot_token"] = "***HIDDEN***"
    formatted = json.dumps(safe_copy, indent=2, ensure_ascii=False)
    await message.answer(f"<b>Current config.json:</b>\n<pre>{formatted}</pre>", parse_mode="HTML")


@router.message(IsSuperAdmin(), Command("cfg_set_modgroup"))
async def cmd_set_modgroup(message: Message, command: CommandObject):
    if not command.args or not command.args.lstrip("-").isdigit():
        return await message.answer("Usage: <code>/cfg_set_modgroup -100123456789</code>", parse_mode="HTML")
    config.data["mod_group_id"] = int(command.args)
    config.save()
    await message.answer(f"✅ <code>mod_group_id</code> updated to <code>{command.args}</code>", parse_mode="HTML")


@router.message(IsSuperAdmin(), Command("cfg_set_family"))
async def cmd_set_family(message: Message, command: CommandObject):
    """Format: /cfg_set_family p8 topic_id channel_id Family Name"""
    if not command.args:
        return await message.answer(
            "Usage: <code>/cfg_set_family [key] [topic_id] [channel_id] [Name]</code>\n"
            "Example: <code>/cfg_set_family p8 14 -1003333333 Pixel 8 Series</code>",
            parse_mode="HTML"
        )
    parts = command.args.split(maxsplit=3)
    if len(parts) < 4:
        return await message.answer("⚠️ Not enough arguments.")

    key, topic_id, channel_id, name = parts[0].lower(), int(parts[1]), int(parts[2]), parts[3]
    config.set_family_data(key, name, topic_id, channel_id)
    await message.answer(f"✅ Family <b>{name}</b> (<code>{key}</code>) saved to config.json.", parse_mode="HTML")


@router.message(IsSuperAdmin(), Command("cfg_add_admin"))
async def cmd_add_admin(message: Message, command: CommandObject):
    if not command.args or not command.args.isdigit():
        return await message.answer("Usage: <code>/cfg_add_admin [user_id]</code>", parse_mode="HTML")
    new_id = int(command.args)
    if new_id not in config.data["admins"]:
        config.data["admins"].append(new_id)
        config.save()
    await message.answer(f"✅ User <code>{new_id}</code> added to admins.", parse_mode="HTML")


@router.message(IsSuperAdmin(), Command("cfg_del_admin"))
async def cmd_del_admin(message: Message, command: CommandObject):
    if not command.args or not command.args.isdigit():
        return await message.answer("Usage: <code>/cfg_del_admin [user_id]</code>", parse_mode="HTML")
    rem_id = int(command.args)
    if rem_id in config.data["admins"]:
        config.data["admins"].remove(rem_id)
        config.save()
    await message.answer(f"🗑 User <code>{rem_id}</code> removed from admins.", parse_mode="HTML")