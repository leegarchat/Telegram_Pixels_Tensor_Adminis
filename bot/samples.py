"""Post-template engine: parsing, validation, rendering and length measurement.

Template syntax (see /new_sample_help):
  {Key | Prompt | type}            — required field,
  {Key | Prompt | ?type}           — optional field (user gets a Skip button),
  {Key | Question | bool | Yes | No} — boolean field rendered as Yes/No buttons.

Supported types: text, url, urls, photo, bool.
Surrounding text is published as-is; *bold* becomes <b>bold</b> (Telegram HTML).
"""
# html: escaping of user input for safe HTML parse_mode; re: placeholder parsing.
import html
import re
# dataclass: lightweight Block record; typing helpers for annotations.
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Matches one `{...}` placeholder (no nesting); inner text is parsed by parse_block().
BLOCK_RE = re.compile(r"\{([^{}]+)\}")
# Matches `*bold*` fragments (single-line) converted to <b>...</b> on render.
BOLD_RE = re.compile(r"\*([^*\n]+)\*")
# Allowed field types after stripping the optional `?` prefix.
VALID_TYPES = {"text", "url", "urls", "photo", "bool"}
# A single http(s) URL on one line; used to validate url/urls answers.
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
# Strips HTML tags when measuring visible length (tags do not count towards limits).
HTML_TAG_RE = re.compile(r"<[^>]+>")
# Telegram limits: photo captions are shorter than plain text messages.
PHOTO_CAPTION_LIMIT = 1024
TEXT_MESSAGE_LIMIT = 4096


@dataclass
class Block:
    """One parsed template placeholder."""

    key: str          # internal field name, unique within the template
    prompt: str       # question shown to the user for this field
    type: str         # one of VALID_TYPES (without the `?` prefix)
    optional: bool    # True when the type was prefixed with `?` (skippable)
    placeholder: str  # original raw text inside the braces (for error messages)
    yes_text: Optional[str] = None  # bool only: output text for the Yes button
    no_text: Optional[str] = None   # bool only: output text for the No button


def parse_block(raw: str) -> Optional[Block]:
    """Parse the inner text of one `{...}` placeholder into a Block.

    Expected shapes:
      `Key | Prompt | type` (3 parts) for text/url/urls/photo,
      `Key | Prompt | bool | Yes | No` (5 parts) for bool fields.

    Returns:
        Block on success, None when the shape or type is invalid.
    """
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) not in (3, 5):
        return None
    key, prompt, type_part = parts[:3]
    # A leading `?` marks the field optional; it is stripped before type checks.
    optional = type_part.startswith("?")
    ftype = type_part.lstrip("?").strip().lower()
    if not key or not prompt or ftype not in VALID_TYPES:
        return None
    if ftype == "bool":
        # Bool fields must carry both Yes and No output texts (either may be empty).
        if len(parts) != 5:
            return None
        yes_text, no_text = parts[3:]
    elif len(parts) != 3:
        # Non-bool fields must have exactly 3 parts.
        return None
    else:
        yes_text = no_text = None
    return Block(
        key=key,
        prompt=prompt,
        type=ftype,
        optional=optional,
        placeholder=raw,
        yes_text=yes_text,
        no_text=no_text,
    )


def parse_body(body: str) -> Tuple[List[Block], List[str]]:
    """Parse all placeholders in a template body.

    Returns:
        (blocks in order of appearance, error strings for invalid/duplicate placeholders).
    """
    blocks: List[Block] = []
    errors: List[str] = []
    seen = set()
    for m in BLOCK_RE.finditer(body):
        block = parse_block(m.group(1))
        if block is None:
            errors.append(m.group(0))
            continue
        # Duplicate keys would collide in the answers dict, so they are rejected.
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
    """Validate one user answer for a field.

    Args:
        block: Field definition being answered.
        text_html: Text answer (HTML-formatted for `text`, plain for urls).
        photo_id: Telegram file_id when the user sent a photo for a `photo` field.

    Returns:
        (ok, value, error): value is the sanitized string to store, error is
        a human-readable message shown to the user when ok is False.
    """
    if block.type == "photo":
        # Photo fields accept only an actual photo upload (or Skip when optional).
        if photo_id:
            return True, photo_id, None
        return False, None, "An image is expected. Send a photo or press \"Skip\"."
    if block.type == "bool":
        # Bool answers arrive via inline buttons, never via this text path.
        return False, None, "Please select Yes or No using the buttons."

    raw = (text_html or "").strip()
    if not raw:
        return False, None, "The field cannot be empty."

    if block.type == "url":
        # Exactly one URL on the first line; escape it for safe HTML output.
        url = raw.splitlines()[0].strip()
        if not URL_RE.match(url):
            return False, None, "A single URL like <code>https://...</code> is expected."
        return True, html.escape(url), None

    if block.type == "urls":
        # Split on any whitespace; every token must be a valid URL.
        parts = [p for p in re.split(r"[\s\n]+", raw) if p]
        bad = [p for p in parts if not URL_RE.match(p)]
        if bad:
            return False, None, (
                f"Not a URL: <code>{html.escape(bad[0])}</code>. "
                "Send URLs separated by spaces or new lines."
            )
        return True, "\n".join(html.escape(part) for part in parts), None

    # Plain text: keep the user's HTML formatting (bold/italic/links) as-is.
    return True, text_html, None


