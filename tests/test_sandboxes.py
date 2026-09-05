"""The sandbox handle API: the fake backend, and the contract every backend keeps.

These do not need Docker or Modal. They pin the shape ``bgr`` depends on - open,
exec, read/write, expose_port, serve, cleanup - and the backend-aware environment
document, which is what makes a local-vs-Modal equivalence test meaningful rather
than a comparison of two different lies about the network.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from codex_modal.container_entry import _environment_doc, _settings
from codex_modal.sandboxes import open_backend
from codex_modal.sandboxes.base import Capabilities, Sandbox


class _Options:
    export_dir = None


class FakeBackendTests(unittest.TestCase):
    def test_open_exec_and_readwrite(self) -> None:
        backend = open_backend("fake")
        sandbox = backend.open_sandbox(None, _Options(), label="blue")
        self.assertIsInstance(sandbox, Sandbox)

        # The prepared command line is present so a caller's barrier can proceed.
        self.assertTrue(sandbox.read_text(sandbox.COMMAND_FILE).strip())

        sandbox.write_bytes("/work/x", b"hello")
        self.assertEqual(sandbox.read_text("/work/x"), "hello")

        handle = sandbox.exec(["echo", "hi"], background=False)
        self.assertEqual(handle.poll(), 0)
        self.assertEqual(sandbox.calls[-1], ("echo", "hi"))

    def test_expose_port_serves_something_real(self) -> None:
        import httpx

        backend = open_backend("fake")
        sandbox = backend.open_sandbox(None, _Options(), label="blue")
        url = sandbox.expose_port(8080)
        self.assertEqual(httpx.get(url + "/").status_code, 200)
        # Idempotent: asking again returns the same URL, not a second server.
        self.assertEqual(sandbox.expose_port(8080), url)
        sandbox.stop()

    def test_unhealthy_service_fails_the_barrier(self) -> None:
        import httpx

        backend = open_backend("fake")
        backend.healthy = False
        sandbox = backend.open_sandbox(None, _Options(), label="blue")
        url = sandbox.expose_port(8080)
        self.assertEqual(httpx.get(url + "/").status_code, 503)
        sandbox.stop()

    def test_run_codex_invokes_the_behaviour(self) -> None:
        seen = {}

        def behaviour(sandbox) -> int:
            seen["label"] = sandbox.label
            return 0

        backend = open_backend("fake", behaviours={"red-01": behaviour})
        sandbox = backend.open_sandbox(None, _Options(), label="red-01")
        handle = sandbox.run_codex(log_path=Path("/tmp/ignored-by-fake"), background=False)
        self.assertEqual(handle.poll(), 0)
        self.assertEqual(seen["label"], "red-01")

    def test_capabilities_are_declared(self) -> None:
        backend = open_backend("fake")
        sandbox = backend.open_sandbox(None, _Options(), label="blue")
        caps = sandbox.capabilities()
        self.assertIsInstance(caps, Capabilities)
        self.assertEqual(caps.backend, "fake")

    def test_unknown_backend_fails_closed(self) -> None:
        from codex_modal.sandboxes.base import SandboxError

        with self.assertRaises(SandboxError):
            open_backend("nonsense")


class EnvironmentDocTests(unittest.TestCase):
    """The env doc must describe the isolation actually in force."""

    def test_local_and_modal_docs_differ_on_raw_sockets(self) -> None:
        local = _environment_doc({"backend": "local", "allow_ports": [80, 443]})
        modal = _environment_doc({"backend": "modal", "allow_ports": [80, 443]})
        self.assertIn("available", local)
        self.assertIn("NET_RAW", local)
        # gVisor drops NET_RAW/NET_ADMIN/SYS_PTRACE, and the doc must say so
        # rather than repeat local Docker's claim.
        self.assertIn("unavailable", modal)
        self.assertNotEqual(local, modal)

    def test_settings_round_trip_new_fields(self) -> None:
        settings = _settings(
            {
                "slug": "m",
                "display_model": "M",
                "context_window": 40960,
                "reasoning_effort": "high",
                "reasoning_levels": ["low", "high"],
                "provider_base_url": "http://x/v1",
                "apply_patch": False,
                "subagents": True,
                "temperature": 0.7,
            }
        )
        self.assertFalse(settings.apply_patch)
        self.assertTrue(settings.subagents)
        self.assertEqual(settings.temperature, 0.7)


if __name__ == "__main__":
    unittest.main()
