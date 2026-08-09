"""Run Codex inside a locked-down local container instead of on this machine.

Topology
--------

    host (wrapper)                     no container ever sees the host
      |
      |  docker CLI only
      v
    +---------------------------+      +--------------------------------+
    | cmsbx-<id>-egress         |      | cmsbx-<id>-agent               |
    | networks: edge + inner    |<---->| network: inner (internal only) |
    | holds the upstream token  |      | Codex, approvals bypassed      |
    +---------------------------+      +--------------------------------+
            |                                   no route anywhere else
            v
        public internet

The inner network is created with Docker's ``--internal`` flag, so it has no
default route at all: the agent container cannot reach the internet, the host,
or the LAN, and it has no working external DNS. Its only peer is the egress
broker, which refuses any destination that is not a globally routable address.
A default-deny iptables policy inside the agent's own network namespace is the
second, independent layer.

Nothing from the host filesystem is mounted. The workspace, the home directory,
and the wrapper state directory are Docker volumes created for the run and
destroyed with it, and anything the user wants copied in is copied, not bound.
After the run the wrapper pulls the raw Codex logs, session rollouts, and the
full egress record back out with ``docker cp``.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..codex_config import ModelSettings
from ..paths import PROJECT_ROOT, STATE_ROOT

ASSETS = Path(__file__).resolve().parent / "assets"
DEFAULT_CODEX_VERSION = "0.147.0"
PROXY_PORT = 3128
MODEL_PORT = 8081
CONSOLE_LOG_LIMIT = 8 * 1024 * 1024


@dataclass
class SandboxOptions:
    """Host-side knobs for the container sandbox."""

    codex_version: str = DEFAULT_CODEX_VERSION
    agent_image: str | None = None
    build: bool = False
    extra_packages: str = ""
    allow_ports: tuple[int, ...] = (80, 443)
    allow_hosts: tuple[str, ...] = ()
    firewall: str = "enforce"  # enforce | warn | off
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 1024
    copy_in: Path | None = None
    export_dir: Path | None = None
    export_work: bool = False
    keep: bool = False
    # Codex's `exec` mode writes tracing to stdout, so `info` buries the actual
    # answer. The session rollout JSONL is exported either way; raise this when
    # the console transcript itself is what needs analysing.
    rust_log: str = "error"
    danger: bool = True
    command: tuple[str, ...] = ()
    env_doc: bool = True
    env_note: str | None = None
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxSpec:
    """Everything the sandbox needs to talk to a model."""

    settings: ModelSettings
    upstream_url: str
    upstream_authorization: str | None
    codex_arguments: list[str]


class DockerError(RuntimeError):
    pass


def _docker() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise DockerError(
            "Docker is required for --docker mode but the `docker` CLI is not on PATH."
        )
    return executable


def _run(
    arguments: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = 300.0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [_docker(), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        raise DockerError(
            f"docker {' '.join(arguments[:3])} failed (exit {result.returncode})"
            + (f": {detail}" if detail else ".")
        )
    return result


def _quiet(arguments: list[str]) -> None:
    _run(arguments, check=False, timeout=120.0)


def assert_docker_available() -> None:
    result = _run(["version", "--format", "{{.Server.Version}}"], check=False, timeout=60.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DockerError(
            "Docker is installed but the daemon is not reachable. Start Docker Desktop "
            "or the Docker service and try again."
            + (f" ({detail})" if detail else "")
        )


def _digest(paths: list[Path], extra: str) -> str:
    digest = hashlib.sha256(extra.encode("utf-8"))
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _image_exists(reference: str) -> bool:
    return _run(["image", "inspect", reference], check=False, timeout=60.0).returncode == 0


def _build_image(
    reference: str, dockerfile: Path, build_args: dict[str, str], *, force: bool
) -> str:
    if _image_exists(reference) and not force:
        return reference
    print(f"Building sandbox image {reference} (first run only)...")
    arguments = ["build", "-t", reference, "-f", str(dockerfile)]
    for key, value in build_args.items():
        arguments.extend(["--build-arg", f"{key}={value}"])
    arguments.append(str(ASSETS))
    result = subprocess.run(
        [_docker(), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600.0,
        check=False,
    )
    if result.returncode != 0:
        raise DockerError(f"Building {reference} failed (exit {result.returncode}).")
    return reference


def ensure_images(options: SandboxOptions) -> tuple[str, str]:
    agent_dockerfile = ASSETS / "Dockerfile.agent"
    egress_dockerfile = ASSETS / "Dockerfile.egress"
    proxy_source = ASSETS / "egress_proxy.py"
    init_script = ASSETS / "agent-init.sh"

    if options.agent_image:
        agent_reference = options.agent_image
        if not _image_exists(agent_reference):
            raise DockerError(f"Sandbox image {agent_reference!r} is not present locally.")
    else:
        tag = _digest(
            [agent_dockerfile, init_script],
            f"{options.codex_version}|{options.extra_packages}",
        )
        agent_reference = f"codex-modal-agent:{tag}"
        _build_image(
            agent_reference,
            agent_dockerfile,
            {
                "CODEX_VERSION": options.codex_version,
                "EXTRA_PACKAGES": options.extra_packages,
            },
            force=options.build,
        )

    egress_reference = f"codex-modal-egress:{_digest([egress_dockerfile, proxy_source], '1')}"
    _build_image(egress_reference, egress_dockerfile, {}, force=options.build)
    return agent_reference, egress_reference


def _candidate_subnets(count: int) -> list[str]:
    generator = random.SystemRandom()
    return [
        f"10.{generator.randrange(160, 250)}.{generator.randrange(0, 256)}.0/24"
        for _ in range(count)
    ]


def _create_networks(run_id: str) -> tuple[str, str, str, str]:
    """Create the internal and edge networks; return names plus both static IPs."""

    inner = f"cmsbx-{run_id}-inner"
    edge = f"cmsbx-{run_id}-edge"
    last_error: Exception | None = None
    for subnet in _candidate_subnets(8):
        base = subnet.rsplit(".", 1)[0]
        try:
            _run(
                [
                    "network",
                    "create",
                    "--internal",
                    "--driver",
                    "bridge",
                    "--subnet",
                    subnet,
                    "--gateway",
                    f"{base}.1",
                    inner,
                ]
            )
        except DockerError as error:
            last_error = error
            continue
        try:
            _run(["network", "create", "--driver", "bridge", edge])
        except DockerError:
            _quiet(["network", "rm", inner])
            raise
        return inner, edge, f"{base}.2", f"{base}.3"
    raise DockerError(
        "Could not allocate a private subnet for the sandbox network."
        + (f" Last error: {last_error}" if last_error else "")
    )


def _write_run_spec(directory: Path, spec: SandboxSpec, options: SandboxOptions, proxy_ip: str) -> Path:
    settings = spec.settings
    document = {
        "slug": settings.slug,
        "display_model": settings.display_model,
        "context_window": settings.context_window,
        "reasoning_effort": settings.reasoning_effort,
        "reasoning_levels": list(settings.reasoning_levels),
        "provider_base_url": f"http://{proxy_ip}:{MODEL_PORT}/v1",
        "persist_history": settings.persist_history,
        "workspace": "/work",
        "codex_arguments": list(spec.codex_arguments),
        "danger": options.danger,
        "command": list(options.command),
        "env_doc": options.env_doc,
        "env_note": options.env_note,
        "allow_ports": list(options.allow_ports),
        "allow_hosts": list(options.allow_hosts),
        "rust_log": options.rust_log,
        "environment": dict(options.environment),
        "validate_strict": True,
    }
    path = directory / "run.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _interactive() -> tuple[bool, bool]:
    try:
        stdin_tty = sys.stdin is not None and sys.stdin.isatty()
        stdout_tty = sys.stdout is not None and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False, False
    return True, stdin_tty and stdout_tty


def _export(reference: str, source: str, destination: Path) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["cp", "-a", f"{reference}:{source}", str(destination)],
        check=False,
        timeout=300.0,
    )
    return result.returncode == 0


def _export_logs(container: str, destination: Path) -> None:
    result = _run(["logs", "--timestamps", container], check=False, timeout=180.0)
    text = (result.stdout or "") + (result.stderr or "")
    if len(text) > CONSOLE_LOG_LIMIT:
        text = text[:CONSOLE_LOG_LIMIT] + "\n...truncated by codex-modal...\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8", errors="replace", newline="\n")


def run_sandbox(spec: SandboxSpec, options: SandboxOptions) -> int:
    assert_docker_available()
    agent_image, egress_image = ensure_images(options)

    run_id = f"{int(time.time()):x}-{os.getpid():x}"
    export_dir = options.export_dir or (STATE_ROOT / "docker-runs" / run_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    staging = export_dir / ".staging"
    staging.mkdir(parents=True, exist_ok=True)

    inner_network, edge_network, proxy_ip, agent_ip = _create_networks(run_id)
    proxy_container = f"cmsbx-{run_id}-egress"
    agent_container = f"cmsbx-{run_id}-agent"
    volumes = [f"cmsbx-{run_id}-{name}" for name in ("home", "work", "state", "sandbox")]
    home_volume, work_volume, state_volume, sandbox_volume = volumes
    exit_code = 1
    started = False

    try:
        for volume in volumes:
            _run(["volume", "create", volume])

        proxy_environment = {
            "EGRESS_PORT": str(PROXY_PORT),
            "MODEL_PORT": str(MODEL_PORT),
            "EGRESS_ALLOW_PORTS": ",".join(str(port) for port in options.allow_ports),
            "EGRESS_ALLOW_HOSTS": ",".join(options.allow_hosts),
            "MODEL_UPSTREAM": spec.upstream_url,
            "MODEL_PREFIX": "/v1",
        }
        if spec.upstream_authorization:
            proxy_environment["MODEL_AUTHORIZATION"] = spec.upstream_authorization

        proxy_arguments = [
            "create",
            "--name",
            proxy_container,
            "--network",
            inner_network,
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
        for key, value in proxy_environment.items():
            proxy_arguments.extend(["-e", f"{key}={value}"])
        proxy_arguments.append(egress_image)
        _run(proxy_arguments)
        # The broker needs a second interface for the outside world. The agent
        # container is never attached to this one.
        _run(["network", "connect", edge_network, proxy_container])
        _run(["start", proxy_container])

        agent_environment = {
            "SANDBOX_PROXY_IP": proxy_ip,
            "SANDBOX_PROXY_PORT": str(PROXY_PORT),
            "SANDBOX_MODEL_PORT": str(MODEL_PORT),
            "SANDBOX_FIREWALL": options.firewall,
            "HTTP_PROXY": f"http://{proxy_ip}:{PROXY_PORT}",
            "HTTPS_PROXY": f"http://{proxy_ip}:{PROXY_PORT}",
            "http_proxy": f"http://{proxy_ip}:{PROXY_PORT}",
            "https_proxy": f"http://{proxy_ip}:{PROXY_PORT}",
            "ALL_PROXY": f"http://{proxy_ip}:{PROXY_PORT}",
            "NO_PROXY": f"{proxy_ip},localhost,127.0.0.1",
            "no_proxy": f"{proxy_ip},localhost,127.0.0.1",
            "CODEX_MODAL_STATE_ROOT": "/sandbox-state",
        }
        keep_stdin, allocate_tty = _interactive()
        agent_arguments = [
            "create",
            "--name",
            agent_container,
            "--hostname",
            "codex-sandbox",
            "--network",
            inner_network,
            "--ip",
            agent_ip,
            # Writable rootfs so the agent can `apt install`, build, and modify
            # the system freely. It is a throwaway container with no host mount,
            # so this power stays inside the box.
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,exec,size=2g",
            "--mount",
            f"type=volume,source={home_volume},target=/home/agent",
            "--mount",
            f"type=volume,source={work_volume},target=/work",
            "--mount",
            f"type=volume,source={state_volume},target=/sandbox-state",
            "--mount",
            f"type=volume,source={sandbox_volume},target=/sandbox",
            # Docker's default capability set is chosen to be safe from the host;
            # keep all of it and add NET_ADMIN (for the in-container firewall),
            # NET_RAW (nmap/ping), and SYS_PTRACE (gdb/strace). Not `--privileged`
            # and no `no-new-privileges`, so the agent is near-root inside the box
            # yet cannot reach host devices. The Docker internal network (no route
            # off it) and the broker remain the boundary.
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
        if keep_stdin:
            agent_arguments.append("--interactive")
        if allocate_tty:
            agent_arguments.append("--tty")
        for key, value in agent_environment.items():
            agent_arguments.extend(["-e", f"{key}={value}"])
        agent_arguments.append(agent_image)
        _run(agent_arguments)

        _write_run_spec(staging, spec, options, proxy_ip)
        _run(["cp", "-a", str(staging / "run.json"), f"{agent_container}:/sandbox/run.json"])
        _run(["cp", "-a", str(ASSETS / "agent-init.sh"), f"{agent_container}:/sandbox/agent-init.sh"])
        # Stage the package so host bytecode (built by a different interpreter)
        # and any stray local files never reach the sandbox.
        package = staging / "codex_modal"
        shutil.rmtree(package, ignore_errors=True)
        shutil.copytree(
            PROJECT_ROOT / "codex_modal",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        _run(["cp", "-a", str(package), f"{agent_container}:/sandbox"])
        if options.copy_in is not None:
            source = options.copy_in.resolve()
            if not source.is_dir():
                raise DockerError(f"--docker-copy-in path {source} is not a directory.")
            _run(["cp", "-a", f"{source}{os.sep}.", f"{agent_container}:/sandbox/copy-in"])

        print(
            f"Sandbox {run_id}: agent {agent_ip} on internal network {inner_network}; "
            f"egress broker {proxy_ip} allows ports "
            f"{','.join(str(port) for port in options.allow_ports)} to public addresses only."
        )
        started = True
        start_arguments = ["start", "--attach"]
        if keep_stdin:
            start_arguments.append("--interactive")
        start_arguments.append(agent_container)
        exit_code = subprocess.run(
            [_docker(), *start_arguments], check=False
        ).returncode
        return exit_code
    finally:
        try:
            if started:
                _export_logs(agent_container, export_dir / "agent-console.log")
                _export_logs(proxy_container, export_dir / "egress.jsonl")
                _export(agent_container, "/sandbox-state/.", export_dir / "codex-state")
                if options.export_work:
                    _export(agent_container, "/work/.", export_dir / "work")
            (export_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "agent_image": agent_image,
                        "egress_image": egress_image,
                        "model": spec.settings.display_model,
                        "model_slug": spec.settings.slug,
                        "upstream": spec.upstream_url,
                        "upstream_credential": "injected by broker"
                        if spec.upstream_authorization
                        else "none",
                        "inner_network": inner_network,
                        "edge_network": edge_network,
                        "agent_ip": agent_ip,
                        "proxy_ip": proxy_ip,
                        "allow_ports": list(options.allow_ports),
                        "allow_hosts": list(options.allow_hosts),
                        "firewall": options.firewall,
                        "danger": options.danger,
                        "exit_code": exit_code,
                        "kept": options.keep,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            shutil.rmtree(staging, ignore_errors=True)
            print(f"Sandbox logs exported to {export_dir}")
        finally:
            if options.keep:
                print(
                    "Keeping sandbox resources by request. Remove them with:\n"
                    f"  docker rm -f {agent_container} {proxy_container}\n"
                    f"  docker network rm {inner_network} {edge_network}\n"
                    f"  docker volume rm {' '.join(volumes)}",
                    file=sys.stderr,
                )
            else:
                _quiet(["rm", "-f", agent_container])
                _quiet(["rm", "-f", proxy_container])
                _quiet(["network", "rm", inner_network])
                _quiet(["network", "rm", edge_network])
                for volume in volumes:
                    _quiet(["volume", "rm", "-f", volume])


def prune_sandboxes() -> tuple[int, int, int]:
    """Remove leftover sandbox containers, networks, and volumes."""

    containers = _run(
        ["ps", "-aq", "--filter", "label=codex-modal-sandbox=1"], check=False
    ).stdout.split()
    for container in containers:
        _quiet(["rm", "-f", container])
    networks = [
        name
        for name in _run(
            ["network", "ls", "--format", "{{.Name}}"], check=False
        ).stdout.split()
        if name.startswith("cmsbx-")
    ]
    for network in networks:
        _quiet(["network", "rm", network])
    volumes = [
        name
        for name in _run(
            ["volume", "ls", "--format", "{{.Name}}"], check=False
        ).stdout.split()
        if name.startswith("cmsbx-")
    ]
    for volume in volumes:
        _quiet(["volume", "rm", "-f", volume])
    return len(containers), len(networks), len(volumes)
