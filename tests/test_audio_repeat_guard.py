"""Genie TTS repeated-audio mitigation regression tests (no AstrBot runtime)."""
from __future__ import annotations

import ast
import re
import asyncio
import hashlib
import json
import math
import random
import time
import uuid
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_repeat_guard as guard

ROOT = Path(__file__).resolve().parents[1]


def wav_file(path, values, sample_rate=32000):
    from array import array
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(array("h", values).tobytes())
    return str(path)


def noise(seconds, seed):
    rng = random.Random(seed)
    return [rng.randint(-18000, 18000) for _ in range(int(32000 * seconds))]


def test_detects_repeated_phrase_inside_one_audio(tmp_path):
    assert guard._np is not None
    motif = noise(1.8, 33)
    audio = wav_file(tmp_path / "loop.wav", motif + noise(0.9, 81) + motif)
    kind, signature = guard.classify_audio_repeat(audio)
    assert kind == "internal"
    assert signature is not None


def test_detects_fixed_audio_across_different_generation_outputs(tmp_path):
    motif = noise(2.1, 33)
    prior = wav_file(tmp_path / "first.wav", motif + noise(2.4, 81))
    current = wav_file(tmp_path / "second.wav", noise(0.9, 82) + motif + noise(0.6, 83))
    _, old_fingerprint = guard.classify_audio_repeat(prior)
    result, _ = guard.classify_audio_repeat(current, [old_fingerprint])
    assert result == "cross"


def test_different_audio_does_not_trigger_and_silence_ignored(tmp_path):
    a = wav_file(tmp_path / "a.wav", noise(4.8, 1))
    b = wav_file(tmp_path / "b.wav", noise(4.8, 2))
    kind, prior = guard.classify_audio_repeat(a)
    assert kind is None
    kind, _ = guard.classify_audio_repeat(b, [prior])
    assert kind is None
    quiet = wav_file(tmp_path / "quiet.wav", [0] * (32000 * 4))
    assert guard.classify_audio_repeat(quiet) == (None, None)


def test_non_wav_and_missing_numpy_fails_open(tmp_path, monkeypatch):
    p = tmp_path / "bad.wav"
    p.write_text("not a wav")
    assert guard.classify_audio_repeat(p) == (None, None)
    good = wav_file(tmp_path / "valid.wav", noise(2.8, 13))
    monkeypatch.setattr(guard, "_np", None)
    assert guard.classify_audio_repeat(good) == (None, None)


def test_schema_uses_separate_server_split_and_conservative_guard():
    cfg = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert cfg["enable_sentence_splitting"]["default"] is False
    assert cfg["tts_server_split_sentence"]["default"] is False
    assert cfg["enable_tts_audio_repeat_guard"]["default"] is True
    assert cfg["enable_tts_leak_guard"]["default"] is True
    src = (ROOT / "tts_engine.py").read_text(encoding="utf-8")
    assert '"split_sentence": self.config.get("tts_server_split_sentence", False) is True' in src
    assert "classify_audio_repeat(" in src


def engine_method(name):
    tree = ast.parse((ROOT / "tts_engine.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSEngine")
    fn = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    fn.decorator_list = []
    import logging
    namespace = {
        "logger": logging.getLogger("genie-tts-guard-test"), "time": time,
        "PAUSE_MARKER_PATTERN": re.compile(r"\[pause\s*=\s*(\d+)\s*(?:ms)?\]", re.I),
        "hashlib": hashlib, "_modelscope_auth_headers": lambda *_: {},
        "classify_audio_repeat": guard.classify_audio_repeat,
        "wave": wave, "uuid": uuid, "SAMPLE_RATE": 32000,
        "BYTES_PER_SAMPLE": 2, "CHANNELS": 1,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 str(ROOT / "tts_engine.py"), "exec"), namespace)
    return namespace[name], namespace


class HTTPResponse:
    def raise_for_status(self):
        return None


class StreamResponse:
    def __init__(self):
        self.payload = b"\x00" * 6400

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self.payload


class AsyncStream:
    async def __aenter__(self):
        return StreamResponse()

    async def __aexit__(self, *args):
        return False


class FakeHTTP:
    def __init__(self):
        self.tts_payloads = []

    async def post(self, *args, **kwargs):
        return HTTPResponse()

    def stream(self, *args, **kwargs):
        self.tts_payloads.append(kwargs["json"])
        return AsyncStream()


@pytest.mark.parametrize("server_split", [False, True])
def test_actual_tts_post_body_respects_server_split_switch(tmp_path, server_split):
    fn, _ = engine_method("_stream_tts_to_file")
    http = FakeHTTP()
    fake = SimpleNamespace(
        config={"tts_server_split_sentence": server_split},
        http_client=http,
        temp_audio_dir=tmp_path,
        _discard_temp_file=lambda *args: None,
    )
    file, seconds = asyncio.run(
        fn(fake, "https://tts.example", "airi", "こんにちは。", 4, "test-session")
    )
    assert file and seconds > 0
    assert http.tts_payloads[0] == {
        "character_name": "airi",
        "text": "こんにちは。",
        "split_sentence": server_split,
    }


