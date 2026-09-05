"""The local Docker backend in handle form.

Same two-container topology as ``docker/sandbox.py`` - agent on an ``--internal``
network, egress broker holding the upstream credential - but opened rather than
run: the container starts idling on ``sleep infinity`` with its Codex command
line already prepared, and the caller drives it with ``exec``.

That inversion is what lets a run keep the operator's container alive while other
participants come and go, and it removes the need to *discover* a container by
matching a PID hex suffix against ``docker ps``: the handle knows its own name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..docker.sandbox import (
    ASSETS as DOCKER_ASSETS,
)
from ..docker.sandbox import (
    MODEL_PORT,
    PROXY_PORT,
    DockerError,
    SandboxOptions,
    SandboxSpec,
    _create_networks,
    _export,
    _export_logs,
    _quiet,
    _run,
    _write_run_spec,
    assert_docker_available,
    ensure_images,
)
from ..paths import PROJECT_ROOT, STATE_ROOT
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
from .hosting import (
    ASSETS,
    ensure_cloudflared,
    reserve_port,
    start_process,
    start_python_service,
    start_tunnel,
)

CONNECTOR_REMOTE = "/tmp/.svc-connector.py"


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


class DockerExecHandle(ExecHandle):
    """A ``docker exec`` running against an open sandbox."""

    def __init__(
        self,
        argv: Sequence[str],
        process: subprocess.Popen[bytes],
        *,
        stream: Any = None,
        capture: bool = False,
    ) -> None:
        super().__init__(argv)
        self._process = process
        self._stream = stream
        self._capture = capture

    def poll(self) -> int | None:
        code = self._process.poll()
        if code is not None:
            self.exit_code = int(code)
        return self.exit_code

    def wait(self, timeout_s: float | None = None) -> int:
        try:
            if self._capture:
                out, err = self._process.communicate(timeout=timeout_s)
                self.stdout = (out or b"").decode("utf-8", "replace")
                self.stderr = (err or b"").decode("utf-8", "replace")
            else:
                self._process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"{' '.join(self.argv[:3])} exceeded {timeout_s}s") from error
        finally:
            self._close()
        self.exit_code = int(self._process.returncode)
        return self.exit_code

    def kill(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        self._close()

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None


class LocalSandbox(Sandbox):
    backend = "local"

    def __init__(
        self,
        *,
        run_id: str,
        label: str,
        spec: SandboxSpec,
        options: SandboxOptions,
        images: tuple[str, str],
        networks: tuple[str, str],
        addresses: tuple[str, str],
        export_directory: Path,
        state_root: Path,
        url_acceptable: Callable[[str], bool] | None,
    ) -> None:
        self.run_id = run_id
        self.label = label
        self.name = f"cmsbx-{run_id}-agent"
        self.egress_name = f"cmsbx-{run_id}-egress"
        self.prefix = f"cmsbx-{run_id}"
        self.spec = spec
        self.options = options
        self.images = images
        self.networks = networks
        self.addresses = addresses
        self.export_directory = export_directory
        self._state_root = state_root
        self._url_acceptable = url_acceptable
        self._published: dict[int, str] = {}
        self._owned: list[Any] = []
        self._started = True
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
        arguments = ["docker", "exec"]
        if stdin is not None:
            arguments.append("-i")
        if user:
            arguments.extend(["--user", user])
        for key, value in (env or {}).items():
            arguments.extend(["-e", f"{key}={value}"])
        if workdir:
            arguments.extend(["-w", workdir])
        arguments.append(self.name)
        arguments.extend(str(item) for item in argv)

        stream = None
        if background and log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stream = log_path.open("ab", buffering=0)
            stdout: Any = stream
            stderr: Any = stream
            capture = False
        else:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
            capture = True
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=_creation_flags(),
        )
        handle = DockerExecHandle(argv, process, stream=stream, capture=capture)
        if stdin is not None and process.stdin is not None and background:
            process.stdin.write(stdin)
            process.stdin.close()
        if background:
            return handle
        if stdin is not None and capture:
            out, err = process.communicate(input=stdin, timeout=timeout_s)
            handle.stdout = (out or b"").decode("utf-8", "replace")
            handle.stderr = (err or b"").decode("utf-8", "replace")
            handle.exit_code = int(process.returncode)
            return handle
        handle.wait(timeout_s)
        return handle

    def read_text(self, path: str) -> str:
        handle = self.exec(
            ["sh", "-c", f"cat {path} 2>/dev/null || true"],
            user="agent",
            timeout_s=30.0,
        )
        return handle.stdout if handle.exit_code == 0 else ""

    def write_bytes(self, path: str, data: bytes, *, owner: str | None = None) -> None:
        directory = os.path.dirname(path) or "/"
        handle = self.exec(
            ["sh", "-c", f"mkdir -p {directory} && cat > {path}"],
            user="root",
            stdin=data,
            timeout_s=120.0,
        )
        if handle.exit_code != 0:
            raise SandboxError(f"{self.name}: could not write {path}: {handle.stderr.strip()}")
        if owner:
            self.exec(["chown", owner, path], user="root", timeout_s=30.0)

    def download(self, remote: str, local: Path) -> bool:
        local.mkdir(parents=True, exist_ok=True)
        return _export(self.name, remote, local)

    def expose_port(self, port: int) -> str:
        """Publish ``port`` through a host bridge plus an in-sandbox connector."""

        if port in self._published:
            return self._published[port]
        import secrets as _secrets

        bridge_port = reserve_port()
        public_code = _secrets.token_hex(16)
        connector_code = _secrets.token_hex(24)
        logs = self.export_directory
        bridge = start_python_service(
            "codex_modal.sandboxes.bridge",
            [
                "--public-code",
                public_code,
                "--connector-code",
                connector_code,
                "--port",
                str(bridge_port),
            ],
            project_root=PROJECT_ROOT,
            log_path=logs / "publisher.log",
            port=bridge_port,
        )
        self._owned.append(bridge)
        tunnel = self._clean_tunnel(
            origin=f"http://127.0.0.1:{bridge_port}",
            log_path=logs / "publisher-tunnel.log",
        )
        self._owned.append(tunnel)

        source = (ASSETS / "connector.py").read_bytes()
        self.write_bytes(CONNECTOR_REMOTE, source, owner="agent:agent")
        self.exec(
            [
                "python3",
                CONNECTOR_REMOTE,
                "--relay",
                tunnel.url,
                "--code",
                connector_code,
                "--port",
                str(port),
            ],
            user="agent",
            background=True,
            log_path=logs / "connector.log",
        )
        url = f"{tunnel.url}/p/{public_code}"
        self._published[port] = url
        return url

    def stop(self, *, timeout_s: float = 10.0) -> None:
        if not self._started:
            return
        _run(
            ["stop", "--timeout", str(int(timeout_s)), self.name],
            check=False,
            timeout=timeout_s + 30,
        )
        self._started = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        for owned in reversed(self._owned):
            try:
                owned.stop()
            except Exception:  # noqa: BLE001 - teardown must not mask a run error
                pass
        self._owned.clear()
        try:
            _export_logs(self.name, self.export_directory / "agent-console.log")
            _export_logs(self.egress_name, self.export_directory / "egress.jsonl")
            _export(self.name, "/sandbox-state/.", self.export_directory / "codex-state")
            if self.options.export_work:
                _export(self.name, "/work/.", self.export_directory / "work")
            self._write_manifest()
        finally:
            if not self.options.keep:
                _quiet(["rm", "-f", self.name])
                _quiet(["rm", "-f", self.egress_name])
                for network in self.networks:
                    _quiet(["network", "rm", network])
                for suffix in ("home", "work", "state", "sandbox"):
                    _quiet(["volume", "rm", "-f", f"{self.prefix}-{suffix}"])

    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend="local",
            net_admin=True,
            net_raw=True,
            sys_ptrace=True,
            egress_broker=True,
            host_reachable=False,
        )

    # ---- internals -------------------------------------------------------

    def _clean_tunnel(self, *, origin: str, log_path: Path, attempts: int = 5) -> Any:
        """Open a tunnel whose hostname itself passes the caller's word gate.

        Quick-tunnel hostnames are drawn from an English word list, so one can
        happen to contain a term the caller's participants must never see.
        Discard those and ask for another rather than failing on a coin flip.
        """

        binary = ensure_cloudflared(self._state_root)
        for _ in range(attempts):
            tunnel = start_tunnel(
                binary=binary,
                origin=origin,
                health_path="/healthz",
                log_path=log_path,
                cwd=PROJECT_ROOT,
            )
            if self._url_acceptable is None or self._url_acceptable(tunnel.url):
                return tunnel
            tunnel.stop()
        raise SandboxError(f"No acceptable public hostname after {attempts} attempts for {origin}.")

    def _write_manifest(self) -> None:
        document = {
            "run_id": self.run_id,
            "label": self.label,
            "backend": "local",
            "agent_image": self.images[0],
            "egress_image": self.images[1],
            "model": self.spec.settings.display_model,
            "model_slug": self.spec.settings.slug,
            "upstream": self.spec.upstream_url,
            "upstream_credential": (
                "injected by broker" if self.spec.upstream_authorization else "none"
            ),
            "inner_network": self.networks[0],
            "edge_network": self.networks[1],
            "proxy_ip": self.addresses[0],
            "agent_ip": self.addresses[1],
            "allow_ports": list(self.options.allow_ports),
            "allow_hosts": list(self.options.allow_hosts),
            "firewall": self.options.firewall,
            "danger": self.options.danger,
            "published": dict(self._published),
            "capabilities": self.capabilities().as_dict(),
        }
        path = self.export_directory / "manifest.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


class LocalBackend(Backend):
    name = "local"

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        project_root: Path | None = None,
        options: BackendOptions | None = None,
        url_acceptable: Callable[[str], bool] | None = None,
    ) -> None:
        self.state_root = (state_root or STATE_ROOT).resolve()
        self.project_root = (project_root or PROJECT_ROOT).resolve()
        self.options = options or BackendOptions()
        self.url_acceptable = url_acceptable
        self._images: tuple[str, str] | None = None
        self._services: list[Any] = []

    def open_sandbox(
        self,
        spec: SandboxSpec,
        options: SandboxOptions,
        *,
        label: str = "",
        expose_ports: Sequence[int] = (),
    ) -> LocalSandbox:
        # expose_ports is accepted for signature parity; locally a port is
        # published on demand through the bridge, with nothing to reserve.
        assert_docker_available()
        if self._images is None:
            self._images = ensure_images(options)
        agent_image, egress_image = self._images

        import secrets as _secrets

        run_id = f"{int(time.time()):x}-{_secrets.token_hex(3)}"
        export_directory = options.export_dir or (self.state_root / "docker-runs" / run_id)
        export_directory.mkdir(parents=True, exist_ok=True)
        staging = export_directory / ".staging"
        staging.mkdir(parents=True, exist_ok=True)

        # An opened sandbox always idles; the command line Codex will run is
        # prepared into codex-command.txt either way, and the caller execs it.
        idle = replace(options, command=("sleep", "infinity"))

        inner, edge, proxy_ip, agent_ip = _create_networks(run_id)
        sandbox = LocalSandbox(
            run_id=run_id,
            label=label,
            spec=spec,
            options=idle,
            images=(agent_image, egress_image),
            networks=(inner, edge),
            addresses=(proxy_ip, agent_ip),
            export_directory=export_directory,
            state_root=self.state_root,
            url_acceptable=self.url_acceptable,
        )
        try:
            for suffix in ("home", "work", "state", "sandbox"):
                _run(["volume", "create", f"cmsbx-{run_id}-{suffix}"])
            self._create_egress(run_id, spec, idle, egress_image, inner, edge, proxy_ip)
            self._create_agent(run_id, idle, agent_image, inner, agent_ip, proxy_ip)
            _write_run_spec(staging, spec, idle, proxy_ip)
            agent = f"cmsbx-{run_id}-agent"
            _run(["cp", "-a", str(staging / "run.json"), f"{agent}:/sandbox/run.json"])
            _run(
                [
                    "cp",
                    "-a",
                    str(DOCKER_ASSETS / "agent-init.sh"),
                    f"{agent}:/sandbox/agent-init.sh",
                ]
            )
            package = staging / "codex_modal"
            shutil.rmtree(package, ignore_errors=True)
            shutil.copytree(
                PROJECT_ROOT / "codex_modal",
                package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            _run(["cp", "-a", str(package), f"{agent}:/sandbox"])
            if idle.copy_in is not None:
                source = idle.copy_in.resolve()
                if not source.is_dir():
                    raise DockerError(f"copy_in path {source} is not a directory.")
                _run(["cp", "-a", f"{source}{os.sep}.", f"{agent}:/sandbox/copy-in"])
            _run(["start", agent])
        except Exception:
            sandbox.cleanup()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return sandbox

    def serve(self, spec: ServiceSpec) -> Service:
        port = reserve_port()
        arguments = [item.replace("{port}", str(port)) for item in spec.arguments]
        log_path = spec.log_path or (self.state_root / "services" / f"{spec.name}.log")
        process = start_python_service(
            spec.module,
            arguments,
            project_root=spec.source_root or self.project_root,
            log_path=log_path,
            port=port,
            health_path=spec.health_path,
        )
        tunnel = self._clean_tunnel(
            origin=f"http://127.0.0.1:{port}",
            health_path=spec.health_path,
            log_path=log_path.with_name(log_path.stem + "-tunnel.log"),
        )
        self._services.extend([tunnel, process])

        def _stop() -> None:
            tunnel.stop()
            process.stop()

        return Service(name=spec.name, url=tunnel.url, port=port, _stop=_stop)

    def close(self) -> None:
        for owned in reversed(self._services):
            try:
                owned.stop()
            except Exception:  # noqa: BLE001 - teardown must not mask a run error
                pass
        self._services.clear()

    # ---- internals -------------------------------------------------------

    def _clean_tunnel(self, *, origin: str, health_path: str, log_path: Path, attempts: int = 5):
        binary = ensure_cloudflared(self.state_root)
        for _ in range(attempts):
            tunnel = start_tunnel(
                binary=binary,
                origin=origin,
                health_path=health_path,
                log_path=log_path,
                cwd=self.project_root,
            )
            if self.url_acceptable is None or self.url_acceptable(tunnel.url):
                return tunnel
            tunnel.stop()
        raise SandboxError(f"No acceptable public hostname after {attempts} attempts for {origin}.")

    def _create_egress(
        self,
        run_id: str,
        spec: SandboxSpec,
        options: SandboxOptions,
        image: str,
        inner: str,
        edge: str,
        proxy_ip: str,
    ) -> None:
        environment = {
            "EGRESS_PORT": str(PROXY_PORT),
            "MODEL_PORT": str(MODEL_PORT),
            "EGRESS_ALLOW_PORTS": ",".join(str(port) for port in options.allow_ports),
            "EGRESS_ALLOW_HOSTS": ",".join(options.allow_hosts),
            "MODEL_UPSTREAM": spec.upstream_url,
            "MODEL_PREFIX": "/v1",
        }
        if spec.upstream_authorization:
            environment["MODEL_AUTHORIZATION"] = spec.upstream_authorization
        arguments = [
            "create",
            "--name",
            f"cmsbx-{run_id}-egress",
            "--network",
            inner,
            "--ip",
            proxy_ip,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--pids-limit",
            "256",
            "--restart",
            "no",
            "--label",
            "codex-modal-sandbox=1",
        ]
        for key, value in environment.items():
            arguments.extend(["-e", f"{key}={value}"])
        arguments.append(image)
        _run(arguments)
        _run(["network", "connect", edge, f"cmsbx-{run_id}-egress"])
        _run(["start", f"cmsbx-{run_id}-egress"])

    def _create_agent(
        self,
        run_id: str,
        options: SandboxOptions,
        image: str,
        inner: str,
        agent_ip: str,
        proxy_ip: str,
    ) -> None:
        proxy = f"http://{proxy_ip}:{PROXY_PORT}"
        environment = {
            "SANDBOX_PROXY_IP": proxy_ip,
            "SANDBOX_PROXY_PORT": str(PROXY_PORT),
            "SANDBOX_MODEL_PORT": str(MODEL_PORT),
            "SANDBOX_FIREWALL": options.firewall,
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "ALL_PROXY": proxy,
            "NO_PROXY": f"{proxy_ip},localhost,127.0.0.1",
            "no_proxy": f"{proxy_ip},localhost,127.0.0.1",
            "CODEX_MODAL_STATE_ROOT": "/sandbox-state",
        }
        prefix = f"cmsbx-{run_id}"
        arguments = [
            "create",
            "--name",
            f"{prefix}-agent",
            "--hostname",
            "codex-sandbox",
            "--network",
            inner,
            "--ip",
            agent_ip,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,exec,size=2g",
            "--mount",
            f"type=volume,source={prefix}-home,target=/home/agent",
            "--mount",
            f"type=volume,source={prefix}-work,target=/work",
            "--mount",
            f"type=volume,source={prefix}-state,target=/sandbox-state",
            "--mount",
            f"type=volume,source={prefix}-sandbox,target=/sandbox",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "NET_RAW",
            "--cap-add",
            "SYS_PTRACE",
            "--memory",
            options.memory,
            "--cpus",
            options.cpus,
            "--pids-limit",
            str(options.pids_limit),
            "--restart",
            "no",
            "--label",
            "codex-modal-sandbox=1",
        ]
        for key, value in environment.items():
            arguments.extend(["-e", f"{key}={value}"])
        arguments.append(image)
        _run(arguments)


def prune_local() -> tuple[int, int, int]:
    """Remove every leftover sandbox container, network and volume."""

    from ..docker.sandbox import prune_sandboxes

    return prune_sandboxes()


__all__ = [
    "DockerExecHandle",
    "LocalBackend",
    "LocalSandbox",
    "prune_local",
    "start_process",
]
