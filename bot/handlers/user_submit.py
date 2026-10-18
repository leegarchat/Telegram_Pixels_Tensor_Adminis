"""Private-chat submission wizard: guides users from /start to moderation review.

Flow:
  1. begin_submission — rate-limit check, then family picker (SubmitPost.family).
  2. step_family/step_sample — choose channel family and post template.
  3. ask_field + step_field_* — answer each template Block in order
     (text/url/urls via message, photo via image, bool via buttons, Skip for optional).
  4. show_preview — render_sample(), enforce Telegram length limits.
  5. confirm_send — post header + content to the mod-group topic, persist to SQLite.

All messages here are restricted to private chats (router filters below).
"""
# html/time: escaping display names and rate-limit bookkeeping.
import html
import time

# Router/F/Bot: aiogram routing primitives; Message/CallbackQuery: update types;
# CommandStart: /start filter; FSMContext: per-user wizard state.
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

# config: families/templates/cooldown/admins; post_links: SQLite persistence.
from bot.config_manager import config
from bot.database import post_links
# Template engine pieces: limits, Block model, footer, parsing, rendering, measuring, validation.
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
# Wizard states and inline/reply keyboards for each step.
from bot.states import SubmitPost
from bot.keyboards.inline import (
    get_families_kb, get_samples_kb, get_skip_kb, get_bool_kb,
    get_user_confirm_kb, get_mod_decision_kb, get_user_menu_kb,
    get_restart_kb,
    REQUEST_PUBLICATION_BUTTON,
)

# Accept only private-chat updates; group updates are handled by other routers.
router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
# In-memory per-(user, family) timestamp of the last successful submission (resets on restart).
# Key: (user_id, family_key). Global legacy keys are no longer used; each topic has its own
# cooldown via families.<key>.cooldown_seconds with fallback to config cooldown_seconds.
user_cooldowns = {}


def get_effective_cooldown(family_key: str) -> int:
    """Return the effective submission cooldown for a family (topic override or global default)."""
    family = config.get_family(family_key)
    if isinstance(family.get("cooldown_seconds"), (int, float)):
        return max(0, int(family["cooldown_seconds"]))
    return max(0, int(config.data.get("cooldown_seconds", 600)))


def author_display_tag(user) -> str:
    """Return the plain author tag stored in SQLite (@username or full name)."""
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "full_name", str(getattr(user, "id", "")))


def format_post(data: dict) -> str:
    """Compose the final moderated text: rendered body + optional submit footer."""
    # Look up the template to learn its footer setting; fall back to stored FSM override.
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


