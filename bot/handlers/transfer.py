import html
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from bot.config_manager import config
from bot.database import DATABASE_PATH, post_links
from bot.filters import InModGroup, IsSuperAdmin
from bot.samples import parse_body

router = Router()
router.message.filter(InModGroup())

MAX_IMPORT_SIZE = 20 * 1024 * 1024
MAX_ARCHIVE_CONTENT_SIZE = 50 * 1024 * 1024


def strip_bot_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_bot_tokens(item)
            for key, item in value.items()
            if key.lower() != "bot_token"
        }
    if isinstance(value, list):
        return [strip_bot_tokens(item) for item in value]
    return value


def exported_config_bytes() -> bytes:
    payload = strip_bot_tokens(config.data)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def validate_config_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("The config document must contain a JSON object.")
    if not isinstance(payload.get("families"), dict):
        raise ValueError("The config must contain a 'families' object.")
    if "admins" in payload and not isinstance(payload["admins"], list):
        raise ValueError("The config 'admins' value must be a list.")

    safe_payload = strip_bot_tokens(payload)
    safe_payload["bot_token"] = config.data["bot_token"]
    return safe_payload


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def remove_database_sidecars() -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{DATABASE_PATH}{suffix}").unlink(missing_ok=True)


def validate_samples_payload(samples: Any) -> dict[str, dict[str, str]]:
    if not isinstance(samples, dict):
        raise ValueError("The samples value must be a JSON object.")

    validated: dict[str, dict[str, str]] = {}
    for key, sample in samples.items():
        if not isinstance(key, str) or not isinstance(sample, dict):
            raise ValueError("Each sample must be an object with a string key.")
        name = sample.get("name")
        body = sample.get("body")
        if not isinstance(name, str) or not isinstance(body, str):
            raise ValueError(f"Sample '{key}' must have string 'name' and 'body' values.")
        _, errors = parse_body(body)
        if errors or not body.strip():
            raise ValueError(f"Sample '{key}' has invalid placeholders or an empty body.")
        footer = sample.get("append_submit_link", True)
        if not isinstance(footer, bool):
            raise ValueError(f"Sample '{key}' has an invalid append_submit_link setting.")
        validated[key] = {
            "name": name,
            "body": body,
            "append_submit_link": footer,
        }
    return validated


def sample_export_for_family(family_key: str, family: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "tgadmin-samples-v1",
        "scope": "topic",
        "family_key": family_key,
        "topic_id": family.get("topic_id"),
        "channel_id": family.get("channel_id"),
        "samples": family.get("samples", {}),
    }


def current_topic_family(message: Message):
    if message.message_thread_id is None:
        return None, None
    family_key, family = config.find_family_by_topic(message.message_thread_id)
    if family is None or not family.get("channel_id"):
        return None, None
    return family_key, family


async def require_topic(message: Message) -> bool:
    if message.message_thread_id is None:
        await message.reply("Run this command inside a forum topic.")
        return False
    return True


async def read_replied_document(message: Message, bot: Bot) -> bytes | None:
    replied = message.reply_to_message
    if replied is None or replied.document is None:
        await message.reply(
            "Reply to a JSON or ZIP document with this command to import it."
        )
        return None
    if replied.document.file_size and replied.document.file_size > MAX_IMPORT_SIZE:
        await message.reply("The import file is too large (20 MB maximum).")
        return None

    downloaded = io.BytesIO()
    await bot.download(replied.document.file_id, destination=downloaded)
    data = downloaded.getvalue()
    if len(data) > MAX_IMPORT_SIZE:
        await message.reply("The import file is too large (20 MB maximum).")
        return None
    return data


async def send_document(
    message: Message,
    bot: Bot,
    content: bytes,
    filename: str,
    caption: str,
) -> None:
    await bot.send_document(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        document=BufferedInputFile(content, filename=filename),
        caption=caption,
    )


def decode_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The attached file is not valid UTF-8 JSON.") from error


