import html
import re
from urllib.parse import urlparse

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject
from aiogram.utils.text_decorations import html_decoration

from bot.config_manager import config
from bot.database import post_links
from bot.filters import InModGroup

router = Router()
router.message.filter(InModGroup())
router.callback_query.filter(InModGroup())


async def publish_to_channel(bot: Bot, channel_id: int, text: str, photo_id: str = None) -> Message:
    if photo_id:
        return await bot.send_photo(chat_id=channel_id, photo=photo_id, caption=text, parse_mode="HTML")
    return await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


def save_topic_post_link(source: Message, published: Message, channel_id: int) -> None:
    save_topic_post_link_ids(
        source.chat.id,
        source.message_id,
        source.message_thread_id,
        published,
        channel_id,
    )


def save_topic_post_link_ids(
    source_chat_id: int,
    source_message_id: int,
    source_thread_id: int | None,
    published: Message,
    channel_id: int,
) -> None:
    post_links.add_link(
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        source_thread_id=source_thread_id,
        channel_id=channel_id,
        channel_message_id=published.message_id,
        media_type="photo" if published.photo else "text",
    )


def formatted_text_from_offset(message: Message, start: int) -> str:
    raw_text = message.text or message.caption or ""
    start_utf16 = len(raw_text[:start].encode("utf-16-le")) // 2
    total_utf16 = len(raw_text.encode("utf-16-le")) // 2
    entities = message.entities or message.caption_entities or []
    adjusted_entities = []
    for entity in entities:
        entity_start = entity.offset
        entity_end = entity.offset + entity.length
        if entity_end <= start_utf16 or entity_start >= total_utf16:
            continue
        clipped_start = max(entity_start, start_utf16)
        clipped_end = min(entity_end, total_utf16)
        adjusted_entities.append(entity.model_copy(update={
            "offset": clipped_start - start_utf16,
            "length": clipped_end - clipped_start,
        }))
    return html_decoration.unparse(raw_text[start:], adjusted_entities)


def command_args_start(message: Message) -> int:
    raw_text = message.text or message.caption or ""
    match = re.match(r"^\s*/[^\s]+\s*", raw_text)
    return match.end() if match else 0


def formatted_command_args(message: Message) -> str:
    return formatted_text_from_offset(message, command_args_start(message)).strip()


def formatted_args_after_first_token(message: Message, raw_args: str) -> str:
    match = re.match(r"^\S+\s+", raw_args)
    if not match:
        return ""
    return formatted_text_from_offset(
        message,
        command_args_start(message) + match.end(),
    ).strip()


def extract_message_id_from_url(value: str) -> int | None:
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return None
    path_parts = [part for part in urlparse(value).path.split("/") if part]
    for part in reversed(path_parts):
        if part.isdigit():
            return int(part)
    return None


def is_current_channel_post_url(message: Message, value: str, channel_id: int) -> bool:
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return False
    path = [part for part in urlparse(value).path.split("/") if part]
    if len(path) == 3 and path[0] == "c" and path[1].isdigit():
        internal_channel_id = str(abs(channel_id))
        if internal_channel_id.startswith("100"):
            internal_channel_id = internal_channel_id[3:]
        return path[1] == internal_channel_id

    _, family = config.find_family_by_topic(message.message_thread_id)
    if family is None or len(path) != 2:
        return False
    channel_url = family.get("channel_url", "")
    if channel_url.startswith("https://t.me/"):
        username = channel_url.removeprefix("https://t.me/").strip("/").lstrip("@")
        return path[0].lstrip("@") == username and path[1].isdigit()
    return False


def is_mod_group_message_url(value: str) -> bool:
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        return False
    path = [part for part in urlparse(value).path.split("/") if part]
    mod_group_id = config.data.get("mod_group_id")
    if not mod_group_id or len(path) not in (3, 4) or path[0] != "c":
        return False
    internal_group_id = str(abs(mod_group_id))
    if internal_group_id.startswith("100"):
        internal_group_id = internal_group_id[3:]
    return path[1] == internal_group_id and all(part.isdigit() for part in path[2:])


def resolve_post_link(message: Message, reference: Message | str):
    if isinstance(reference, Message):
        return post_links.find_by_source(message.chat.id, reference.message_id)

    if not is_mod_group_message_url(reference):
        return None
    source_message_id = extract_message_id_from_url(reference)
    if source_message_id is None:
        return None
    return post_links.find_by_source(message.chat.id, source_message_id)


