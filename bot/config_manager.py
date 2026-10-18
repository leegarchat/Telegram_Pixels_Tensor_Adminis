"""Persistent JSON configuration store (config.json) with topic/channel bindings.

Config schema (see config_example.json):
  bot_token, bot_username, proxy_json, mod_group_id, cooldown_seconds,
  admins: [telegram user ids],
  families: { <family_key>: {name, topic_id, channel_id, channel_url,
                             channel_title, tag?, topic_name?, samples?} },
  topic_names: { "<topic_id>": "<forum topic title>" }

A single global `config` instance is created on import and shared by all handlers.
"""
# json: config file format; re: sample-key normalization; Path: file locations.
import json
import re
from pathlib import Path
from typing import Dict, Any

# config.json lives next to the project root (parent of the `bot/` package).
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigManager:
    """Load, mutate and persist the JSON config file.

    The class keeps an in-memory `data` dict; every mutating method calls
    `save()` so the file is always the source of truth after the call.
    """

    def __init__(self, path: Path = CONFIG_PATH):
        """Bind to a config file path and load it immediately."""
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Read and parse the JSON file into `self.data`."""
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def save(self) -> None:
        """Write `self.data` back to disk (pretty-printed, UTF-8 preserved)."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def is_admin(self, user_id: int) -> bool:
        """Return True when the Telegram user id is listed in `admins`."""
        return user_id in self.data.get("admins", [])

    def get_family(self, key: str) -> Dict[str, Any]:
        """Return the family dict for `key`, or an empty dict when unknown."""
        return self.data.get("families", {}).get(key, {})

    def set_family_data(self, key: str, name: str, topic_id: int, channel_id: int) -> None:
        """Create/update a family entry (legacy /cfg_set_family path).

        Preserves an existing `samples` section so template data is not lost
        when only the binding fields change.
        """
        if "families" not in self.data:
            self.data["families"] = {}
        # Keep previously configured templates for this family, if any.
        existing = self.data["families"].get(key, {})
        self.data["families"][key] = {
            "name": name,
            "topic_id": topic_id,
            "channel_id": channel_id,
            # Spread existing samples back in; omitted when the family is new.
            **({"samples": existing["samples"]} if "samples" in existing else {})
        }
        self.save()

    # --- Topic -> channel bindings (stored in families) ---

    def find_family_by_topic(self, topic_id: int):
        """Find the (key, family) pair bound to a forum topic id.

        Returns:
            Tuple (key, family dict), or (None, None) when no topic matches.
        """
        for key, fam in self.data.get("families", {}).items():
            if fam.get("topic_id") == topic_id:
                return key, fam
        return None, None

    def set_topic_name(self, topic_id: int, name: str) -> None:
        """Cache the forum topic title for nicer display names.

        Updates both the global `topic_names` map and the bound family entry
        (family `name` follows the topic title unless a custom `tag` is set).
        """
        name = name.strip()
        if not name:
            return
        # Global id -> title cache used as a fallback display source.
        topic_names = self.data.setdefault("topic_names", {})
        topic_names[str(topic_id)] = name
        _, family = self.find_family_by_topic(topic_id)
        if family is not None:
            family["topic_name"] = name
            if not family.get("tag"):
                family["name"] = name
        self.save()

    def get_topic_name(self, topic_id: int) -> str | None:
        """Resolve the best display name for a topic: cache, then family fields."""
        # Prefer the explicit title cache (updated on topic create/edit events).
        name = self.data.get("topic_names", {}).get(str(topic_id))
        if name:
            return name
        _, family = self.find_family_by_topic(topic_id)
        if family is None:
            return None
        # Fall back to the stored topic_name, or family name when no custom tag exists.
        return family.get("topic_name") or (
            None if family.get("tag") else family.get("name")
        )

    def bind_topic(
        self,
        topic_id: int,
        channel_id: int,
        channel_url: str,
        channel_title: str,
        tag: str | None = None,
        topic_name: str | None = None,
    ) -> str:
        """Bind a channel to a forum topic; create the family entry if missing.

        Args:
            topic_id: Forum topic (message_thread_id) inside the mod group.
            channel_id: Target channel/chat id posts are published to.
            channel_url: Public t.me URL (or `id:<id>` fallback).
            channel_title: Human-readable channel title.
            tag: Optional custom label; becomes the family display `name`.
            topic_name: Cached forum topic title, if known.

        Returns:
            The family key that was updated or created (`topic_<id>` for new links).
        """
        if "families" not in self.data:
            self.data["families"] = {}

        # Reuse the existing family for this topic, or allocate a fresh key.
        key, fam = self.find_family_by_topic(topic_id)
        if fam is None:
            key = f"topic_{topic_id}"
            suffix = 2
            # Avoid collisions when a stale `topic_<id>` key already exists.
            while key in self.data["families"]:
                key = f"topic_{topic_id}_{suffix}"
                suffix += 1
            fam = {"name": f"Topic {topic_id}", "topic_id": topic_id}
            self.data["families"][key] = fam

        # Resolve the display name: explicit topic title wins over stored values.
        cached_topic_name = (
            topic_name
            or self.data.get("topic_names", {}).get(str(topic_id))
            or fam.get("topic_name")
        )
        fallback_name = fam.get("name") if not fam.get("tag") else None
        if cached_topic_name:
            fam["topic_name"] = cached_topic_name
        if tag:
            # A custom tag overrides the display name and is remembered separately.
            fam["tag"] = tag.strip()
            fam["name"] = tag.strip()
        else:
            fam.pop("tag", None)
            fam["name"] = cached_topic_name or fallback_name or f"Topic {topic_id}"

        # Store/refresh the channel binding, then persist.
        fam["topic_id"] = topic_id
        fam["channel_id"] = channel_id
        fam["channel_url"] = channel_url
        fam["channel_title"] = channel_title
        self.save()
        return key

    def unbind_topic(self, topic_id: int):
        """Remove the channel binding of a topic but keep the family entry.

        Returns:
            (key, family, removed dict) on success, or None when not bound.
        """
        key, fam = self.find_family_by_topic(topic_id)
        if fam is None:
            return None
        # Pop channel fields; family name/samples/topic metadata are retained.
        removed = {
            "channel_id": fam.pop("channel_id", None),
            "channel_url": fam.pop("channel_url", None),
            "channel_title": fam.pop("channel_title", None),
        }
        self.save()
        return key, fam, removed

    def get_channel_for_topic(self, topic_id: int | None) -> int | None:
        """Return the channel id bound to a topic, or None when unbound/unknown."""
        if topic_id is None:
            return None
        _, fam = self.find_family_by_topic(topic_id)
        return fam.get("channel_id") if fam else None

    def list_links(self):
        """List all family entries that have a topic_id, sorted by topic_id.

        Used by /get_links to render the topic -> channel overview.
        """
        links = [
            {"key": key, **fam}
            for key, fam in self.data.get("families", {}).items()
            if fam.get("topic_id") is not None
        ]
        links.sort(key=lambda item: item["topic_id"])
        return links

    # --- Topic-scoped post templates ---

    @staticmethod
    def normalize_sample_key(name: str) -> str:
        """Normalize a template name into a dict key: lowercase, non-word chars to '_'.

        Example: "My Kernel!" -> "my_kernel".
        """
        key = re.sub(r"[^\w\-]+", "_", name.strip().lower(), flags=re.UNICODE)
        return key.strip("_")

    def list_samples(self, family_key: str) -> Dict[str, Any]:
        """Return all templates ({key: sample}) configured for a family."""
        return self.get_family(family_key).get("samples", {})

    def get_sample(self, family_key: str, key: str) -> Dict[str, Any]:
        """Return a single template dict, or {} when family/template is unknown."""
        return self.list_samples(family_key).get(key, {})

    def delete_sample(self, family_key: str, key: str) -> bool:
        """Delete a template; returns False when the key did not exist."""
        samples = self.data.get("families", {}).get(family_key, {}).get("samples", {})
        if key not in samples:
            return False
        del samples[key]
        self.save()
        return True

    def save_sample(
        self,
        family_key: str,
        name: str,
        body: str,
        append_submit_link: bool | None = None,
    ):
        """Create or update a post template inside a family.

        Args:
            family_key: Family the template belongs to (must already exist).
            name: Display name shown on the template picker button.
            body: Raw template text with `{Key | Prompt | type}` placeholders.
            append_submit_link: Footer toggle; None preserves the previous value
                (defaulting to True for brand-new templates).

        Returns:
            Tuple (normalized key, created bool).
        """
        family = self.data.get("families", {}).get(family_key)
        if family is None:
            raise KeyError(f"Unknown family: {family_key}")
        if "samples" not in family:
            family["samples"] = {}
        key = self.normalize_sample_key(name)
        created = key not in family["samples"]
        existing = family["samples"].get(key, {})
        # Resolve footer flag: explicit value wins, else keep old setting, else on.
        footer_enabled = (
            append_submit_link
            if append_submit_link is not None
            else existing.get("append_submit_link", True)
        )
        family["samples"][key] = {
            "name": name.strip(),
            "body": body,
            "append_submit_link": footer_enabled,
        }
        self.save()
        return key, created


# Global singleton: import `config` anywhere to read/mutate config.json.
config = ConfigManager()
