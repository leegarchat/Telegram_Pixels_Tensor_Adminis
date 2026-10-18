"""Forum-topic <-> channel link management and group help (/link, /unlink, /get_links).

Each forum topic in the moderation group can be bound to exactly one channel.
The binding (plus an optional tag) is stored in config.json families and drives:
  * where user submissions from that family are queued,
  * where /new publishes and /change / /remove operate.
"""
# re: chat-ref normalization and URL checks; shlex: quoted tag parsing; escape: HTML safety.
import re
import shlex
from html import escape

# Router/Bot/F: routing and API; TelegramBadRequest: expected get_chat failures;
# Command/CommandObject: slash parsing; Message: updates.
from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

# config: binding CRUD; InModGroup: restrict everything here to the mod group.
from bot.config_manager import config
from bot.filters import InModGroup

router = Router()
router.message.filter(InModGroup())


def build_help_text() -> str:
    """Compose the short /help index with pointers to per-section subhelps."""
    sample_count = sum(
        len(family.get("samples", {}))
        for family in config.data.get("families", {}).values()
    )
    return (
        "📖 <b>Bot help</b> (moderators, this group only)\n"
        "Users start a submission in DM with <code>/start</code>.\n\n"
        "• <code>/help links</code> — topic → channel bindings\n"
        "• <code>/help mod</code> — approve / edit / delete posts\n"
        "• <code>/help bans</code> — /ban, /pban, /ban_list\n"
        "• <code>/help cooldown</code> — per-topic submission pauses\n"
        "• <code>/help samples</code> — post templates\n"
        "• <code>/help backup</code> — export / import\n"
        "• <code>/help config</code> — superadmin settings\n"
        f"• <code>/help all</code> — full reference at once\n\n"
        f"Topic templates configured: <b>{sample_count}</b>"
    )


def build_help_section(section: str) -> str | None:
    """Return one help section by key, or None for unknown keys."""
    sections = {
        "links": (
            "<b>🔗 Topic → channel links</b>\n"
            "• <code>/link &lt;url|@username&gt; [tag]</code> — bind this topic to a channel\n"
            "• <code>/unlink</code> — remove the binding of the current topic\n"
            "• <code>/get_links</code> — list all topic/channel bindings"
        ),
        "mod": (
            "<b>🛠 Moderation</b> (reply to a submission or topic message)\n"
            "• <code>/approve [text]</code> — publish (optionally edited)\n"
            "• <code>/cancel [reason]</code> — reject without ban\n"
            "• <code>/new [text]</code> — publish straight to the topic channel\n"
            "• <code>/change ...</code> / <code>/remove ...</code> — edit/delete by reply, "
            "topic URL, channel URL or message id\n"
            "• ✅ / ❌ buttons under each submission"
        ),
        "bans": (
            "<b>🚫 Bans</b> (reply to a submission, or pass a user id)\n"
            "• <code>/ban [20m|1h] [reason]</code> — reject + ban in this topic\n"
            "• <code>/unban [id]</code> — lift the topic ban\n"
            "• <code>/pban [20m|1h] [reason]</code> — reject + ban bot-wide\n"
            "• <code>/punban [id]</code> — lift the global ban\n"
            "• <code>/ban_list</code> — this topic + global bans; <code>/ban_list all</code> — everything\n"
            "No duration = permanent. Author id/tag is stored with each post."
        ),
        "cooldown": (
            "<b>⏳ Cooldowns</b> (per-topic submission pauses)\n"
            "• <code>/cooldown</code> — show this topic's pause (or global default in General)\n"
            "• <code>/cooldown 10m</code> — set pause for this topic (or global default in General)\n"
            "• <code>/cooldown off</code> — disable the pause\n"
            "• <code>/cooldowns</code> — list global default + all topic overrides"
        ),
        "samples": (
            "<b>📝 Post templates</b> (inside a linked topic)\n"
            "• <code>/new_sample Name [--footer on|off]</code> — create/update a template\n"
            "• <code>/new_sample_help</code> — full placeholder guide\n"
            "• <code>/sample_list</code> — list templates\n"
            "• <code>/sample &lt;name&gt;</code> — config + preview\n"
            "• <code>/del_sample &lt;name&gt;</code> — delete a template"
        ),
        "backup": (
            "<b>💾 Import/export</b> (run in a topic; import by replying to the file)\n"
            "• <code>/export_sample</code> · <code>/import_sample</code> — current topic\n"
            "• <code>/export_sample_all</code> · <code>/import_sample_all</code> — all topics\n"
            "• <code>/export_config</code> · <code>/import_config</code> (superadmin) — config, no token\n"
            "• <code>/export_all</code> · <code>/import_all</code> (superadmin) — ZIP with config + DB"
        ),
        "config": (
            "<b>⚙️ Configuration</b> (superadmins only)\n"
            "• <code>/cfg_show</code> · <code>/cfg_set_modgroup</code> · <code>/cfg_set_family</code>\n"
            "• <code>/cfg_add_admin</code> · <code>/cfg_del_admin</code>"
        ),
    }
    return sections.get(section)


