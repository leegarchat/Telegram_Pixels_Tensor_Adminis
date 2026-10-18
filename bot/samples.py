import html
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

BLOCK_RE = re.compile(r"\{([^{}]+)\}")
BOLD_RE = re.compile(r"\*([^*\n]+)\*")
VALID_TYPES = {"text", "url", "urls", "photo"}
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
PHOTO_CAPTION_LIMIT = 1024
TEXT_MESSAGE_LIMIT = 4096


@dataclass
class Block:
    key: str
    prompt: str
    type: str
    optional: bool
    placeholder: str


def parse_block(raw: str) -> Optional[Block]:
    """Parse a single placeholder: {Key | Prompt | type} ('?' prefix = optional)."""
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        return None
    key, prompt, type_part = parts
    optional = type_part.startswith("?")
    ftype = type_part.lstrip("?").strip().lower()
    if not key or not prompt or ftype not in VALID_TYPES:
        return None
    return Block(key=key, prompt=prompt, type=ftype, optional=optional, placeholder=raw)


def parse_body(body: str) -> Tuple[List[Block], List[str]]:
    """Return (valid blocks, errors for invalid placeholders)."""
    blocks: List[Block] = []
    errors: List[str] = []
    seen = set()
    for m in BLOCK_RE.finditer(body):
        block = parse_block(m.group(1))
        if block is None:
            errors.append(m.group(0))
            continue
        if block.key in seen:
            errors.append(f"{m.group(0)} — duplicate key '{block.key}'")
            continue
        seen.add(block.key)
        blocks.append(block)
    return blocks, errors


def validate_answer(
    block: Block,
    text_html: Optional[str] = None,
    photo_id: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate a user answer. Return (ok, value, error)."""
    if block.type == "photo":
        if photo_id:
            return True, photo_id, None
        return False, None, "An image is expected. Send a photo or press \"Skip\"."

    raw = (text_html or "").strip()
    if not raw:
        return False, None, "The field cannot be empty."

    if block.type == "url":
        url = raw.splitlines()[0].strip()
        if not URL_RE.match(url):
            return False, None, "A single URL like <code>https://...</code> is expected."
        return True, url, None

    if block.type == "urls":
        parts = [p for p in re.split(r"[\s\n]+", raw) if p]
        bad = [p for p in parts if not URL_RE.match(p)]
        if bad:
            return False, None, (
                f"Not a URL: <code>{bad[0]}</code>. "
                "Send URLs separated by spaces or new lines."
            )
        return True, "\n".join(parts), None

    return True, text_html, None


def _is_label_line(line: str) -> bool:
    """A line like 'Known Issues:' (optionally wrapped in *bold*)."""
    stripped = line.strip().rstrip("*_ ").strip()
    return stripped.endswith(":") and len(stripped) <= 70


def telegram_text_length(text: str) -> int:
    """Count visible Telegram text in UTF-16 code units, excluding HTML markup."""
    visible_text = html.unescape(HTML_TAG_RE.sub("", text))
    return len(visible_text.encode("utf-16-le", errors="replace")) // 2


def add_submit_footer(body: str, bot_username: str, enabled: bool) -> str:
    if not enabled:
        return body
    username = html.escape(bot_username.lstrip("@"))
    footer = f"👉 <a href='https://t.me/{username}'>Submit a post</a>"
    return f"{body}\n\n{footer}" if body else footer


def render_sample(body: str, answers: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Assemble the final post text from answers. Return (text, photo_id)."""
    photo_id: Optional[str] = None
    meta: List[List[Any]] = []

    for line in body.split("\n"):
        converted = BOLD_RE.sub(r"<b>\1</b>", line)
        any_block = False
        any_answered = False

        def repl(m):
            nonlocal any_block, any_answered, photo_id
            block = parse_block(m.group(1))
            if block is None:
                return m.group(0)
            any_block = True
            val = answers.get(block.key)
            if not val:
                return ""
            any_answered = True
            if block.type == "photo":
                if photo_id is None:
                    photo_id = val
                return ""
            return str(val)

        new_line = BLOCK_RE.sub(repl, converted)
        keep = (not any_block) or any_answered
        meta.append([new_line, keep, keep and _is_label_line(line)])

    # Drop orphan labels ("Known Issues:") when everything under them was skipped
    n = len(meta)
    for i, item in enumerate(meta):
        if not item[2]:
            continue
        j = i + 1
        found_content = False
        while j < n:
            line_j, keep_j, is_label_j = meta[j]
            if is_label_j:
                break
            if keep_j and line_j.strip():
                found_content = True
                break
            j += 1
        if not found_content:
            item[1] = False

    text = "\n".join(line.rstrip() for line, keep, _ in meta if keep)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, photo_id


def fixed_template_text(body: str) -> str:
    """Return template literals with placeholders removed, preserving surrounding text."""
    text = BLOCK_RE.sub("", body)
    text = BOLD_RE.sub(r"<b>\1</b>", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text
