"""In-container launcher for `codex-modal --docker`.

The host wrapper copies this package plus a run spec into the sandbox and starts
it as an unprivileged user. The same configuration generator used for local runs
builds the isolated `CODEX_HOME` here, so the Modal-only provider pinning, the
one-model catalog, and the telemetry lockdown are identical in both modes. The
difference is that the provider base URL points at the egress broker, the
sandbox holds no upstream credential, and Codex is allowed to run without
approvals because the container is the boundary.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .codex_config import ModelSettings, prepare_run_configuration
from .options import first_codex_word
from .paths import CODEX_HOME

RUN_SPEC_PATH = Path("/sandbox/run.json")
PLACEHOLDER_TOKEN = "wk-sandbox-no-credential.ws-sandbox-no-credential"

# Subcommands that define their own copy of the bypass flag; for anything else it
# has to be supplied before the subcommand.
SUBCOMMAND_DANGER = {"exec", "e", "review", "resume", "fork"}

DANGER_OVERRIDES = (
    ("approval_policy", '"never"'),
    ("sandbox_mode", '"danger-full-access"'),
)


def _load_spec(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read the sandbox run spec at {path}: {error}") from error
    if not isinstance(document, dict):
        raise RuntimeError(f"The sandbox run spec at {path} is not a JSON object.")
    return document


def _settings(spec: dict[str, object]) -> ModelSettings:
    return ModelSettings(
        slug=str(spec["slug"]),
        display_model=str(spec["display_model"]),
        context_window=int(spec["context_window"]),
        reasoning_effort=str(spec["reasoning_effort"]),
        reasoning_levels=tuple(str(level) for level in spec["reasoning_levels"]),
        provider_base_url=str(spec["provider_base_url"]),
        persist_history=bool(spec.get("persist_history", True)),
    )


ENV_DOC_BEGIN = "<!-- codex-modal sandbox environment (auto-generated) -->"
ENV_DOC_END = "<!-- end codex-modal sandbox environment -->"


def _environment_doc(spec: dict[str, object]) -> str:
    ports = ", ".join(str(port) for port in spec.get("allow_ports", []) or []) or "80, 443"
    hosts = spec.get("allow_hosts") or []
    host_line = (
        "restricted to: " + ", ".join(str(host) for host in hosts)
        if hosts
        else "any public host"
    )
    note = str(spec.get("env_note") or "").strip()
    note_block = f"\nTask notes: {note}\n" if note else ""
    return f"""{ENV_DOC_BEGIN}
# Runtime environment

Disposable Debian Linux container on the user's machine. Approvals and Codex's
sandbox are off on purpose: run commands directly. The container is the boundary
and is discarded on exit, so you cannot see or reach the host; work freely in
`/work` (HOME is `/home/agent`).

- Root: you are user `agent` with passwordless `sudo`; the rootfs is writable.
- Network: internet only via the preset HTTP/HTTPS proxy, port(s) {ports}, {host_line};
  no direct egress and no external DNS (the proxy resolves names). apt/pip/npm/
  curl/git are already proxy-configured. The host and LAN are unreachable.
- Installed: Python (pip/uv), Node (npm/pnpm), Go, Rust, JDK, gcc/clang/make/
  cmake/gdb; git, ripgrep, jq, sqlite3, ffmpeg; nmap, tcpdump, netcat, socat,
  dig, whois; headless Chromium at `/usr/bin/chromium`. Install anything else
  with `sudo apt-get`, `pip`, or `npm`.
- Chromium: drive with Playwright (Python) or puppeteer-core; launch with
  `executable_path=/usr/bin/chromium` and args `--no-sandbox --disable-dev-shm-usage`.
  For external sites, point the browser at the proxy (Chromium ignores the env
  proxy): Playwright `proxy={{"server": "$HTTP_PROXY"}}`.
- LLM: an OpenAI-compatible endpoint for the same model you run is at
  `$SANDBOX_MODEL_BASE_URL` (model `$SANDBOX_MODEL_NAME`); no API key needed (the
  proxy adds auth). Use it for sub-tasks, e.g. POST `.../chat/completions` or
  `.../responses`.
- Subagents: you can delegate by running Codex recursively —
  `codex exec --skip-git-repo-check "<task>"` spawns a fresh independent agent on
  the same model (it inherits this config). Run several with `&` + `wait` for
  parallel fan-out; wrap it in a script if that helps. Each subagent is one-shot
  (no shared live context) — pass what it needs in the task and via files.
