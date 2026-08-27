import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelScopeGenieAuthContractTest(unittest.TestCase):
    def test_schema_exposes_modelscope_token(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("modelscope_api_token", schema)

    def test_tts_engine_scopes_bearer_to_modelscope_inference_hosts(self):
        source = (ROOT / "tts_engine.py").read_text(encoding="utf-8")
        self.assertIn("api-inference.modelscope.net", source)
        self.assertIn("MODELSCOPE_API_TOKEN", source)
        self.assertIn('"Authorization": f"Bearer {token}"', source)

    def test_both_genie_requests_receive_auth_headers(self):
        source = (ROOT / "tts_engine.py").read_text(encoding="utf-8")
        self.assertIn(
            'f"{server_url}/set_reference_audio", json=ref_payload, headers=auth_headers, timeout=60',
            source,
        )
        self.assertIn(
            '"POST", f"{server_url}/tts", json=tts_payload, headers=auth_headers, timeout=tts_timeout',
            source,
        )


if __name__ == "__main__":
    unittest.main()
