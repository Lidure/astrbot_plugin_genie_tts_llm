import json
import unittest
from pathlib import Path

from conversation_tts_policy import (
    is_private_tts_active,
    parse_character_default_emotions,
    resolve_default_emotion,
)


class PrivateTtsAndRoleDefaultsTests(unittest.TestCase):
    def test_private_tts_default_can_be_temporarily_disabled(self):
        self.assertTrue(
            is_private_tts_active(
                enable_by_default=True,
                session_id="private-a",
                active_sessions=set(),
                w_active_sessions=set(),
                inactive_private_sessions=set(),
            )
        )
        self.assertFalse(
            is_private_tts_active(
                enable_by_default=True,
                session_id="private-a",
                active_sessions=set(),
                w_active_sessions=set(),
                inactive_private_sessions={"private-a"},
            )
        )
        self.assertTrue(
            is_private_tts_active(
                enable_by_default=False,
                session_id="private-a",
                active_sessions={"private-a"},
                w_active_sessions=set(),
                inactive_private_sessions=set(),
            )
        )

    def test_explicit_private_close_wins_over_active_session(self):
        self.assertFalse(
            is_private_tts_active(
                enable_by_default=True,
                session_id="private-a",
                active_sessions={"private-a"},
                w_active_sessions={"private-a"},
                inactive_private_sessions={"private-a"},
            )
        )

    def test_character_default_emotion_parser_accepts_character_equals_emotion(self):
        parsed = parse_character_default_emotions(
            ["airi=平静", "oka = 温柔", "kisaki=开心", "bad-entry"]
        )
        self.assertEqual(
            parsed,
            {"airi": "平静", "oka": "温柔", "kisaki": "开心"},
        )

    def test_default_emotion_prefers_character_mapping_then_global_then_first(self):
        emotions = ["悲伤", "开心", "关心"]
        self.assertEqual(
            resolve_default_emotion(
                "kisaki", emotions, "悲伤", {"kisaki": "关心"}
            ),
            "关心",
        )
        self.assertEqual(
            resolve_default_emotion("kisaki", emotions, "悲伤", {}),
            "悲伤",
        )
        self.assertEqual(
            resolve_default_emotion("kisaki", emotions, "平静", {}),
            "悲伤",
        )

    def test_invalid_character_specific_default_does_not_break_fallback(self):
        self.assertEqual(
            resolve_default_emotion(
                "oka", ["平静", "开心"], "开心", {"oka": "不存在"}
            ),
            "开心",
        )

    def test_schema_defaults_all_private_chats_to_tts_enabled(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        self.assertTrue(schema["enable_private_tts_by_default"]["default"])
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn('self.config.get("enable_private_tts_by_default", True)', source)

    def test_schema_and_main_wire_private_default_and_role_emotion_resolution(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("enable_private_tts_by_default", schema)
        self.assertIn("character_default_emotions", schema)

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("self.inactive_private_sessions", source)
        self.assertIn("_is_private_tts_active", source)
        self.assertGreaterEqual(
            source.count("self._is_private_tts_active(session_id, group_id)"), 2
        )
        self.assertIn("_get_default_emotion_for_character", source)
        self.assertIn("enable_private_tts_by_default", source)
        self.assertIn("character_default_emotions", source)
        self.assertIn("self.inactive_private_sessions.add(session_id)", source)
        self.assertIn("self.inactive_private_sessions.discard(session_id)", source)


if __name__ == "__main__":
    unittest.main()
