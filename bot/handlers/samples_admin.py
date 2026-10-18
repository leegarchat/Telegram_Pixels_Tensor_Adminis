"""Topic-scoped post-template management (/new_sample family of commands).

Templates live under `families.<key>.samples` in config.json and are edited
exclusively from inside a linked moderation topic, so each channel/topic owns
its own template set. This module implements creation, listing, inspection,
preview and deletion.
"""
# html: escaping template names/bodies in replies; shlex: parsing quoted /new_sample headers.
import html
import shlex

# Router: handler registration; Command/CommandObject: slash-command parsing; Message: updates.
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

# config: template CRUD; InModGroup: group-only gate; samples: parsing/validation/rendering.
from bot.config_manager import config
from bot.filters import InModGroup
from bot.samples import (
    PHOTO_CAPTION_LIMIT,
    TEXT_MESSAGE_LIMIT,
    add_submit_footer,
    fixed_template_text,
    parse_body,
    render_sample,
    telegram_text_length,
)

# Every handler here requires the moderation group (topics provide the family context).
router = Router()
router.message.filter(InModGroup())

# Long help text sent by /new_sample_help and reused on /new_sample usage errors.
NEW_SAMPLE_HELP = (
    "📝 <b>How to create a post template (/new_sample)</b>\n\n"
    "<b>Command format:</b>\n"
    "<code>/new_sample Name [--footer on|off]</code>\n"
    "Run this command in this group, inside a linked moderation topic. The first line after the command "
    "is the template name — it becomes a button for that topic's channel. Everything after "
    "the first line is the template body, which can be fixed text, placeholders, or both.\n\n"
    "<b>Placeholder format:</b>\n"
    "<code>{Key | Prompt | type}</code>\n"
    "• <b>Key</b> — internal field name (unique within the template)\n"
    "• <b>Prompt</b> — the question shown to the user\n"
    "• <b>type</b> — the answer type:\n"
    "  <code>text</code> — any text (HTML allowed)\n"
    "  <code>url</code> — a single URL (<code>https://...</code>)\n"
    "  <code>urls</code> — multiple URLs separated by spaces or new lines\n"
    "  <code>photo</code> — banner image for the post\n"
    "  <code>bool</code> — Yes/No buttons with configured output text for each choice\n"
    "    <code>{Key | Question | bool | Text for Yes | Text for No}</code>; either output can be empty\n"
    "Prefix the type with <code>?</code> to make the field optional — the user gets "
    "a \"Skip\" button: <code>?text</code>, <code>?url</code>, <code>?urls</code>, "
    "<code>?photo</code>, <code>?bool</code>\n\n"
    "Templates may also contain fixed text with no placeholders; it is published as written.\n"
    "The submit-link footer is on by default. Use <code>--footer off</code> to disable it "
    "or <code>--footer on</code> to enable it. Updating without this option preserves its setting.\n\n"
    "<b>Notes:</b>\n"
    "• Text around placeholders is published as is; <code>*bold*</code> becomes &lt;b&gt;bold&lt;/b&gt;.\n"
    "• A line with placeholders is kept only when at least one field on it is answered "
    "(empty/skip removes the whole line, separators included).\n"
    "• A label line above the fields (ending with \":\", e.g. <code>Known Issues:</code>) "
    "is removed too when every field below it — down to the next label — is skipped or empty. "
    "Fixed text counts as content and keeps its label.\n"
    "• The first photo field becomes the post banner.\n"
    "• You can send the command as a reply to a message with the body if it is too long.\n"
    "• Re-running /new_sample with the same name updates the template in this topic only.\n\n"
    "<b>Example:</b>\n"
    "<code>/new_sample Kernel --footer off\n"
    "🐧 *{Kernel Name | Enter kernel name and build version | text}*\n"
    "📱 *Devices:* {Codename | List supported device codenames | text}\n\n"
    "📱 *Pixel 8: {Pixel 8 Support | Does this ROM support Pixel 8 (shiba)? | bool | Supports Pixel 8 (shiba) | }*\n\n"
    "🛠 *Changelog:*\n"
    "{Changelog | Describe the list of changes | text}\n\n"
    "⚠️ *Known Issues:*\n"
    "{Bugs | Describe known issues (or press Skip) | ?text}\n\n"
    "🔗 *Links:*\n"
    "{Download | Direct download link | url}\n"
    "{Extra Links | Extra links | ?urls}\n"
    "{Banner | Banner image | ?photo}</code>\n\n"
    "<b>Related commands:</b>\n"
    "• <code>/sample_list</code> — list all templates\n"
    "• <code>/sample &lt;name&gt;</code> — template config + preview\n"
    "• <code>/del_sample &lt;name&gt;</code> — delete a template in this topic"
)


