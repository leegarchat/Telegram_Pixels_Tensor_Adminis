"""Per-topic and global bans plus per-topic submission cooldowns.

Moderator commands (any member of the closed mod group — NOT only superadmins):
  /ban [duration] [reason]    — reply to a submission: reject it and ban the author
                               in the current topic. No duration = permanent topic ban.
  /unban [user_id]            — reply or user id: lift the current-topic ban.
  /pban [duration] [reason]    — reply to a submission: reject it and ban the author
                               bot-wide. No duration = permanent global ban.
  /punban [user_id]           — reply or user id: lift the global ban.
  /ban_list [all]             — list bans for the current topic (+global). `all`
                               lists every topic (handy in General).
  /cooldown [duration|off]    — show (no args) or set the submission cooldown for
                               the current topic. In General it shows/sets the
                               global default. Examples: `/cooldown 10m`, `/cooldown off`.
  /cooldowns                  — list cooldown overrides for all topics.

Duration grammar (case-insensitive): 30s, 20m, 1h, 1d, 1w, plain seconds (600),
combinations (1h30m), `0`/`off`/`permanent` for permanent-or-disabled contexts.

Ban targets resolve in order: pending submission (reply) -> published post link
with stored author -> user id parsed from the reply header (<code>id</code>) ->
explicit user-id argument. The author tag stored at submission time is reused
so /ban_list can display names without extra Telegram API calls.
"""
# html: escaping reasons/tags; re: duration tokens and header id fallback; time: expiry math.
import html
import re
import time

# Router/Command: routing and slash parsing; Message: updates.
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

# config: family names + cooldown storage; post_links: bans/pending/links store.
from bot.config_manager import config
from bot.database import GLOBAL_BAN_KEY, post_links
# InModGroup: every command here is available to all members of the closed group.
from bot.filters import InModGroup
# Reuse button cleanup from the moderation module (avoids duplicating edit logic).
from bot.handlers.mod_actions import remove_pending_buttons

router = Router()
router.message.filter(InModGroup())


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

# Single chunk like "20m" or "1.5h"; full grammar is one or more chunks glued together.
_DURATION_CHUNK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])", re.IGNORECASE)
# Suffix multipliers in seconds.
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration_token(token: str) -> int | None:
    """Parse one duration token into seconds.

    Returns:
        Seconds as int, 0 for permanent/disabled aliases, or None when the
        token is not a duration at all (caller then treats it as reason text).
    """
    text = token.strip().lower()
    if text in {"permanent", "permanently", "perm", "forever", "inf", "infinity"}:
        return 0
    if text in {"off", "no", "none", "disable", "disabled"}:
        return 0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        # Bare number = seconds (e.g. `/cooldown 600`, `/ban 3600`).
        return int(float(text))
    chunks = _DURATION_CHUNK_RE.findall(text)
    if not chunks:
        return None
    # Ensure the whole token is consumed by duration chunks (reject "1hxx").
    joined = "".join(num + unit for num, unit in chunks)
    if re.sub(r"\s+", "", text) != joined.lower():
        return None
    total = sum(float(num) * _DURATION_MULTIPLIERS[unit.lower()] for num, unit in chunks)
    return int(total)


def split_duration_and_reason(args: str | None) -> tuple[int | None, str | None]:
    """Split `/ban 1h spam links` into (3600, 'spam links').

    The first token is consumed as a duration only when it parses; otherwise the
    ban is permanent and the full args string is the reason.
    """
    args = (args or "").strip()
    if not args:
        return None, None
    first, _, rest = args.partition(" ")
    duration = parse_duration_token(first)
    if duration is None:
        return None, args
    if duration <= 0:
        # Explicit permanent marker: `/ban permanent spam` — drop the marker word.
        return None, (rest.strip() or None)
    return duration, (rest.strip() or None)


def parse_cooldown_value(raw: str) -> int | None:
    """Parse a /cooldown argument into seconds (0 = disabled); None when invalid."""
    raw = raw.strip()
    if not raw:
        return None
    duration = parse_duration_token(raw.split()[0])
    if duration is None:
        return None
    return max(0, duration)


def format_duration(seconds: int | None) -> str:
    """Humanize seconds: 90 -> '1m 30s', None/<=0 -> 'permanent'."""
    if seconds is None or seconds <= 0:
        return "permanent"
    parts = []
    for suffix, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        chunk, seconds = divmod(int(seconds), size)
        if chunk:
            parts.append(f"{chunk}{suffix}")
    return " ".join(parts) or "0s"


def format_ban_until(ban: dict) -> str:
    """Render the remaining ban time: 'permanent' or '2h 15m (until <date>)'."""
    expires_at = ban.get("expires_at")
    if not expires_at:
        return "permanent"
    remaining = int(expires_at - time.time())
    if remaining <= 0:
        return "expired"
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
    return f"{format_duration(remaining)} (until {stamp})"


