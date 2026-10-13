"""Regression tests for optional automatic speech pauses (without AstrBot import)."""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def helper():
    tree = ast.parse((ROOT / "tts_engine.py").read_text(encoding="utf-8"))
    func = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "inject_natural_pauses")
    scope = {"re": re, "MAX_NATURAL_PAUSE_MS": 1200}
    scope["NATURAL_PAUSE_BOUNDARY_PATTERN"] = re.compile(r"""[。！？!?…]+[」』】）》”’"']*|[、，,]+""")
    scope["has_pronounceable"] = lambda text: bool(re.search(r"[^\W_]+", re.sub(r"\[pause=\d+\]", "", text)))
    exec(compile(ast.Module(body=[func], type_ignores=[]), "tts_engine.py", "exec"), scope)
    return scope["inject_natural_pauses"]


def test_pauses_do_not_change_single_sentence():
    fn = helper()
    assert fn("こんにちは。") == "こんにちは。"
    assert fn("今日は晴れ。", 500, 160) == "今日は晴れ。"
    assert fn("", 500, 160) == ""


def test_sentence_and_soft_pause():
    fn = helper()
    assert fn("こんにちは。今日はいい天気ですね！", 550, 160) == "こんにちは。[pause=550]今日はいい天気ですね！"
    assert fn("こんにちは、今日は晴れ。", 550, 160) == "こんにちは、[pause=160]今日は晴れ。"
    assert fn("えっ！？本当に？", 550, 160) == "えっ！？[pause=550]本当に？"
    assert fn("『そうです。』次です。", 550, 160) == "『そうです。』[pause=550]次です。"


def test_preserve_existing_markers_and_disabled_pause():
    fn = helper()
    assert fn("こんにちは。[pause=600]今日は晴れ。", 550, 160) == "こんにちは。[pause=600]今日は晴れ。"
    assert fn("こんにちは。今日は晴れ。", 0, 0) == "こんにちは。今日は晴れ。"
    assert fn("やあ！[pause=300]またね。", 550, 160) == "やあ！[pause=300]またね。"


def test_boundaries_and_clamps():
    fn = helper()
    assert fn("ひとつ。ふたつ。", 9999, 160) == "ひとつ。[pause=1200]ふたつ。"
    assert fn("ひとつ。ふたつ。", -1, 0) == "ひとつ。ふたつ。"
    assert fn("ひとつ。ふたつ。", "invalid", 160) == "ひとつ。ふたつ。"
    assert fn("3.14、いいね！") == "3.14、[pause=160]いいね！"


def test_config_opt_in_and_engine_used_for_all_synthesis_paths():
    cfg = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert cfg["enable_natural_pauses"]["default"] is False
    assert cfg["natural_pause_sentence_ms"]["default"] == 500
    engine = (ROOT / "tts_engine.py").read_text(encoding="utf-8")
    assert 'self.config.get("enable_natural_pauses", False) and self.config.get("tts_server_pause_markers_supported", False) is True' in engine
    assert "paused_text = inject_natural_pauses(" in engine
