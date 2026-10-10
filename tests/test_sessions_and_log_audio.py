"""Run with: python -m unittest discover -s tests -v (requires AstrBot SDK)."""

import asyncio
import base64
import importlib
import logging
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("genie_session_tests")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
state = importlib.import_module(pkg.__name__ + ".session_state")
main = importlib.import_module(pkg.__name__ + ".main")
web = importlib.import_module(pkg.__name__ + ".web_api")
logs = importlib.import_module(pkg.__name__ + ".run_log")
from quart import Quart


def plugin():
    p = main.GenieTtsLlmPlugin.__new__(main.GenieTtsLlmPlugin)
    p.config = {"group_whitelist": ["100"], "group_blacklist": ["999"],
                "enable_private_tts_by_default": False}
    p.active_groups, p.inactive_groups = set(), set()
    p.inactive_private_sessions = set()
    p.active_sessions, p.w_active_sessions = set(), set()
    p.session_emotions, p.session_w_settings, p.known_sessions = {}, {}, {}
    p.sent_voices = {}
    p._state_restored, p._state_lock = asyncio.Event(), asyncio.Lock()
    p._state_restored.set()
    p.put_kv_data, p.get_kv_data = AsyncMock(), AsyncMock(return_value=None)
    return p


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = plugin()

    def test_config_visible_without_command_and_live_changes(self):
        d = state.session_payload(self.p)
        self.assertEqual(d["sessions"], [])
        self.assertEqual(d["group_rules"][0]["sources"], ["配置群白名单"])
        self.assertTrue(self.p._is_group_tts_active("100"))
        self.p.config["group_whitelist"] = []
        self.assertFalse(self.p._is_group_tts_active("100"))
        self.p.active_groups.add("100")  # /ttg is independent of config.
        self.assertTrue(self.p._is_group_tts_active("100"))
        self.p.inactive_groups.add("100")
        self.assertFalse(self.p._is_group_tts_active("100"))

    def test_blacklist_and_manual_session_precedence(self):
        p = self.p
        p.config["enable_group_tts_by_default"] = True
        self.assertFalse(state.effective_state(p, "private")["enabled"])
        self.assertTrue(state.effective_state(p, "group", "100")["enabled"])
        p.inactive_groups.add("100")
        self.assertFalse(state.effective_state(p, "group", "100")["enabled"])
        p.active_sessions.add("group")
        self.assertTrue(state.effective_state(p, "group", "100")["enabled"])
        self.assertFalse(state.effective_state(p, "group", "999")["enabled"])

    async def test_llm_hook_observes_config_only_session_and_persists(self):
        p = self.p
        p._should_generate_tts_now = lambda _: True
        p._should_inject_llm_emotion_tags = lambda: False
        p._should_inject_llm_translation_tags = lambda: False
        p._build_llm_tool_prompt = lambda _: None
        p.checked_auto_tts_sessions, p.pending_auto_tts_sessions = set(), set()
        event = types.SimpleNamespace(unified_msg_origin="bot-a:GroupMessage:user_100",
                                      message_obj=types.SimpleNamespace(group_id="100"))
        await p.inject_llm_prompt(event, types.SimpleNamespace(system_prompt=""))
        d = state.session_payload(p)
        self.assertTrue(d["sessions"][0]["effective"]["enabled"])
        self.assertFalse(d["sessions"][0]["tts_active"])
        saved = p.put_kv_data.call_args.args[1]
        self.assertEqual(set(saved["known_sessions"][event.unified_msg_origin]), {"group_id", "last_seen"})
        self.assertEqual(saved["active_groups"], [])
        await p._observe_session(event)
        p.put_kv_data.assert_awaited_once()  # repeated tool rounds are coalesced
        event.unified_msg_origin = "bot-b:GroupMessage:user_100"
        await p._observe_session(event)
        self.assertEqual(len(p.known_sessions), 2)

    async def test_restore_migration_and_roundtrip(self):
        p = self.p
        p.get_kv_data.return_value = {
            "active_groups": ["100", "200"], "inactive_groups": ["300"],
            "known_sessions": {"sid": {"group_id": "100", "last_seen": 123}},
        }
        await p._restore_state()
        self.assertEqual(p.active_groups, {"200"})
        self.assertEqual(p.known_sessions["sid"]["last_seen"], 123)
        await p._persist_state()
        saved = p.put_kv_data.call_args.args[1]
        self.assertEqual(saved["version"], 2)
        p.get_kv_data.return_value = saved
        p.active_groups.clear()
        await p._restore_state()
        self.assertEqual(p.active_groups, {"200"})
        p.config["enable_state_persistence"] = False
        p.put_kv_data.reset_mock()
        await p._observe_session(types.SimpleNamespace(unified_msg_origin="private",
                                message_obj=types.SimpleNamespace(group_id=None)))
        p.put_kv_data.assert_not_awaited()

    def test_bounded_history_and_saved_settings(self):
        raw = {str(i): {"group_id": "100", "last_seen": i} for i in range(700)}
        self.p.known_sessions = state.restore_known_sessions(raw)
        self.assertEqual(len(self.p.known_sessions), 500)
        self.assertNotIn("0", self.p.known_sessions)
        self.p.active_sessions.add("0")
        self.assertEqual(len(state.session_payload(self.p)["sessions"]), 501)

    async def test_webui_mode_switch_is_exclusive(self):
        api = web.GenieWebApi.__new__(web.GenieWebApi)
        api.plugin = self.p
        api._read_body = AsyncMock(return_value={"target": "sid", "scope": "session", "kind": "w", "enabled": True})
        self.p.active_sessions.add("sid")
        async with Quart(__name__).app_context():
            result = await (await api._handle_session_toggle()).get_json()
            self.assertEqual(result["status"], "ok")
            self.assertNotIn("sid", self.p.active_sessions)
            self.assertIn("sid", self.p.w_active_sessions)
            api._read_body.return_value["kind"] = "tts"
            await api._handle_session_toggle()
            self.assertNotIn("sid", self.p.w_active_sessions)
            self.assertIn("sid", self.p.active_sessions)

    async def test_group_commands_and_session_close_keep_inheritance(self):
        p = self.p
        event = types.SimpleNamespace(
            unified_msg_origin="bot:GroupMessage:100",
            message_obj=types.SimpleNamespace(group_id="100"), plain_result=lambda text: text,
        )
        await anext(p.start_tts(event))
        reply = await anext(p.stop_tts(event))
        self.assertIn("本群仍", reply)
        self.assertTrue(state.effective_state(p, event.unified_msg_origin, "100")["enabled"])
        await anext(p.stop_group_tts(event))
        self.assertFalse(state.effective_state(p, event.unified_msg_origin, "100")["enabled"])
        await anext(p.start_group_tts(event))
        self.assertTrue(state.effective_state(p, event.unified_msg_origin, "100")["enabled"])

    async def test_webui_saved_whitelist_applies_immediately(self):
        class Config(dict):
            def save_config(self):
                pass
        self.p.config = Config(self.p.config)
        api = web.GenieWebApi.__new__(web.GenieWebApi)
        api.plugin = self.p
        api._read_body = AsyncMock(return_value={"values": {"group_whitelist": ["200"]}})
        async with Quart(__name__).app_context():
            response = await (await api._handle_config_save()).get_json()
            self.assertEqual(response["status"], "ok")
            self.assertFalse(response["data"]["needs_reload"])
            groups = (await (await api._handle_sessions()).get_json())["data"]["group_rules"]
            self.assertEqual(groups[0]["group_id"], "200")
            self.assertFalse(self.p._is_group_tts_active("100"))
            self.assertTrue(self.p._is_group_tts_active("200"))


class LogAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "audio"
        self.root.mkdir()
        self.p = plugin()
        self.p.tts_engine = types.SimpleNamespace(temp_audio_dir=self.root)
        self.p.run_log = logs.RunLog()
        logger = logging.getLogger("genie-test-" + self.tmp.name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        self.p.run_log.attach(logger)
        self.addCleanup(self.p.run_log.detach)
        self.api = web.GenieWebApi.__new__(web.GenieWebApi)
        self.api.plugin = self.p
        self.app = Quart(__name__)

    def trace(self, name="first.wav", data=b"RIFF" + b"x" * 2000000):
        path = self.root / name
        path.write_bytes(data)
        trace = self.p.run_log.begin_synth("auto", session="same-session")
        trace.ok(audio_path=str(path))
        return trace, path, data

    async def request_audio(self, trace_id, **over):
        params = {"id": str(trace_id), "instance": self.p.run_log.instance_id, "offset": "0", **over}
        self.api._query = lambda key: params.get(key)
        async with self.app.app_context():
            return await (await self.api._handle_log_audio()).get_json()

    async def test_exact_original_bytes_across_chunks_and_log_link(self):
        first, _, original = self.trace()
        second, _, _ = self.trace("second.wav", b"different audio")
        entries, _ = self.p.run_log.buffer.query()
        self.assertEqual([int(e["extra"]["synth_id"]) for e in entries], [second.id, first.id])
        assembled = b""
        while len(assembled) < len(original):
            response = await self.request_audio(first.id, offset=str(len(assembled)))
            self.assertEqual(response["status"], "ok")
            assembled += base64.b64decode(response["data"]["audio_base64"])
        self.assertEqual(assembled, original)
        self.assertTrue(response["data"]["done"])

    async def test_deleted_file_and_no_audio_and_restart(self):
        trace, path, _ = self.trace()
        path.unlink()
        result = await self.request_audio(trace.id)
        self.assertEqual(result["data"]["state"], "cleared")
        self.assertNotIn("audio_base64", result["data"])
        pending = self.p.run_log.begin_synth("auto")
        self.assertEqual((await self.request_audio(pending.id))["data"]["state"], "not_generated")
        self.assertEqual((await self.request_audio(trace.id, instance="old-instance"))["data"]["state"], "expired")
        self.p.run_log.synth.clear()
        self.assertEqual((await self.request_audio(trace.id))["data"]["state"], "expired")

    async def test_live_availability_in_both_log_lists(self):
        trace, path, _ = self.trace()
        self.api._query = lambda _: None
        async with self.app.app_context():
            available = await (await self.api._handle_synth_logs()).get_json()
            self.assertEqual(available["data"]["items"][0]["audio"]["state"], "available")
            path.unlink()
            runtime = await (await self.api._handle_logs()).get_json()
            synths = await (await self.api._handle_synth_logs()).get_json()
            self.assertEqual(runtime["data"]["items"][0]["audio"]["state"], "cleared")
            self.assertEqual(synths["data"]["items"][0]["audio"]["state"], "cleared")

    async def test_reject_paths_outside_generated_audio(self):
        outside = Path(self.tmp.name) / "private.wav"
        outside.write_bytes(b"secret")
        trace = self.p.run_log.begin_synth("auto").ok(audio_path=str(outside))
        self.assertEqual((await self.request_audio(trace.id))["data"]["state"], "unavailable")
        good, _, _ = self.trace()
        self.assertEqual((await self.request_audio(good.id, offset="-1"))["status"], "error")
        self.assertEqual((await self.request_audio(good.id, offset="invalid"))["status"], "error")

    async def test_workbench_synthesis_is_traced(self):
        _, path, _ = self.trace()
        self.api._read_body = AsyncMock(return_value={
            "text": "你好", "character": "test", "emotion": "normal",
            "ref_audio_path": "reference_audio/test.wav", "ref_audio_text": "参考文本",
        })
        self.p.tts_engine.synthesize = AsyncMock(return_value=str(path))
        self.p.tts_engine._server_urls = lambda: ["https://example.invalid"]
        self.p.tts_engine._expected_audio_seconds = lambda _: 1.0
        self.p.tts_engine.queue_size = lambda: 0
        self.api._history = []
        async with self.app.app_context():
            response = await (await self.api._handle_synthesize()).get_json()
        self.assertEqual(response["status"], "ok")
        rows, _ = self.p.run_log.synth.query(source="webui")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(self.api._log_audio_info(rows[0])["state"], "available")

    def test_frontend_endpoint_inventory(self):
        js = (ROOT / "pages/studio/app.js").read_text(encoding="utf-8")
        table = re.search(r"var ENDPOINTS = \[(.*?)\n\];", js, re.S).group(1)
        listed = re.findall(r'\["[^"]+", "(GET|POST)", "([^"]+)"', table)
        expected = {(r[1], r[0]) for r in self.api._routes()}
        self.assertEqual(set(listed), expected)
        self.assertEqual(len(listed), len(expected))


if __name__ == "__main__":
    unittest.main()