def display_user_tag(username: str | None, user_id: int) -> str:
    """Render a stored author tag for HTML output, escaped safely."""
    if username and username.startswith("@"):
        return html.escape(username)
    if username:
        return f"{html.escape(username)} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


# ---------------------------------------------------------------------------
# Cooldown storage (per-topic overrides + global default)
# ---------------------------------------------------------------------------

def get_family_cooldown(family_key: str) -> int:
    """Effective cooldown for a family: topic override, else the global default."""
    family = config.get_family(family_key)
    if isinstance(family.get("cooldown_seconds"), (int, float)):
        return max(0, int(family["cooldown_seconds"]))
    return max(0, int(config.data.get("cooldown_seconds", 600)))


def set_family_cooldown(family_key: str, seconds: int) -> None:
    """Persist a per-topic cooldown override (seconds, 0 = disabled)."""
    if "families" not in config.data:
        config.data["families"] = {}
    family = config.data["families"].setdefault(family_key, {})
    family["cooldown_seconds"] = max(0, int(seconds))
    config.save()


def describe_cooldown(seconds: int) -> str:
    """Render a cooldown value: 'disabled' for 0, otherwise a human duration."""
    return "disabled" if seconds <= 0 else format_duration(seconds)


# ---------------------------------------------------------------------------
# Ban target resolution
# ---------------------------------------------------------------------------

def _user_id_from_header_text(text: str | None) -> int | None:
    """Fallback: extract the author id from a 'New Submission ... (<code>id</code>)' header."""
    if not text:
        return None
    match = re.search(r"\(<code>(\d+)</code>\)", text)
    return int(match.group(1)) if match else None


def resolve_ban_target(message: Message) -> tuple[int | None, str | None, dict | None, dict | None]:
    """Resolve (user_id, username, pending_post, post_link) for a /ban-like command.

    Priority: pending submission behind the reply -> published link with stored
    author -> author id parsed from the reply header -> explicit user-id argument.
    """
    pending = None
    link = None
    if message.reply_to_message:
        reply = message.reply_to_message
        pending = post_links.get_pending_submission(message.chat.id, reply.message_id)
        if pending:
            return pending["user_id"], pending.get("username"), pending, None
        link = post_links.find_by_source(message.chat.id, reply.message_id)
        if link and link.get("author_user_id"):
            return link["author_user_id"], link.get("author_username"), None, link
        header_id = _user_id_from_header_text(reply.text or reply.caption)
        if header_id:
            username = link.get("author_username") if link else None
            return header_id, username, None, link

    # No usable reply — accept a raw user id as the first argument token.
    raw_args = (message.text or message.caption or "").split()
    for token in raw_args[1:]:
        cleaned = token.strip().lstrip("@")
        if cleaned.isdigit():
            return int(cleaned), None, None, None
    return None, None, pending, link


def current_family_key(message: Message) -> tuple[str | None, dict | None]:
    """Resolve the family bound to the message topic, or (None, None) in General."""
    if message.message_thread_id is None:
        return None, None
    return config.find_family_by_topic(message.message_thread_id)


# ---------------------------------------------------------------------------
# /ban — per-topic ban (reply to a submission)
# ---------------------------------------------------------------------------

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    """Reject the replied submission and ban its author in the current topic.

    Usage (inside a linked topic, as a reply):
      /ban                  — permanent topic ban, submission rejected silently
      /ban 20m spam         — temporary topic ban with a reason sent to the user
    """
    family_key, family = current_family_key(message)
    if family_key is None:
        return await message.reply("⚠️ Run <code>/ban</code> inside a linked topic (or use <code>/pban</code> for a global ban).", parse_mode="HTML")

    user_id, username, pending, _ = resolve_ban_target(message)
    if user_id is None:
        return await message.reply("⚠️ Reply to a submission (or pass a user id): <code>/ban [20m|1h] [reason]</code>", parse_mode="HTML")

    duration, reason_text = split_duration_and_reason(command.args)
    reason = html.escape(reason_text.strip()) if reason_text else None

    # Drop the queued submission like /cancel does, so the queue stays clean.
    if pending:
        await remove_pending_buttons(message.bot, message.chat.id, pending["content_message_id"])
        post_links.delete_pending_submission(message.chat.id, pending["content_message_id"])
        if not username:
            username = pending.get("username")

    ban = post_links.add_ban(
        user_id=user_id,
        family_key=family_key,
        username=username,
        reason=reason_text,
        banned_by=message.from_user.id if message.from_user else None,
        duration_seconds=duration,
    )

    topic_name = html.escape(str(family.get("name", family_key)))
    user_label = display_user_tag(ban.get("username") or username, user_id)
    until = format_ban_until(ban)
    await message.reply(
        f"🚫 User {user_label} banned in <b>{topic_name}</b> ({until})"
        + (f".\n<b>Reason:</b> {reason}" if reason else "."),
        parse_mode="HTML",
    )
    # Notify the offender; failures (blocked bot / deleted account) must not break moderation.
    try:
        notice = f"🚫 You were banned in <b>{topic_name}</b> ({until})."
        if reason:
            notice += f"\n<b>Reason:</b> {reason}"
        await message.bot.send_message(user_id, notice, parse_mode="HTML")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /unban — lift a per-topic ban
