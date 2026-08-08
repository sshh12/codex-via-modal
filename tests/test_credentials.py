from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codex_modal.credentials import from_environment, parse_combined


class CredentialTests(unittest.TestCase):
    def test_combined_proxy_token(self) -> None:
        credential = parse_combined("wk-abc.ws-secret")
        self.assertEqual(credential.token_id, "wk-abc")
        self.assertEqual(credential.token_secret, "ws-secret")
        self.assertEqual(credential.combined, "wk-abc.ws-secret")

    def test_invalid_proxy_token_is_rejected(self) -> None:
        for value in ("", "wk-abc", "abc.ws-secret", "wk-a.bad", "wk-a.ws-b\nextra"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_combined(value)

    def test_combined_environment_token_wins(self) -> None:
        environment = {
            "MODAL_PROXY_TOKEN": "wk-combined.ws-secret",
            "MODAL_PROXY_TOKEN_ID": "wk-separate",
            "MODAL_PROXY_TOKEN_SECRET": "ws-other",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(from_environment().combined, "wk-combined.ws-secret")

    def test_separate_environment_token(self) -> None:
        environment = {
            "MODAL_PROXY_TOKEN_ID": "wk-separate",
            "MODAL_PROXY_TOKEN_SECRET": "ws-other",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(from_environment().combined, "wk-separate.ws-other")


if __name__ == "__main__":
    unittest.main()
