from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest.mock import patch

from codex_modal.codex_config import (
    BASE_CONFIG,
    ModelSettings,
    guard_overrides,
    prepare_run_configuration,
)
from codex_modal.paths import PROJECT_ROOT


@unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
class CodexConfigurationTests(unittest.TestCase):
    def test_installed_codex_accepts_one_model_profile(self) -> None:
        settings = ModelSettings(
            slug="test-endpoint.us-west.modal.direct",
            display_model="org/test-model",
            context_window=262_144,
            reasoning_effort="high",
            reasoning_levels=("low", "high"),
            provider_base_url="https://inference.us-west.modal.direct/v1",
            persist_history=False,
        )
        injected = {
            "OPENAI_API_KEY": "must-not-survive",
            "CODEX_API_KEY": "must-not-survive",
            "CODEX_PERMISSION_PROFILE": ":ambient-managed-profile",
            "CODEX_THREAD_ID": "ambient-thread-id",
            "OPENAI_BASE_URL": "https://example.invalid",
            "MODAL_TOKEN_SECRET": "must-not-survive",
        }
        with patch.dict(os.environ, injected, clear=False):
            configuration = prepare_run_configuration(
                settings,
                workspace=PROJECT_ROOT,
                proxy_token="wk-test.ws-secret",
            )
        try:
            catalog = json.loads(configuration.catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(catalog["models"]), 1)
            self.assertEqual(catalog["models"][0]["slug"], settings.slug)
            self.assertEqual(catalog["models"][0]["apply_patch_tool_type"], "freeform")
            self.assertEqual(catalog["models"][0]["input_modalities"], ["text"])

            profile = configuration.profile_path.read_text(encoding="utf-8")
            self.assertIn('model_provider = "modal"', profile)
            self.assertIn('wire_api = "responses"', profile)
            self.assertIn('persistence = "none"', profile)

            command = configuration.command_prefix()
            self.assertIn("--strict-config", command)
            self.assertIn("--profile", command)
            self.assertEqual(
                configuration.environment["MODAL_PROXY_TOKEN"], "wk-test.ws-secret"
            )
            for key in injected:
                self.assertNotIn(key, configuration.environment)
        finally:
            configuration.clean_up()

    def test_guards_pin_models_and_disable_exporters(self) -> None:
        settings = ModelSettings(
            slug="only-model.us-west.modal.direct",
            display_model="org/model",
            context_window=131_072,
            reasoning_effort="high",
            reasoning_levels=("low", "high"),
            provider_base_url="https://inference.us-west.modal.direct/v1",
            persist_history=True,
        )
        values = dict(guard_overrides(settings, PROJECT_ROOT / "catalog.json"))
        self.assertEqual(values["model"], '"only-model.us-west.modal.direct"')
        self.assertEqual(values["review_model"], '"only-model.us-west.modal.direct"')
        self.assertEqual(values["model_provider"], '"modal"')
        self.assertEqual(values["agents.enabled"], "false")
        self.assertEqual(values["features.guardian_approval"], "false")
        self.assertEqual(values["features.remote_compaction_v2"], "false")
        self.assertEqual(values["otel.exporter"], '"none"')
        self.assertEqual(values["otel.metrics_exporter"], '"none"')
        self.assertEqual(values["otel.trace_exporter"], '"none"')

    def test_base_config_has_only_modal_provider_and_telemetry_off(self) -> None:
        self.assertEqual(BASE_CONFIG.count("[model_providers."), 1)
        self.assertIn("[model_providers.modal]", BASE_CONFIG)
        self.assertIn("[analytics]\nenabled = false", BASE_CONFIG)
        self.assertIn('metrics_exporter = "none"', BASE_CONFIG)
        self.assertIn('trace_exporter = "none"', BASE_CONFIG)


if __name__ == "__main__":
    unittest.main()