def find_topic_family(message: Message):
    """Resolve the (family_key, family) bound to the message's forum topic.

    Returns (None, None) when sent outside a topic or when the topic has no
    channel binding — every template command requires a linked topic.
    """
    if message.message_thread_id is None:
        return None, None
    family_key, family = config.find_family_by_topic(message.message_thread_id)
    if family is None or not family.get("channel_id"):
        return None, None
    return family_key, family


def find_sample(family_key: str, query: str):
    """Find a template by normalized key or by case-insensitive display name.

    Returns (key, sample) or (None, None) when nothing matches.
    """
    samples = config.list_samples(family_key)
    key = config.normalize_sample_key(query)
    if key in samples:
        return key, samples[key]
    for k, s in samples.items():
        if s.get("name", "").lower() == query.strip().lower():
            return k, s
    return None, None


def parse_sample_command_header(line: str) -> tuple[str, bool | None]:
    """Parse the `/new_sample Name [--footer on|off]` first line via shlex.

    Returns:
        (template name, footer flag or None when the option is absent).

    Raises:
        ValueError: malformed --footer option or a non-/new_sample command line.
    """
    parts = shlex.split(line)
    if not parts or parts[0].split("@", 1)[0] != "/new_sample":
        return "", None

    name_parts = []
    footer_setting = None
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "--footer":
            # Long form with a separate value: `--footer on` / `--footer off`.
            if index + 1 >= len(parts) or parts[index + 1].lower() not in {"on", "off"}:
                raise ValueError("--footer must be followed by 'on' or 'off'.")
            footer_setting = parts[index + 1].lower() == "on"
            index += 2
            continue
        if part.startswith("--footer="):
            # Inline form: `--footer=on` / `--footer=off`.
            value = part.split("=", 1)[1].lower()
            if value not in {"on", "off"}:
                raise ValueError("--footer must be set to 'on' or 'off'.")
            footer_setting = value == "on"
            index += 1
            continue
        # Anything else is part of the (possibly multi-word, quoted) template name.
        name_parts.append(part)
        index += 1
    return " ".join(name_parts).strip(), footer_setting


@router.message(Command("new_sample_help"))
async def cmd_new_sample_help(message: Message):
    """Send the full template-authoring guide."""
    await message.reply(NEW_SAMPLE_HELP, parse_mode="HTML")


