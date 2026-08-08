"""Small, shell-free adapter around the pinned Modal CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

from .credentials import ProxyCredential
from .paths import PROJECT_ROOT


ENDPOINT_ID_PATTERN = re.compile(r"^ep-[A-Za-z0-9]{22}$")
ENDPOINT_ID_SEARCH = re.compile(r"\b(ep-[A-Za-z0-9]{22})\b")
APP_ID_PATTERN = re.compile(r"^ap-[A-Za-z0-9]{22}$")
APP_ID_SEARCH = re.compile(r"\b(ap-[A-Za-z0-9]{22})\b")
OWNED_VOLUME_PATTERN = re.compile(r"^cm-[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
WORKSPACE_LINE = re.compile(
    r"^Workspace:\s+([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\s+\(ac-[A-Za-z0-9]+\)\s*$",
    re.MULTILINE,
)


def command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "modal", *arguments]


def _utf8_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = (os.environ if base is None else base).copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _completed(
    arguments: Iterable[str],
    *,
    environment: dict[str, str] | None = None,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command(*arguments),
        cwd=PROJECT_ROOT,
        env=_utf8_environment(environment),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _failure(label: str, result: subprocess.CompletedProcess[str]) -> RuntimeError:
    detail = (result.stderr or result.stdout).strip()
    if detail:
        return RuntimeError(f"{label} failed: {detail}")
    return RuntimeError(f"{label} failed with exit code {result.returncode}.")


def token_login_present() -> bool:
    try:
        return _completed(["token", "info"], timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def current_workspace_slug() -> str:
    """Return the workspace DNS slug used in authenticated Modal server URLs."""

    try:
        result = _completed(["token", "info"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Reading the active Modal workspace failed: {error}") from error
    if result.returncode != 0:
        raise _failure("Reading the active Modal workspace", result)
    match = WORKSPACE_LINE.search(result.stdout)
    if match is None:
        raise RuntimeError(
            "Modal authentication is configured, but codex-modal could not parse the "
            "workspace slug from `modal token info`."
        )
    return match.group(1)


def interactive_login() -> None:
    result = subprocess.run(
        command("setup"), cwd=PROJECT_ROOT, env=_utf8_environment(), check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"Modal login failed with exit code {result.returncode}.")


def _json_value(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        values: list[Any] = []
        for index, character in enumerate(text):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                values.append(value)
            except json.JSONDecodeError:
                continue
        if values:
            return values[-1]
        raise RuntimeError("The Modal CLI returned output that was not valid JSON.")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def create_proxy_token() -> ProxyCredential:
    result = _completed(["workspace", "proxy-tokens", "create", "--json"])
    if result.returncode != 0:
        raise _failure("Creating a Modal proxy token", result)
    document = _json_value(result.stdout)
    token_id = next((value for value in _strings(document) if value.startswith("wk-")), None)
    token_secret = next(
        (value for value in _strings(document) if value.startswith("ws-")), None
    )
    if not token_id or not token_secret:
        raise RuntimeError(
            "Modal created a proxy token, but codex-modal could not parse its ID and secret. "
            "The secret is only shown once; create another token after updating this wrapper."
        )
    return ProxyCredential(token_id, token_secret)


def allow_proxy_token(token_id: str, environment_name: str) -> None:
    result = _completed(
        ["workspace", "proxy-tokens", "allow", token_id, environment_name]
    )
    if result.returncode != 0:
        raise _failure(
            f"Associating the proxy token with Modal environment {environment_name!r}",
            result,
        )


def list_endpoints(environment_name: str | None = None) -> list[dict[str, Any]]:
    arguments = ["endpoint", "list", "--json"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    result = _completed(arguments)
    if result.returncode != 0:
        raise _failure("Listing Modal endpoints", result)
    if not result.stdout.strip():
        return []
    document = _json_value(result.stdout)
    if isinstance(document, dict):
        for key in ("endpoints", "data", "items"):
            if isinstance(document.get(key), list):
                document = document[key]
                break
    if not isinstance(document, list):
        raise RuntimeError("Modal endpoint list returned an unexpected JSON shape.")
    return [row for row in document if isinstance(row, dict)]


def list_apps(environment_name: str | None = None) -> list[dict[str, Any]]:
    arguments = ["app", "list", "--json"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    result = _completed(arguments)
    if result.returncode != 0:
        raise _failure("Listing Modal apps", result)
    if not result.stdout.strip():
        return []
    document = _json_value(result.stdout)
    if isinstance(document, dict):
        for key in ("apps", "data", "items"):
            if isinstance(document.get(key), list):
                document = document[key]
                break
    if not isinstance(document, list):
        raise RuntimeError("Modal app list returned an unexpected JSON shape.")
    return [row for row in document if isinstance(row, dict)]


def create_endpoint(arguments: list[str]) -> tuple[str, str | None]:
    """Run endpoint creation with live output and return (combined output, endpoint ID)."""

    process = subprocess.Popen(
        command("endpoint", "create", *arguments),
        cwd=PROJECT_ROOT,
        env=_utf8_environment(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    chunks: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        chunks.append(line)
        print(line, end="", flush=True)
    return_code = process.wait()
    output = "".join(chunks)
    if return_code != 0:
        detail = output.strip()
        raise RuntimeError(
            f"Modal endpoint creation failed with exit code {return_code}"
            + (f": {detail}" if detail else ".")
        )
    match = ENDPOINT_ID_SEARCH.search(output)
    return output, match.group(1) if match else None


def deploy_app(
    module: str,
    app_name: str,
    environment_name: str | None,
    environment: dict[str, str],
) -> tuple[str, str | None]:
    """Deploy a Modal app with live build output and return its exact app ID when shown."""

    arguments = ["deploy", "--name", app_name, "-m", module]
    if environment_name:
        arguments[1:1] = ["--env", environment_name]
    process = subprocess.Popen(
        command(*arguments),
        cwd=PROJECT_ROOT,
        env=_utf8_environment(environment),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    chunks: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        chunks.append(line)
        print(line, end="", flush=True)
    return_code = process.wait()
    output = "".join(chunks)
    if return_code != 0:
        detail = output.strip()
        raise RuntimeError(
            f"Modal app deployment failed with exit code {return_code}"
            + (f": {detail}" if detail else ".")
        )
    match = APP_ID_SEARCH.search(output)
    return output, match.group(1) if match else None


def invoke_deployed_function(
    app_name: str,
    function_name: str,
    environment_name: str | None,
) -> Any:
    """Invoke one function from a deployed app using the pinned Modal SDK."""

    try:
        import modal

        function = modal.Function.from_name(
            app_name,
            function_name,
            environment_name=environment_name,
        )
        with modal.enable_output():
            return function.remote()
    except Exception as error:
        raise RuntimeError(
            f"Invoking Modal function {app_name!r}/{function_name!r} failed: {error}"
        ) from error


def stop_endpoint(
    endpoint_id: str,
    environment_name: str | None = None,
    *,
    quiet: bool = False,
) -> bool:
    if not ENDPOINT_ID_PATTERN.fullmatch(endpoint_id):
        raise ValueError(f"Refusing to stop invalid endpoint ID {endpoint_id!r}.")
    arguments = ["endpoint", "stop", endpoint_id, "--yes"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    try:
        result = _completed(arguments, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        if not quiet:
            print(f"Endpoint cleanup failed: {error}", file=sys.stderr)
        return False
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if output and not quiet:
        print(output)
    lowered = output.lower()
    return result.returncode == 0 or "already stopped" in lowered or "not found" in lowered


def stop_app(
    app_id: str,
    environment_name: str | None = None,
    *,
    quiet: bool = False,
) -> bool:
    if not APP_ID_PATTERN.fullmatch(app_id):
        raise ValueError(f"Refusing to stop invalid app ID {app_id!r}.")
    arguments = ["app", "stop", app_id, "--yes"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    try:
        result = _completed(arguments, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        if not quiet:
            print(f"App cleanup failed: {error}", file=sys.stderr)
        return False
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if output and not quiet:
        print(output)
    lowered = output.lower()
    return result.returncode == 0 or "already stopped" in lowered or "not found" in lowered


def delete_owned_volume(
    volume_name: str,
    environment_name: str | None = None,
    *,
    quiet: bool = False,
) -> bool:
    if not OWNED_VOLUME_PATTERN.fullmatch(volume_name):
        raise ValueError(
            f"Refusing to delete non-wrapper-owned Volume name {volume_name!r}."
        )
    arguments = ["volume", "delete", volume_name, "--yes", "--allow-missing"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    try:
        result = _completed(arguments, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        if not quiet:
            print(f"Volume cleanup failed: {error}", file=sys.stderr)
        return False
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if output and not quiet:
        print(output)
    lowered = output.lower()
    return result.returncode == 0 or "not found" in lowered or "does not exist" in lowered


def scrubbed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in list(environment):
        upper = key.upper()
        if upper.startswith("MODAL_PROXY_TOKEN") or upper in {
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
        }:
            environment.pop(key, None)
    return _utf8_environment(environment)
