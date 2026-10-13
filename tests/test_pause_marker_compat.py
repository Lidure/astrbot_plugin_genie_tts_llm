"""Ensure unsupported pause directives never reach the real Genie /tts payload."""
import ast
import asyncio
import json
import logging
import re
import time
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAUSE_PATTERN = re.compile(r"\[pause\s*=\s*(\d+)\s*(?:ms)?\]", re.I)
SENTENCE_PATTERN = re.compile(r"[。！？!?…]+[」』】）》”’\"']*|[、，,]+")


def isolate(method):
    tree = ast.parse((ROOT / "tts_engine.py").read_text(encoding="utf-8"))
    engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TTSEngine")
    node = next(n for n in engine.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method)
    ns = {
        "asyncio": asyncio, "re": re, "time": time, "uuid": uuid, "wave": wave,
        "PAUSE_MARKER_PATTERN": PAUSE_PATTERN,
        "logger": logging.getLogger("test-pause-compat"),
        "DEFAULT_MAX_TEXT_LENGTH": 300,
        "has_pronounceable": lambda s: bool(re.search(r"[A-Za-z\u3040-\u30ff\u4e00-\u9fff]", s)),
        "MAX_NATURAL_PAUSE_MS": 1200,
        "NATURAL_PAUSE_BOUNDARY_PATTERN": SENTENCE_PATTERN,
        "_modelscope_auth_headers": lambda *_: {},
        "SAMPLE_RATE": 32000, "CHANNELS": 1, "BYTES_PER_SAMPLE": 2,
    }
    pauses = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "inject_natural_pauses")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[pauses, node], type_ignores=[])), "tts_engine.py", "exec"), ns)
    return ns[method]


class FakeSynth:
    def __init__(self, supports, *, custom=False):
        self.config = {
            "enable_natural_pauses": True,
            "enable_custom_pause_marker": custom,
            "tts_server_pause_markers_supported": supports,
            "natural_pause_sentence_ms": 550,
            "natural_pause_clause_ms": 160,
            "enable_sentence_splitting": False,
            "enable_tts_text_cleaning": False,
        }
        self._synthesis_lock = asyncio.Lock()
        self.tts_server_index = 0
        self.stats = {"skipped_no_speech": 0, "text_truncated": 0}
        self.sent = []

    def _server_urls(self):
        return ["https://tts.example"]

    def _ensure_terminal_punctuation(self, text, language):
        return text

    async def _attempt_synthesis_on_server(self, **kwargs):
        self.sent.append(kwargs["text"])
        return "/tmp/synth.wav"


@pytest.mark.parametrize("supported", [False, True])
def test_actual_synthesis_text_only_has_markers_with_explicit_support(supported):
    f = FakeSynth(supported)
    fn = isolate("_synthesize_audio")
    result = asyncio.run(fn(f, "airi", "/ref.wav", "reference", "こんにちは。今日は晴れ！", "s", "jp"))
    assert result == "/tmp/synth.wav"
    if supported:
        assert f.sent == ["こんにちは。[pause=550]今日は晴れ！"]
    else:
        assert f.sent == ["こんにちは。今日は晴れ！"]


@pytest.mark.parametrize("supported,expected", [
    (False, "やあ。ありがとう！"),
    (True, "やあ。[pause=700]ありがとう！"),
])
def test_custom_user_directives_removed_when_backend_unverified(supported, expected):
    f = FakeSynth(supported, custom=True)
    fn = isolate("_synthesize_audio")
    asyncio.run(fn(f, "airi", "/ref.wav", "reference", "やあ。[pause=700]ありがとう！", "s", "jp"))
    assert f.sent == [expected]


class FakeHttp:
    def __init__(self):
        self.json_payloads = []

    def stream(self, method, url, **kwargs):
        self.json_payloads.append(kwargs["json"])

        class Manager:
            async def __aenter__(self):
                class Response:
                    def raise_for_status(self):
                        return None

                    async def aiter_bytes(self):
                        yield b"\x00" * 6400
                return Response()
            async def __aexit__(self, *args):
                return False
        return Manager()


@pytest.mark.parametrize("supported,expected", [
    (False, "おはよう。こんにちは。"),
    (True, "おはよう。[pause=550]こんにちは。"),
])
def test_final_network_payload_defensive_strip(tmp_path, supported, expected):
    http = FakeHttp()
    f = SimpleNamespace(
        config={"tts_server_pause_markers_supported": supported, "tts_server_split_sentence": False},
        temp_audio_dir=tmp_path,
        http_client=http,
        _discard_temp_file=lambda *_: None,
    )
    fn = isolate("_stream_tts_to_file")
    path, seconds = asyncio.run(fn(f, "https://tts.example", "airi",
                                   "おはよう。[pause=550]こんにちは。", 15, "session"))
    assert Path(path).exists() and seconds > 0
    assert http.json_payloads == [{
        "character_name": "airi",
        "text": expected,
        "split_sentence": False,
    }]


def test_schema_opt_in_defaults_off():
    cfg = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert cfg["tts_server_pause_markers_supported"]["default"] is False
    assert cfg["enable_natural_pauses"]["default"] is False
