"""ModelScope Studio/Genie auth regression tests without AstrBot runtime."""
import ast
import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def _get_auth_helper():
    tree = ast.parse((ROOT / "tts_engine.py").read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_modelscope_auth_headers")
    scope = {"os": os, "urlparse": urlparse}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "tts_engine.py", "exec"), scope)
    return scope[fn.name]


def test_schema_has_modelscope_server_and_token_field():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "modelscope_api_token" in schema
    assert schema["tts_servers"]["default"] == ["https://your-space.ms.show"]
    assert schema["llm_injection_settings"]["items"]["translation_workflow"]["default"] in ("llm_injection", "provider_translation")


def test_auth_only_for_inference_host(monkeypatch):
    helper = _get_auth_helper()
    monkeypatch.setenv("MODELSCOPE_API_TOKEN", "env_secret")
    assert helper("https://foo.api-inference.modelscope.net", {}) == {"Authorization": "Bearer env_secret"}
    assert helper("https://api-inference.modelscope.net", {"modelscope_api_token": "preferred"}) == {"Authorization": "Bearer preferred"}
    for url in (
        "https://your-space.ms.show", "https://example.com", "https://api-inference.modelscope.net.attacker.org",
        "https://api-inference.modelscope.net@attacker.org", "http://api-inference.modelscope.net",
        "https://maliciousapi-inference.modelscope.net", "https://api-inference.modelscope.net.evil.ms.show",
    ):
        assert helper(url, {"modelscope_api_token": "do_not_leak"}) == {}, url


def test_genie_endpoints_use_auth_headers():
    tree = ast.parse((ROOT / "tts_engine.py").read_text(encoding="utf-8"))
    source = ast.unparse(tree)
    assert source.count("headers=_modelscope_auth_headers(server_url, self.config)") == 3
    assert "set_reference_audio" in source and "/tts" in source and "/ui/options" in source


def test_legacy_custom_features_exposed():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["enable_private_tts_by_default"]["default"] is True
    assert schema["llm_injection_settings"]["items"]["enable_provider_emotion_autofill"]["default"] is False
    assert "character_default_emotions" in schema
    main=(ROOT / "main.py").read_text(encoding="utf-8")
    assert 'filter.command("tts-role"' in main
    assert '"inactive_private_sessions": sorted(self.inactive_private_sessions)' in main
