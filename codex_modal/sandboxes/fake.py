"""An in-process backend for testing a harness without Docker, Modal, or a model.

Nothing here talks to a container. ``exec`` records the command, ``expose_port``
stands up a one-line HTTP server on loopback so a caller's readiness barrier can
actually succeed or actually fail, and ``run_codex`` calls a Python behaviour the
test supplies for that participant. That makes a full multi-participant run - the
phase machine, the barrier, scoring, teardown - testable in under a second.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import (
    Backend,
    BackendOptions,
    Capabilities,
    ExecHandle,
    Sandbox,
    Service,
    ServiceSpec,
)

#: A behaviour is called with the sandbox once "Codex" starts, and returns the
#: exit code that participant should report.
Behaviour = Callable[["FakeSandbox"], int]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class FakeExecHandle(ExecHandle):
    def __init__(self, argv: Sequence[str], worker: Callable[[], int] | None = None) -> None:
        super().__init__(argv)
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        if worker is None:
            self.exit_code = 0
            self._done.set()
        else:
            self._thread = threading.Thread(target=self._run, args=(worker,), daemon=True)
            self._thread.start()

    def _run(self, worker: Callable[[], int]) -> None:
        try:
            self.exit_code = int(worker())
        except BaseException as error:  # noqa: BLE001 - surfaced as a non-zero exit
            self.stderr = f"{type(error).__name__}: {error}"
            self.exit_code = 1
        finally:
            self._done.set()

    def poll(self) -> int | None:
        return self.exit_code if self._done.is_set() else None

    def wait(self, timeout_s: float | None = None) -> int:
        if not self._done.wait(timeout=timeout_s):
            raise TimeoutError(f"{' '.join(self.argv[:3])} exceeded {timeout_s}s")
        return int(self.exit_code or 0)

    def kill(self) -> None:
        self._done.set()
        if self.exit_code is None:
            self.exit_code = 137


class _Echo(http.server.BaseHTTPRequestHandler):
    healthy = True

    def do_GET(self) -> None:
        if not type(self).healthy:
            self.send_error(503, "unavailable")
            return
        body = json.dumps({"status": "ok", "path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@dataclass
class FakeSandbox(Sandbox):
    """A sandbox that exists only as a dictionary and a thread."""

    label: str = ""
    export_directory: Path | None = None
    behaviour: Behaviour | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    backend: str = "fake"
    name: str = "fake"
    healthy: bool = True
    stopped: bool = False
    cleaned: bool = False
    _servers: list[Any] = field(default_factory=list)
    _published: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = f"fake-{self.label or 'sandbox'}"
        # cvm's container entry would write this; a caller that waits for it
        # must find it here too, or every fake run would hang at the barrier.
        self.files.setdefault(self.COMMAND_FILE, b"codex exec --skip-git-repo-check <prompt>\n")

    def exec(
        self,
        argv: Sequence[str],
        *,
        user: str = "agent",
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
        timeout_s: float | None = 60.0,
        stdin: bytes | None = None,
        background: bool = False,
        log_path: Path | None = None,
    ) -> ExecHandle:
        self.calls.append(tuple(str(item) for item in argv))
        handle = FakeExecHandle(argv)
        handle.stdout = ""
        return handle

    def read_text(self, path: str) -> str:
        return self.files.get(path, b"").decode("utf-8", "replace")

    def write_bytes(self, path: str, data: bytes, *, owner: str | None = None) -> None:
        self.files[path] = data

    def download(self, remote: str, local: Path) -> bool:
        local.mkdir(parents=True, exist_ok=True)
        return True

    def expose_port(self, port: int) -> str:
        if port in self._published:
            return self._published[port]
        handler = type("_Bound", (_Echo,), {"healthy": self.healthy})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        self._published[port] = url
        return url

    def run_codex(
        self,
        *,
        log_path: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        background: bool = True,
    ) -> ExecHandle:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"fake codex for {self.label}\n", encoding="utf-8", newline="\n")
        behaviour = self.behaviour
        worker = (lambda: behaviour(self)) if behaviour is not None else (lambda: 0)
        handle = FakeExecHandle(("codex", "exec"), worker)
        if not background:
            handle.wait(timeout_s)
        return handle

    def stop(self, *, timeout_s: float = 10.0) -> None:
        self.stopped = True
        for server in self._servers:
            server.shutdown()
        self._servers.clear()

    def cleanup(self) -> None:
        self.cleaned = True
        self.stop()
        if self.export_directory is not None:
            self.export_directory.mkdir(parents=True, exist_ok=True)
            (self.export_directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": self.name,
                        "label": self.label,
                        "backend": "fake",
                        "capabilities": self.capabilities().as_dict(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend="fake",
            net_admin=False,
            net_raw=False,
            sys_ptrace=False,
            egress_broker=False,
            host_reachable=True,
        )


class FakeBackend(Backend):
    """Hands out :class:`FakeSandbox` handles and runs services on loopback."""

    name = "fake"

    def __init__(
        self,
        *,
        options: BackendOptions | None = None,
        project_root: Path | None = None,
        behaviours: Mapping[str, Behaviour] | None = None,
        healthy: bool = True,
    ) -> None:
        self.options = options or BackendOptions()
        self.project_root = (project_root or Path.cwd()).resolve()
        self.behaviours: dict[str, Behaviour] = dict(behaviours or {})
        self.healthy = healthy
        self.sandboxes: list[FakeSandbox] = []
        self.services: list[Service] = []

    def open_sandbox(
        self,
        spec: Any,
        options: Any,
        *,
        label: str = "",
        expose_ports: Sequence[int] = (),
    ) -> FakeSandbox:
        export = getattr(options, "export_dir", None)
        sandbox = FakeSandbox(
            label=label,
            export_directory=Path(export) if export else None,
            behaviour=self.behaviours.get(label),
            healthy=self.healthy,
        )
        self.sandboxes.append(sandbox)
        return sandbox

    def serve(self, spec: ServiceSpec) -> Service:
        """Run the real service module on loopback - no tunnel, no container."""

        from .hosting import reserve_port, start_python_service

        port = reserve_port()
        arguments = [item.replace("{port}", str(port)) for item in spec.arguments]
        log_path = spec.log_path or (self.project_root / f"{spec.name}.log")
        process = start_python_service(
            spec.module,
            arguments,
            project_root=spec.source_root or self.project_root,
            log_path=log_path,
            port=port,
            health_path=spec.health_path,
        )
        service = Service(
            name=spec.name,
            url=f"http://127.0.0.1:{port}",
            port=port,
            _stop=process.stop,
        )
        self.services.append(service)
        return service

    def close(self) -> None:
        for service in reversed(self.services):
            service.stop()
        self.services.clear()


__all__ = ["Behaviour", "FakeBackend", "FakeExecHandle", "FakeSandbox"]
