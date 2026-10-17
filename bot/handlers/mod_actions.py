from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandObject

from bot.config_manager import config, pending_posts

router = Router()


async def publish_to_channel(bot: Bot, channel_id: int, text: str, photo_id: str = None) -> Message:
    if photo_id:
        return await bot.send_photo(chat_id=channel_id, photo=photo_id, caption=text, parse_mode="HTML")
    return await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


# 1. Быстрый Approve / Reject по инлайн-кнопкам
@router.callback_query(F.data.in_({"mod:approve", "mod:reject"}))
async def mod_inline_callback(call: CallbackQuery, bot: Bot):
    post = pending_posts.pop(call.message.message_id, None)
    if not post:
        return await call.answer("⚠️ Post not found in memory or already processed.", show_alert=True)

    fam_cfg = config.get_family(post["family"])
    if call.data == "mod:approve":
        await publish_to_channel(bot, fam_cfg["channel_id"], post["text"], post["photo_id"])
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"✅ Approved & published by {call.from_user.mention_html()}", parse_mode="HTML")
        await bot.send_message(post["user_id"], "🎉 Your submission has been approved and published!")
    else:
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"❌ Rejected by {call.from_user.mention_html()}", parse_mode="HTML")
        await bot.send_message(post["user_id"], "❌ Your submission was rejected by moderation without explanation.")


# 2. Команда /approve (чистая или с кастомным отредактированным текстом/баннером)
@router.message(Command("approve"), F.reply_to_message)
async def cmd_approve_reply(message: Message, command: CommandObject, bot: Bot):
    reply_id = message.reply_to_message.message_id
    post = pending_posts.pop(reply_id, None)
    if not post:
        return await message.reply("⚠️ Target submission not found or already processed.")

    fam_cfg = config.get_family(post["family"])
    new_text = command.args if command.args else post["text"]
    new_photo = message.photo[-1].file_id if message.photo else post["photo_id"]

    await publish_to_channel(bot, fam_cfg["channel_id"], new_text, new_photo)
    await message.reply_to_message.edit_reply_markup(reply_markup=None)
    await message.reply("✅ Published to channel (with edits)." if command.args else "✅ Published to channel.")
    await bot.send_message(post["user_id"], "🎉 Your submission has been approved and published!")


# 3. Команда /cancel [причина]
@router.message(Command("cancel"), F.reply_to_message)
async def cmd_cancel_reply(message: Message, command: CommandObject, bot: Bot):
    reply_id = message.reply_to_message.message_id
    post = pending_posts.pop(reply_id, None)
    if not post:
        return await message.reply("⚠️ Target submission not found or already processed.")

    reason = command.args.strip() if command.args else None
    user_notice = (
        f"❌ Your submission was rejected by moderation.\n<b>Reason:</b> {reason}"
        if reason else "❌ Rejected by moderation without explanation."
    )

    await message.reply_to_message.edit_reply_markup(reply_markup=None)
    await message.reply(f"🚫 Submission rejected. User notified.")
    await bot.send_message(post["user_id"], user_notice, parse_mode="HTML")


# 4. Прямые команды управления каналом (/new, /change, /remove)
def resolve_channel_by_topic(thread_id: int) -> int | None:
    for fam in config.data.get("families", {}).values():
        if fam.get("topic_id") == thread_id:
            return fam.get("channel_id")
    return None


@router.message(Command("new"))
async def cmd_direct_new(message: Message, command: CommandObject, bot: Bot):
    channel_id = resolve_channel_by_topic(message.message_thread_id)
    if not channel_id or not command.args:
        return await message.reply("Usage inside a device topic: <code>/new [post text]</code>", parse_mode="HTML")

    photo_id = message.photo[-1].file_id if message.photo else None
    sent = await publish_to_channel(bot, channel_id, command.args, photo_id)
    await message.reply(f"✅ Published directly. Channel Msg ID: <code>{sent.message_id}</code>", parse_mode="HTML")


@router.message(Command("change"))
async def cmd_direct_change(message: Message, command: CommandObject, bot: Bot):
    channel_id = resolve_channel_by_topic(message.message_thread_id)
    if not channel_id or not command.args:
        return await message.reply("Usage inside topic: <code>/change [msg_id] [new text]</code>", parse_mode="HTML")

    parts = command.args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        return await message.reply("⚠️ Provide valid message ID and new text.")

    msg_id, new_text = int(parts[0]), parts[1]
    await bot.edit_message_text(chat_id=channel_id, message_id=msg_id, text=new_text, parse_mode="HTML")
    await message.reply(f"✏️ Message <code>{msg_id}</code> updated.", parse_mode="HTML")


@router.message(Command("remove"))
async def cmd_direct_remove(message: Message, command: CommandObject, bot: Bot):
    channel_id = resolve_channel_by_topic(message.message_thread_id)
    if not channel_id or not command.args:
        return await message.reply("Usage: <code>/remove [msg_id or link]</code>", parse_mode="HTML")

    arg = command.args.strip().rstrip("/")
    msg_id_str = arg.split("/")[-1]
    if not msg_id_str.isdigit():
        return await message.reply("⚠️ Could not parse message ID.")

    await bot.delete_message(chat_id=channel_id, message_id=int(msg_id_str))
    await message.reply(f"🗑 Message <code>{msg_id_str}</code> deleted from channel.", parse_mode="HTML")