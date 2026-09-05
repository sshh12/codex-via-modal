"""Backend-neutral sandbox handles.

``docker/sandbox.py`` runs a sandbox to completion and returns an exit code. That
is the right shape for ``codex-modal --docker``, and the wrong shape for a harness
that has to keep one container alive while it drives others: it forces the caller
to discover the container out-of-band and gives it nowhere to hang a port.

This module is the handle form of the same thing - open a sandbox, then talk to
it - with exactly the verbs a multi-participant run needs::

    open_sandbox(spec, options)          -> Sandbox
    Sandbox.exec(argv, ...)              -> ExecHandle
    Sandbox.read_text / write_bytes / download
    Sandbox.expose_port(port)            -> public URL
    Sandbox.stop / cleanup
    Backend.serve(service_spec)          -> Service

``expose_port`` is the verb that earns the abstraction. Locally it is a host
bridge plus an in-container connector, because the agent sits on a Docker
``--internal`` network with no inbound route. On Modal it is one call to
``tunnels()``. Callers see a URL either way.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    """A sandbox could not be opened, driven, or torn down."""


class ExecHandle(ABC):
    """A command running (or finished) inside a sandbox."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = tuple(argv)
        self.exit_code: int | None = None
        self.stdout: str = ""
        self.stderr: str = ""

    @abstractmethod
    def poll(self) -> int | None:
        """The exit code, or None while the command is still running."""

    @abstractmethod
    def wait(self, timeout_s: float | None = None) -> int:
        """Block for the exit code; raise TimeoutError past ``timeout_s``."""

    @abstractmethod
    def kill(self) -> None:
        """Stop the command; safe to call after it has already exited."""


@dataclass
class Service:
    """A host- or sandbox-side HTTP service published at a public URL."""

    name: str
    url: str
    port: int
    _stop: Any = None

    def stop(self) -> None:
        if self._stop is not None:
            stop, self._stop = self._stop, None
            stop()


@dataclass(frozen=True)
class ServiceSpec:
    """A small Python HTTP service a run needs reachable from every sandbox.

    ``arguments`` may contain the literal token ``{port}``; the backend allocates
    the port and substitutes it, because who owns the port differs per backend.
    """

    name: str
    module: str
    arguments: tuple[str, ...] = ()
    health_path: str = "/healthz"
    source_root: Path | None = None
    packages: tuple[str, ...] = ("fastapi", "uvicorn")
    log_path: Path | None = None


@dataclass(frozen=True)
class Capabilities:
    """What a backend's isolation actually gives the agent.

    Recorded in the run manifest rather than assumed: gVisor drops NET_ADMIN,
    NET_RAW and SYS_PTRACE, so ``nmap -sS``, ``ping`` and ``tcpdump`` behave
    differently on Modal than under local Docker. That is a real capability
    change for a participant probing a service, not a footnote.
    """

    backend: str
    net_admin: bool
    net_raw: bool
    sys_ptrace: bool
    egress_broker: bool
    host_reachable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "net_admin": self.net_admin,
            "net_raw": self.net_raw,
            "sys_ptrace": self.sys_ptrace,
            "egress_broker": self.egress_broker,
            "host_reachable": self.host_reachable,
        }


class Sandbox(ABC):
    """One isolated container running (or ready to run) Codex."""

    #: Where cvm's container entry writes the fully-formed Codex command line
    #: (profile, pinned model, bypass placement, composed prompt) in shell mode.
    COMMAND_FILE = "/sandbox-state/codex-home/codex-command.txt"

    backend: str = "unknown"
    name: str = ""
    export_directory: Path | None = None

    @abstractmethod
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
        """Run one command. ``background=True`` returns before it finishes."""

    @abstractmethod
    def read_text(self, path: str) -> str:
        """File contents, or "" when the file is missing or unreadable."""

    @abstractmethod
    def write_bytes(self, path: str, data: bytes, *, owner: str | None = None) -> None:
        """Write a file inside the sandbox, creating parents as needed."""

    @abstractmethod
    def download(self, remote: str, local: Path) -> bool:
        """Copy a sandbox path out to the host; False when it does not exist."""

    @abstractmethod
    def expose_port(self, port: int) -> str:
        """Publish a port the sandbox listens on and return its public URL."""

    @abstractmethod
    def stop(self, *, timeout_s: float = 10.0) -> None:
        """Stop the sandbox's processes, leaving artifacts exportable."""

    @abstractmethod
    def cleanup(self) -> None:
        """Export artifacts and destroy every resource the sandbox owns."""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """What this backend's isolation actually permits."""

    # ---- shared, backend-independent -------------------------------------

    def wait_for_prepared_command(self, *, timeout_s: float = 180.0) -> str:
        """Block until the idling sandbox has written its Codex command line.

        Bare ``codex`` would miss the generated profile, the pinned model and the
        bypass placement, so the prepared line - which already carries the
        composed prompt as its final argument - is the only correct thing to run.
        """

        deadline = time.monotonic() + timeout_s
        while True:
            text = self.read_text(self.COMMAND_FILE).strip()
            if text:
                return text
            if time.monotonic() >= deadline:
                raise SandboxError(
                    f"{self.name}: the sandbox never prepared its Codex command line."
                )
            time.sleep(0.5)

    def run_codex(
        self,
        *,
        log_path: Path,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        background: bool = True,
    ) -> ExecHandle:
        """Run the command line the sandbox prepared for itself."""

        self.wait_for_prepared_command()
        environment = {
            "HOME": "/home/agent",
            "CODEX_HOME": "/sandbox-state/codex-home",
            **dict(env or {}),
        }
        return self.exec(
            ["sh", "-c", 'exec sh -c "$(cat ' + self.COMMAND_FILE + ')"'],
            user="agent",
            env=environment,
            workdir="/work",
            timeout_s=timeout_s,
            background=background,
            log_path=log_path,
        )


class Backend(ABC):
    """A place sandboxes and host services can be created."""

    name: str = "unknown"

    @abstractmethod
    def open_sandbox(
        self,
        spec: Any,
        options: Any,
        *,
        label: str = "",
        expose_ports: Sequence[int] = (),
    ) -> Sandbox:
        """Create and start an idling sandbox from a cvm SandboxSpec/SandboxOptions.

        ``expose_ports`` names ports that may later be published. Modal can only
        publish ports declared at create time, so they are declared up front even
        though the local backend does not need them until ``expose_port``.
        """

    @abstractmethod
    def serve(self, spec: ServiceSpec) -> Service:
        """Start a small HTTP service and publish it at a URL sandboxes can reach."""

    def close(self) -> None:
        """Release backend-wide resources (pooled tunnels, apps, images)."""


@dataclass
class BackendOptions:
    """Backend-wide knobs that are not per-sandbox."""

    #: Delete idle sandboxes after this many seconds with nothing to do. Cost
    #: control: a wedged rollout must not keep billing until someone notices.
    idle_timeout_s: int = 900
    #: Modal only - the pinned image reference carrying codex_modal and Codex.
    image_reference: str | None = None
    region: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
