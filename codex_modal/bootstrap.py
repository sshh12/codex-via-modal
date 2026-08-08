"""Lazy dependency bootstrapping for the tiny shell launchers."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys

from .paths import REQUIREMENTS_PATH


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def dependencies_ready() -> bool:
    return _version("modal") == "1.5.3" and _version("keyring") is not None


def ensure_dependencies() -> None:
    """Install pinned runtime dependencies into the launcher's virtualenv."""

    if dependencies_ready():
        return
    print("Bootstrapping codex-modal's local Python dependencies...", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)],
        check=False,
    )
    if result.returncode != 0 or not dependencies_ready():
        raise RuntimeError(
            "Could not install codex-modal dependencies. Check Python/pip and network access, "
            f"then run: {sys.executable} -m pip install -r {REQUIREMENTS_PATH}"
        )
