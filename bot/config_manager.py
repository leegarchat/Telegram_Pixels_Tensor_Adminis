import json
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
        self.data["families"][key] = {
            "name": name,
            "topic_id": topic_id,
            "channel_id": channel_id
        }
        self.save()


config = ConfigManager()

# In-memory хранилище активных предложек: {mod_group_msg_id: {user_id, family, text, photo_id}}
pending_posts: Dict[int, Dict[str, Any]] = {}