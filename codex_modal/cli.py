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
from .custom_deploy import (
    CUSTOM_APP_MODULE,
    CustomDeployment,
    build_custom_deployment,
    custom_app_name,
    custom_app_base_url,
)
from .lifecycle import (
    EndpointRoute,
    cleanup_owned_resource,
    create_owned_app_state,
    create_owned_state,
    direct_endpoint_base_url,
    finish_state,
    resolve_app_id,
    resolve_endpoint_id,
    run_process_with_heartbeat,
    start_watchdog,
    sweep_stale_endpoints,
    wait_for_attached_endpoint,
    wait_for_direct_app,
    wait_for_endpoint,
)
from .docker import SandboxOptions, SandboxSpec, run_sandbox
from .options import (
    WrapperOptions,
    assert_isolated_codex_arguments,
    codex_needs_inference,
    codex_sets_own_policy,
    endpoint_target,
    load_presets,
    parse_arguments,
    resolve_model,
)
from .paths import CODEX_HOME, STATE_ROOT, caller_cwd


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
  --modal-model-revision REV         Exact model/base revision for a custom app
  --modal-custom-hf-repo REPO        Fine-tuned Hugging Face weights
  --modal-custom-hf-revision REV     Revision of custom weights
  --modal-hf-token-env ENV           Read a private HF token from this environment variable
  --modal-context-window TOKENS      Override advertised context (minimum 8192)
  --modal-reasoning-effort LEVEL     minimal|low|medium|high|xhigh|max|ultra
  --modal-reasoning-levels CSV       Levels advertised in Codex's model catalog

Non-catalog custom app:
  --modal-self-managed               Deploy the generic SGLang Modal app
  --modal-gpu TYPE[:COUNT]           Required explicit GPU allocation, e.g. B200:2
  --modal-base-volume NAME           Optional Volume holding reusable base weights
  --modal-base-volume-path PATH      Base checkpoint path inside that Volume
  --modal-sglang-image IMAGE         Override the pinned SGLang container image
  --modal-sglang-arg ARG             Repeat --flag or --flag=value engine arguments
  --modal-cpu CORES                  Serving CPU allocation (default: 8)
  --modal-memory MIB                 Serving memory allocation (default: 98304)
  --modal-scaledown-window SECONDS   Idle scale-down window (default: 300)
  --modal-target-inputs COUNT        Target input concurrency (default: 16)

Endpoint placement/lifecycle:
  --modal-endpoint-name NAME         Explicit name for a newly-created endpoint
  --modal-use-endpoint NAME_OR_HOST  Attach to an existing endpoint; never stop it
  --modal-use-app NAME               Attach to an existing self-managed app; never stop it
  --modal-env ENV                    Modal environment
  --modal-routing-region REGION      us-west (default), us-east, ca-central, eu-west, ap-south
  --modal-compute-region REGION      Repeat to allow multiple compute regions
  --modal-colocate-compute           Put compute in the routing region
  --modal-startup-timeout SECONDS    Provisioning timeout (default: 2700)
  --modal-no-wait                    Launch Codex before readiness is confirmed
  --modal-keep-endpoint              Keep a newly-created endpoint after Codex exits
  --modal-cleanup                    Recover stale wrapper-owned endpoints

Local Docker sandbox (Codex runs in yolo mode, isolated from this machine):
  --docker                           Run Codex in a throwaway container instead of here
  --docker-upstream URL              OpenAI-compatible base URL; skips Modal entirely
  --docker-upstream-auth-env ENV     Host env var holding that upstream's token
  --docker-model-slug SLUG           Model name sent to a --docker-upstream server
  --docker-allow-port PORT           Repeat to widen the egress port allow-list (80,443)
  --docker-allow-host HOST           Repeat to restrict egress to these hosts (.suffix ok)
  --docker-firewall MODE             enforce (default), warn, or off
  --docker-copy-in DIR               Copy a directory into the sandbox workspace
  --docker-export DIR                Where to write logs (default .codex-modal/docker-runs)
  --docker-export-work               Also copy the sandbox workspace back out
  --docker-rust-log SPEC             RUST_LOG for Codex inside the container
  --docker-memory / --docker-cpus / --docker-pids
                                     Container resource caps (4g / 2 / 1024)
  --docker-image REF                 Use an existing local sandbox image
  --docker-codex-version VER         Codex npm version baked into the image
  --docker-packages "A B"            Extra apt packages for the sandbox image
  --docker-build                     Force a rebuild of the sandbox images
  --docker-keep                      Keep containers/volumes for post-mortem
  --docker-shell                     Open a shell in the sandbox instead of Codex
  --docker-env-note TEXT             Extra text appended to the sandbox AGENTS.md
  --docker-no-env-doc                Do not auto-write the environment AGENTS.md
  --docker-prune                     Remove leftover sandbox containers/networks/volumes

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