def _is_label_line(line: str) -> bool:
    """Detect orphan-label lines such as `Known Issues:` (optionally wrapped in *bold*).

    Such labels are dropped from the rendered post when every field below them
    was skipped, so empty sections do not appear in the published text.
    """
    stripped = line.strip().rstrip("*_ ").strip()
    return stripped.endswith(":") and len(stripped) <= 70


def telegram_text_length(text: str) -> int:
    """Count visible Telegram characters in UTF-16 code units, excluding HTML tags.

    Telegram enforces its 1024/4096 limits in UTF-16 units (emoji count as 2),
    so len() on the Python string would undercount them.
    """
    visible_text = html.unescape(HTML_TAG_RE.sub("", text))
    return len(visible_text.encode("utf-16-le", errors="replace")) // 2


def add_submit_footer(body: str, bot_username: str, enabled: bool) -> str:
    """Append the 'Submit a post' deep-link footer unless disabled for the template."""
    if not enabled:
        return body
    # Escape the username defensively; it is embedded in an HTML href attribute.
    username = html.escape(bot_username.lstrip("@"))
    footer = f"👉 <a href='https://t.me/{username}'>Submit a post</a>"
    return f"{body}\n\n{footer}" if body else footer


def render_sample(body: str, answers: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Assemble the final post text from a template body and collected answers.

    Args:
        body: Raw template text with placeholders.
        answers: Mapping of block key -> sanitized answer (None/"" = skipped).

    Returns:
        (rendered HTML text, photo file_id or None). The first answered `photo`
        field becomes the banner; other photo fields contribute nothing visible.
    """
    photo_id: Optional[str] = None
    # Per-line metadata: [rendered_line, keep_flag, is_label_flag].
    meta: List[List[Any]] = []

    for line in body.split("\n"):
        any_block = False      # True when the line contains at least one placeholder
        any_answered = False   # True when at least one placeholder got a value

        replacements: Dict[str, str] = {}

        def replace_block(raw: str) -> str:
            """Replace one placeholder with its answer (closure over line flags)."""
            nonlocal any_block, any_answered, photo_id
            any_block = True
            block = parse_block(raw)
            if block is None:
                # Should not happen (validated at creation), keep it literally.
                return "{" + raw + "}"
            val = answers.get(block.key)
            if not val:
                # Skipped optional field contributes an empty string.
                return ""
            if block.type == "photo":
                # First photo wins as the post banner; it renders no inline text.
                if photo_id is None:
                    photo_id = val
                return ""
            any_answered = True
            if block.type == "bool":
                # Bool output may itself contain *bold* markup — convert it now.
                return BOLD_RE.sub(r"<b>\1</b>", str(val))
            return str(val)

        # Replace placeholders via invisible tokens so that the subsequent
        # *bold* -> <b> conversion applies to fixed text but not to user values
        # (values are already escaped / pre-formatted by validate_answer).
        parts = []
        cursor = 0
        for block_index, match in enumerate(BLOCK_RE.finditer(line)):
            token = f"\x00SAMPLE_BLOCK_{block_index}\x00"
            parts.extend((line[cursor:match.start()], token))
            replacements[token] = replace_block(match.group(1))
            cursor = match.end()
        parts.append(line[cursor:])
        new_line = BOLD_RE.sub(r"<b>\1</b>", "".join(parts))
        for token, value in replacements.items():
            new_line = new_line.replace(token, value)
        # Lines without placeholders are always kept; lines with placeholders
        # survive only when at least one field was answered.
        keep = (not any_block) or any_answered
        meta.append([new_line, keep, keep and _is_label_line(line)])

    # Second pass: drop label lines ("Known Issues:") whose whole section is empty.
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

    # Join surviving lines, collapse 3+ newlines, and trim edge whitespace.
    text = "\n".join(line.rstrip() for line, keep, _ in meta if keep)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, photo_id


def fixed_template_text(body: str) -> str:
    """Render only the fixed (non-placeholder) part of a template for length checks.

    Used at template-creation time to reject bodies whose static text alone
    already exceeds Telegram limits. Placeholders are stripped, *bold* is
    converted, and blank runs are collapsed — mirroring render_sample().
    """
    text = BLOCK_RE.sub("", body)
    text = BOLD_RE.sub(r"<b>\1</b>", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text
