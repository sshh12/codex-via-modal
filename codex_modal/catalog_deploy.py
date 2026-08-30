"""Deploy a self-managed model straight from a JSON catalog.

    python -m codex_modal.catalog_deploy self-managed-catalog.json 0
    python -m codex_modal.catalog_deploy self-managed-catalog.json glm-5.3-flash-uncensored
    python -m codex_modal.catalog_deploy self-managed-catalog.json 0 --gpu H200:4 --dry-run

The catalog is the single source of truth for how a model is brought up; this
module turns one entry into the equivalent ``codex-modal --modal-self-managed``
invocation and runs it, then prints the served endpoint URL. Adding a model is a
catalog entry, not code.

Catalog shape - a JSON object with a ``models`` array (a bare array also works).
Each entry:

    name              label for selection by name                (required)
    source_repo       Hugging Face repo to serve                 (required)
    endpoint_name     Modal app / DNS label; also the attach name (required)
    gpu               e.g. "H100:8" (``--gpu`` overrides)         (required)
    region            Modal routing region        (default us-west)
    hf_token_env      env var holding the HF token, if the repo is gated
    context_window    served context length            (default 262144)
    reasoning_effort  default effort                       (default high)
    reasoning_levels  advertised effort ladder      (default low/high/max)
    scaledown_window  idle seconds before scale-to-zero  (default 900)
    keep_endpoint     leave the endpoint up after deploy  (default true)
    sglang_args       list of extra "--flag=value" SGLang args
    cpu, memory, target_inputs, sglang_image   optional overrides
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .custom_deploy import custom_app_base_url, custom_app_name
from .modal_cli import current_workspace_slug
from .paths import PROJECT_ROOT, SELF_MANAGED_CATALOG_PATH


def _load_models(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"error: cannot read catalog {path}: {error}")
    if isinstance(document, list):
        models = document
    elif isinstance(document, dict) and isinstance(document.get("models"), list):
        models = document["models"]
    else:
        raise SystemExit(f"error: {path} must be a JSON array or an object with a 'models' array.")
    if not all(isinstance(entry, dict) for entry in models):
        raise SystemExit(f"error: every entry in {path} must be a JSON object.")
    return models


def _select(models: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    if selector.lstrip("-").isdigit():
        index = int(selector)
        if not -len(models) <= index < len(models):
            raise SystemExit(f"error: index {index} is out of range (0..{len(models) - 1}).")
        return models[index]
    for entry in models:
        if str(entry.get("name", "")) == selector or str(entry.get("endpoint_name", "")) == selector:
            return entry
    names = ", ".join(str(entry.get("name", "?")) for entry in models) or "<none>"
    raise SystemExit(f"error: no catalog entry named {selector!r}. Available: {names}")


def _codex_modal_argv(entry: dict[str, Any], gpu: str) -> list[str]:
    argv = [
        sys.executable, "-m", "codex_modal",
        "--modal-self-managed",
        "--modal-gpu", gpu,
        "--modal-custom-hf-repo", str(entry["source_repo"]),
        "--modal-endpoint-name", str(entry["endpoint_name"]),
        "--modal-routing-region", str(entry.get("region", "us-west")),
        "--modal-context-window", str(int(entry.get("context_window", 262144))),
        "--modal-reasoning-effort", str(entry.get("reasoning_effort", "high")),
        "--modal-reasoning-levels", ",".join(str(x) for x in entry.get("reasoning_levels", ["low", "high", "max"])),
        "--modal-scaledown-window", str(int(entry.get("scaledown_window", 900))),
    ]
    if entry.get("keep_endpoint", True):
        argv.append("--modal-keep-endpoint")
    if entry.get("hf_token_env"):
        argv += ["--modal-hf-token-env", str(entry["hf_token_env"])]
    if entry.get("sglang_image"):
        argv += ["--modal-sglang-image", str(entry["sglang_image"])]
    for optional, flag in (("cpu", "--modal-cpu"), ("memory", "--modal-memory"),
                           ("target_inputs", "--modal-target-inputs"),
                           ("startup_timeout", "--modal-startup-timeout")):
        if entry.get(optional) is not None:
            argv += [flag, str(entry[optional])]
    for sglang_arg in entry.get("sglang_args", []):
        argv += ["--modal-sglang-arg", str(sglang_arg)]
    argv += ["--", "exec", "--skip-git-repo-check", "Reply with exactly OK."]
    return argv


def _served_url(endpoint_name: str, region: str) -> str | None:
    try:
        slug = current_workspace_slug()
        return custom_app_base_url(slug, custom_app_name(endpoint_name, slug), region)
    except Exception as error:  # noqa: BLE001 - slug lookup can fail many ways; non-fatal
        print(f"warning: could not compute the served URL yet ({error}); read it from the "
              "logs below.", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modal-deploy", description="Deploy a self-managed model from a JSON catalog."
    )
    parser.add_argument(
        "catalog", nargs="?", default=str(SELF_MANAGED_CATALOG_PATH),
        help=f"path to the catalog JSON (default: {SELF_MANAGED_CATALOG_PATH.name})",
    )
    parser.add_argument("selector", help="entry index (e.g. 0) or name")
    parser.add_argument("--gpu", help="override the entry's gpu, e.g. H100:8")
    parser.add_argument("--dry-run", action="store_true", help="print the command and exit")
    args = parser.parse_args(argv)

    entry = _select(_load_models(Path(args.catalog).expanduser()), args.selector)
    for required in ("source_repo", "endpoint_name", "gpu"):
        if not entry.get(required):
            raise SystemExit(f"error: catalog entry is missing required field {required!r}.")
    gpu = args.gpu or str(entry["gpu"])
    endpoint_name = str(entry["endpoint_name"])
    region = str(entry.get("region", "us-west"))

    command = _codex_modal_argv(entry, gpu)
    url = _served_url(endpoint_name, region)

    print(f"catalog   : {args.catalog}")
    print(f"model     : {entry.get('name', endpoint_name)}  ({entry['source_repo']})")
    print(f"gpu       : {gpu}   region: {region}   endpoint: {endpoint_name}")
    if url:
        print(f"served URL: {url}")
    print(f"command   : python -m {' '.join(command[2:])}\n")

    if args.dry_run:
        return 0

    result = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    if result.returncode == 0:
        print(f"\nEndpoint {endpoint_name!r} is up. Attach by name, e.g.:")
        print(f"  codex-modal --modal-use-app {endpoint_name} -- exec 'hi'")
        print(f"  blue-green-red:  --model <role>=modal:{endpoint_name}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
