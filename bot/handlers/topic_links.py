import re
import shlex
from html import escape

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config_manager import config
from bot.filters import InModGroup

router = Router()
router.message.filter(InModGroup())


def build_help_text() -> str:
    sample_count = sum(
        len(family.get("samples", {}))
        for family in config.data.get("families", {}).values()
    )
    return (
        "📖 <b>Bot commands (available only in this group)</b>\n\n"
        "Users start a submission in the bot DM with <code>/start</code> or the "
        "<b>Request a publication</b> keyboard button.\n\n"
        "<b>Topic → channel links</b>\n"
        "• <code>/link &lt;url|@username&gt; [tag]</code> — bind this topic to a channel; tag becomes its label\n"
        "  If omitted, the topic name is used. Quote multi-word tags if needed.\n"
        "  (the bot must be an admin of that channel with posting rights)\n"
        "• <code>/unlink</code> — remove the binding of the current topic\n"
        "• <code>/get_links</code> — list all topic/channel bindings\n\n"
        "<b>Direct channel management</b>\n"
        "• <code>/new [text]</code> — publish to the topic's channel (photo goes as a caption)\n"
        "• Reply to a published topic message with <code>/change New text</code> or <code>/remove</code>\n"
        "• You can also use the topic message URL: <code>/change [topic_url] New text</code> or <code>/remove [topic_url]</code>\n"
        "• Direct channel IDs remain supported: <code>/change [channel_msg_id] [text]</code>, <code>/remove [channel_msg_id]</code>\n\n"
        "<b>Moderation of submissions</b>\n"
        "• <code>/approve [text]</code> — as a reply to a submission: publish (optionally edited)\n"
        "• <code>/cancel [reason]</code> — as a reply to a submission: reject\n"
        "• ✅ / ❌ buttons under the submission message\n\n"
        "<b>Configuration (superadmins only)</b>\n"
        "• <code>/cfg_show</code> · <code>/cfg_set_modgroup</code> · <code>/cfg_set_family</code>\n"
        "• <code>/cfg_add_admin</code> · <code>/cfg_del_admin</code>\n\n"
        "<b>Post templates (only in this group, inside a linked topic)</b>\n"
        "• <code>/new_sample Name [--footer on|off]</code> — create/update a topic template\n"
        "  The body can be fixed text or contain placeholders:\n"
        "  <code>{Key | Prompt | type}</code>, types: text, url, urls, photo;\n"
        "  <code>?</code> before the type marks an optional field (<code>?text</code>, <code>?photo</code>)\n"
        "  The submit-link footer is on by default; use <code>--footer off</code> to disable it.\n"
        "• <code>/new_sample_help</code> — full template formatting guide\n"
        "• <code>/sample_list</code> — list templates for this topic's channel\n"
        "• <code>/sample &lt;name&gt;</code> — show this topic's template config + preview\n"
        f"• Total topic-scoped templates: <b>{sample_count}</b>\n\n"
        "<b>Import/export (run in a topic; import by replying to the exported file)</b>\n"
        "• <code>/export_sample</code> · <code>/import_sample</code> — current topic templates\n"
        "• <code>/export_sample_all</code> · <code>/import_sample_all</code> — templates for all topics\n"
        "• <code>/export_config</code> · <code>/import_config</code> — full config without bot token\n"
        "• <code>/export_all</code> · <code>/import_all</code> — ZIP backup with config (no token) and SQLite DB"
    )


def normalize_chat_ref(raw: str) -> str:
    """Normalize a user-provided link/username into a bot.get_chat() argument."""
    s = raw.strip().strip('"').strip("'")
    s = re.sub(r"^(https?://)?(www\.)?t\.me/", "", s, flags=re.IGNORECASE)
    s = s.split("?")[0].rstrip("/")

    if s.startswith("c/"):
        parts = s.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return f"-100{parts[1]}"
    if s.startswith("+"):
        return f"https://t.me/+{s[1:]}"
    if s.startswith("joinchat/"):
        return f"https://t.me/{s}"

    s = s.split("/")[0]
    if s.startswith("@"):
        s = s[1:]
    if re.fullmatch(r"-?\d{6,}", s):
        return s
    return f"@{s}"


def topic_url(mod_group_id: int, topic_id: int) -> str:
    internal = str(abs(mod_group_id))
    if internal.startswith("100"):
        internal = internal[3:]
    return f"https://t.me/c/{internal}/{topic_id}"


def parse_link_args(raw: str) -> tuple[str, str]:
    parts = shlex.split(raw)
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]).strip()


def get_topic_title_from_message(message: Message) -> str | None:
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
    topic_id = message.message_thread_id or message.message_id
    title = get_topic_title_from_message(message)
    if title:
        config.set_topic_name(topic_id, title)


@router.message(F.forum_topic_edited)
async def track_topic_edited(message: Message):
    topic_id = message.message_thread_id
    title = get_topic_title_from_message(message)
    if topic_id and title:
        config.set_topic_name(topic_id, title)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(build_help_text(), parse_mode="HTML")


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject, bot: Bot):
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
        f"<b>{removed['channel_title']}</b> (<code>{removed['channel_id']}</code>)"
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
    links = config.list_links()
    if not links:
        return await message.reply(
            "ℹ️ There are no topic → channel links yet. Create one with /link."
        )

    mod_group_id = config.data.get("mod_group_id", 0)
    lines = ["📡 <b>All topic → channel links</b>\n"]
    for i, link in enumerate(links, 1):
        name = link.get("channel_title") or link.get("name") or link["key"]
        t_url = topic_url(mod_group_id, link["topic_id"])
        channel = link.get("channel_title") or "—"
        channel_id = link.get("channel_id")
        channel_url = link.get("channel_url")
        channel_part = f"<b>{channel}</b>"
        if channel_url and channel_url.startswith("https://"):
            channel_part = f"<a href='{channel_url}'>{channel}</a>"
        lines.append(
            f"{i}. <b>{name}</b> (<code>{link['key']}</code>)\n"
            f"   Topic: <a href='{t_url}'>#{link['topic_id']}</a>\n"
            f"   Channel: {channel_part}"
            + (f" <code>{channel_id}</code>" if channel_id else " <i>(not bound)</i>")
        )
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
