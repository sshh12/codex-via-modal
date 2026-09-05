"""Open a sandbox on a backend and drive it, rather than running it to completion.

    from codex_modal.sandboxes import open_backend
    backend = open_backend("local")
    box = backend.open_sandbox(spec, options, label="operator")
    box.run_codex(log_path=Path("console.log"))
    url = box.expose_port(8080)

Backends are imported lazily so that neither `docker` nor `modal` is required
just to import this package.
"""

from __future__ import annotations

from collections.abc import Callable
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

BACKENDS = ("local", "modal", "fake")


def open_backend(
    name: str = "local",
    *,
    options: BackendOptions | None = None,
    project_root: Path | None = None,
    state_root: Path | None = None,
    url_acceptable: Callable[[str], bool] | None = None,
    **extra: Any,
) -> Backend:
    """Return a backend by name. Unknown names fail closed with the valid set."""

    if name == "local":
        from .local import LocalBackend

        return LocalBackend(
            state_root=state_root,
            project_root=project_root,
            options=options,
            url_acceptable=url_acceptable,
        )
    if name == "modal":
        from .modal_backend import ModalBackend

        return ModalBackend(
            options=options,
            project_root=project_root,
            url_acceptable=url_acceptable,
            **extra,
        )
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend(options=options, project_root=project_root, **extra)
    raise SandboxError(f"Unknown sandbox backend {name!r}; expected one of {', '.join(BACKENDS)}.")


__all__ = [
    "BACKENDS",
    "Backend",
    "BackendOptions",
    "Capabilities",
    "ExecHandle",
    "Sandbox",
    "SandboxError",
    "Service",
    "ServiceSpec",
    "open_backend",
]