- Files: creating data/output files with shell redirection or a heredoc is fine;
  reserve `apply_patch` for source you edit. Don't deliberate over which to use.
{note_block}{ENV_DOC_END}
"""


def _write_environment_doc(workspace: Path, spec: dict[str, object]) -> None:
    """Publish the environment description as AGENTS.md.

    Codex has no append-system-prompt flag; AGENTS.md in the workspace is the
    supported way to add instructions to the model's context. If the workspace
    already has one (e.g. via --docker-copy-in), append our block instead of
    clobbering it, and refresh it in place on a re-run.
    """

    agents = workspace / "AGENTS.md"
    doc = _environment_doc(spec)
    try:
        existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    except OSError:
        existing = ""
    if ENV_DOC_BEGIN in existing and ENV_DOC_END in existing:
        head, _, rest = existing.partition(ENV_DOC_BEGIN)
        _, _, tail = rest.partition(ENV_DOC_END)
        merged = f"{head.rstrip()}\n\n{doc.strip()}\n{tail.lstrip()}".strip() + "\n"
    elif existing.strip():
        merged = f"{existing.rstrip()}\n\n{doc}"
    else:
        merged = doc
    try:
        agents.write_text(merged, encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"codex-sandbox: could not write AGENTS.md: {error}", file=sys.stderr)


def _prepare_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    gitconfig = Path.home() / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n\tname = Codex Sandbox\n\temail = codex-sandbox@localhost\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    inside = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inside.returncode != 0:
        # Codex expects a repository; without one both the TUI and `exec` nag or
        # refuse, and the agent loses its own undo path.
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _enable_recursive_codex(settings, catalog_path: Path) -> None:
    """Make a bare `codex exec` inside the container reuse this run's model.

    The parent receives model/provider/catalog via -c flags, so `config.toml`
    alone doesn't pin them. Writing them into the container's CODEX_HOME lets a
    subagent (`codex exec "..."`) inherit the same isolated Modal-only route with
    no flags. Container-only: the host's config is never touched.
    """

    config = CODEX_HOME / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return
    # Point the provider at the broker's model port (the base config carries a
    # default us-west URL we must not leave in place for children).
    new_url = f'base_url = {json.dumps(settings.provider_base_url)}'
    text = re.sub(r'(?m)^base_url = .*$', new_url, text, count=1)
    compact = max(8192, int(settings.context_window * 0.90))
    # These are top-level keys, so they must be inserted before the first TOML
    # table header — appending at the end would scope them under the last table.
    block = (
        "\n# A bare `codex exec` runs as an isolated subagent on the same model.\n"
        "# The container is the boundary, so subagents also run yolo.\n"
        f"model = {json.dumps(settings.slug)}\n"
        'model_provider = "modal"\n'
        f"model_catalog_json = {json.dumps(str(catalog_path))}\n"
        f"model_context_window = {settings.context_window}\n"
        f"model_auto_compact_token_limit = {compact}\n"
        f"model_reasoning_effort = {json.dumps(settings.reasoning_effort)}\n"
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
    )
    match = re.search(r'(?m)^\[', text)
    if match:
        merged = text[: match.start()] + block + "\n" + text[match.start() :]
    else:
        merged = text + block
    try:
        config.write_text(merged, encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"codex-sandbox: could not enable recursive codex: {error}", file=sys.stderr)


def build_command(
    prefix: list[str], codex_arguments: list[str], *, danger: bool
) -> list[str]:
    """Insert the bypass flag where the installed Codex CLI accepts it."""

    if not danger:
        return prefix + codex_arguments
    overrides: list[str] = []
    for key, value in DANGER_OVERRIDES:
        overrides.extend(["-c", f"{key}={value}"])
    flag = "--dangerously-bypass-approvals-and-sandbox"
    subcommand = first_codex_word(codex_arguments)
    if subcommand in SUBCOMMAND_DANGER:
        index = codex_arguments.index(subcommand)
        arguments = (
            codex_arguments[: index + 1] + [flag] + codex_arguments[index + 1 :]
        )
        return prefix + overrides + arguments
    return prefix + overrides + [flag] + codex_arguments


def main(argv: list[str] | None = None) -> int:
    extra = list(sys.argv[1:] if argv is None else argv)
    spec = _load_spec(RUN_SPEC_PATH)
    workspace = Path(str(spec.get("workspace", "/work")))
    _prepare_workspace(workspace)
    if spec.get("env_doc", True):
        _write_environment_doc(workspace, spec)

    shell_command = [str(item) for item in spec.get("command", []) or []]
    settings = _settings(spec)
    configuration = prepare_run_configuration(
        settings,
        workspace=workspace,
        proxy_token=PLACEHOLDER_TOKEN,
        validate_strict=bool(spec.get("validate_strict", True)) and not shell_command,
    )
    codex_arguments = [str(item) for item in spec.get("codex_arguments", [])] + extra
    danger = bool(spec.get("danger", True))
    if danger:
        # Enable bare `codex exec` recursion (subagents) inside the yolo sandbox.
        _enable_recursive_codex(settings, configuration.catalog_path)
    codex_command = build_command(
        configuration.command_prefix(), codex_arguments, danger=danger
    )

    environment = configuration.environment
    environment.setdefault("RUST_LOG", str(spec.get("rust_log", "info")))
    # Let the agent call the same model for its own LLM sub-tasks. The broker's
    # model port is OpenAI-compatible and injects the credential, so no key is
    # needed inside the sandbox.
    environment["SANDBOX_MODEL_BASE_URL"] = settings.provider_base_url
    environment["SANDBOX_MODEL_NAME"] = settings.slug
    for key, value in dict(spec.get("environment", {}) or {}).items():
        environment[str(key)] = str(value)

    if shell_command:
        # Debug entry: the firewall, the unprivileged user, and the generated
        # CODEX_HOME are all already in place, so `codex` is one command away.
        hint = Path(environment["CODEX_HOME"]) / "codex-command.txt"
        hint.write_text(
            " ".join(shlex.quote(part) for part in codex_command) + "\n", encoding="utf-8"
        )
        print(
            f"codex-sandbox: shell mode. The prepared Codex command is in {hint}.",
            file=sys.stderr,
            flush=True,
        )
        command = shell_command
    else:
        command = codex_command
        print(
            f"codex-sandbox: launching Codex ({settings.display_model}) against "
            f"{settings.provider_base_url} with"
            + (" approvals and sandboxing bypassed." if danger else " default policy."),
            file=sys.stderr,
            flush=True,
        )
    os.chdir(workspace)
    os.execvpe(command[0], command, environment)
    return 1  # pragma: no cover - execvpe does not return


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, KeyError, ValueError) as error:
        print(f"codex-sandbox: error: {error}", file=sys.stderr)
        raise SystemExit(1)
