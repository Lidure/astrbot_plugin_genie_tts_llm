import ast
import json
import unittest
from pathlib import Path

import emotion_routing
from emotion_routing import extract_emotion_directive, parse_provider_emotion_result


class EmotionRoutingTests(unittest.TestCase):
    def test_extract_emotion_directive_accepts_spacing_case_and_fullwidth_brackets(self):
        cases = [
            ("你好 [emotion=开心]", "你好", "开心"),
            ("你好 [ Emotion = 悲伤 ]", "你好", "悲伤"),
            ("你好 ［emotion＝关心］", "你好", "关心"),
        ]
        for text, expected_text, expected_emotion in cases:
            with self.subTest(text=text):
                cleaned, emotion = extract_emotion_directive(text)
                self.assertEqual(cleaned, expected_text)
                self.assertEqual(emotion, expected_emotion)

    def test_parse_provider_emotion_result_accepts_common_suffix_formats(self):
        emotions = ["开心", "悲伤", "关心"]
        cases = [
            ("今日はいい天気ですね。[开心]", "今日はいい天気ですね。", "开心"),
            ("今日はいい天気ですね。 [emotion=开心]", "今日はいい天気ですね。", "开心"),
            ("今日はいい天気ですね。【关心】", "今日はいい天気ですね。", "关心"),
            ("今日はいい天気ですね。\n情感：悲伤", "今日はいい天気ですね。", "悲伤"),
        ]
        for text, expected_text, expected_emotion in cases:
            with self.subTest(text=text):
                translated, emotion = parse_provider_emotion_result(text, emotions)
                self.assertEqual(translated, expected_text)
                self.assertEqual(emotion, expected_emotion)

    def test_parse_provider_emotion_result_does_not_accept_unknown_emotion(self):
        translated, emotion = parse_provider_emotion_result(
            "今日はいい天気ですね。[愤怒]", ["开心", "悲伤"]
        )
        self.assertEqual(translated, "今日はいい天気ですね。[愤怒]")
        self.assertIsNone(emotion)

    def test_provider_emotion_autofill_only_runs_when_no_explicit_emotion(self):
        helper = getattr(emotion_routing, "should_provider_autofill_emotion", None)
        self.assertIsNotNone(helper, "provider emotion autofill routing helper is missing")
        if helper is None:
            return

        self.assertTrue(
            helper(
                enabled=True,
                translation_workflow="provider_translation",
                has_manual_emotion=False,
                has_injected_emotion=False,
                w_mode_active=False,
            )
        )
        self.assertFalse(
            helper(
                enabled=True,
                translation_workflow="provider_translation",
                has_manual_emotion=True,
                has_injected_emotion=False,
                w_mode_active=False,
            )
        )
        self.assertFalse(
            helper(
                enabled=True,
                translation_workflow="provider_translation",
                has_manual_emotion=False,
                has_injected_emotion=True,
                w_mode_active=False,
            )
        )
        self.assertFalse(
            helper(
                enabled=True,
                translation_workflow="llm_injection",
                has_manual_emotion=False,
                has_injected_emotion=False,
                w_mode_active=False,
            )
        )
        self.assertTrue(
            helper(
                enabled=False,
                translation_workflow="provider_translation",
                has_manual_emotion=False,
                has_injected_emotion=False,
                w_mode_active=True,
            )
        )

    def test_provider_emotion_autofill_has_opt_in_config_and_main_integration(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        settings = schema["llm_injection_settings"]["items"]
        self.assertIn("enable_provider_emotion_autofill", settings)
        self.assertFalse(settings["enable_provider_emotion_autofill"]["default"])

        source = Path("main.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("should_provider_autofill_emotion", source)
        self.assertIn("enable_provider_emotion_autofill", source)
        self.assertIn("Provider自动补判情感", source)

    def test_main_prioritizes_manual_then_llm_then_provider_autofill(self):
        source = Path("main.py").read_text(encoding="utf-8")
        priority_block = '''        if session_setting:\n            target_emotion = session_setting["emotion"]\n            emotion_source = "会话固定情感"\n        elif enable_llm_emotion and injected_emotion:\n            target_emotion = injected_emotion\n            emotion_source = "LLM情感标签"\n        elif not provider_emotion_autofill:\n            target_emotion = self._get_default_emotion_for_character(char_name)\n'''
        self.assertIn(priority_block, source)
        self.assertIn("if provider_emotion_autofill and not target_emotion:", source)
        self.assertIn('else "Provider自动补判情感"', source)

    def test_llm_tool_preserves_explicit_emotion_before_provider_autofill(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("has_manual_tool_emotion = bool(emotion_name)", source)
        self.assertIn("has_manual_emotion=has_manual_tool_emotion", source)
        self.assertIn("LLM工具 Provider自动补判情感", source)

    def test_main_uses_shared_session_role_resolution_in_emotion_paths(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("from .emotion_routing import", source)
        self.assertGreaterEqual(source.count("self._resolve_tts_profile(session_id)"), 3)
        self.assertIn("parse_provider_emotion_result(", source)


if __name__ == "__main__":
    unittest.main()
