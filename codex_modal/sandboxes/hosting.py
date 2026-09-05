"""Host-side plumbing the local backend needs: processes, tunnels, a bridge.

An agent container sits on a Docker ``--internal`` network, so nothing can reach
into it and (on Docker Desktop) the host cannot even route to its address. The
local implementation of ``Sandbox.expose_port`` therefore inverts the direction:
a small **bridge** runs on the host with a public Cloudflare quick-tunnel in
front of it, and a **connector** inside the sandbox long-polls that bridge for
requests and replays them against ``localhost:<port>``. The connector's only
egress is the broker, which is exactly the path every other participant uses, so
publishing a port grants the sandbox no capability it did not already have.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

ASSETS = Path(__file__).resolve().parent / "assets"
_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class HostServiceError(RuntimeError):
    pass


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


@dataclass
class ManagedProcess:
    process: subprocess.Popen[bytes]
    log_path: Path
    _stream: object

    @property
    def pid(self) -> int:
        return self.process.pid

    def stop(self, *, timeout_s: float = 8.0) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout_s)
        try:
            self._stream.close()  # type: ignore[attr-defined]
        except OSError:
            pass


def start_process(
    arguments: Sequence[str],
    *,
    log_path: Path,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("ab", buffering=0)
    env = os.environ.copy()
    if environment:
        env.update(environment)
    process = subprocess.Popen(
        list(arguments),
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=stream,
        creationflags=_creation_flags(),
        start_new_session=os.name != "nt",
    )
    return ManagedProcess(process=process, log_path=log_path, _stream=stream)


def wait_http(url: str, *, process: ManagedProcess | None = None, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.process.poll() is not None:
            tail = process.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise HostServiceError(
                f"Host service exited early ({process.process.returncode}): {tail}"
            )
        try:
            response = httpx.get(url, timeout=5.0, follow_redirects=True)
            if response.status_code == 200:
                return
            last_error = HostServiceError(f"HTTP {response.status_code}")
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(0.4)
    raise HostServiceError(f"Timed out waiting for {url}: {last_error}")


def start_python_service(
    module: str,
    arguments: Sequence[str],
    *,
    project_root: Path,
    log_path: Path,
    port: int,
    health_path: str = "/healthz",
) -> ManagedProcess:
    process = start_process(
        [sys.executable, "-m", module, *arguments],
        log_path=log_path,
        cwd=project_root,
        environment={"PYTHONUNBUFFERED": "1"},
    )
    wait_http(f"http://127.0.0.1:{port}{health_path}", process=process)
    return process


def _download_name() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    if system == "windows":
        return f"cloudflared-windows-{architecture}.exe", "cloudflared.exe"
    if system == "darwin":
        return f"cloudflared-darwin-{architecture}.tgz", "cloudflared"
    return f"cloudflared-linux-{architecture}", "cloudflared"


def ensure_cloudflared(state_root: Path) -> Path:
    existing = shutil.which("cloudflared")
    if existing:
        return Path(existing).resolve()
    remote_name, local_name = _download_name()
    if remote_name.endswith(".tgz"):
        raise HostServiceError(
            "Automatic cloudflared installation on macOS is unavailable; "
            "install cloudflared on PATH."
        )
    binary = state_root / "bin" / local_name
    if binary.is_file() and binary.stat().st_size > 1_000_000:
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_suffix(binary.suffix + ".download")
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/" + remote_name
    )
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                for chunk in response.iter_bytes():
                    stream.write(chunk)
        temporary.replace(binary)
        if os.name != "nt":
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    except (OSError, httpx.HTTPError) as error:
        temporary.unlink(missing_ok=True)
        raise HostServiceError(f"Could not install cloudflared: {error}") from error
    return binary


@dataclass
class PublicTunnel:
    url: str
    process: ManagedProcess

    def stop(self) -> None:
        self.process.stop()


def start_tunnel(
    *,
    binary: Path,
    origin: str,
    health_path: str,
    log_path: Path,
    cwd: Path,
    timeout_s: float = 60.0,
) -> PublicTunnel:
    process = start_process(
        [str(binary), "tunnel", "--url", origin, "--protocol", "http2", "--no-autoupdate"],
        log_path=log_path,
        cwd=cwd,
    )
    deadline = time.monotonic() + timeout_s
    url: str | None = None
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            break
        text = log_path.read_text(encoding="utf-8", errors="replace")
        match = _TUNNEL_URL_RE.search(text)
        if match:
            url = match.group(0)
            break
        time.sleep(0.3)
    if url is None:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        process.stop()
        raise HostServiceError(f"Cloudflare tunnel did not publish a URL: {tail}")
    try:
        wait_http(url.rstrip("/") + health_path, process=process, timeout_s=timeout_s)
    except Exception:
        process.stop()
        raise
    return PublicTunnel(url=url, process=process)
