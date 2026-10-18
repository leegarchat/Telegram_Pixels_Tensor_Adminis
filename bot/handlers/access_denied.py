"""Fallback 'no access' replies for restricted commands.

Real handlers use aiogram filters (IsSuperAdmin / InModGroup) that fail
silently for unauthorized users. This router is included LAST, so it only
fires when no real handler matched, and explains why nothing happened.

Two cases:
  1. Superadmin commands (cfg_*, import_config, import_all) called by a
     non-admin in any chat -> 'no access, superadmins only'.
  2. Moderation-group commands called outside the closed mod group ->
     'available only in the moderation group'.
"""
# Router: fallback registration; Message: updates; Command: slash-command matching.
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

# config: admin list + mod_group_id for the membership checks below.
from bot.config_manager import config

# This router has no global filter on purpose: it must see updates from any chat.
router = Router()

# Shown when a non-superadmin touches an admin-only command.
DENIED_SUPERADMIN_TEXT = "⛔ You don't have access to this command. It is available to superadmins only."
# Shown when a mod-group command is used outside the closed group.
DENIED_MODGROUP_TEXT = "⛔ This command is available only in the private moderation group."


@router.message(Command(
    "cfg_show",
    "cfg_set_modgroup",
    "cfg_set_family",
    "cfg_add_admin",
    "cfg_del_admin",
    "import_config",
    "import_all",
))
async def deny_superadmin_command(message: Message):
    """Reply 'no access' when a non-admin reaches a superadmin-only command.

    Admins never see this: their real handler (registered in an earlier router)
    already processed the update, so we return silently for them.
    """
    if message.from_user and config.is_admin(message.from_user.id):
        return
    await message.reply(DENIED_SUPERADMIN_TEXT)


@router.message(Command(
    "link",
    "unlink",
    "get_links",
    "help",
    "new",
    "change",
    "remove",
    "approve",
    "cancel",
    "ban",
    "unban",
    "pban",
    "punban",
    "ban_list",
    "banlist",
    "bans",
    "cooldown",
    "set_cooldown",
    "cooldowns",
    "get_cooldowns",
    "new_sample",
    "new_sample_help",
    "sample_list",
    "sample",
    "del_sample",
    "export_sample",
    "import_sample",
    "export_sample_all",
    "import_sample_all",
    "export_config",
    "export_all",
))
async def deny_outside_modgroup(message: Message):
    """Reply when a mod-group command is used outside the closed group.

    Inside the group we stay silent: either the real handler ran, or it will
    reply with its own usage error (e.g. 'run inside a topic').
    """
    if message.chat and message.chat.id == config.data.get("mod_group_id"):
        return
    await message.reply(DENIED_MODGROUP_TEXT)
