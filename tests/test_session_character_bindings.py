import json
import tempfile
import unittest
from pathlib import Path

from session_character_bindings import SessionCharacterBindings


class SessionCharacterBindingsTests(unittest.TestCase):
    def test_unknown_session_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings = SessionCharacterBindings(Path(tmp) / "session_characters.json")
            self.assertIsNone(bindings.get("missing-session"))

    def test_set_persists_and_reload_restores_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_characters.json"
            bindings = SessionCharacterBindings(path)
            bindings.set("session-a", "oka")

            reloaded = SessionCharacterBindings(path)
            self.assertEqual(reloaded.get("session-a"), "oka")

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload, {"session-a": "oka"})

    def test_clear_removes_binding_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_characters.json"
            bindings = SessionCharacterBindings(path)
            bindings.set("session-a", "kisaki")
            bindings.clear("session-a")

            self.assertIsNone(bindings.get("session-a"))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {},
            )

    def test_corrupt_file_falls_back_to_empty_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_characters.json"
            path.write_text("{not-json", encoding="utf-8")

            bindings = SessionCharacterBindings(path)
            self.assertIsNone(bindings.get("session-a"))

    def test_main_wires_session_binding_into_role_resolution_and_command(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("from .session_character_bindings import SessionCharacterBindings", source)
        self.assertIn("self.session_character_bindings = SessionCharacterBindings(", source)
        self.assertIn('plugin_data_dir / "session_characters.json"', source)
        self.assertIn('alias={"语音角色"}', source)
        self.assertIn("self.session_character_bindings.get(session_id)", source)
        self.assertIn("self.session_character_bindings.set(session_id, character_name)", source)
        self.assertIn("self.session_character_bindings.clear(session_id)", source)


if __name__ == "__main__":
    unittest.main()
