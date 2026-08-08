from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_modal import lifecycle


class LifecycleTests(unittest.TestCase):
    def test_direct_endpoint_url_matches_modal_server_naming(self) -> None:
        self.assertEqual(
            lifecycle.direct_endpoint_base_url("workspace", "my-endpoint", "us-west"),
            "https://workspace--ep-my-endpoint-server.us-west.modal.direct/v1",
        )

    def test_route_falls_back_to_direct_responses_capability(self) -> None:
        shared = "https://inference.us-west.modal.direct/v1"
        direct = "https://workspace--ep-model-server.us-west.modal.direct/v1"

        def document(url: str, _token: str) -> dict:
            if url == f"{shared}/models":
                return {"data": []}
            if url == f"{direct}/models":
                return {"data": [{"id": "org/model"}]}
            if url == direct.removesuffix("/v1") + "/openapi.json":
                return {"paths": {"/v1/responses": {}}}
            raise AssertionError(url)

        with patch.object(lifecycle, "_authenticated_json", side_effect=document):
            route = lifecycle._wait_for_available_route(
                endpoint_host="endpoint.us-west.modal.direct",
                shared_base_url=shared,
                direct_base_url=direct,
                preferred_model="org/model",
                proxy_token="wk-test.ws-test",
                deadline=time.monotonic() + 1,
                state_path=None,
            )

        self.assertEqual(route.model_slug, "org/model")
        self.assertEqual(route.base_url, direct)
        self.assertEqual(route.source, "direct")

    def test_live_status_advances_to_route_readiness(self) -> None:
        endpoint_id = "ep-" + "A" * 22
        expected = lifecycle.EndpointRoute(
            model_slug="org/model",
            base_url="https://example.invalid/v1",
            source="direct",
        )
        with (
            patch.object(
                lifecycle.modal_cli,
                "list_endpoints",
                return_value=[{"endpoint_id": endpoint_id, "status": "live"}],
            ),
            patch.object(
                lifecycle, "_wait_for_available_route", return_value=expected
            ) as wait,
        ):
            result = lifecycle.wait_for_endpoint(
                endpoint_id=endpoint_id,
                endpoint_host="endpoint.us-west.modal.direct",
                environment_name=None,
                shared_base_url="https://inference.us-west.modal.direct/v1",
                direct_base_url="https://workspace--ep-endpoint-server.us-west.modal.direct/v1",
                preferred_model="org/model",
                proxy_token="wk-test.ws-test",
                timeout_seconds=30,
                state_path=None,
            )

        self.assertEqual(result, expected)
        wait.assert_called_once()

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

    def test_watchdog_cleans_exact_app_and_owned_volume(self) -> None:
        app_id = "ap-" + "B" * 22
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / f"owned-{app_id}.json"
            state_path.write_text(
                json.dumps(
                    {
                        "resource_kind": "app",
                        "app_id": app_id,
                        "app_name": "test-app",
                        "volume_name": "cm-test-app",
                        "environment": "test",
                        "owner_pid": os.getpid(),
                        "owner_identity": "not-the-current-process",
                        "cancel_path": str(root / "cancel"),
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(lifecycle, "RUNTIME_ROOT", root),
                patch.object(lifecycle.modal_cli, "stop_app", return_value=True) as stop,
                patch.object(
                    lifecycle.modal_cli, "delete_owned_volume", return_value=True
                ) as delete,
            ):
                self.assertEqual(lifecycle.watchdog_main(state_path), 0)
            stop.assert_called_once_with(app_id, "test", quiet=True)
            delete.assert_called_once_with("cm-test-app", "test", quiet=True)
            self.assertFalse(state_path.exists())

    def test_app_id_resolution_ignores_stopped_same_name(self) -> None:
        active_id = "ap-" + "C" * 22
        stopped_id = "ap-" + "D" * 22
        rows = [
            {
                "app_id": stopped_id,
                "description": "same-name",
                "state": "stopped",
            },
            {
                "app_id": active_id,
                "description": "same-name",
                "state": "deployed",
            },
        ]
        with patch.object(lifecycle.modal_cli, "list_apps", return_value=rows):
            self.assertEqual(lifecycle.resolve_app_id("same-name", None), active_id)


if __name__ == "__main__":
    unittest.main()