@router.message(Command("new_sample"))
async def cmd_new_sample(message: Message):
    """Create or update a template in the current topic.

    Name and options come from the command's first line; the body is everything
    after that line, or the replied-to message when the body is sent separately
    (useful when it exceeds convenient message length).
    """
    family_key, family = find_topic_family(message)
    if family is None or not family.get("channel_id"):
        return await message.reply(
            "⚠️ Run /new_sample inside a topic that is linked to a channel with /link."
        )

    raw = (message.text or message.caption or "").strip()
    lines = raw.splitlines()
    try:
        name, footer_setting = parse_sample_command_header(lines[0])
    except ValueError as error:
        return await message.reply(f"⚠️ {html.escape(str(error))}", parse_mode="HTML")
    body = "\n".join(lines[1:]).strip()

    # Allow `/new_sample Name` as a reply to a long body message.
    if not body and message.reply_to_message:
        body = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()

    if not name or not body:
        return await message.reply(
            f"{NEW_SAMPLE_HELP}\n\n⚠️ Name and template body are required.",
            parse_mode="HTML"
        )

    sample_key = config.normalize_sample_key(name)
    if not sample_key:
        return await message.reply(
            f"{NEW_SAMPLE_HELP}\n\n⚠️ A valid template name (with letters/digits) and body are required.",
            parse_mode="HTML"
        )
    if len(f"sample:{sample_key}".encode("utf-8")) > 64:
        return await message.reply(
            "⚠️ Template name is too long (exceeds Telegram's 64-byte button callback limit)."
        )

    # Reject invalid or duplicate placeholders before persisting anything.
    blocks, errors = parse_body(body)
    if errors:
        listed = "\n".join(f"<code>{html.escape(e)}</code>" for e in errors)
        return await message.reply(
            f"⚠️ Invalid placeholders:\n{listed}\n\n"
            "Format: <code>{Key | Prompt | type}</code>\n"
            "Types: text, url, urls, photo, bool; optional field — <code>?text</code>, <code>?photo</code>, etc.\n"
            "Boolean fields use <code>{Key | Question | bool | Yes text | No text}</code>.\n"
            "See /new_sample_help for the full guide.",
            parse_mode="HTML"
        )

    # Pre-check: fixed text plus footer must already fit within Telegram limits,
    # otherwise even empty user answers could not be published.
    fixed_text = fixed_template_text(body)
    existing_sample = config.get_sample(family_key, sample_key)
    footer_enabled = (
        footer_setting
        if footer_setting is not None
        else existing_sample.get("append_submit_link", True)
    )
    fixed_text = add_submit_footer(
        fixed_text,
        config.data.get("bot_username", "bot"),
        footer_enabled,
    )
    fixed_length = telegram_text_length(fixed_text)
    has_photo_field = any(block.type == "photo" for block in blocks)
    limit = PHOTO_CAPTION_LIMIT if has_photo_field else TEXT_MESSAGE_LIMIT
    if fixed_length > limit:
        excess = fixed_length - limit
        limit_kind = "caption" if has_photo_field else "message"
        return await message.reply(
            f"⚠️ The template's fixed output is {fixed_length} characters, "
            f"which exceeds Telegram's {limit}-character {limit_kind} limit by {excess}. "
            "Shorten the fixed template text or remove the photo field.",
        )

    key, created = config.save_sample(
        family_key,
        name,
        body,
        append_submit_link=footer_setting,
    )
    # Summarize the stored fields so the author can verify required/optional counts.
    fields = "\n".join(
        f"{i}. <b>{html.escape(b.key)}</b> — {b.type}{' (optional)' if b.optional else ''}"
        for i, b in enumerate(blocks, 1)
    )
    verb = "created ✅" if created else "updated ♻️"
    await message.reply(
        f"Template <b>{html.escape(name)}</b> (<code>{html.escape(key)}</code>) {verb} for "
        f"<b>{html.escape(str(family.get('name', family_key)))}</b>.\n"
        f"Fields: {len(blocks)}\n{fields or '(fixed-text template)'}\n"
        f"Submit-link footer: {'on' if footer_enabled else 'off'}\n\n"
        f"Preview it with <code>/sample {key}</code>",
        parse_mode="HTML"
    )