Catalog-compatible fine-tune:
  codex-modal --modal-model Qwen/Qwen3.6-27B \
    --modal-custom-hf-repo your-org/your-finetune

For a model outside Modal's endpoint catalog, add --modal-self-managed and an explicit
--modal-gpu. Codex provider/model/config override flags are deliberately blocked.
'''


DIRECT_RESPONSES_REASONING = {"minimal", "low", "medium", "high"}


def _direct_reasoning_settings(
    effort: str, levels: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    direct_levels = tuple(
        level for level in levels if level in DIRECT_RESPONSES_REASONING
    )
    if effort not in DIRECT_RESPONSES_REASONING:
        effort = "high"
    if effort not in direct_levels:
        direct_levels = (*direct_levels, effort)
    return effort, direct_levels


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


def _app_stop_command(app_id: str, environment_name: str | None) -> str:
    arguments = [sys.executable, "-m", "modal", "app", "stop", app_id, "--yes"]
    if environment_name:
        arguments.extend(["--env", environment_name])
    return shlex.join(arguments)


def _volume_delete_command(volume_name: str, environment_name: str | None) -> str:
    arguments = [
        sys.executable,
        "-m",
        "modal",
        "volume",
        "delete",
        volume_name,
        "--yes",
        "--allow-missing",
    ]
    if environment_name:
        arguments.extend(["--env", environment_name])
    return shlex.join(arguments)


def _sandbox_options(options: WrapperOptions) -> SandboxOptions:
    from .docker.sandbox import DEFAULT_CODEX_VERSION

    danger = not codex_sets_own_policy(options.codex_arguments)
    if not danger:
        print(
            "An explicit Codex approval/sandbox option was supplied, so the container "
            "will not run Codex in bypass mode."
        )
    return SandboxOptions(
        codex_version=options.docker_codex_version or DEFAULT_CODEX_VERSION,
        agent_image=options.docker_image,
        build=options.docker_build,
        extra_packages=options.docker_packages or "",
        allow_ports=tuple(options.docker_allow_ports) or (80, 443),
        allow_hosts=tuple(options.docker_allow_hosts),
        firewall=options.docker_firewall,
        memory=options.docker_memory or "4g",
        cpus=options.docker_cpus or "2",
        pids_limit=options.docker_pids or 1024,
        copy_in=Path(options.docker_copy_in).expanduser() if options.docker_copy_in else None,
        export_dir=Path(options.docker_export).expanduser() if options.docker_export else None,
        export_work=options.docker_export_work,
        keep=options.docker_keep,
        rust_log=options.docker_rust_log or "error",
        danger=danger,
        command=("/bin/bash",) if options.docker_shell else (),
        env_doc=not options.docker_no_env_doc,
        env_note=options.docker_env_note,
    )


def _upstream_authorization(options: WrapperOptions) -> str | None:
    """Read the upstream credential on the host; it never enters the sandbox."""

    name = options.docker_upstream_auth_env
    if not name:
        return None
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name!r} is empty or missing.")
    lowered = value.lower()
    if lowered.startswith("bearer ") or lowered.startswith("basic "):
        return value
    return f"Bearer {value}"


def _launch_in_docker(
    options: WrapperOptions,
    settings: ModelSettings,
    *,
    upstream_url: str,
    authorization: str | None,
) -> int:
    spec = SandboxSpec(
        settings=settings,
        upstream_url=upstream_url,
        upstream_authorization=authorization,
        codex_arguments=list(options.codex_arguments),
    )
    return run_sandbox(spec, _sandbox_options(options))


def _require_action_only(options: WrapperOptions) -> None:
    if options.codex_arguments:
        raise ValueError(
            f"The codex-modal {options.action!r} action cannot be combined with Codex arguments."
        )


def _run_docker_upstream(options: WrapperOptions, resolved) -> int:
    """Container mode against an arbitrary OpenAI-compatible upstream, no Modal."""

    upstream = options.docker_upstream or ""
    authorization = _upstream_authorization(options)
    settings = ModelSettings(
        slug=options.docker_model_slug or resolved.display_model,
        display_model=resolved.display_model,
        context_window=resolved.context_window,
        reasoning_effort=resolved.reasoning_effort,
        reasoning_levels=resolved.reasoning_levels,
        provider_base_url=upstream,
        persist_history=options.persist_history,
    )
    if options.dry_run:
        print("Dry run passed (Docker sandbox, no Modal resources).")
        print(f"  Model:            {settings.display_model}")
        print(f"  Model slug:       {settings.slug}")
        print(f"  Broker upstream:  {upstream}")
        print(
            "  Credential:       "
            + (
                f"host env {options.docker_upstream_auth_env} -> broker only"
                if authorization
                else "none"
            )
        )
        print(f"  Egress ports:     {options.docker_allow_ports or [80, 443]}")
        print(f"  Firewall:         {options.docker_firewall}")
        print(f"  Log export root:  {STATE_ROOT / 'docker-runs'}")
        return 0
    return _launch_in_docker(
        options, settings, upstream_url=upstream, authorization=authorization
    )


def _run(options: WrapperOptions) -> int:
    document = load_presets()
    resolved = resolve_model(options, document)
    assert_isolated_codex_arguments(options.codex_arguments)
    if options.force_token:
        raise ValueError("--modal-force-token is only valid with `codex-modal setup`.")

    workspace = caller_cwd()
    if options.docker and options.docker_upstream:
        return _run_docker_upstream(options, resolved)
    endpoint_name, endpoint_host, shared_base_url = endpoint_target(
        options, resolved.display_model
    )
    initial_custom_app_name = (
        custom_app_name(endpoint_name, "workspace")
        if options.self_managed
        else endpoint_name
    )
    custom_deployment: CustomDeployment | None = (
        build_custom_deployment(options, resolved, initial_custom_app_name)
        if options.self_managed
        else None
    )
    needs_inference = codex_needs_inference(options.codex_arguments)
    if custom_deployment is not None:
        initial_base_url = custom_app_base_url(
            "workspace", custom_deployment.app_name, options.routing_region
        )
        model_slug = (
            resolved.display_model
            if needs_inference or options.dry_run
            else f"offline.{options.routing_region}.modal.invalid"
        )
    elif options.use_app:
        initial_base_url = custom_app_base_url(
            "ws", endpoint_name, options.routing_region
        )
        model_slug = (
            resolved.display_model
            if needs_inference or options.dry_run
            else f"offline.{options.routing_region}.modal.invalid"
        )
    else:
        initial_base_url = shared_base_url
        model_slug = (
            endpoint_host
            if needs_inference or options.dry_run
            else f"offline.{options.routing_region}.modal.invalid"
        )
    settings = ModelSettings(
        slug=model_slug,
        display_model=resolved.display_model,
        context_window=resolved.context_window,
        reasoning_effort=resolved.reasoning_effort,
        reasoning_levels=resolved.reasoning_levels,
        provider_base_url=initial_base_url,
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
            if custom_deployment is not None:
                print("  Serving mode:     self-managed SGLang Modal app")
                print(f"  GPU allocation:   {custom_deployment.gpu}")
                if custom_deployment.base_volume:
                    print(f"  Reuse Volume:     {custom_deployment.base_volume}")
                print(f"  Temporary Volume: {custom_deployment.volume_name}")
            elif options.use_app:
                print("  Serving mode:     existing self-managed SGLang Modal app")
            else:
                print("  Serving mode:     Modal managed endpoint")
            print(f"  Endpoint model:   {model_slug}")
            print(f"  Responses URL:    {initial_base_url}")
            print(f"  Isolated home:    {CODEX_HOME}")
            if options.use_endpoint or options.use_app:
                action = "attach; never stop"
            elif options.keep_endpoint:
                action = "create; keep"
            else:
                action = "create; exact-ID cleanup + detached watchdog"
            print(f"  Resource action:  {action}")
            print(
                "  Telemetry:        analytics/feedback off; OTel log/metric/trace exporters none"
            )
            print(
                "  Model isolation:  one-model catalog; main/review pinned; agents, memories, "
                "Guardian, apps, search, and remote compaction disabled"
            )
            if options.docker:
                print(
                    "  Execution:        local Docker sandbox; Codex bypasses approvals "
                    "inside the container"
                )
                print(f"  Broker upstream:  {initial_base_url} (token attached by broker)")
                print(f"  Egress ports:     {options.docker_allow_ports or [80, 443]}")
                print(f"  Firewall:         {options.docker_firewall}")
                print(f"  Log export root:  {STATE_ROOT / 'docker-runs'}")
        finally:
            configuration.clean_up()
        return 0

    if not needs_inference:
        if options.docker:
            return _launch_in_docker(
                options, settings, upstream_url=initial_base_url, authorization=None
            )
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
    configuration = None
    owned_endpoint_id: str | None = None
    owned_app_id: str | None = None
    state_path: Path | None = None
    try:
        _ensure_modal_login(options)
        workspace_slug = modal_cli.current_workspace_slug()
        if custom_deployment is not None:
            safe_app_name = custom_app_name(endpoint_name, workspace_slug)
            if safe_app_name != endpoint_name:
                print(
                    f"Shortening custom app name to {safe_app_name!r} so the full "
                    "Modal Server DNS label remains valid."
                )
            endpoint_name = safe_app_name
            custom_deployment = build_custom_deployment(
                options, resolved, endpoint_name
            )
        if custom_deployment is not None or options.use_app:
            direct_base_url = custom_app_base_url(
                workspace_slug, endpoint_name, options.routing_region
            )
        else:
            direct_base_url = direct_endpoint_base_url(
                workspace_slug, endpoint_name, options.routing_region
            )
        route = EndpointRoute(
            model_slug=resolved.display_model,
            base_url=direct_base_url,
            source="direct-unverified",
        )

        if custom_deployment is not None:
            sweep_stale_endpoints(verbose=True)
            print(
                f"Deploying custom Modal app {endpoint_name!r} for "
                f"{resolved.display_model} on {custom_deployment.gpu}..."
            )
            try:
                _, parsed_id = modal_cli.deploy_app(
                    CUSTOM_APP_MODULE,
                    endpoint_name,
                    options.environment_name,
                    custom_deployment.deployment_environment(),
                )
            except RuntimeError:
                try:
                    owned_app_id = resolve_app_id(
                        endpoint_name, options.environment_name
                    )
                except RuntimeError:
                    pass
                raise
            owned_app_id = parsed_id or resolve_app_id(
                endpoint_name, options.environment_name
            )
            if not options.keep_endpoint:
                state_path = create_owned_app_state(
                    owned_app_id,
                    endpoint_name,
                    custom_deployment.volume_name,
                    options.environment_name,
                )
                start_watchdog(state_path)
            print(
                "Preparing custom weights on CPU before allocating the serving GPU(s)..."
            )
            manifest = modal_cli.invoke_deployed_function(
                endpoint_name, "prepare_model", options.environment_name
            )
            if isinstance(manifest, dict):
                print(
                    "Model preparation complete: "
                    f"downloaded {manifest.get('downloaded_files', '?')} file(s); "
                    f"reused {manifest.get('reused_files', '?')} base file(s)."
                )
            if options.wait_for_endpoint:
                route = wait_for_direct_app(
                    direct_base_url=direct_base_url,
                    preferred_model=resolved.display_model,
                    proxy_token=credential.combined,
                    timeout_seconds=options.startup_timeout,
                    state_path=state_path,
                )
            else:
                print(
                    "WARNING: readiness waiting is disabled; Codex may fail until the route is live.",
                    file=sys.stderr,
                )

        elif not (options.use_endpoint or options.use_app):
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
                route = wait_for_endpoint(
                    endpoint_id=owned_endpoint_id,
                    endpoint_host=endpoint_host,
                    environment_name=options.environment_name,
                    shared_base_url=shared_base_url,
                    direct_base_url=direct_base_url,
                    preferred_model=resolved.display_model,
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
            if options.use_app:
                route = wait_for_direct_app(
                    direct_base_url=direct_base_url,
                    preferred_model=resolved.display_model,
                    proxy_token=credential.combined,
                    timeout_seconds=options.startup_timeout,
                    state_path=None,
                )
            else:
                route = wait_for_attached_endpoint(
                    endpoint_host=endpoint_host,
                    shared_base_url=shared_base_url,
                    direct_base_url=direct_base_url,
                    preferred_model=resolved.display_model,
                    proxy_token=credential.combined,
                    timeout_seconds=options.startup_timeout,
                )
        else:
            print(
                "WARNING: readiness waiting is disabled; Codex may fail until the route is live.",
                file=sys.stderr,
            )

        reasoning_effort = resolved.reasoning_effort
        reasoning_levels = resolved.reasoning_levels
        if route.source.startswith("direct"):
            reasoning_effort, reasoning_levels = _direct_reasoning_settings(
                reasoning_effort, reasoning_levels
            )
            if reasoning_effort != resolved.reasoning_effort:
                print(
                    f"Direct Responses route cannot stream reasoning effort "
                    f"{resolved.reasoning_effort!r}; using {reasoning_effort!r}."
                )

        settings = ModelSettings(
            slug=route.model_slug,
            display_model=resolved.display_model,
            context_window=resolved.context_window,
            reasoning_effort=reasoning_effort,
            reasoning_levels=reasoning_levels,
            provider_base_url=route.base_url,
            persist_history=options.persist_history,
        )
        if options.docker:
            print(
                f"Launching Codex in a local container against the {route.source} "
                "Responses route; the Modal proxy token stays on this machine and is "
                "attached by the egress broker."
            )
            return _launch_in_docker(
                options,
                settings,
                upstream_url=route.base_url,
                authorization=f"Bearer {credential.combined}",
            )

        configuration = prepare_run_configuration(
            settings,
            workspace=workspace,
            proxy_token=credential.combined,
            validate_strict=True,
        )
        if options.hf_token_env:
            configuration.environment.pop(options.hf_token_env, None)

        print(
            f"Launching Codex with model {route.model_slug} through the "
            f"{route.source} Responses route and an isolated Modal-only config..."
        )
        return run_process_with_heartbeat(
            configuration.command_prefix() + options.codex_arguments,
            cwd=workspace,
            environment=configuration.environment,
            state_path=state_path,
        )
    finally:
        if owned_app_id and custom_deployment is not None:
            if options.keep_endpoint:
                print(
                    "WARNING: keeping custom app and model Volume by request. Stop/delete "
                    "them later with:\n  "
                    + _app_stop_command(owned_app_id, options.environment_name)
                    + "\n  "
                    + _volume_delete_command(
                        custom_deployment.volume_name, options.environment_name
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    f"Stopping wrapper-owned Modal app {owned_app_id} and deleting "
                    f"temporary Volume {custom_deployment.volume_name!r}..."
                )
                cleaned = cleanup_owned_resource(
                    {
                        "resource_kind": "app",
                        "app_id": owned_app_id,
                        "volume_name": custom_deployment.volume_name,
                        "environment": options.environment_name,
                    }
                )
                if cleaned and state_path is not None:
                    finish_state(state_path)
                elif not cleaned:
                    if state_path is not None:
                        detail = " The detached watchdog will retry after this wrapper exits."
                    else:
                        detail = " No watchdog was registered before the failure."
                    print(
                        "WARNING: immediate custom-app cleanup failed." + detail,
                        file=sys.stderr,
                    )
                    print(
                        "Manual fallback:\n  "
                        + _app_stop_command(owned_app_id, options.environment_name)
                        + "\n  "
                        + _volume_delete_command(
                            custom_deployment.volume_name, options.environment_name
                        ),
                        file=sys.stderr,
                    )
        elif custom_deployment is not None and not options.keep_endpoint:
            modal_cli.delete_owned_volume(
                custom_deployment.volume_name, options.environment_name
            )
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
        if configuration is not None:
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
    if options.action == "docker-prune":
        _require_action_only(options)
        from .docker.sandbox import prune_sandboxes

        containers, networks, volumes = prune_sandboxes()
        print(
            f"Removed {containers} sandbox container(s), {networks} network(s), "
            f"and {volumes} volume(s)."
        )
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