def normalize_chat_ref(raw: str) -> str:
    """Normalize a user-supplied channel reference into a bot.get_chat() argument.

    Accepts t.me URLs (with or without scheme/www), `c/<internal>` private links,
    invite links (`+hash`, `joinchat/hash`), numeric ids and @usernames.
    """
    s = raw.strip().strip('"').strip("'")
    # Strip a leading t.me URL prefix so only the path/username part remains.
    s = re.sub(r"^(https?://)?(www\.)?t\.me/", "", s, flags=re.IGNORECASE)
    s = s.split("?")[0].rstrip("/")

    if s.startswith("c/"):
        # Private channel/topic link: c/<internal_id>/... -> -100<internal_id>.
        parts = s.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return f"-100{parts[1]}"
    if s.startswith("+"):
        return f"https://t.me/+{s[1:]}"
    if s.startswith("joinchat/"):
        return f"https://t.me/{s}"

    # Public reference: keep only the first path segment (username or numeric id).
    s = s.split("/")[0]
    if s.startswith("@"):
        s = s[1:]
    if re.fullmatch(r"-?\d{6,}", s):
        return s
    return f"@{s}"


def topic_url(mod_group_id: int, topic_id: int) -> str:
    """Build a clickable t.me/c link to a forum topic of the mod group."""
    internal = str(abs(mod_group_id))
    if internal.startswith("100"):
        internal = internal[3:]
    return f"https://t.me/c/{internal}/{topic_id}"


def parse_link_args(raw: str) -> tuple[str, str]:
    """Split `/link` arguments into (chat_ref, tag) using shell-like quoting.

    The tag may be quoted (`/link @ch "My Tag"`); an absent tag returns "".
    """
    parts = shlex.split(raw)
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]).strip()


def get_topic_title_from_message(message: Message) -> str | None:
    """Extract the forum topic title from topic-created/edited service messages.

    Checks the message itself first, then the replied-to message, because /link
    is often sent as a reply right after topic creation.
    """
    for candidate in (message, message.reply_to_message):
        if candidate is None:
            continue
        created = getattr(candidate, "forum_topic_created", None)
        edited = getattr(candidate, "forum_topic_edited", None)
        title = getattr(created, "name", None) or getattr(edited, "name", None)
        if title:
            return title.strip()
    return None


@router.message(F.forum_topic_created)
async def track_topic_created(message: Message):
    """Cache the title of a newly created forum topic for display names."""
    topic_id = message.message_thread_id or message.message_id
    title = get_topic_title_from_message(message)
    if title:
        config.set_topic_name(topic_id, title)


@router.message(F.forum_topic_edited)
async def track_topic_edited(message: Message):
    """Refresh the cached title when a forum topic is renamed."""
    topic_id = message.message_thread_id
    title = get_topic_title_from_message(message)
    if topic_id and title:
        config.set_topic_name(topic_id, title)


