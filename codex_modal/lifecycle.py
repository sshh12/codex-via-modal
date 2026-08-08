"""Endpoint ownership, readiness, crash recovery, and Codex process lifetime."""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import modal_cli
from .paths import PROJECT_ROOT, RUNTIME_ROOT, ensure_state_dirs


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_state(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("endpoint state is not a JSON object")
    endpoint_id = str(document.get("endpoint_id", ""))
    if not modal_cli.ENDPOINT_ID_PATTERN.fullmatch(endpoint_id):
        raise ValueError("endpoint state contains an invalid endpoint ID")
    return document


def _filetime_value(value: Any) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _windows_process_identity(process_id: int) -> str | None:
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return f"windows-filetime:{_filetime_value(creation)}"
    finally:
        kernel32.CloseHandle(handle)


def process_identity(process_id: int) -> str | None:
    """Return an OS process birth identity, preventing cleanup from trusting a reused PID."""

    if os.name == "nt":
        return _windows_process_identity(process_id)
    stat_path = Path(f"/proc/{process_id}/stat")
    try:
        raw = stat_path.read_text(encoding="ascii")
        fields_after_name = raw[raw.rfind(")") + 2 :].split()
        return f"proc-start-ticks:{fields_after_name[19]}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(process_id)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        value = result.stdout.strip()
        return f"ps-lstart:{value}" if result.returncode == 0 and value else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _process_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        return _windows_process_identity(process_id) is not None
    try:
        os.kill(process_id, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def owner_is_alive(state: dict[str, Any]) -> bool:
    try:
        process_id = int(state["owner_pid"])
    except (KeyError, TypeError, ValueError):
        return False
    expected = state.get("owner_identity")
    if expected:
        return process_identity(process_id) == expected
    return _process_alive(process_id)


def create_owned_state(
    endpoint_id: str,
    endpoint_name: str,
    environment_name: str | None,
) -> Path:
    if not modal_cli.ENDPOINT_ID_PATTERN.fullmatch(endpoint_id):
        raise ValueError(f"Invalid endpoint ID {endpoint_id!r}.")
    ensure_state_dirs()
    state_path = RUNTIME_ROOT / f"owned-{endpoint_id}.json"
    cancel_path = RUNTIME_ROOT / f"cancel-{endpoint_id}-{secrets.token_hex(5)}"
    now = _utc_now()
    state = {
        "endpoint_id": endpoint_id,
        "endpoint_name": endpoint_name,
        "environment": environment_name,
        "owner_pid": os.getpid(),
        "owner_identity": process_identity(os.getpid()),
        "created_at_utc": now,
        "heartbeat_utc": now,
        "cancel_path": str(cancel_path),
    }
    _atomic_json(state_path, state)
    return state_path


def heartbeat(state_path: Path | None) -> None:
    if state_path is None:
        return
    try:
        state = _read_state(state_path)
        state["heartbeat_utc"] = _utc_now()
        _atomic_json(state_path, state)
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def finish_state(state_path: Path) -> None:
    try:
        state = _read_state(state_path)
        raw_cancel_path = state.get("cancel_path")
        if raw_cancel_path:
            Path(str(raw_cancel_path)).write_text("cleanup complete\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass


def start_watchdog(state_path: Path) -> None:
    arguments = [sys.executable, "-m", "codex_modal", "__watchdog", str(state_path)]
    options: dict[str, Any] = {
        "cwd": PROJECT_ROOT,
        "env": modal_cli.scrubbed_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        options["start_new_session"] = True
    subprocess.Popen(arguments, **options)


def _log_cleanup(endpoint_id: str, message: str) -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_ROOT / f"cleanup-{endpoint_id}.log"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{_utc_now()} {message}\n")


def watchdog_main(state_path: Path) -> int:
    """Hidden detached subcommand: stop one exact endpoint after its owner disappears."""

    try:
        initial = _read_state(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    endpoint_id = str(initial["endpoint_id"])
    while True:
        try:
            state = _read_state(state_path)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as error:
            _log_cleanup(endpoint_id, f"invalid state; refusing cleanup: {error}")
            return 1
        cancel_path = Path(str(state.get("cancel_path", "")))
        if str(cancel_path) and cancel_path.exists():
            return 0
        if not owner_is_alive(state):
            break
        time.sleep(3)

    environment_name = state.get("environment") or None
    _log_cleanup(endpoint_id, "owner exited; starting endpoint cleanup")
    for attempt in range(1, 13):
        if modal_cli.stop_endpoint(endpoint_id, environment_name, quiet=True):
            _log_cleanup(endpoint_id, f"endpoint stopped on attempt {attempt}")
            finish_state(state_path)
            return 0
        _log_cleanup(endpoint_id, f"cleanup attempt {attempt} failed")
        time.sleep(min(30, 2 * attempt))
    _log_cleanup(endpoint_id, "cleanup retries exhausted; state retained for next sweep")
    return 1


def sweep_stale_endpoints(*, verbose: bool = True) -> tuple[int, int]:
    """Stop endpoints whose exact ownership records no longer have a live owner."""

    ensure_state_dirs()
    stopped = 0
    active = 0
    for state_path in sorted(RUNTIME_ROOT.glob("owned-*.json")):
        try:
            state = _read_state(state_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if verbose:
                print(
                    f"WARNING: refusing malformed cleanup state {state_path}: {error}",
                    file=sys.stderr,
                )
            continue
        endpoint_id = str(state["endpoint_id"])
        if owner_is_alive(state):
            active += 1
            if verbose:
                print(f"Leaving active wrapper-owned endpoint {endpoint_id} alone.")
            continue
        if verbose:
            print(f"Recovering stale wrapper-owned endpoint {endpoint_id}...")
        if modal_cli.stop_endpoint(endpoint_id, state.get("environment") or None):
            finish_state(state_path)
            stopped += 1
        elif verbose:
            print(
                f"WARNING: cleanup failed; retained {state_path} for a later retry.",
                file=sys.stderr,
            )
    return stopped, active


def resolve_endpoint_id(endpoint_name: str, environment_name: str | None) -> str:
    """Resolve the unique generated name to an exact endpoint ID after creation."""

    last_error: Exception | None = None
    for _ in range(10):
        try:
            rows = modal_cli.list_endpoints(environment_name)
            matches = [row for row in rows if str(row.get("name", "")) == endpoint_name]
            for row in matches:
                candidate = str(row.get("endpoint_id") or row.get("id") or "")
                if modal_cli.ENDPOINT_ID_PATTERN.fullmatch(candidate):
                    return candidate
        except Exception as error:  # provisioning can briefly race list visibility
            last_error = error
        time.sleep(2)
    suffix = f" Last list error: {last_error}" if last_error else ""
    raise RuntimeError(
        f"Endpoint {endpoint_name!r} was created, but its exact ID could not be resolved; "
        "codex-modal will not guess at a cleanup target. Stop that named endpoint from the "
        f"Modal dashboard or CLI.{suffix}"
    )


def _endpoint_row(
    rows: list[dict[str, Any]], endpoint_id: str
) -> dict[str, Any] | None:
    for row in rows:
        candidate = str(row.get("endpoint_id") or row.get("id") or "")
        if candidate == endpoint_id:
            return row
    return None


def wait_for_endpoint(
    *,
    endpoint_id: str,
    endpoint_host: str,
    environment_name: str | None,
    shared_base_url: str,
    proxy_token: str,
    timeout_seconds: int,
    state_path: Path | None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    last_list_error: str | None = None
    ready_statuses = {"running", "ready", "succeeded", "active", "deployed"}
    failed_words = ("failed", "cancelled", "canceled", "stopped", "error")

    while time.monotonic() < deadline:
        heartbeat(state_path)
        try:
            row = _endpoint_row(
                modal_cli.list_endpoints(environment_name), endpoint_id
            )
            last_list_error = None
        except RuntimeError as error:
            row = None
            current_error = str(error)
            if current_error != last_list_error:
                print(f"Waiting for Modal endpoint list: {current_error}")
                last_list_error = current_error
        if row is not None:
            status = str(row.get("status", "unknown"))
            if status != last_status:
                print(f"Modal endpoint status: {status}")
                last_status = status
            lowered = status.lower()
            if any(word in lowered for word in failed_words):
                raise RuntimeError(
                    f"Modal endpoint provisioning ended with status {status!r}."
                )
            if lowered in ready_statuses:
                break
        time.sleep(10)
    else:
        raise RuntimeError(
            f"Timed out after {timeout_seconds} seconds provisioning {endpoint_id}."
        )

    _wait_for_shared_route(
        endpoint_host=endpoint_host,
        shared_base_url=shared_base_url,
        proxy_token=proxy_token,
        deadline=deadline,
        state_path=state_path,
    )


def wait_for_attached_endpoint(
    *,
    endpoint_host: str,
    shared_base_url: str,
    proxy_token: str,
    timeout_seconds: int,
) -> None:
    print(f"Waiting for attached Modal Responses route: {endpoint_host}")
    _wait_for_shared_route(
        endpoint_host=endpoint_host,
        shared_base_url=shared_base_url,
        proxy_token=proxy_token,
        deadline=time.monotonic() + timeout_seconds,
        state_path=None,
    )


def _wait_for_shared_route(
    *,
    endpoint_host: str,
    shared_base_url: str,
    proxy_token: str,
    deadline: float,
    state_path: Path | None,
) -> None:
    models_url = f"{shared_base_url.rstrip('/')}/models"
    while time.monotonic() < deadline:
        heartbeat(state_path)
        request = urllib.request.Request(
            models_url,
            headers={"Authorization": f"Bearer {proxy_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.loads(response.read().decode("utf-8"))
            rows = document.get("data", []) if isinstance(document, dict) else []
            model_ids = {
                str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")
            }
            if endpoint_host in model_ids:
                print(f"Modal shared Responses route is ready: {endpoint_host}")
                return
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise RuntimeError(
                    f"Modal shared endpoint authentication failed (HTTP {error.code}). "
                    "Check the proxy token and its RBAC environment association."
                ) from error
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(5)
    raise RuntimeError(
        "The endpoint did not appear in Modal's shared Responses model list before the "
        "startup timeout."
    )


def run_process_with_heartbeat(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    state_path: Path | None,
) -> int:
    """Run Codex on the caller's terminal while maintaining endpoint ownership."""

    process = subprocess.Popen(arguments, cwd=cwd, env=environment)
    previous_term = signal.getsignal(signal.SIGTERM)

    def terminate_requested(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_requested)
    try:
        while True:
            heartbeat(state_path)
            try:
                return_code = process.wait(timeout=5)
                return 128 + abs(return_code) if return_code < 0 else return_code
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)
