"""The Modal backend: the same sandbox contract, run on Modal Sandboxes.

Five differences from local Docker are structural, not incidental, and every one
of them is recorded in the run manifest rather than glossed over:

1. **No second container.** Locally the egress broker is its own container on the
   agent's internal network. A Modal Sandbox is one container, so the broker runs
   inside it on loopback. It still holds the upstream credential and still
   injects the ``Authorization`` header, but ``allow_hosts`` becomes *advisory*:
   the agent could talk to the broker's port directly. Modal's own
   ``outbound_domain_allowlist`` / ``block_network`` is the enforcing layer.
2. **gVisor drops NET_ADMIN, NET_RAW and SYS_PTRACE.** The in-container iptables
   firewall cannot be applied, so ``SANDBOX_FIREWALL`` is forced to ``warn``;
   ``nmap -sS``, ``ping`` and ``tcpdump`` behave differently for a participant.
3. **No create-then-copy-then-start.** The container starts immediately, so
   ``codex_modal`` and the Codex CLI must already be in a pinned image.
4. **``exec`` has no ``user=``.** Anything meant to run unprivileged is wrapped
   in ``gosu``; the sandbox's own file API writes as root, so writes are chowned.
5. **Inbound is native.** ``encrypted_ports`` plus ``tunnels()`` publishes a port
   directly, so the local bridge-and-connector pair has nothing to do here.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import (
    Backend,
    BackendOptions,
    Capabilities,
    ExecHandle,
    Sandbox,
    SandboxError,
    Service,
    ServiceSpec,
)

BROKER_PORT = 3128
MODEL_PORT = 8081
DEFAULT_APP_NAME = "codex-modal-sandboxes"


def _modal() -> Any:
    try:
        import modal
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise SandboxError(
            "The modal backend needs the `modal` package (pip install modal)."
        ) from error
    return modal


class ModalExecHandle(ExecHandle):
    """A ``ContainerProcess`` running inside a Modal Sandbox."""

    def __init__(
        self,
        argv: Sequence[str],
        process: Any,
        *,
        log_path: Path | None = None,
        capture: bool = True,
    ) -> None:
        super().__init__(argv)
        self._process = process
        self._log_path = log_path
        self._capture = capture
        self._drained = False

    def poll(self) -> int | None:
        code = self._process.poll()
        if code is not None:
            self.exit_code = int(code)
        return self.exit_code

    def wait(self, timeout_s: float | None = None) -> int:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            code = self._process.poll()
            if code is not None:
                self.exit_code = int(code)
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"{' '.join(self.argv[:3])} exceeded {timeout_s}s")
            time.sleep(0.5)
        self._drain()
        return int(self.exit_code or 0)

    def kill(self) -> None:
        try:
            self._process.terminate()
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass

    def _drain(self) -> None:
        if self._drained:
            return
        self._drained = True
        try:
            self.stdout = self._process.stdout.read() or ""
            self.stderr = self._process.stderr.read() or ""
        except Exception:  # noqa: BLE001 - streams close with the container
            pass
        if self._log_path is not None and (self.stdout or self.stderr):
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(self.stdout)
                stream.write(self.stderr)


class ModalSandbox(Sandbox):
    backend = "modal"

    def __init__(
        self,
        *,
        sandbox: Any,
        label: str,
        spec: Any,
        options: Any,
        export_directory: Path,
        image_reference: str,
    ) -> None:
        self._sandbox = sandbox
        self.label = label
        self.name = f"modal-{sandbox.object_id}"
        self.spec = spec
        self.options = options
        self.export_directory = export_directory
        self.image_reference = image_reference
        self._published: dict[int, str] = {}
        self._cleaned = False

    # ---- verbs -----------------------------------------------------------

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
        command = [str(item) for item in argv]
        # Modal's exec has no `user=`; gosu is the image's own drop-privileges
        # helper and is already how agent-init.sh hands control to the agent.
        if user and user != "root":
            command = ["gosu", user, *command]
        kwargs: dict[str, Any] = {"text": True}
        if workdir:
            kwargs["workdir"] = workdir
        if env:
            kwargs["env"] = dict(env)
        if timeout_s is not None and not background:
            kwargs["timeout"] = int(timeout_s)
        process = self._sandbox.exec(*command, **kwargs)
        if stdin is not None:
            process.stdin.write(stdin.decode("utf-8", "replace"))
            process.stdin.write_eof()
            process.stdin.drain()
        handle = ModalExecHandle(argv, process, log_path=log_path)
        if background:
            return handle
        handle.wait(timeout_s)
        return handle

    def read_text(self, path: str) -> str:
        handle = self.exec(
            ["sh", "-c", f"cat {shlex.quote(path)} 2>/dev/null || true"],
            user="agent",
            timeout_s=60.0,
        )
        return handle.stdout if handle.exit_code == 0 else ""

    def write_bytes(self, path: str, data: bytes, *, owner: str | None = None) -> None:
        directory = os.path.dirname(path) or "/"
        self.exec(["sh", "-c", f"mkdir -p {shlex.quote(directory)}"], user="root", timeout_s=60.0)
        with self._sandbox.open(path, "wb") as stream:
            stream.write(data)
        # The file API writes as root; anything the agent must read (or run) is
        # handed over explicitly rather than left root-owned.
        self.exec(["chown", owner or "agent:agent", path], user="root", timeout_s=60.0)

    def download(self, remote: str, local: Path) -> bool:
        """Stream a sandbox directory out as a tar and unpack it locally."""

        local.mkdir(parents=True, exist_ok=True)
        source = remote.rstrip("/.") or "/"
        process = self._sandbox.exec(
            "sh",
            "-c",
            f"tar -C {shlex.quote(source)} -cf - . 2>/dev/null",
            text=False,
        )
        payload = process.stdout.read()
        if process.wait() != 0 or not payload:
            return False
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                archive.extractall(local)
        except (tarfile.TarError, OSError):
            return False
        return True

    def expose_port(self, port: int) -> str:
        if port in self._published:
            return self._published[port]
        tunnels = self._sandbox.tunnels()
        tunnel = tunnels.get(port)
        if tunnel is None:
            raise SandboxError(
                f"Port {port} was not declared in encrypted_ports when the sandbox "
                "was opened; Modal can only publish ports named at create time."
            )
        self._published[port] = tunnel.url
        return tunnel.url

    def stop(self, *, timeout_s: float = 10.0) -> None:
        try:
            self._sandbox.terminate()
        except Exception:  # noqa: BLE001 - already gone is success
            pass

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.download("/sandbox-state/.", self.export_directory / "codex-state")
            if getattr(self.options, "export_work", False):
                self.download("/work/.", self.export_directory / "work")
            self._write_manifest()
        finally:
            self.stop()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend="modal",
            net_admin=False,
            net_raw=False,
            sys_ptrace=False,
            egress_broker=True,
            host_reachable=False,
        )

    def _write_manifest(self) -> None:
        document = {
            "run_id": self._sandbox.object_id,
            "label": self.label,
            "backend": "modal",
            "agent_image": self.image_reference,
            "egress_image": self.image_reference,
            "model": self.spec.settings.display_model,
            "model_slug": self.spec.settings.slug,
            "upstream": self.spec.upstream_url,
            "upstream_credential": (
                "injected by broker" if self.spec.upstream_authorization else "none"
            ),
            "allow_ports": list(getattr(self.options, "allow_ports", ())),
            "allow_hosts": list(getattr(self.options, "allow_hosts", ())),
            "firewall": "warn",
            "published": dict(self._published),
            "capabilities": self.capabilities().as_dict(),
            "notes": [
                "gVisor drops NET_ADMIN/NET_RAW/SYS_PTRACE: raw sockets and ptrace differ",
                "the egress broker is on loopback, so allow_hosts is advisory",
            ],
        }
        path = self.export_directory / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


class ModalBackend(Backend):
    name = "modal"

    def __init__(
        self,
        *,
        options: BackendOptions | None = None,
        app_name: str = DEFAULT_APP_NAME,
        project_root: Path | None = None,
        url_acceptable: Callable[[str], bool] | None = None,
    ) -> None:
        self.options = options or BackendOptions()
        self.app_name = app_name
        self.project_root = (project_root or Path.cwd()).resolve()
        self.url_acceptable = url_acceptable
        self._app: Any = None
        self._services: list[Any] = []

    # ---- lifecycle -------------------------------------------------------

    def _app_handle(self) -> Any:
        if self._app is None:
            self._app = _modal().App.lookup(self.app_name, create_if_missing=True)
        return self._app

    def _agent_image(self) -> Any:
        reference = self.options.image_reference
        if not reference:
            raise SandboxError(
                "The modal backend needs a pinned agent image. Build and push "
                "codex_modal/docker/assets/Dockerfile.agent (with codex_modal baked "
                "in) and pass its digest as BackendOptions.image_reference."
            )
        # agent-init.sh forwards "$@" to container_entry rather than exec'ing it,
        # so an inherited ENTRYPOINT would swallow every command we send.
        return _modal().Image.from_registry(reference).entrypoint([])

    def open_sandbox(
        self,
        spec: Any,
        options: Any,
        *,
        label: str = "",
        expose_ports: Sequence[int] = (),
    ) -> ModalSandbox:
        modal = _modal()
        image = self._agent_image()
        export_directory = options.export_dir or (
            self.project_root / "state" / "modal-runs" / (label or "sandbox")
        )
        export_directory.mkdir(parents=True, exist_ok=True)

        create_kwargs: dict[str, Any] = {
            "app": self._app_handle(),
            "image": image,
            "timeout": int(getattr(options, "timeout_s", 0) or 3600),
            "idle_timeout": int(self.options.idle_timeout_s),
            "workdir": "/work",
            "encrypted_ports": [int(port) for port in expose_ports],
            "verbose": False,
        }
        if self.options.region:
            create_kwargs["region"] = self.options.region
        allow_hosts = tuple(getattr(options, "allow_hosts", ()) or ())
        if allow_hosts:
            # Modal's own allowlist is the enforcing layer here, because the
            # broker sits on loopback rather than behind an internal network.
            create_kwargs["outbound_domain_allowlist"] = list(allow_hosts)
        elif not getattr(options, "allow_ports", ()):
            create_kwargs["block_network"] = True

        sandbox = modal.Sandbox.create("sleep", "infinity", **create_kwargs)
        handle = ModalSandbox(
            sandbox=sandbox,
            label=label,
            spec=spec,
            options=options,
            export_directory=export_directory,
            image_reference=str(self.options.image_reference),
        )
        try:
            self._provision(handle, spec, options)
        except Exception:
            handle.cleanup()
            raise
        return handle

    def _provision(self, handle: ModalSandbox, spec: Any, options: Any) -> None:
        from dataclasses import replace

        from ..docker.sandbox import (
            _write_run_spec,  # local import: docker CLI not needed
        )

        staging = handle.export_directory / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        # The broker listens on loopback inside this same container, and the
        # environment document must say so rather than describing local Docker.
        run_spec = _write_run_spec(
            staging, spec, replace(options, backend="modal"), "127.0.0.1"
        )
        handle.write_bytes("/sandbox/run.json", run_spec.read_bytes(), owner="root:root")
        if getattr(options, "copy_in", None) is not None:
            self._upload_tree(handle, Path(options.copy_in), "/sandbox/copy-in")

        broker_environment = {
            "EGRESS_PORT": str(BROKER_PORT),
            "MODEL_PORT": str(MODEL_PORT),
            "EGRESS_ALLOW_PORTS": ",".join(
                str(port) for port in getattr(options, "allow_ports", ())
            ),
            "EGRESS_ALLOW_HOSTS": ",".join(getattr(options, "allow_hosts", ())),
            "MODEL_UPSTREAM": spec.upstream_url,
            "MODEL_PREFIX": "/v1",
        }
        if spec.upstream_authorization:
            broker_environment["MODEL_AUTHORIZATION"] = spec.upstream_authorization
        handle.exec(
            ["python3", "/opt/codex-modal/codex_modal/docker/assets/egress_proxy.py"],
            user="root",
            env=broker_environment,
            background=True,
            log_path=handle.export_directory / "egress.jsonl",
        )
        # gVisor has no NET_ADMIN, so the in-container iptables layer cannot be
        # applied; Modal's network policy is the boundary and we say so.
        handle.exec(
            ["/sandbox/agent-init.sh"],
            user="root",
            env={
                "SANDBOX_PROXY_IP": "127.0.0.1",
                "SANDBOX_PROXY_PORT": str(BROKER_PORT),
                "SANDBOX_MODEL_PORT": str(MODEL_PORT),
                "SANDBOX_FIREWALL": "warn",
                "CODEX_MODAL_STATE_ROOT": "/sandbox-state",
                "HTTP_PROXY": f"http://127.0.0.1:{BROKER_PORT}",
                "HTTPS_PROXY": f"http://127.0.0.1:{BROKER_PORT}",
                "NO_PROXY": "localhost,127.0.0.1",
            },
            background=True,
            log_path=handle.export_directory / "agent-console.log",
        )

    def _upload_tree(self, handle: ModalSandbox, source: Path, remote: str) -> None:
        source = source.resolve()
        if not source.is_dir():
            raise SandboxError(f"copy_in path {source} is not a directory.")
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            target = f"{remote}/{path.relative_to(source).as_posix()}"
            handle.write_bytes(target, path.read_bytes(), owner="agent:agent")

    # ---- services --------------------------------------------------------

    def serve(self, spec: ServiceSpec) -> Service:
        """Run a small HTTP service in its own Modal Sandbox and publish it."""

        modal = _modal()
        port = 8000
        source_root = (spec.source_root or self.project_root).resolve()
        image = (
            modal.Image.debian_slim()
            .pip_install(*spec.packages)
            .add_local_dir(str(source_root), remote_path="/srv", copy=True)
        )
        arguments = [item.replace("{port}", str(port)) for item in spec.arguments]
        sandbox = modal.Sandbox.create(
            "python",
            "-m",
            spec.module,
            *arguments,
            app=self._app_handle(),
            image=image,
            workdir="/srv",
            timeout=int(self.options.idle_timeout_s) + 3600,
            encrypted_ports=[port],
            verbose=False,
            **({"region": self.options.region} if self.options.region else {}),
        )
        url = sandbox.tunnels()[port].url
        if self.url_acceptable is not None and not self.url_acceptable(url):
            sandbox.terminate()
            raise SandboxError(f"Modal published an unacceptable hostname for {spec.name}: {url}")
        self._services.append(sandbox)
        return Service(name=spec.name, url=url, port=port, _stop=sandbox.terminate)

    def close(self) -> None:
        for sandbox in reversed(self._services):
            try:
                sandbox.terminate()
            except Exception:  # noqa: BLE001 - teardown must not mask a run error
                pass
        self._services.clear()


__all__ = ["ModalBackend", "ModalExecHandle", "ModalSandbox"]
