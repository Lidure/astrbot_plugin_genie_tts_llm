import unittest
from pathlib import Path

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

    def test_main_uses_shared_session_role_resolution_in_emotion_paths(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("from .emotion_routing import", source)
        self.assertGreaterEqual(source.count("self._resolve_tts_profile(session_id)"), 3)
        self.assertIn("parse_provider_emotion_result(", source)


if __name__ == "__main__":
    unittest.main()