# ---------------------------------------------------------------------------

@router.message(Command("unban"))
async def cmd_unban(message: Message):
    """Lift the current-topic ban: reply to any message of the user or pass their id.

    Usage: reply with <code>/unban</code>, or <code>/unban 123456789</code>.
    """
    family_key, family = current_family_key(message)
    if family_key is None:
        return await message.reply("⚠️ Run <code>/unban</code> inside a topic (or use <code>/punban</code> for a global unban).", parse_mode="HTML")

    user_id, username, _, _ = resolve_ban_target(message)
    if user_id is None:
        return await message.reply("⚠️ Reply to a message of the banned user or pass their id: <code>/unban 123456789</code>", parse_mode="HTML")

    removed = post_links.remove_ban(user_id, family_key)
    topic_name = html.escape(str(family.get("name", family_key)))
    user_label = display_user_tag(username, user_id)
    if removed:
        await message.reply(f"✅ User {user_label} unbanned in <b>{topic_name}</b>.", parse_mode="HTML")
    else:
        await message.reply(f"ℹ️ User {user_label} has no active ban in <b>{topic_name}</b>.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /pban / /punban — global (bot-wide) bans
# ---------------------------------------------------------------------------

@router.message(Command("pban"))
async def cmd_pban(message: Message, command: CommandObject):
    """Reject the replied submission and ban its author bot-wide (all topics).

    Usage (as a reply):
      /pban                 — permanent global ban
      /pban 1h spam         — temporary global ban with a reason
    """
    user_id, username, pending, _ = resolve_ban_target(message)
    if user_id is None:
        return await message.reply("⚠️ Reply to a submission (or pass a user id): <code>/pban [20m|1h] [reason]</code>", parse_mode="HTML")

    duration, reason_text = split_duration_and_reason(command.args)
    reason = html.escape(reason_text.strip()) if reason_text else None

    if pending:
        await remove_pending_buttons(message.bot, message.chat.id, pending["content_message_id"])
        post_links.delete_pending_submission(message.chat.id, pending["content_message_id"])
        if not username:
            username = pending.get("username")

    ban = post_links.add_ban(
        user_id=user_id,
        family_key=GLOBAL_BAN_KEY,
        username=username,
        reason=reason_text,
        banned_by=message.from_user.id if message.from_user else None,
        duration_seconds=duration,
    )
    user_label = display_user_tag(ban.get("username") or username, user_id)
    until = format_ban_until(ban)
    await message.reply(
        f"⛔ User {user_label} banned <b>globally</b> ({until})"
        + (f".\n<b>Reason:</b> {reason}" if reason else "."),
        parse_mode="HTML",
    )
    try:
        notice = f"⛔ You were banned globally ({until})."
        if reason:
            notice += f"\n<b>Reason:</b> {reason}"
        await message.bot.send_message(user_id, notice, parse_mode="HTML")
    except Exception:
        pass


@router.message(Command("punban"))
async def cmd_punban(message: Message):
    """Lift a global ban: reply to any message of the user or pass their id."""
    user_id, username, _, _ = resolve_ban_target(message)
    if user_id is None:
        return await message.reply("⚠️ Reply to a message of the banned user or pass their id: <code>/punban 123456789</code>", parse_mode="HTML")

    removed = post_links.remove_ban(user_id, GLOBAL_BAN_KEY)
    user_label = display_user_tag(username, user_id)
    if removed:
        await message.reply(f"✅ User {user_label} unbanned globally.", parse_mode="HTML")
    else:
        await message.reply(f"ℹ️ User {user_label} has no active global ban.", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /ban_list — show active bans
# ---------------------------------------------------------------------------

@router.message(Command("ban_list", "banlist", "bans"))
async def cmd_ban_list(message: Message, command: CommandObject):
    """List active bans: current topic (+global), or everything with `all`.

    Usage: <code>/ban_list</code> inside a topic, <code>/ban_list all</code> anywhere.
    """
    want_all = (command.args or "").strip().lower() in {"all", "global", "*"}
    family_key, family = current_family_key(message)

    if want_all or family_key is None:
        bans = post_links.list_bans()
        title = "🚫 <b>All active bans</b>"
    else:
        bans = post_links.list_bans(family_key)
        topic_name = html.escape(str(family.get("name", family_key)))
        title = f"🚫 <b>Active bans — {topic_name}</b>"

    if not bans:
        hint = "" if (want_all or family_key is None) else " and no global bans."
        return await message.reply(f"{title}\nℹ️ No active bans{hint}", parse_mode="HTML")

    # Group global bans first so topic moderators immediately see bot-wide blocks.
    bans.sort(key=lambda b: (0 if b["family_key"] == GLOBAL_BAN_KEY else 1, b["family_key"]))

    lines = [title, ""]
    for ban in bans:
        scope = "GLOBAL" if ban["family_key"] == GLOBAL_BAN_KEY else html.escape(str(config.get_family(ban["family_key"]).get("name", ban["family_key"])))
        user_label = display_user_tag(ban.get("username"), ban["user_id"])
        reason = f" — {html.escape(ban['reason'])}" if ban.get("reason") else ""
        lines.append(
            f"• {user_label} · <b>{scope}</b> · {format_ban_until(ban)}{reason}"
        )
    lines.append("")
    lines.append("Unban: <code>/unban [id]</code> in topic, <code>/punban [id]</code> globally.")
    await message.reply("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /cooldown — per-topic submission cooldowns
# ---------------------------------------------------------------------------

@router.message(Command("cooldown", "set_cooldown"))
async def cmd_cooldown(message: Message, command: CommandObject):
    """Show or set the submission cooldown.

    Inside a topic: shows/sets that topic's cooldown (moderators).
    In General: shows/sets the global default for topics without an override.
    Examples: <code>/cooldown</code>, <code>/cooldown 10m</code>, <code>/cooldown off</code>.
    """
    raw = (command.args or "").strip()
    family_key, family = current_family_key(message)

    if not raw:
        # Show mode: current topic value + global default for context.
        if family_key is None:
            default = max(0, int(config.data.get("cooldown_seconds", 600)))
            return await message.reply(
                f"⏳ Global default cooldown: <b>{describe_cooldown(default)}</b>\n"
                f"Set it with <code>/cooldown 10m</code> or disable with <code>/cooldown off</code>.",
                parse_mode="HTML",
            )
        current = get_family_cooldown(family_key)
        default = max(0, int(config.data.get("cooldown_seconds", 600)))
        topic_name = html.escape(str(family.get("name", family_key)))
        override_note = " (topic override)" if "cooldown_seconds" in family else " (global default)"
        return await message.reply(
            f"⏳ Cooldown for <b>{topic_name}</b>: <b>{describe_cooldown(current)}</b>{override_note}\n"
            f"Global default: <b>{describe_cooldown(default)}</b>.\n"
            f"Set with <code>/cooldown 10m</code>, disable with <code>/cooldown off</code>.",
            parse_mode="HTML",
        )

    seconds = parse_cooldown_value(raw)
    if seconds is None:
        return await message.reply(
            "⚠️ Usage: <code>/cooldown [30s|10m|1h|off]</code> — e.g. <code>/cooldown 10m</code>.",
            parse_mode="HTML",
        )

    if family_key is None:
        # In General: adjust the global default applied to topics without overrides.
        config.data["cooldown_seconds"] = seconds
        config.save()
        return await message.reply(
            f"✅ Global default cooldown set to <b>{describe_cooldown(seconds)}</b>.",
            parse_mode="HTML",
        )

    set_family_cooldown(family_key, seconds)
    topic_name = html.escape(str(config.get_family(family_key).get("name", family_key)))
    await message.reply(
        f"✅ Cooldown for <b>{topic_name}</b> set to <b>{describe_cooldown(seconds)}</b>.",
        parse_mode="HTML",
    )


@router.message(Command("cooldowns", "get_cooldowns"))
async def cmd_cooldowns(message: Message):
    """List the global default plus every per-topic cooldown override."""
    default = max(0, int(config.data.get("cooldown_seconds", 600)))
    lines = [f"⏳ <b>Submission cooldowns</b>\nGlobal default: <b>{describe_cooldown(default)}</b>\n"]
    for link in config.list_links():
        key = link["key"]
        current = get_family_cooldown(key)
        marker = " (override)" if "cooldown_seconds" in config.get_family(key) else ""
        name = html.escape(str(link.get("channel_title") or link.get("name") or key))
        lines.append(f"• <b>{name}</b> (<code>{key}</code>): <b>{describe_cooldown(current)}</b>{marker}")
    await message.reply("\n".join(lines), parse_mode="HTML")
