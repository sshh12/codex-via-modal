from __future__ import annotations

import unittest

from codex_modal.options import (
    assert_isolated_codex_arguments,
    codex_needs_inference,
    endpoint_target,
    load_presets,
    parse_arguments,
    resolve_model,
)


class OptionTests(unittest.TestCase):
    def test_default_is_deepseek_v4(self) -> None:
        options = parse_arguments([])
        model = resolve_model(options, load_presets())
        self.assertEqual(model.base_model, "deepseek-ai/DeepSeek-V4-Flash-0731")
        self.assertEqual(model.context_window, 1_000_000)
        self.assertEqual(model.reasoning_effort, "max")
        self.assertIn("max", model.reasoning_levels)

    def test_arbitrary_model_uses_conservative_defaults(self) -> None:
        options = parse_arguments(["--modal-model", "org/model"])
        model = resolve_model(options, load_presets())
        self.assertEqual(model.context_window, 131_072)
        self.assertEqual(model.reasoning_effort, "high")
        self.assertEqual(model.reasoning_levels, ("low", "medium", "high"))

    def test_custom_weights_keep_base_architecture(self) -> None:
        options = parse_arguments(
            [
                "--modal-model",
                "Qwen/Qwen3.6-27B",
                "--modal-custom-hf-repo",
                "acme/qwen-ft",
            ]
        )
        model = resolve_model(options, load_presets())
        self.assertEqual(model.base_model, "Qwen/Qwen3.6-27B")
        self.assertEqual(model.display_model, "acme/qwen-ft")

    def test_full_attached_hostname_selects_routing_region(self) -> None:
        options = parse_arguments(
            ["--modal-use-endpoint", "my-model.us-east.modal.direct"]
        )
        name, host, base_url = endpoint_target(options, "ignored/model")
        self.assertEqual(name, "my-model")
        self.assertEqual(host, "my-model.us-east.modal.direct")
        self.assertEqual(base_url, "https://inference.us-east.modal.direct/v1")

    def test_wrapper_actions_are_subcommands(self) -> None:
        self.assertEqual(parse_arguments(["setup", "--modal-env", "prod"]).action, "setup")
        self.assertEqual(parse_arguments(["cleanup"]).action, "cleanup")

    def test_existing_custom_app_is_an_attach_only_target(self) -> None:
        options = parse_arguments(
            [
                "--modal-use-app",
                "codex-custom-model-1234abcd",
                "--modal-model",
                "org/custom-model",
            ]
        )
        self.assertEqual(options.use_app, "codex-custom-model-1234abcd")
        self.assertFalse(options.self_managed)
        name, _, _ = endpoint_target(options, "org/custom-model")
        self.assertEqual(name, "codex-custom-model-1234abcd")

    def test_custom_app_attachment_cannot_redeploy(self) -> None:
        with self.assertRaises(ValueError):
            parse_arguments(
                [
                    "--modal-use-app",
                    "codex-custom-model-1234abcd",
                    "--modal-self-managed",
                    "--modal-gpu",
                    "B200:2",
                ]
            )

    def test_model_provider_escape_flags_are_blocked(self) -> None:
        blocked = [
            ["--model", "gpt-5.5"],
            ["--config", 'model_provider="openai"'],
            ["--profile", "other"],
            ["--oss"],
            ["--search"],
            ["exec", "--model=other", "hello"],
        ]
        for arguments in blocked:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    assert_isolated_codex_arguments(arguments)

    def test_external_codex_commands_are_blocked(self) -> None:
        for command in ("login", "cloud", "app", "mcp-server", "remote-control"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    assert_isolated_codex_arguments([command])

    def test_literal_after_double_dash_is_not_treated_as_override(self) -> None:
        assert_isolated_codex_arguments(["exec", "--", "--model", "is literal text"])

    def test_inference_classification(self) -> None:
        self.assertTrue(codex_needs_inference([]))
        self.assertTrue(codex_needs_inference(["exec", "hello"]))
        self.assertTrue(codex_needs_inference(["resume", "--last"]))
        self.assertFalse(codex_needs_inference(["debug", "models"]))
        self.assertFalse(codex_needs_inference(["features", "list"]))
        self.assertFalse(codex_needs_inference(["--version"]))

    def test_invalid_combinations_fail_early(self) -> None:
        with self.assertRaises(ValueError):
            parse_arguments(["--modal-context-window", "4096"])
        with self.assertRaises(ValueError):
            parse_arguments(
                ["--modal-colocate-compute", "--modal-compute-region", "us-west"]
            )
        with self.assertRaises(ValueError):
            parse_arguments(["--modal-custom-hf-revision", "main"])
        with self.assertRaises(ValueError):
            parse_arguments(["--modal-self-managed", "--modal-model", "org/model"])
        with self.assertRaises(ValueError):
            parse_arguments(["--modal-gpu", "H100"])
        with self.assertRaises(ValueError):
            parse_arguments(
                [
                    "--modal-self-managed",
                    "--modal-gpu",
                    "H100",
                    "--modal-base-volume",
                    "cache",
                ]
            )


if __name__ == "__main__":
    unittest.main()
