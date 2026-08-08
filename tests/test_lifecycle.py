from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_modal import lifecycle


class LifecycleTests(unittest.TestCase):
    def test_current_process_has_stable_identity(self) -> None:
        identity = lifecycle.process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity, lifecycle.process_identity(os.getpid()))

    def test_watchdog_stops_only_recorded_exact_id(self) -> None:
        endpoint_id = "ep-" + "A" * 22
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / f"owned-{endpoint_id}.json"
            cancel_path = root / "cancel"
            state_path.write_text(
                json.dumps(
                    {
                        "endpoint_id": endpoint_id,
                        "endpoint_name": "test-endpoint",
                        "environment": "test",
                        "owner_pid": os.getpid(),
                        "owner_identity": "not-the-current-process",
                        "cancel_path": str(cancel_path),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(lifecycle, "RUNTIME_ROOT", root),
                patch.object(lifecycle.modal_cli, "stop_endpoint", return_value=True) as stop,
            ):
                self.assertEqual(lifecycle.watchdog_main(state_path), 0)
            stop.assert_called_once_with(endpoint_id, "test", quiet=True)
            self.assertFalse(state_path.exists())
            self.assertTrue(cancel_path.exists())

    def test_invalid_endpoint_state_is_never_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "owned-bad.json"
            state_path.write_text(
                json.dumps({"endpoint_id": "not-an-endpoint", "owner_pid": 0}),
                encoding="utf-8",
            )
            with patch.object(lifecycle.modal_cli, "stop_endpoint") as stop:
                self.assertEqual(lifecycle.watchdog_main(state_path), 0)
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
