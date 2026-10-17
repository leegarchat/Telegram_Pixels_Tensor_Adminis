import time
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from bot.config_manager import config, pending_posts
from bot.states import SubmitPost
from bot.keyboards.inline import (
    get_families_kb, get_post_types_kb, get_skip_banner_kb,
    get_user_confirm_kb, get_mod_decision_kb
)

router = Router()
user_cooldowns = {}


def format_post(data: dict) -> str:
    fam_name = config.get_family(data["family"]).get("name", data["family"].upper())
    bot_user = config.data.get("bot_username", "bot")
    return (
        f"📱 <b>{fam_name} | {data['post_type']}</b>\n"
        f"🏷 <b>Tags:</b> {data['tags']}\n\n"
        f"{data['content']}\n\n"
        f"👉 <a href='https://t.me/{bot_user}'>Submit a post</a>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    now = time.time()
    cd = config.data.get("cooldown_seconds", 600)
    last_time = user_cooldowns.get(message.from_user.id, 0)

    if not config.is_admin(message.from_user.id) and (now - last_time) < cd:
        rem = int(cd - (now - last_time))
        return await message.answer(f"⏳ Rate limit active. Please wait {rem}s before submitting again.")

    await state.clear()
    await state.set_state(SubmitPost.family)
    await message.answer("Select the target Pixel family for your publication:", reply_markup=get_families_kb())


@router.callback_query(SubmitPost.family, F.data.startswith("fam:"))
async def step_family(call: CallbackQuery, state: FSMContext):
    fam_key = call.data.split(":")[1]
    await state.update_data(family=fam_key)
    await state.set_state(SubmitPost.post_type)
    await call.message.edit_text("Select publication type:", reply_markup=get_post_types_kb())


@router.callback_query(SubmitPost.post_type, F.data.startswith("type:"))
async def step_type(call: CallbackQuery, state: FSMContext):
    p_type = call.data.split(":")[1]
    await state.update_data(post_type=p_type)
    await state.set_state(SubmitPost.tags)
    await call.message.edit_text("Send hashtags or tags (e.g., <code>#AOSP #Android16 #Shiba</code>):", parse_mode="HTML")


@router.message(SubmitPost.tags, F.text)
async def step_tags(message: Message, state: FSMContext):
    await state.update_data(tags=message.text)
    await state.set_state(SubmitPost.content)
    await message.answer(
        "Send the main body of your post (HTML/links supported: changelogs, download links, notes):"
    )


@router.message(SubmitPost.content, F.text)
async def step_content(message: Message, state: FSMContext):
    await state.update_data(content=message.html_text)
    await state.set_state(SubmitPost.banner)
    await message.answer("Send a banner image (as Photo) or skip this step:", reply_markup=get_skip_banner_kb())


@router.message(SubmitPost.banner, F.photo)
async def step_banner_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await show_preview(message, state)


@router.callback_query(SubmitPost.banner, F.data == "banner:skip")
async def step_banner_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(photo_id=None)
    await call.message.delete()
    await show_preview(call.message, state)


async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    preview_text = format_post(data)
    await state.update_data(final_text=preview_text)
    await state.set_state(SubmitPost.confirm)

    if data.get("photo_id"):
        await message.answer_photo(
            photo=data["photo_id"],
            caption=preview_text,
            parse_mode="HTML",
            reply_markup=get_user_confirm_kb()
        )
    else:
        await message.answer(
            text=preview_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_user_confirm_kb()
        )


@router.callback_query(SubmitPost.confirm, F.data == "submit:restart")
async def confirm_restart(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await state.set_state(SubmitPost.family)
    await call.message.answer("Select the target Pixel family:", reply_markup=get_families_kb())


@router.callback_query(SubmitPost.confirm, F.data == "submit:send")
async def confirm_send(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    fam_cfg = config.get_family(data["family"])
    mod_group_id = config.data.get("mod_group_id")

    user_tag = f"@{call.from_user.username}" if call.from_user.username else f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.full_name}</a>"
    mod_header = (
        f"📥 <b>New Submission</b> from {user_tag} (<code>{call.from_user.id}</code>)\n"
        f"Target: <b>{fam_cfg.get('name')}</b> (<code>#{data['family']}</code>)\n"
        f"────────────────────\n"
    )
    full_mod_text = mod_header + data["final_text"]

    if data.get("photo_id"):
        sent = await bot.send_photo(
            chat_id=mod_group_id,
            message_thread_id=fam_cfg.get("topic_id"),
            photo=data["photo_id"],
            caption=full_mod_text,
            parse_mode="HTML",
            reply_markup=get_mod_decision_kb()
        )
    else:
        sent = await bot.send_message(
            chat_id=mod_group_id,
            message_thread_id=fam_cfg.get("topic_id"),
            text=full_mod_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_mod_decision_kb()
        )

    pending_posts[sent.message_id] = {
        "user_id": call.from_user.id,
        "family": data["family"],
        "text": data["final_text"],
        "photo_id": data.get("photo_id")
    }
    user_cooldowns[call.from_user.id] = time.time()

    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("✅ Your post has been sent to moderators for review.")