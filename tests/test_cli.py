from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from codex_modal import cli, modal_cli


class CliTests(unittest.TestCase):
    def test_direct_route_clamps_reasoning_to_sglang_streaming_schema(self) -> None:
        effort, levels = cli._direct_reasoning_settings(
            "max", ("low", "high", "max")
        )
        self.assertEqual(effort, "high")
        self.assertEqual(levels, ("low", "high"))

    def test_current_workspace_slug_is_parsed_without_exposing_token_details(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Token: ak-redacted\n"
                "Workspace: sshh12 (ac-ABCDEFGHIJKLMNOPQRSTUVWXYZ)\n"
                "User: user (us-ABCDEFGHIJKLMNOPQRSTUVWXYZ)\n"
            ),
            stderr="",
        )
        with patch.object(modal_cli, "_completed", return_value=result):
            self.assertEqual(modal_cli.current_workspace_slug(), "sshh12")


if __name__ == "__main__":
    unittest.main()
