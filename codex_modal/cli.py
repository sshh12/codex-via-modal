"""The cross-platform codex-modal command-line application."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import modal_cli
from .bootstrap import ensure_dependencies
from .codex_config import ModelSettings, prepare_run_configuration
from .credentials import ProxyCredential, from_environment, load_proxy_token, store_proxy_token
from .lifecycle import (
    create_owned_state,
    finish_state,
    resolve_endpoint_id,
    run_process_with_heartbeat,
    start_watchdog,
    sweep_stale_endpoints,
    wait_for_attached_endpoint,
    wait_for_endpoint,
)
from .options import (
    WrapperOptions,
    assert_isolated_codex_arguments,
    codex_needs_inference,
    endpoint_target,
    load_presets,
    parse_arguments,
    resolve_model,
)
from .paths import CODEX_HOME, caller_cwd


HELP = r'''codex-modal - run Codex exclusively through a lifecycle-managed Modal endpoint

Usage:
  codex-modal setup [--modal-env ENV]
  codex-modal cleanup
  codex-modal [WRAPPER OPTIONS] [CODEX OPTIONS/COMMAND/PROMPT]

First use:
  codex-modal setup
  codex-modal --modal-dry-run
  codex-modal

Model selection:
  --modal-list                       List local presets
  --modal-pick                       Pick a preset or arbitrary model interactively
  --modal-preset NAME                Select a preset
  --modal-model REPO                 Modal catalog/base Hugging Face repo ID
  --modal-custom-hf-repo REPO        Fine-tuned Hugging Face weights
  --modal-custom-hf-revision REV     Revision of custom weights
  --modal-hf-token-env ENV           Read a private HF token from this environment variable
  --modal-context-window TOKENS      Override advertised context (minimum 8192)
  --modal-reasoning-effort LEVEL     minimal|low|medium|high|xhigh|max|ultra
  --modal-reasoning-levels CSV       Levels advertised in Codex's model catalog

Endpoint placement/lifecycle:
  --modal-endpoint-name NAME         Explicit name for a newly-created endpoint
  --modal-use-endpoint NAME_OR_HOST  Attach to an existing endpoint; never stop it
  --modal-env ENV                    Modal environment
  --modal-routing-region REGION      us-west (default), us-east, ca-central, eu-west, ap-south
  --modal-compute-region REGION      Repeat to allow multiple compute regions
  --modal-colocate-compute           Put compute in the routing region
  --modal-startup-timeout SECONDS    Provisioning timeout (default: 2700)
  --modal-no-wait                    Launch Codex before readiness is confirmed
  --modal-keep-endpoint              Keep a newly-created endpoint after Codex exits
  --modal-cleanup                    Recover stale wrapper-owned endpoints

Setup and behavior:
  setup, --modal-setup               Login and create/store a Modal proxy token once
  --modal-force-token                Create a replacement proxy token during setup
  cleanup                            Same as --modal-cleanup
  --modal-no-history                 Disable isolated Codex transcript persistence
  --modal-dry-run                    Validate config without login or endpoint creation
  --modal-help                       Show this help
  --                                Stop wrapper parsing and pass the rest to Codex

DeepSeek V4 default:
  codex-modal --modal-preset deepseek-v4-flash-0731

Arbitrary compatible fine-tune:
  codex-modal --modal-model Qwen/Qwen3.6-27B \
    --modal-custom-hf-repo your-org/your-finetune

The base model must be supported by Modal's endpoint catalog and must be the architecture
for custom weights. Codex provider/model/config override flags are deliberately blocked.
'''


def _print_presets(document: dict[str, object]) -> None:
    default_name = str(document.get("default", ""))
    presets = document.get("presets", {})
    assert isinstance(presets, dict)
    for name, raw in presets.items():
        preset = raw if isinstance(raw, dict) else {}
        marker = "*" if name == default_name else " "
        print(
            f"{marker} {name}: {preset.get('base_model', '')}"
            f" - {preset.get('description', '')}"
        )


def _setup(options: WrapperOptions) -> ProxyCredential:
    ensure_dependencies()
    if not modal_cli.token_login_present():
        print("Modal CLI login is not configured; starting `modal setup`...")
        modal_cli.interactive_login()
    existing = load_proxy_token()
    environment_credential = from_environment()
    if options.force_token and environment_credential is not None:
        raise RuntimeError(
            "--modal-force-token cannot replace an environment-provided token. Unset "
            "MODAL_PROXY_TOKEN (or its ID/secret pair) first."
        )
    if existing is not None and not options.force_token:
        credential = existing
        storage = "the existing environment/keyring/private-file configuration"
        print("A Modal proxy token is already configured; reusing it.")
    else:
        print("Creating a Modal workspace proxy token...")
        credential = modal_cli.create_proxy_token()
        storage = store_proxy_token(credential)
        print(f"Stored the new proxy token in {storage}; its secret was not printed.")
        if options.force_token:
            print("The prior token was not deleted; revoke it in Modal if it is no longer needed.")
    if options.environment_name:
        modal_cli.allow_proxy_token(credential.token_id, options.environment_name)
        print(
            f"Associated proxy token {credential.token_id} with Modal environment "
            f"{options.environment_name!r}."
        )
    print("codex-modal setup is complete.")
    return credential


def _credential_or_setup(options: WrapperOptions) -> ProxyCredential:
    ensure_dependencies()
    credential = load_proxy_token()
    if credential is not None:
        return credential
    if sys.stdin.isatty() and sys.stdout.isatty():
        answer = input("No Modal proxy token is configured. Run one-time setup now? [Y/n] ").strip()
        if answer.lower() in {"", "y", "yes"}:
            return _setup(options)
    raise RuntimeError(
        "No Modal proxy token is configured. Run `codex-modal setup` or set "
        "MODAL_PROXY_TOKEN=wk-<id>.ws-<secret>."
    )


def _ensure_modal_login(options: WrapperOptions) -> None:
    if modal_cli.token_login_present():
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        answer = input("Modal CLI login is missing. Run one-time setup now? [Y/n] ").strip()
        if answer.lower() in {"", "y", "yes"}:
            _setup(options)
            return
    raise RuntimeError("Modal CLI login is missing. Run `codex-modal setup`.")


def _creation_arguments(options: WrapperOptions, endpoint_name: str, base_model: str) -> list[str]:
    arguments = [
        "--name",
        endpoint_name,
        "--model",
        base_model,
        "--routing-region",
        options.routing_region,
    ]
    if options.environment_name:
        arguments.extend(["--env", options.environment_name])
    for region in options.compute_regions:
        arguments.extend(["--compute-region", region])
    if options.colocate_compute:
        arguments.append("--colocate-compute")
    if options.custom_hf_repo:
        arguments.extend(["--custom-hf-repo", options.custom_hf_repo])
        if options.custom_hf_revision:
            arguments.extend(["--custom-hf-revision", options.custom_hf_revision])
        if options.hf_token_env:
            value = os.environ.get(options.hf_token_env)
            if not value:
                raise RuntimeError(
                    f"Environment variable {options.hf_token_env!r} is empty or missing."
                )
            arguments.extend(["--custom-hf-token", value])
    return arguments


def _stop_command(endpoint_id: str, environment_name: str | None) -> str:
    arguments = [sys.executable, "-m", "modal", "endpoint", "stop", endpoint_id, "--yes"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    return shlex.join(arguments)


def _require_action_only(options: WrapperOptions) -> None:
    if options.codex_arguments:
        raise ValueError(
            f"The codex-modal {options.action!r} action cannot be combined with Codex arguments."
        )


def _run(options: WrapperOptions) -> int:
    document = load_presets()
    resolved = resolve_model(options, document)
    assert_isolated_codex_arguments(options.codex_arguments)
    if options.force_token:
        raise ValueError("--modal-force-token is only valid with `codex-modal setup`.")

    workspace = caller_cwd()
    endpoint_name, endpoint_host, shared_base_url = endpoint_target(
        options, resolved.display_model
    )
    needs_inference = codex_needs_inference(options.codex_arguments)
    model_slug = endpoint_host if needs_inference or options.dry_run else (
        f"offline.{options.routing_region}.modal.invalid"
    )
    settings = ModelSettings(
        slug=model_slug,
        display_model=resolved.display_model,
        context_window=resolved.context_window,
        reasoning_effort=resolved.reasoning_effort,
        reasoning_levels=resolved.reasoning_levels,
        shared_base_url=shared_base_url,
        persist_history=options.persist_history,
    )

    if options.dry_run:
        configuration = prepare_run_configuration(
            settings, workspace=workspace, proxy_token=None, validate_strict=True
        )
        try:
            print("Dry run passed.")
            print(f"  Base model:       {resolved.base_model}")
            if resolved.custom_hf_repo:
                print(f"  Custom weights:   {resolved.custom_hf_repo}")
            print(f"  Endpoint model:   {endpoint_host}")
            print(f"  Responses URL:    {shared_base_url}")
            print(f"  Isolated home:    {CODEX_HOME}")
            if options.use_endpoint:
                action = "attach; never stop"
            elif options.keep_endpoint:
                action = "create; keep"
            else:
                action = "create; exact-ID cleanup + detached watchdog"
            print(f"  Endpoint action:  {action}")
            print(
                "  Telemetry:        analytics/feedback off; OTel log/metric/trace exporters none"
            )
            print(
                "  Model isolation:  one-model catalog; main/review pinned; agents, memories, "
                "Guardian, apps, search, and remote compaction disabled"
            )
        finally:
            configuration.clean_up()
        return 0

    if not needs_inference:
        configuration = prepare_run_configuration(
            settings, workspace=workspace, proxy_token=None, validate_strict=False
        )
        try:
            return run_process_with_heartbeat(
                configuration.command_prefix(strict=False, include_profile=False)
                + options.codex_arguments,
                cwd=workspace,
                environment=configuration.environment,
                state_path=None,
            )
        finally:
            configuration.clean_up()

    credential = _credential_or_setup(options)
    configuration = prepare_run_configuration(
        settings,
        workspace=workspace,
        proxy_token=credential.combined,
        validate_strict=True,
    )
    if options.hf_token_env:
        configuration.environment.pop(options.hf_token_env, None)

    owned_endpoint_id: str | None = None
    state_path: Path | None = None
    try:
        if not options.use_endpoint:
            _ensure_modal_login(options)
            sweep_stale_endpoints(verbose=True)
            print(
                f"Creating Modal endpoint {endpoint_name!r} for {resolved.display_model}..."
            )
            _, parsed_id = modal_cli.create_endpoint(
                _creation_arguments(options, endpoint_name, resolved.base_model)
            )
            owned_endpoint_id = parsed_id or resolve_endpoint_id(
                endpoint_name, options.environment_name
            )
            if not options.keep_endpoint:
                state_path = create_owned_state(
                    owned_endpoint_id, endpoint_name, options.environment_name
                )
                start_watchdog(state_path)
            if options.wait_for_endpoint:
                wait_for_endpoint(
                    endpoint_id=owned_endpoint_id,
                    endpoint_host=endpoint_host,
                    environment_name=options.environment_name,
                    shared_base_url=shared_base_url,
                    proxy_token=credential.combined,
                    timeout_seconds=options.startup_timeout,
                    state_path=state_path,
                )
            else:
                print(
                    "WARNING: readiness waiting is disabled; Codex may fail until the route is live.",
                    file=sys.stderr,
                )

        elif options.wait_for_endpoint:
            wait_for_attached_endpoint(
                endpoint_host=endpoint_host,
                shared_base_url=shared_base_url,
                proxy_token=credential.combined,
                timeout_seconds=options.startup_timeout,
            )

        print(
            f"Launching Codex with model {endpoint_host} and an isolated Modal-only config..."
        )
        return run_process_with_heartbeat(
            configuration.command_prefix() + options.codex_arguments,
            cwd=workspace,
            environment=configuration.environment,
            state_path=state_path,
        )
    finally:
        if owned_endpoint_id:
            if options.keep_endpoint:
                print(
                    "WARNING: keeping endpoint by request. Stop it later with:\n  "
                    + _stop_command(owned_endpoint_id, options.environment_name),
                    file=sys.stderr,
                )
            else:
                print(f"Stopping wrapper-owned Modal endpoint {owned_endpoint_id}...")
                stopped = modal_cli.stop_endpoint(
                    owned_endpoint_id, options.environment_name
                )
                if stopped and state_path is not None:
                    finish_state(state_path)
                elif not stopped:
                    print(
                        "WARNING: immediate cleanup failed. The detached watchdog will retry "
                        "after this wrapper exits.",
                        file=sys.stderr,
                    )
                    print(
                        "Manual fallback: "
                        + _stop_command(owned_endpoint_id, options.environment_name),
                        file=sys.stderr,
                    )
        configuration.clean_up()


def _main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    if options.action == "help":
        print(HELP)
        return 0
    if options.action == "list":
        _require_action_only(options)
        _print_presets(load_presets())
        return 0
    if options.action == "setup":
        _require_action_only(options)
        _setup(options)
        return 0
    if options.action == "cleanup":
        _require_action_only(options)
        ensure_dependencies()
        if not modal_cli.token_login_present():
            raise RuntimeError("Modal CLI login is missing. Run `codex-modal setup`.")
        stopped, active = sweep_stale_endpoints(verbose=True)
        print(f"Cleanup complete: stopped {stopped}; left {active} active owner(s) alone.")
        return 0
    return _run(options)


def main(arguments: list[str] | None = None) -> int:
    try:
        return _main(list(sys.argv[1:] if arguments is None else arguments))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"codex-modal: error: {error}", file=sys.stderr)
        return 1