def samples_from_document(payload: Any, family_key: str) -> dict[str, dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("The sample document must contain a JSON object.")

    if "samples" in payload:
        samples = payload["samples"]
    elif isinstance(payload.get("families"), dict):
        family_data = payload["families"].get(family_key)
        if not isinstance(family_data, dict):
            raise ValueError(f"The document does not include family '{family_key}'.")
        samples = family_data.get("samples", {})
    else:
        samples = payload
    return validate_samples_payload(samples)


def sqlite_snapshot(path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        snapshot_path = Path(directory) / "snapshot.sqlite3"
        source = sqlite3.connect(path)
        target = sqlite3.connect(snapshot_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return snapshot_path.read_bytes()


def validate_database_file(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
    except sqlite3.DatabaseError as error:
        raise ValueError("The database in the backup is invalid.") from error
    if integrity != "ok" or "post_links" not in tables:
        raise ValueError("The backup database is corrupt or has an unsupported schema.")


def install_full_backup(config_payload: Any, database_bytes: bytes) -> None:
    safe_config = validate_config_payload(config_payload)
    config_bytes = json.dumps(safe_config, ensure_ascii=False, indent=2).encode("utf-8")

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATABASE_PATH.parent) as directory:
        staged_database = Path(directory) / "staged.sqlite3"
        staged_database.write_bytes(database_bytes)
        validate_database_file(staged_database)

        old_config = config.path.read_bytes()
        old_database = sqlite_snapshot(DATABASE_PATH) if DATABASE_PATH.exists() else None
        try:
            os.replace(staged_database, DATABASE_PATH)
            remove_database_sidecars()
            write_atomic(config.path, config_bytes)
        except Exception:
            if old_database is None:
                DATABASE_PATH.unlink(missing_ok=True)
            else:
                write_atomic(DATABASE_PATH, old_database)
            remove_database_sidecars()
            write_atomic(config.path, old_config)
            raise

    config.data = safe_config
    post_links.initialize()


@router.message(Command("export_config"))
async def cmd_export_config(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    await send_document(
        message,
        bot,
        exported_config_bytes(),
        "config_export.json",
        "Config export. The bot token is excluded.",
    )


@router.message(IsSuperAdmin(), Command("import_config"))
async def cmd_import_config(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    data = await read_replied_document(message, bot)
    if data is None:
        return
    try:
        safe_config = validate_config_payload(decode_json(data))
        write_atomic(
            config.path,
            json.dumps(safe_config, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except (ValueError, OSError) as error:
        return await message.reply(f"Import failed: {html.escape(str(error))}")
    config.data = safe_config
    await message.reply("Config imported. The current bot token was preserved.")


@router.message(Command("export_sample"))
async def cmd_export_sample(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    family_key, family = current_topic_family(message)
    if family is None:
        return await message.reply("This topic is not linked to a channel.")
    payload = sample_export_for_family(family_key, family)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    await send_document(
        message,
        bot,
        data,
        f"samples_{family_key}.json",
        f"Template export for {family.get('name', family_key)}.",
    )


@router.message(Command("import_sample"))
async def cmd_import_sample(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    family_key, family = current_topic_family(message)
    if family is None:
        return await message.reply("This topic is not linked to a channel.")
    data = await read_replied_document(message, bot)
    if data is None:
        return
    try:
        samples = samples_from_document(decode_json(data), family_key)
    except ValueError as error:
        return await message.reply(f"Import failed: {html.escape(str(error))}")

    family["samples"] = samples
    config.save()
    await message.reply(
        f"Imported {len(samples)} template(s) for {html.escape(family.get('name', family_key))}."
    )


@router.message(Command("export_sample_all"))
async def cmd_export_sample_all(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    families = {
        key: {
            "name": family.get("name"),
            "topic_id": family.get("topic_id"),
            "channel_id": family.get("channel_id"),
            "samples": family.get("samples", {}),
        }
        for key, family in config.data.get("families", {}).items()
    }
    payload = {"format": "tgadmin-samples-v1", "scope": "all", "families": families}
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    await send_document(
        message,
        bot,
        data,
        "all_samples.json",
        "Template export for all configured topics.",
    )


@router.message(Command("import_sample_all"))
async def cmd_import_sample_all(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    data = await read_replied_document(message, bot)
    if data is None:
        return
    try:
        payload = decode_json(data)
        incoming_families = payload.get("families") if isinstance(payload, dict) else None
        if not isinstance(incoming_families, dict):
            raise ValueError("The document must contain a 'families' object.")

        prepared: dict[str, dict[str, dict[str, str]]] = {}
        unmatched = []
        for source_key, source_data in incoming_families.items():
            if not isinstance(source_data, dict):
                raise ValueError(f"Family '{source_key}' must be an object.")
            source_topic_id = source_data.get("topic_id")
            target_key = None
            target_family = config.get_family(source_key)
            if (
                target_family
                and (source_topic_id is None or target_family.get("topic_id") == source_topic_id)
            ):
                target_key = source_key
            elif source_topic_id is not None:
                target_key, target_family = config.find_family_by_topic(source_topic_id)
            if target_key is None or not target_family or not target_family.get("channel_id"):
                unmatched.append(str(source_key))
                continue
            prepared[target_key] = validate_samples_payload(source_data.get("samples", {}))
    except ValueError as error:
        return await message.reply(f"Import failed: {html.escape(str(error))}")

    for family_key, samples in prepared.items():
        config.data["families"][family_key]["samples"] = samples
    if prepared:
        config.save()
    skipped = f" Unmatched entries: {', '.join(unmatched)}." if unmatched else ""
    await message.reply(
        f"Imported template sets for {len(prepared)} topic(s).{html.escape(skipped)}"
    )


@router.message(Command("export_all"))
async def cmd_export_all(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.json", exported_config_bytes())
        archive.writestr("tgadmin.sqlite3", sqlite_snapshot(DATABASE_PATH))
        archive.writestr(
            "manifest.json",
            json.dumps({"format": "tgadmin-full-backup-v1"}, indent=2),
        )
    await send_document(
        message,
        bot,
        archive_data.getvalue(),
        "tgadmin_full_backup.zip",
        "Full backup: config without the bot token and the SQLite post-link database.",
    )


@router.message(IsSuperAdmin(), Command("import_all"))
async def cmd_import_all(message: Message, bot: Bot):
    if not await require_topic(message):
        return
    data = await read_replied_document(message, bot)
    if data is None:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if not {"config.json", "tgadmin.sqlite3"}.issubset(names):
                raise ValueError("The ZIP must contain config.json and tgadmin.sqlite3.")
            if sum(item.file_size for item in archive.infolist()) > MAX_ARCHIVE_CONTENT_SIZE:
                raise ValueError("The uncompressed ZIP contents exceed the 50 MB limit.")
            config_payload = decode_json(archive.read("config.json"))
            database_bytes = archive.read("tgadmin.sqlite3")
        install_full_backup(config_payload, database_bytes)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as error:
        return await message.reply(f"Import failed: {html.escape(str(error))}")
    await message.reply(
        "Full backup imported: config and SQLite database restored; the current bot token was preserved."
    )
