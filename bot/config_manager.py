import json
import re
from pathlib import Path
from typing import Dict, Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.data.get("admins", [])

    def get_family(self, key: str) -> Dict[str, Any]:
        return self.data.get("families", {}).get(key, {})

    def set_family_data(self, key: str, name: str, topic_id: int, channel_id: int) -> None:
        if "families" not in self.data:
            self.data["families"] = {}
        existing = self.data["families"].get(key, {})
        self.data["families"][key] = {
            "name": name,
            "topic_id": topic_id,
            "channel_id": channel_id,
            **({"samples": existing["samples"]} if "samples" in existing else {})
        }
        self.save()

    # --- Topic -> channel bindings (stored in families) ---

    def find_family_by_topic(self, topic_id: int):
        for key, fam in self.data.get("families", {}).items():
            if fam.get("topic_id") == topic_id:
                return key, fam
        return None, None

    def set_topic_name(self, topic_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            return
        topic_names = self.data.setdefault("topic_names", {})
        topic_names[str(topic_id)] = name
        _, family = self.find_family_by_topic(topic_id)
        if family is not None:
            family["topic_name"] = name
            if not family.get("tag"):
                family["name"] = name
        self.save()

    def get_topic_name(self, topic_id: int) -> str | None:
        name = self.data.get("topic_names", {}).get(str(topic_id))
        if name:
            return name
        _, family = self.find_family_by_topic(topic_id)
        if family is None:
            return None
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
        """Bind a channel to a topic. Updates an existing family with this topic_id
        or creates a new one (key topic_<id>). Returns the family key."""
        if "families" not in self.data:
            self.data["families"] = {}

        key, fam = self.find_family_by_topic(topic_id)
        if fam is None:
            key = f"topic_{topic_id}"
            suffix = 2
            while key in self.data["families"]:
                key = f"topic_{topic_id}_{suffix}"
                suffix += 1
            fam = {"name": f"Topic {topic_id}", "topic_id": topic_id}
            self.data["families"][key] = fam

        cached_topic_name = (
            topic_name
            or self.data.get("topic_names", {}).get(str(topic_id))
            or fam.get("topic_name")
        )
        fallback_name = fam.get("name") if not fam.get("tag") else None
        if cached_topic_name:
            fam["topic_name"] = cached_topic_name
        if tag:
            fam["tag"] = tag.strip()
            fam["name"] = tag.strip()
        else:
            fam.pop("tag", None)
            fam["name"] = cached_topic_name or fallback_name or f"Topic {topic_id}"

        fam["topic_id"] = topic_id
        fam["channel_id"] = channel_id
        fam["channel_url"] = channel_url
        fam["channel_title"] = channel_title
        self.save()
        return key

    def unbind_topic(self, topic_id: int):
        """Remove the channel binding of a topic. Returns (key, family, removed) or None."""
        key, fam = self.find_family_by_topic(topic_id)
        if fam is None:
            return None
        removed = {
            "channel_id": fam.pop("channel_id", None),
            "channel_url": fam.pop("channel_url", None),
            "channel_title": fam.pop("channel_title", None),
        }
        self.save()
        return key, fam, removed

    def get_channel_for_topic(self, topic_id: int | None) -> int | None:
        if topic_id is None:
            return None
        _, fam = self.find_family_by_topic(topic_id)
        return fam.get("channel_id") if fam else None

    def list_links(self):
        """All family entries that have a topic_id, sorted by topic_id."""
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
        key = re.sub(r"[^\w\-]+", "_", name.strip().lower(), flags=re.UNICODE)
        return key.strip("_")

    def list_samples(self, family_key: str) -> Dict[str, Any]:
        return self.get_family(family_key).get("samples", {})

    def get_sample(self, family_key: str, key: str) -> Dict[str, Any]:
        return self.list_samples(family_key).get(key, {})

    def save_sample(
        self,
        family_key: str,
        name: str,
        body: str,
        append_submit_link: bool | None = None,
    ):
        """Create/update a template. Returns (key, created)."""
        family = self.data.get("families", {}).get(family_key)
        if family is None:
            raise KeyError(f"Unknown family: {family_key}")
        if "samples" not in family:
            family["samples"] = {}
        key = self.normalize_sample_key(name)
        created = key not in family["samples"]
        existing = family["samples"].get(key, {})
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


config = ConfigManager()

# In-memory storage of pending submissions: {mod_group_msg_id: {user_id, family, text, photo_id}}
pending_posts: Dict[int, Dict[str, Any]] = {}