async def edit_published_post(bot: Bot, link: dict, text: str) -> None:
    if link["media_type"] == "photo":
        await bot.edit_message_caption(
            chat_id=link["channel_id"],
            message_id=link["channel_message_id"],
            caption=text,
            parse_mode="HTML",
        )
    else:
        await bot.edit_message_text(
            chat_id=link["channel_id"],
            message_id=link["channel_message_id"],
            text=text,
            parse_mode="HTML",
        )


async def edit_unmapped_post(
    bot: Bot, channel_id: int, message_id: int, text: str
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=channel_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
        )
    except TelegramBadRequest as text_error:
        try:
            await bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=text,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            raise text_error


async def remove_pending_buttons(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


# 1. Quick Approve / Reject via inline buttons
@router.callback_query(F.data.in_({"mod:approve", "mod:reject"}))
async def mod_inline_callback(call: CallbackQuery, bot: Bot):
    post = post_links.get_pending_submission(call.message.chat.id, call.message.message_id)
    if not post:
        return await call.answer("⚠️ Submission not found or already processed.", show_alert=True)

    fam_cfg = config.get_family(post["family_key"])
    if call.data == "mod:approve":
        published = await publish_to_channel(
            bot, fam_cfg["channel_id"], post["text"], post["photo_id"]
        )
        save_topic_post_link(call.message, published, fam_cfg["channel_id"])
        post_links.delete_pending_submission(
            call.message.chat.id, post["content_message_id"]
        )
        await call.message.edit_reply_markup(reply_markup=None)
        receipt = await call.message.reply(
            f"✅ Approved & published by {call.from_user.mention_html()}",
            parse_mode="HTML",
        )
        save_topic_post_link(receipt, published, fam_cfg["channel_id"])
        await bot.send_message(post["user_id"], "🎉 Your submission has been approved and published!")
    else:
        post_links.delete_pending_submission(
            call.message.chat.id, post["content_message_id"]
        )
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"❌ Rejected by {call.from_user.mention_html()}", parse_mode="HTML")
        await bot.send_message(post["user_id"], "❌ Your submission was rejected by moderation without explanation.")


# 2. /approve command (plain or with a custom edited text/banner)
@router.message(Command("approve"), F.reply_to_message)
async def cmd_approve_reply(message: Message, command: CommandObject, bot: Bot):
    reply_id = message.reply_to_message.message_id
    post = post_links.get_pending_submission(message.chat.id, reply_id)
    if not post:
        return await message.reply("⚠️ Target submission not found or already processed.")

    fam_cfg = config.get_family(post["family_key"])
    new_text = formatted_command_args(message) if command.args else post["text"]
    new_photo = message.photo[-1].file_id if message.photo else post["photo_id"]

    published = await publish_to_channel(bot, fam_cfg["channel_id"], new_text, new_photo)
    save_topic_post_link(message.reply_to_message, published, fam_cfg["channel_id"])
    save_topic_post_link_ids(
        message.chat.id,
        post["content_message_id"],
        message.reply_to_message.message_thread_id,
        published,
        fam_cfg["channel_id"],
    )
    await remove_pending_buttons(bot, message.chat.id, post["content_message_id"])
    post_links.delete_pending_submission(message.chat.id, post["content_message_id"])
    receipt = await message.reply(
        "✅ Published to channel (with edits)." if command.args else "✅ Published to channel."
    )
    save_topic_post_link(receipt, published, fam_cfg["channel_id"])
    await bot.send_message(post["user_id"], "🎉 Your submission has been approved and published!")


# 3. /cancel command [reason]
@router.message(Command("cancel"), F.reply_to_message)
async def cmd_cancel_reply(message: Message, command: CommandObject, bot: Bot):
    reply_id = message.reply_to_message.message_id
    post = post_links.get_pending_submission(message.chat.id, reply_id)
    if not post:
        return await message.reply("⚠️ Target submission not found or already processed.")

    reason = html.escape(command.args.strip()) if command.args else None
    user_notice = (
        f"❌ Your submission was rejected by moderation.\n<b>Reason:</b> {reason}"
        if reason else "❌ Rejected by moderation without explanation."
    )

    await remove_pending_buttons(bot, message.chat.id, post["content_message_id"])
    post_links.delete_pending_submission(message.chat.id, post["content_message_id"])
    await message.reply(f"🚫 Submission rejected. User notified.")
    await bot.send_message(post["user_id"], user_notice, parse_mode="HTML")


# 4. Direct channel management commands (/new, /change, /remove)
def resolve_channel_by_topic(thread_id: int) -> int | None:
    return config.get_channel_for_topic(thread_id)