@router.message(Command("sample_list"))
async def cmd_sample_list(message: Message):
    """List all templates of the current topic with field counts and footer flags."""
    family_key, family = find_topic_family(message)
    if family is None:
        return await message.reply(
            "⚠️ Run /sample_list inside a linked moderation topic."
        )

    samples = config.list_samples(family_key)
    if not samples:
        return await message.reply(
            f"No templates are configured for <b>{html.escape(str(family.get('name', family_key)))}</b>. "
            "Create one in this topic with /new_sample (see /new_sample_help).",
            parse_mode="HTML"
        )

    lines = [
        f"📚 <b>Templates for {html.escape(str(family.get('name', family_key)))}</b> (<code>{family_key}</code>)\n"
    ]
    for key, sample in samples.items():
        blocks, _ = parse_body(sample.get("body", ""))
        required = sum(1 for b in blocks if not b.optional)
        optional = len(blocks) - required
        fields_summary = (
            f"{len(blocks)} field(s): {required} required, {optional} optional"
            if blocks else "fixed-text template"
        )
        lines.append(
            f"• <b>{html.escape(str(sample.get('name', key)))}</b> (<code>{html.escape(key)}</code>) — "
            f"{fields_summary}; footer {'on' if sample.get('append_submit_link', True) else 'off'}"
        )
    lines.append("\nUse <code>/sample &lt;name&gt;</code> to inspect a template.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("del_sample"))
async def cmd_del_sample(message: Message, command: CommandObject):
    """Delete one template (by key or display name) from the current topic."""
    family_key, family = find_topic_family(message)
    if family is None:
        return await message.reply("Run /del_sample inside a linked moderation topic.")
    if not command.args:
        return await message.reply("Usage: <code>/del_sample &lt;name&gt;</code>", parse_mode="HTML")

    # Strip surrounding quotes so `"My Template"` also resolves.
    query = command.args.strip().strip('"')
    key, sample = find_sample(family_key, query)
    if sample is None:
        return await message.reply("Template not found. Use /sample_list to list templates.")
    config.delete_sample(family_key, key)
    await message.reply(
        f"Deleted template <b>{html.escape(sample.get('name', key))}</b> "
        f"(<code>{key}</code>) from <b>{html.escape(family.get('name', family_key))}</b>.",
        parse_mode="HTML",
    )


@router.message(Command("sample"))
async def cmd_sample(message: Message, command: CommandObject):
    """Show a template's field schema, raw body, and a rendered preview with dummy answers."""
    family_key, family = find_topic_family(message)
    if family is None:
        return await message.reply(
            "⚠️ Run /sample inside a linked moderation topic."
        )

    if not command.args:
        keys = ", ".join(
            f"<code>{k}</code>" for k in config.list_samples(family_key)
        ) or "—"
        return await message.reply(
            f"Usage: <code>/sample &lt;name&gt;</code>\nAvailable templates: {keys}",
            parse_mode="HTML"
        )

    query = command.args.strip().strip('"')
    key, sample = find_sample(family_key, query)
    if sample is None:
        return await message.reply(
            f"⚠️ Template <code>{html.escape(query)}</code> not found. See /sample_list.",
            parse_mode="HTML"
        )

    body = sample.get("body", "")
    blocks, errors = parse_body(body)

    # Human-readable field list with prompts and bool Yes/No outputs.
    fields_lines = []
    for i, b in enumerate(blocks, 1):
        fields_lines.append(
            f"{i}. <b>{html.escape(b.key)}</b> — {b.type}"
            f"{' (optional)' if b.optional else ' (required)'}\n"
            f"   Prompt: {html.escape(b.prompt)}"
        )
        if b.type == "bool":
            yes_text = html.escape(b.yes_text) if b.yes_text else "(nothing)"
            no_text = html.escape(b.no_text) if b.no_text else "(nothing)"
            fields_lines.append(f"   Yes: {yes_text}\n   No: {no_text}")
    if errors:
        fields_lines.append("⚠️ Invalid placeholders: " + ", ".join(html.escape(e) for e in errors))
    fields_text = "\n".join(fields_lines) if fields_lines else "(no fields)"

    await message.reply(
        f"📄 <b>Template:</b> <b>{html.escape(sample.get('name', key))}</b> (<code>{key}</code>)\n"
        f"<b>Submit-link footer:</b> {'on' if sample.get('append_submit_link', True) else 'off'}\n"
        f"<b>Fields ({len(blocks)}):</b>\n{fields_text}\n\n"
        f"<b>Body (raw):</b>\n<pre>{html.escape(body)}</pre>\n"
        f"<i>Sending preview below with placeholder answers…</i>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    # Render a preview with synthetic answers so authors see the final layout.
    answers = {}
    for b in blocks:
        if b.type == "url":
            answers[b.key] = "https://example.com/file.zip"
        elif b.type == "urls":
            answers[b.key] = "https://example.com/1\nhttps://example.com/2"
        elif b.type == "photo":
            answers[b.key] = "[banner image]"
        elif b.type == "bool":
            answers[b.key] = b.yes_text or b.no_text or ""
        else:
            answers[b.key] = f"[{b.key}]"

    preview_text, photo = render_sample(body, answers)
    preview_text = add_submit_footer(
        preview_text,
        config.data.get("bot_username", "bot"),
        sample.get("append_submit_link", True),
    )
    banner_line = "📎 Banner: yes" if photo else "📎 Banner: no"
    if not preview_text and not photo:
        preview_text = "(the template renders empty)"
    await message.answer(
        f"👁 <b>Preview</b> ({html.escape(sample.get('name', key))}) — {banner_line}\n"
        f"────────────────────\n"
        f"{preview_text}",
        parse_mode="HTML",
        disable_web_page_preview=True
    )
