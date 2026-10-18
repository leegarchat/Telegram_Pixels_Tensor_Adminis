import time

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from bot.config_manager import config, pending_posts
from bot.samples import (
    PHOTO_CAPTION_LIMIT,
    TEXT_MESSAGE_LIMIT,
    Block,
    add_submit_footer,
    parse_body,
    render_sample,
    telegram_text_length,
    validate_answer,
)
from bot.states import SubmitPost
from bot.keyboards.inline import (
    get_families_kb, get_samples_kb, get_skip_kb,
    get_user_confirm_kb, get_mod_decision_kb, get_user_menu_kb,
    get_restart_kb,
    REQUEST_PUBLICATION_BUTTON,
)

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
user_cooldowns = {}


def format_post(data: dict) -> str:
    sample = config.get_sample(data["family"], data["sample"])
    append_submit_link = data.get(
        "append_submit_link",
        sample.get("append_submit_link", True),
    )
    bot_user = config.data.get("bot_username", "bot")
    return add_submit_footer(
        data.get("final_body", ""),
        bot_user,
        append_submit_link,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await begin_submission(message, state)


@router.message(F.text == REQUEST_PUBLICATION_BUTTON)
async def cmd_request_publication(message: Message, state: FSMContext):
    await begin_submission(message, state)


async def begin_submission(message: Message, state: FSMContext):
    now = time.time()
    cd = config.data.get("cooldown_seconds", 600)
    last_time = user_cooldowns.get(message.from_user.id, 0)

    if not config.is_admin(message.from_user.id) and (now - last_time) < cd:
        rem = int(cd - (now - last_time))
        return await message.answer(
            f"⏳ Rate limit active. Please wait {rem}s before submitting again.",
            reply_markup=get_user_menu_kb(),
        )

    families = {
        key: family for key, family in config.data.get("families", {}).items()
        if family.get("topic_id") is not None
        and family.get("channel_id") is not None
        and config.list_samples(key)
    }
    if not families:
        return await message.answer(
            "ℹ️ Submissions are temporarily unavailable: no linked channels have templates.",
            reply_markup=get_user_menu_kb(),
        )

    await state.clear()
    await state.set_state(SubmitPost.family)
    await message.answer(
        "Use the menu button below to start a publication request anytime.",
        reply_markup=get_user_menu_kb(),
    )
    await message.answer(
        "Select the target family for your publication:",
        reply_markup=get_families_kb(),
    )


@router.callback_query(SubmitPost.family, F.data.startswith("fam:"))
async def step_family(call: CallbackQuery, state: FSMContext):
    fam_key = call.data.split(":", 1)[1]
    family = config.get_family(fam_key)
    if not family.get("channel_id") or family.get("topic_id") is None:
        return await call.answer("⚠️ This family is not linked to a channel.", show_alert=True)

    samples = config.list_samples(fam_key)
    if not samples:
        return await call.answer(
            "⚠️ No templates are configured for this channel.", show_alert=True
        )
    await state.update_data(family=fam_key)
    await state.set_state(SubmitPost.sample)
    await call.message.edit_text(
        f"Select post type for <b>{family.get('name', fam_key)}</b>:",
        parse_mode="HTML",
        reply_markup=get_samples_kb(fam_key)
    )


@router.callback_query(SubmitPost.sample, F.data.startswith("sample:"))
async def step_sample(call: CallbackQuery, state: FSMContext):
    sample_key = call.data.split(":", 1)[1]
    data = await state.get_data()
    family_key = data.get("family")
    sample = config.get_sample(family_key, sample_key) if family_key else {}
    if not sample:
        return await call.answer("⚠️ Template not found.", show_alert=True)

    blocks, _ = parse_body(sample["body"])

    await state.update_data(
        sample=sample_key,
        sample_name=sample.get("name", sample_key),
        append_submit_link=sample.get("append_submit_link", True),
        blocks=[b.__dict__ for b in blocks],
        answers={},
        idx=0,
    )
    await state.set_state(SubmitPost.field)
    await call.answer()
    await ask_field(call.message, state)


async def ask_field(target: Message, state: FSMContext):
    data = await state.get_data()
    blocks = data.get("blocks", [])
    idx = data.get("idx", 0)

    if idx >= len(blocks):
        return await show_preview(target, state)

    block = blocks[idx]
    kb = get_skip_kb() if block["optional"] else None
    header = f"<b>{idx + 1}/{len(blocks)}</b>\n"
    await target.answer(header + block["prompt"], parse_mode="HTML", reply_markup=kb)


async def store_and_advance(message: Message, state: FSMContext, value):
    data = await state.get_data()
    idx = data["idx"]
    block = data["blocks"][idx]
    answers = dict(data.get("answers", {}))
    answers[block["key"]] = value
    await state.update_data(answers=answers, idx=idx + 1)
    await ask_field(message, state)


@router.message(SubmitPost.field, F.photo)
async def step_field_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    block = data["blocks"][data["idx"]]
    if block["type"] != "photo":
        return await message.answer(
            f"⚠️ The current field is <b>{block['key']}</b> (type: {block['type']}), not a photo.",
            parse_mode="HTML"
        )
    _, value, _ = validate_answer(Block(**block), photo_id=message.photo[-1].file_id)
    await store_and_advance(message, state, value)


@router.message(SubmitPost.field, F.text)
async def step_field_text(message: Message, state: FSMContext):
    data = await state.get_data()
    block = data["blocks"][data["idx"]]
    if block["type"] == "photo":
        return await message.answer(
            "⚠️ A photo is expected. Send an image"
            + (" or press \"Skip\"." if block["optional"] else ".")
        )

    ok, value, error = validate_answer(Block(**block), text_html=message.html_text)
    if not ok:
        return await message.answer(f"⚠️ {error}\nPlease try again:", parse_mode="HTML")
    await store_and_advance(message, state, value)


@router.callback_query(SubmitPost.field, F.data == "field:skip")
async def step_field_skip(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data.get("idx", 0)
    blocks = data.get("blocks", [])
    if idx >= len(blocks):
        return await call.answer()
    block = blocks[idx]
    if not block["optional"]:
        return await call.answer("This field is required.", show_alert=True)

    answers = dict(data.get("answers", {}))
    answers[block["key"]] = None
    await state.update_data(answers=answers, idx=idx + 1)
    await call.answer()
    await ask_field(call.message, state)


async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    sample = config.get_sample(data["family"], data["sample"])
    body, photo_id = render_sample(sample["body"], data.get("answers", {}))

    if not body and not photo_id:
        await state.clear()
        return await message.answer(
            "⚠️ The template rendered empty — nothing to send. Please start over: /start"
        )

    final_text = format_post({**data, "final_body": body})
    text_length = telegram_text_length(final_text)
    limit = PHOTO_CAPTION_LIMIT if photo_id else TEXT_MESSAGE_LIMIT
    if text_length > limit:
        excess = text_length - limit
        limit_kind = "caption" if photo_id else "message"
        await state.set_state(SubmitPost.too_long)
        return await message.answer(
            f"⚠️ The completed post is {text_length} characters, exceeding Telegram's "
            f"{limit}-character {limit_kind} limit by {excess}. Shorten your answers, "
            "then start over to submit it again.",
            reply_markup=get_restart_kb(),
        )

    await state.update_data(final_text=final_text, photo_id=photo_id)
    await state.set_state(SubmitPost.confirm)

    if photo_id:
        await message.answer_photo(
            photo=photo_id,
            caption=final_text,
            parse_mode="HTML",
            reply_markup=get_user_confirm_kb()
        )
    else:
        await message.answer(
            text=final_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_user_confirm_kb()
        )


@router.callback_query(SubmitPost.confirm, F.data == "submit:restart")
@router.callback_query(SubmitPost.too_long, F.data == "submit:restart")
async def confirm_restart(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await state.set_state(SubmitPost.family)
    await call.message.answer("Select the target family:", reply_markup=get_families_kb())


@router.callback_query(SubmitPost.confirm, F.data == "submit:send")
async def confirm_send(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    fam_cfg = config.get_family(data["family"])
    mod_group_id = config.data.get("mod_group_id")

    user_tag = (
        f"@{call.from_user.username}"
        if call.from_user.username
        else f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.full_name}</a>"
    )
    mod_header = (
        f"📥 <b>New Submission</b> from {user_tag} (<code>{call.from_user.id}</code>)\n"
        f"Target: <b>{fam_cfg.get('name')}</b> (<code>#{data['family']}</code>)"
        f" · <b>{data.get('sample_name', '')}</b>\n"
        f"────────────────────\n"
    )
    await bot.send_message(
        chat_id=mod_group_id,
        message_thread_id=fam_cfg.get("topic_id"),
        text=mod_header,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    if data.get("photo_id"):
        sent = await bot.send_photo(
            chat_id=mod_group_id,
            message_thread_id=fam_cfg.get("topic_id"),
            photo=data["photo_id"],
            caption=data["final_text"],
            parse_mode="HTML",
            reply_markup=get_mod_decision_kb()
        )
    else:
        sent = await bot.send_message(
            chat_id=mod_group_id,
            message_thread_id=fam_cfg.get("topic_id"),
            text=data["final_text"],
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