@router.message(Command("new"))
async def cmd_direct_new(message: Message, command: CommandObject, bot: Bot):
    channel_id = resolve_channel_by_topic(message.message_thread_id)
    if not channel_id or not command.args:
        return await message.reply("Usage inside a device topic: <code>/new [post text]</code>", parse_mode="HTML")

    photo_id = message.photo[-1].file_id if message.photo else None
    sent = await publish_to_channel(bot, channel_id, formatted_command_args(message), photo_id)
    save_topic_post_link(message, sent, channel_id)
    receipt = await message.reply(
        f"✅ Published directly. Channel Msg ID: <code>{sent.message_id}</code>",
        parse_mode="HTML",
    )
    save_topic_post_link(receipt, sent, channel_id)


@router.message(Command("change"))
async def cmd_direct_change(message: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").strip()
    formatted_args = formatted_command_args(message)
    if message.reply_to_message:
        link = resolve_post_link(message, message.reply_to_message)
        if link is None:
            return await message.reply(
                "⚠️ No published-channel post is linked to the replied topic message."
            )
        if not args:
            return await message.reply(
                "Usage: reply to a linked topic message with <code>/change New post text</code>",
                parse_mode="HTML",
            )
        await edit_published_post(bot, link, formatted_args)
        return await message.reply(
            f"✏️ Linked channel post <code>{link['channel_message_id']}</code> updated.",
            parse_mode="HTML",
        )

    if not args:
        return await message.reply(
            "Reply to a linked topic message with <code>/change New post text</code>, "
            "or use <code>/change [channel_msg_id] [new text]</code>.",
            parse_mode="HTML",
        )

    parts = args.split(maxsplit=1)
    replacement_text = formatted_args_after_first_token(message, args)
    source_link = resolve_post_link(message, parts[0])
    if source_link is not None:
        if len(parts) < 2:
            return await message.reply("⚠️ Provide the replacement text after the topic message URL.")
        await edit_published_post(bot, source_link, replacement_text)
        return await message.reply(
            f"✏️ Linked channel post <code>{source_link['channel_message_id']}</code> updated.",
            parse_mode="HTML",
        )

    channel_id = resolve_channel_by_topic(message.message_thread_id)
    linked_message_id = extract_message_id_from_url(parts[0])
    if (
        channel_id
        and linked_message_id is not None
        and is_current_channel_post_url(message, parts[0], channel_id)
    ):
        if len(parts) < 2:
            return await message.reply("⚠️ Provide the replacement text after the channel post URL.")
        mapped_link = post_links.find_by_channel_post(channel_id, linked_message_id)
        if mapped_link:
            await edit_published_post(bot, mapped_link, replacement_text)
        else:
            await edit_unmapped_post(
                bot, channel_id, linked_message_id, replacement_text
            )
        return await message.reply(
            f"✏️ Message <code>{linked_message_id}</code> updated.",
            parse_mode="HTML",
        )

    if not channel_id or len(parts) < 2 or not parts[0].isdigit():
        return await message.reply("⚠️ Provide a linked topic-message URL or a channel message ID and new text.")

    msg_id = int(parts[0])
    mapped_link = post_links.find_by_channel_post(channel_id, msg_id)
    if mapped_link:
        await edit_published_post(bot, mapped_link, replacement_text)
    else:
        await edit_unmapped_post(bot, channel_id, msg_id, replacement_text)
    await message.reply(f"✏️ Message <code>{msg_id}</code> updated.", parse_mode="HTML")


@router.message(Command("remove"))
async def cmd_direct_remove(message: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").strip()
    link = None
    if message.reply_to_message:
        link = resolve_post_link(message, message.reply_to_message)
        if link is None:
            return await message.reply(
                "⚠️ No published-channel post is linked to the replied topic message."
            )
    elif args:
        link = resolve_post_link(message, args)

    if link is not None:
        channel_id = link["channel_id"]
        msg_id = link["channel_message_id"]
    else:
        channel_id = resolve_channel_by_topic(message.message_thread_id)
        if not channel_id or not args:
            return await message.reply(
                "Reply to a linked topic message or use <code>/remove [channel_msg_id or link]</code>.",
                parse_mode="HTML",
            )
        url_message_id = extract_message_id_from_url(args)
        if url_message_id is not None:
            if not is_current_channel_post_url(message, args, channel_id):
                return await message.reply(
                    "⚠️ No saved post mapping exists for this topic URL, and the URL does not point to this topic's channel."
                )
            msg_id = url_message_id
        elif args.isdigit():
            msg_id = int(args)
        else:
            return await message.reply("⚠️ Could not parse a linked topic or channel message ID.")

    await bot.delete_message(chat_id=channel_id, message_id=msg_id)
    post_links.delete_target(channel_id, msg_id)
    await message.reply(
        f"🗑 Channel post <code>{msg_id}</code> deleted.",
        parse_mode="HTML",
    )