def fake_engine(paths, lengths, *, repeated=None, config=None):
    f = SimpleNamespace()
    f.config = {
        "enable_tts_audio_repeat_guard": True,
        "tts_timeout_seconds": 5,
        **(config or {}),
    }
    f.stats = {"empty_result_retries": 0, "leak_guard_hits": 0,
               "truncation_guard_hits": 0, "audio_repeat_guard_hits": 0}
    f._recent_audio_signatures = deque(maxlen=6)
    f.http_client = FakeHTTP()
    f._get_server_lock = lambda server: asyncio.Lock()
    f._expected_audio_seconds = lambda _: 10.0
    f._minimum_audio_seconds = lambda _: 3.0
    f._leak_guard_enabled = lambda: True
    f._truncation_guard_enabled = lambda: True
    f._duration_penalty = lambda duration, minimum, maximum: (
        (2, duration - maximum) if duration > maximum
        else (1, minimum - duration) if duration < minimum
        else (0, 0.0)
    )
    f.discarded = []
    f._discard_temp_file = lambda path: f.discarded.append(path) if path else None
    f.requests = 0
    iterator = iter(zip(paths, lengths))

    async def stream(*args):
        f.requests += 1
        try:
            return next(iterator)
        except StopIteration:
            return (None, 0.0)

    f._stream_tts_to_file = stream
    return f


def attempt(fake, *, fn=None, **kwargs):
    fn = fn or engine_method("_attempt_synthesis_on_server")[0]
    return asyncio.run(fn(
        fake, "https://tts.example", kwargs.get("character", "airi"),
        "/reference.wav", "参考文字", kwargs.get("text", "今日はいい天気ですね"),
        "test-session",
    ))


def test_retries_overlong_tts_then_rejects_both_abnormal():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    scope["classify_audio_repeat"] = lambda *args: (None, None)
    fake = fake_engine(["/tmp/long1.wav", "/tmp/long2.wav"], [16.0, 14.0])
    assert attempt(fake, fn=fn) is None
    assert fake.requests == 2
    assert set(fake.discarded) == {"/tmp/long1.wav", "/tmp/long2.wav"}
    assert fake.stats["leak_guard_hits"] == 1


def test_good_second_take_survives_initial_overlong_audio():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    scope["classify_audio_repeat"] = lambda *args: (None, None)
    fake = fake_engine(["/tmp/long.wav", "/tmp/normal.wav"], [16.0, 5.0])
    assert attempt(fake, fn=fn) == "/tmp/normal.wav"
    assert fake.requests == 2
    assert "/tmp/long.wav" in fake.discarded


def test_repeating_first_take_retries_then_sends_good_audio():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    calls = []
    def classifier(path, previous):
        calls.append(path)
        return ("internal", None) if path.endswith("loop.wav") else (None, None)
    scope["classify_audio_repeat"] = classifier
    fake = fake_engine(["/tmp/loop.wav", "/tmp/good.wav"], [5.0, 5.0])
    assert attempt(fake, fn=fn) == "/tmp/good.wav"
    assert fake.requests == 2
    assert "/tmp/loop.wav" in fake.discarded
    assert fake.stats["audio_repeat_guard_hits"] == 1


def test_repeat_twice_fails_closed_without_any_voice_send():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    scope["classify_audio_repeat"] = lambda *args: ("cross", None)
    fake = fake_engine(["/tmp/loop1.wav", "/tmp/loop2.wav"], [5.0, 5.0])
    assert attempt(fake, fn=fn) is None
    assert set(fake.discarded) == {"/tmp/loop1.wav", "/tmp/loop2.wav"}


def test_previous_audio_only_compared_when_character_matches_and_text_differs():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    record = []
    signature = object()
    def classifier(path, older):
        record.append(list(older))
        return (None, signature)
    scope["classify_audio_repeat"] = classifier
    fake = fake_engine(["/tmp/normal.wav"], [5.0])
    fake._recent_audio_signatures.append(
        ("airi", hashlib.sha256("今日はいい天気ですね".encode("utf-8")).digest(), signature)
    )
    fake._recent_audio_signatures.append(("other", b"abcd", signature))
    assert attempt(fake, fn=fn) == "/tmp/normal.wav"
    assert record == [[]]
    assert len(fake._recent_audio_signatures) == 3


def test_disabled_repeat_guard_does_not_call_waveform_checks():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    def fail(*args):
        raise AssertionError("never call classifier when disabled")
    scope["classify_audio_repeat"] = fail
    fake = fake_engine(["/tmp/normal.wav"], [5.0],
                       config={"enable_tts_audio_repeat_guard": False})
    assert attempt(fake, fn=fn) == "/tmp/normal.wav"


def test_retry_empty_audio_then_succeed():
    fn, scope = engine_method("_attempt_synthesis_on_server")
    scope["classify_audio_repeat"] = lambda *args: (None, None)
    fake = fake_engine([None, "/tmp/normal.wav"], [0.0, 5.0])
    assert attempt(fake, fn=fn) == "/tmp/normal.wav"
    assert fake.stats["empty_result_retries"] == 1