def format_user_tag(user) -> str:
    """Render a clickable moderator-visible author tag: @username or tg://user link."""
    if user.username:
        return f"@{user.username}"
    # Fall back to a mention link with an HTML-escaped full name.
    full_name = html.escape(user.full_name)
    return f"<a href='tg://user?id={user.id}'>{full_name}</a>"


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Handle /start in DMs by (re)starting the submission wizard."""
    await begin_submission(message, state)


@router.message(F.text == REQUEST_PUBLICATION_BUTTON)
async def cmd_request_publication(message: Message, state: FSMContext):
    """Handle the persistent 'Request a publication' reply-keyboard button."""
    await begin_submission(message, state)


async def begin_submission(message: Message, state: FSMContext):
    """Global-ban check, then show the family picker (or an explanatory notice)."""
    # A global (/pban) ban blocks every topic; per-topic bans/cooldowns are checked later
    # once the user picks a family, so one hot topic does not lock the others.
    active_ban = post_links.is_banned(message.from_user.id)
    if active_ban:
        return await message.answer(
            "🚫 You are banned from submitting posts. Please contact moderation.",
            reply_markup=get_user_menu_kb(),
        )

    # Only families that are fully linked AND have at least one template are offered.
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

    # Reset any stale wizard state, enter the family step, and show pickers.
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
    """Store the chosen family and advance to the template picker."""
    fam_key = call.data.split(":", 1)[1]
    family = config.get_family(fam_key)
    # The family may have been unlinked or deleted after the keyboard was shown.
    if not family.get("channel_id") or family.get("topic_id") is None:
        return await call.answer("⚠️ This family is not linked to a channel.", show_alert=True)

    # Per-topic (/ban) or global (/pban) ban blocks entering this family's flow.
    if not config.is_admin(call.from_user.id) and post_links.is_banned(call.from_user.id, fam_key):
        return await call.answer("🚫 You are banned in this topic.", show_alert=True)

    samples = config.list_samples(fam_key)
    if not samples:
        return await call.answer(
            "⚠️ No templates are configured for this channel.", show_alert=True
        )
    await state.update_data(family=fam_key)
    await state.set_state(SubmitPost.sample)
    await call.message.edit_text(
        f"Select post type for <b>{html.escape(str(family.get('name', fam_key)))}</b>:",
        parse_mode="HTML",
        reply_markup=get_samples_kb(fam_key)
    )


@router.callback_query(SubmitPost.sample, F.data.startswith("sample:"))
async def step_sample(call: CallbackQuery, state: FSMContext):
    """Parse the chosen template into Blocks and ask the first field question."""
    sample_key = call.data.split(":", 1)[1]
    data = await state.get_data()
    family_key = data.get("family")
    sample = config.get_sample(family_key, sample_key) if family_key else {}
    if not sample:
        return await call.answer("⚠️ Template not found.", show_alert=True)

    # Pre-parse placeholders once; the wizard iterates blocks[idx] in order.
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
    """Prompt the user for blocks[idx], or show the preview when all are answered."""
    data = await state.get_data()
    blocks = data.get("blocks", [])
    idx = data.get("idx", 0)

    # All fields collected — move to the preview/confirm stage.
    if idx >= len(blocks):
        return await show_preview(target, state)

    block = blocks[idx]
    # Bool fields get Yes/No (+Skip) buttons; other optional fields get a Skip button.
    if block["type"] == "bool":
        kb = get_bool_kb(block["optional"])
    else:
        kb = get_skip_kb() if block["optional"] else None
    # Numbered header helps users track progress through long templates.
    header = f"<b>{idx + 1}/{len(blocks)}</b>\n"
    await target.answer(header + html.escape(block["prompt"]), parse_mode="HTML", reply_markup=kb)


async def store_and_advance(message: Message, state: FSMContext, value):
    """Save one validated answer and advance the wizard to the next field."""
    data = await state.get_data()
    idx = data["idx"]
    block = data["blocks"][idx]
    answers = dict(data.get("answers", {}))
    answers[block["key"]] = value
    await state.update_data(answers=answers, idx=idx + 1)
    await ask_field(message, state)


@router.message(SubmitPost.field, F.photo)
async def step_field_photo(message: Message, state: FSMContext):
    """Handle photo uploads for `photo`-type fields (stores the largest file_id)."""
    data = await state.get_data()
    block = data["blocks"][data["idx"]]
    if block["type"] != "photo":
        # User sent a photo while a text/url field was expected — keep the wizard in place.
        return await message.answer(
            f"⚠️ The current field is <b>{html.escape(block['key'])}</b> (type: {block['type']}), not a photo.",
            parse_mode="HTML"
        )
    _, value, _ = validate_answer(Block(**block), photo_id=message.photo[-1].file_id)
    await store_and_advance(message, state, value)


@router.message(SubmitPost.field, F.text)
async def step_field_text(message: Message, state: FSMContext):
    """Handle text answers: validate url/urls strictly, keep HTML for text fields."""
    data = await state.get_data()
    block = data["blocks"][data["idx"]]
    if block["type"] == "photo":
        return await message.answer(
            "⚠️ A photo is expected. Send an image"
            + (" or press \"Skip\"." if block["optional"] else ".")
        )
    if block["type"] == "bool":
        return await message.answer("Please select Yes or No using the buttons above.")

    # url/urls answers use raw text (no HTML entities); text answers keep message.html_text
    # so user bold/italic/links survive into the rendered post.
    raw_input = (
        message.text
        if block["type"] in ("url", "urls")
        else message.html_text
    )
    ok, value, error = validate_answer(Block(**block), text_html=raw_input)
    if not ok:
        return await message.answer(f"⚠️ {error}\nPlease try again:", parse_mode="HTML")
    await store_and_advance(message, state, value)


@router.callback_query(SubmitPost.field, F.data.startswith("field:bool:"))
async def step_field_bool(call: CallbackQuery, state: FSMContext):
    """Handle Yes/No button presses for `bool` fields (stores the configured output text)."""
    data = await state.get_data()
    idx = data.get("idx", 0)
    blocks = data.get("blocks", [])
    if idx >= len(blocks):
        return await call.answer()

    block = blocks[idx]
    choice = call.data.rsplit(":", 1)[-1]
    if block["type"] != "bool" or choice not in {"yes", "no"}:
        return await call.answer("This answer does not match the current field.", show_alert=True)

    # Map the button to the template's Yes/No output text (either may be empty).
    answer_key = "yes_text" if choice == "yes" else "no_text"
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await store_and_advance(call.message, state, block.get(answer_key, ""))


@router.callback_query(SubmitPost.field, F.data == "field:skip")
async def step_field_skip(call: CallbackQuery, state: FSMContext):
    """Skip an optional field (stores None so the renderer drops its line)."""
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
    """Render the completed post, enforce length limits, and request confirmation."""
    data = await state.get_data()
    sample = config.get_sample(data["family"], data["sample"])
    body, photo_id = render_sample(sample["body"], data.get("answers", {}))

    # A template where every field was skipped renders empty — nothing to send.
    if not body and not photo_id:
        await state.clear()
        return await message.answer(
            "⚠️ The template rendered empty — nothing to send. Please start over: /start"
        )

    final_text = format_post({**data, "final_body": body})
    text_length = telegram_text_length(final_text)
    limit = PHOTO_CAPTION_LIMIT if photo_id else TEXT_MESSAGE_LIMIT
    if text_length > limit:
        # Telegram would reject the post; force a restart with a shorter text.
        excess = text_length - limit
        limit_kind = "caption" if photo_id else "message"
        await state.set_state(SubmitPost.too_long)
        return await message.answer(
            f"⚠️ The completed post is {text_length} characters, exceeding Telegram's "
            f"{limit}-character {limit_kind} limit by {excess}. Shorten your answers, "
            "then start over to submit it again.",
            reply_markup=get_restart_kb(),
        )

    # Persist the render for confirm_send, then show it with Send/Restart buttons.
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
    """Discard the current draft and return to the family picker."""
    await state.clear()
    await call.message.delete()
    await state.set_state(SubmitPost.family)
    await call.message.answer("Select the target family:", reply_markup=get_families_kb())


@router.callback_query(SubmitPost.confirm, F.data == "submit:send")
async def confirm_send(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Forward the approved-by-user draft to the mod-group topic and queue it."""
    data = await state.get_data()
    fam_cfg = config.get_family(data["family"])
    mod_group_id = config.data.get("mod_group_id")

    # The topic may have been unlinked while the user was filling the form.
    if not fam_cfg.get("channel_id") or fam_cfg.get("topic_id") is None:
        await state.clear()
        return await call.message.answer(
            "⚠️ This topic is no longer linked to a channel. Please start over: /start"
        )

    # Re-check bans and the per-topic cooldown at send time (state may be stale).
    if not config.is_admin(call.from_user.id):
        active_ban = post_links.is_banned(call.from_user.id, data["family"])
        if active_ban:
            await state.clear()
            return await call.message.answer("🚫 You are banned in this topic.")
        cd = get_effective_cooldown(data["family"])
        last_time = user_cooldowns.get((call.from_user.id, data["family"]), 0)
        now = time.time()
        if cd > 0 and (now - last_time) < cd:
            rem = int(cd - (now - last_time))
            return await call.answer(
                f"⏳ Cooldown active for this topic. Please wait {rem}s.",
                show_alert=True,
            )

    # Header identifies the author and target so moderators have full context.
    user_tag = format_user_tag(call.from_user)
    family_name = html.escape(str(fam_cfg.get("name", data["family"])))
    sample_name = html.escape(str(data.get("sample_name", "")))
    mod_header = (
        f"📥 <b>New Submission</b> from {user_tag} (<code>{call.from_user.id}</code>)\n"
        f"Target: <b>{family_name}</b> (<code>#{data['family']}</code>)"
        f" · <b>{sample_name}</b>\n"
        f"────────────────────\n"
    )
    header_message = await bot.send_message(
        chat_id=mod_group_id,
        message_thread_id=fam_cfg.get("topic_id"),
        text=mod_header,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # Content message carries the Approve/Reject buttons; photo posts become captions.
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

    # Persist under both message ids so /approve and /cancel work as replies to either.
    post_links.add_pending_submission(
        mod_chat_id=mod_group_id,
        header_message_id=header_message.message_id,
        content_message_id=sent.message_id,
        user_id=call.from_user.id,
        family_key=data["family"],
        text=data["final_text"],
        photo_id=data.get("photo_id"),
        username=author_display_tag(call.from_user),
    )
    # Start the per-topic cooldown window (in-memory; resets on bot restart).
    user_cooldowns[(call.from_user.id, data["family"])] = time.time()

    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("✅ Your post has been sent to moderators for review.")