@router.message(Command("help"))
async def cmd_help(message: Message, command: CommandObject):
    """Send the help index or one section: /help [links|mod|bans|cooldown|samples|backup|config|all]."""
    arg = (command.args or "").strip().lower()
    if not arg:
        await message.answer(build_help_text(), parse_mode="HTML")
        return
    if arg == "all":
        # Full reference sent as several short messages to stay readable on mobile.
        await message.answer(build_help_text(), parse_mode="HTML")
        for section in ("links", "mod", "bans", "cooldown", "samples", "backup", "config"):
            text = build_help_section(section)
            if text:
                await message.answer(text, parse_mode="HTML")
        return
    text = build_help_section(arg)
    if text is None:
        await message.answer(
            "⚠️ Unknown section. Use: <code>/help links|mod|bans|cooldown|samples|backup|config|all</code>",
            parse_mode="HTML",
        )
        return
    await message.answer(text, parse_mode="HTML")


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject, bot: Bot):
    """Bind the current forum topic to a channel: `/link <url|@username> [tag]`.

    Verifies that the chat exists, that the bot is a member, and that it has
    administrator rights with the `can_post_messages` permission before saving.
    """
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply(
            "⚠️ Run <code>/link</code> inside a topic, not in General.",
            parse_mode="HTML"
        )
    if not command.args:
        return await message.reply(
            "Usage: <code>/link https://t.me/channel [tag]</code> or "
            "<code>/link @channel [tag]</code>",
            parse_mode="HTML"
        )

    try:
        raw_chat_ref, tag = parse_link_args(command.args)
    except ValueError:
        return await message.reply(
            "⚠️ Invalid quoting. Usage: <code>/link https://t.me/channel [tag]</code>",
            parse_mode="HTML",
        )
    if not raw_chat_ref:
        return await message.reply(
            "Usage: <code>/link https://t.me/channel [tag]</code>",
            parse_mode="HTML",
        )

    chat_ref = normalize_chat_ref(raw_chat_ref)
    try:
        chat = await bot.get_chat(chat_ref)
    except TelegramBadRequest as e:
        return await message.reply(
            f"⚠️ Could not fetch the chat <code>{chat_ref}</code>: {e.message}\n"
            "Check the link/username or add the bot to the channel.",
            parse_mode="HTML"
        )

    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest as e:
        return await message.reply(
            f"⚠️ The bot is not a member of the target chat ({e.message}). "
            "Add the bot and grant admin rights.",
            parse_mode="HTML"
        )

    # The bot must be able to post; creator/administrator without posting rights is rejected.
    if member.status not in ("administrator", "creator"):
        return await message.reply(
            "⚠️ The bot is not an administrator of the target chat. Add the bot as an admin."
        )

    if getattr(member, "can_post_messages", None) is False:
        return await message.reply(
            "⚠️ The bot is an admin but cannot post. Enable the \"Post messages\" permission."
        )

    title = chat.title or chat.full_name or chat.username or str(chat.id)
    channel_url = f"https://t.me/{chat.username}" if chat.username else f"id:{chat.id}"
    key = config.bind_topic(
        topic_id,
        chat.id,
        channel_url,
        title,
        tag=tag or None,
        topic_name=get_topic_title_from_message(message),
    )
    family = config.get_family(key)
    safe_title = escape(title)
    safe_tag = escape(family.get("name", key))

    await message.reply(
        f"✅ Link created/updated\n"
        f"Topic: <code>{topic_id}</code> "
        f"(<a href='{topic_url(config.data.get('mod_group_id', 0), topic_id)}'>open</a>)\n"
        f"Channel: <b>{safe_title}</b> (<code>{chat.id}</code>)\n"
        f"Tag: <b>{safe_tag}</b>\n"
        f"Family key: <code>{key}</code>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    """Remove the channel binding of the current topic (family entry is kept)."""
    topic_id = message.message_thread_id
    if not topic_id:
        return await message.reply(
            "⚠️ Run <code>/unlink</code> inside a topic.",
            parse_mode="HTML"
        )

    result = config.unbind_topic(topic_id)
    if result is None:
        return await message.reply("ℹ️ This topic has no channel binding.")

    key, fam, removed = result
    channel_info = (
        f"<b>{escape(str(removed['channel_title']))}</b> (<code>{removed['channel_id']}</code>)"
        if removed["channel_id"] else "—"
    )
    await message.reply(
        f"🗑 Link removed\n"
        f"Topic: <code>{topic_id}</code>\n"
        f"Previous channel: {channel_info}\n"
        f"Family entry <code>{key}</code> kept (without channel_id).",
        parse_mode="HTML"
    )


@router.message(Command("get_links"))
async def cmd_get_links(message: Message):
    """List every topic -> channel binding with clickable topic and channel links."""
    links = config.list_links()
    if not links:
        return await message.reply(
            "ℹ️ There are no topic → channel links yet. Create one with /link."
        )

    mod_group_id = config.data.get("mod_group_id", 0)
    lines = ["📡 <b>All topic → channel links</b>\n"]
    for i, link in enumerate(links, 1):
        name = escape(str(link.get("channel_title") or link.get("name") or link["key"]))
        t_url = topic_url(mod_group_id, link["topic_id"])
        channel = escape(str(link.get("channel_title") or "—"))
        channel_id = link.get("channel_id")
        channel_url = link.get("channel_url")
        channel_part = f"<b>{channel}</b>"
        if channel_url and channel_url.startswith("https://"):
            channel_part = f"<a href='{escape(str(channel_url))}'>{channel}</a>"
        lines.append(
            f"{i}. <b>{name}</b> (<code>{link['key']}</code>)\n"
            f"   Topic: <a href='{t_url}'>#{link['topic_id']}</a>\n"
            f"   Channel: {channel_part}"
            + (f" <code>{channel_id}</code>" if channel_id else " <i>(not bound)</i>")
        )
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
