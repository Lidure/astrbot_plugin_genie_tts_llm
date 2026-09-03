import ast
import unittest
from pathlib import Path


# Regression guard for AstrBot/provider-injected transport metadata such as _ref.
class LlmToolExtraMetadataTests(unittest.TestCase):
    def test_genie_tts_speak_accepts_unknown_tool_metadata(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        module = ast.parse(main_path.read_text(encoding="utf-8"))

        plugin_class = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "GenieTtsLlmPlugin"
        )
        tool_method = next(
            node
            for node in plugin_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "llm_tool_genie_tts_speak"
        )

        self.assertIsNotNone(
            tool_method.args.kwarg,
            "genie_tts_speak must accept unknown AstrBot/provider metadata such as _ref",
        )
        regular_parameter_names = {
            arg.arg
            for arg in (
                list(tool_method.args.posonlyargs)
                + list(tool_method.args.args)
                + list(tool_method.args.kwonlyargs)
            )
        }
        self.assertNotIn(
            "_ref",
            regular_parameter_names,
            "_ref is transport metadata and must not become a business parameter in the tool schema",
        )


if __name__ == "__main__":
    unittest.main()
