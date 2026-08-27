import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional


LOGGER = logging.getLogger(__name__)


class SessionCharacterBindings:
    """Persist per-session Genie character bindings in a small JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.bindings: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.bindings = {}
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("session character bindings must be a JSON object")
            self.bindings = {
                str(session_id): str(character_name)
                for session_id, character_name in payload.items()
                if str(session_id).strip() and str(character_name).strip()
            }
        except Exception as exc:
            LOGGER.warning(
                "Failed to load session character bindings from %s: %s. Falling back to empty mapping.",
                self.path,
                exc,
            )
            self.bindings = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self.bindings, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, self.path)

    def get(self, session_id: str) -> Optional[str]:
        return self.bindings.get(str(session_id))

    def set(self, session_id: str, character_name: str) -> None:
        session_id = str(session_id).strip()
        character_name = str(character_name).strip()
        if not session_id or not character_name:
            raise ValueError("session_id and character_name are required")
        self.bindings[session_id] = character_name
        self._save()

    def clear(self, session_id: str) -> None:
        self.bindings.pop(str(session_id), None)
        self._save()
