"""Wrapper option parsing while preserving Codex's own argument surface."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from .paths import PRESETS_PATH


VALID_REASONING = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
ROUTING_REGIONS = {"us-west", "us-east", "ca-central", "eu-west", "ap-south"}
ENDPOINT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
ENDPOINT_HOST = re.compile(
    r"^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.([a-z0-9-]+)\.modal\.direct$"
)


@dataclass
class WrapperOptions:
    action: str = "run"
    preset: str | None = None
    model: str | None = None
    custom_hf_repo: str | None = None
    custom_hf_revision: str | None = None
    hf_token_env: str | None = None
    endpoint_name: str | None = None
    use_endpoint: str | None = None
    environment_name: str | None = None
    routing_region: str = "us-west"
    compute_regions: list[str] = field(default_factory=list)
    colocate_compute: bool = False
    context_window: int | None = None
    reasoning_effort: str | None = None
    reasoning_levels: tuple[str, ...] | None = None
    startup_timeout: int = 2700
    wait_for_endpoint: bool = True
    keep_endpoint: bool = False
    persist_history: bool = True
    dry_run: bool = False
    pick: bool = False
    force_token: bool = False
    codex_arguments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedModel:
    preset_name: str
    base_model: str
    display_model: str
    custom_hf_repo: str | None
    context_window: int
    reasoning_effort: str
    reasoning_levels: tuple[str, ...]


VALUE_OPTIONS = {
    "--modal-preset": "preset",
    "--modal-model": "model",
    "--modal-custom-hf-repo": "custom_hf_repo",
    "--modal-custom-hf-revision": "custom_hf_revision",
    "--modal-hf-token-env": "hf_token_env",
    "--modal-endpoint-name": "endpoint_name",
    "--modal-use-endpoint": "use_endpoint",
    "--modal-env": "environment_name",
    "--modal-routing-region": "routing_region",
    "--modal-context-window": "context_window",
    "--modal-reasoning-effort": "reasoning_effort",
    "--modal-reasoning-levels": "reasoning_levels",
    "--modal-startup-timeout": "startup_timeout",
}


def _set_action(options: WrapperOptions, action: str) -> None:
    if options.action != "run" and options.action != action:
        raise ValueError(f"Wrapper actions {options.action!r} and {action!r} cannot be combined.")
    options.action = action


def parse_arguments(arguments: list[str]) -> WrapperOptions:
    options = WrapperOptions()
    index = 0
    if arguments and arguments[0] in {"setup", "cleanup"}:
        options.action = arguments[0]
        index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            options.codex_arguments.append("--")
            options.codex_arguments.extend(arguments[index + 1 :])
            break
        if argument in VALUE_OPTIONS or argument == "--modal-compute-region":
            if index + 1 >= len(arguments):
                raise ValueError(f"{argument} requires a value.")
            value = arguments[index + 1]
            if argument == "--modal-compute-region":
                options.compute_regions.append(value)
            else:
                attribute = VALUE_OPTIONS[argument]
                if attribute in {"context_window", "startup_timeout"}:
                    try:
                        value = int(value)
                    except ValueError as error:
                        raise ValueError(f"{argument} requires an integer.") from error
                elif attribute == "reasoning_levels":
                    value = tuple(part.strip() for part in value.split(",") if part.strip())
                setattr(options, attribute, value)
            index += 2
            continue
        flags = {
            "--modal-help": "help",
            "--modal-setup": "setup",
            "--modal-cleanup": "cleanup",
            "--modal-list": "list",
        }
        if argument in flags:
            _set_action(options, flags[argument])
        elif argument == "--modal-pick":
            options.pick = True
        elif argument == "--modal-colocate-compute":
            options.colocate_compute = True
        elif argument == "--modal-no-wait":
            options.wait_for_endpoint = False
        elif argument == "--modal-keep-endpoint":
            options.keep_endpoint = True
        elif argument == "--modal-no-history":
            options.persist_history = False
        elif argument == "--modal-dry-run":
            options.dry_run = True
        elif argument == "--modal-force-token":
            options.force_token = True
        elif argument.startswith("--modal-"):
            raise ValueError(f"Unknown wrapper option {argument!r}. Run codex-modal --modal-help.")
        else:
            options.codex_arguments.append(argument)
        index += 1
    validate_wrapper_options(options)
    return options


def validate_wrapper_options(options: WrapperOptions) -> None:
    if options.context_window is not None and options.context_window < 8192:
        raise ValueError("--modal-context-window must be at least 8192.")
    if options.startup_timeout < 30:
        raise ValueError("--modal-startup-timeout must be at least 30 seconds.")
    if options.routing_region not in ROUTING_REGIONS:
        raise ValueError(f"Unsupported Modal routing region {options.routing_region!r}.")
    for region in options.compute_regions:
        if not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError(f"Invalid Modal compute region {region!r}.")
    if options.colocate_compute and options.compute_regions:
        raise ValueError(
            "--modal-colocate-compute and --modal-compute-region are mutually exclusive."
        )
    if options.custom_hf_revision and not options.custom_hf_repo:
        raise ValueError("--modal-custom-hf-revision requires --modal-custom-hf-repo.")
    if options.hf_token_env and not options.custom_hf_repo:
        raise ValueError("--modal-hf-token-env requires --modal-custom-hf-repo.")
    if options.use_endpoint and options.endpoint_name:
        raise ValueError("--modal-use-endpoint and --modal-endpoint-name are mutually exclusive.")
    if options.endpoint_name:
        assert_endpoint_name(options.endpoint_name)


def load_presets() -> dict[str, Any]:
    try:
        document = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not load model presets from {PRESETS_PATH}: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("presets"), dict):
        raise RuntimeError(f"Invalid preset document: {PRESETS_PATH}")
    return document


def choose_interactively(options: WrapperOptions, document: dict[str, Any]) -> None:
    presets = list(document["presets"].items())
    print("Available model presets:")
    for index, (name, preset) in enumerate(presets, start=1):
        print(f"  {index}) {name} - {preset.get('description', '')}")
    print("  A) arbitrary Hugging Face model or fine-tune")
    choice = input("Selection: ").strip()
    if choice.lower() == "a":
        options.model = input("Modal catalog/base model repo ID: ").strip()
        custom = input("Custom Hugging Face fine-tune repo ID (blank for base model): ").strip()
        if custom:
            options.custom_hf_repo = custom
        return
    try:
        selected = int(choice)
    except ValueError as error:
        raise ValueError("Invalid model selection.") from error
    if selected < 1 or selected > len(presets):
        raise ValueError("Invalid model selection.")
    options.preset = presets[selected - 1][0]


def resolve_model(options: WrapperOptions, document: dict[str, Any]) -> ResolvedModel:
    if options.pick:
        choose_interactively(options, document)
    default_name = str(document.get("default", ""))
    preset_name = options.preset or default_name
    preset = document["presets"].get(preset_name)
    if not isinstance(preset, dict):
        raise ValueError(f"Unknown preset {preset_name!r}. Run codex-modal --modal-list.")
    base_model = options.model or str(preset.get("base_model", ""))
    if not base_model or any(character.isspace() for character in base_model):
        raise ValueError("The Modal catalog/base model repo ID is empty or invalid.")
    display_model = options.custom_hf_repo or base_model
    arbitrary = options.model is not None and options.model != preset.get("base_model")
    context_window = options.context_window or (
        131072 if arbitrary else int(preset["context_window"])
    )
    reasoning_effort = options.reasoning_effort or (
        "high" if arbitrary else str(preset["reasoning_effort"])
    )
    reasoning_levels = options.reasoning_levels or tuple(
        ("low", "medium", "high") if arbitrary else preset["reasoning_levels"]
    )
    if reasoning_effort not in VALID_REASONING:
        raise ValueError(f"Unsupported Codex reasoning effort {reasoning_effort!r}.")
    if any(level not in VALID_REASONING for level in reasoning_levels):
        raise ValueError("One or more --modal-reasoning-levels values are unsupported.")
    if reasoning_effort not in reasoning_levels:
        reasoning_levels = (*reasoning_levels, reasoning_effort)
    return ResolvedModel(
        preset_name=preset_name,
        base_model=base_model,
        display_model=display_model,
        custom_hf_repo=options.custom_hf_repo,
        context_window=context_window,
        reasoning_effort=reasoning_effort,
        reasoning_levels=tuple(reasoning_levels),
    )


def assert_endpoint_name(name: str) -> None:
    if not ENDPOINT_NAME.fullmatch(name):
        raise ValueError(
            "Modal endpoint names must be lowercase DNS labels of at most 63 characters."
        )


def generated_endpoint_name(display_model: str) -> str:
    leaf = display_model.rsplit("/", 1)[-1].lower()
    leaf = re.sub(r"[^a-z0-9]+", "-", leaf).strip("-") or "model"
    suffix = secrets.token_hex(4)
    name = f"codex-{leaf[:43]}-{suffix}".strip("-")
    assert_endpoint_name(name)
    return name


def endpoint_target(options: WrapperOptions, display_model: str) -> tuple[str, str, str]:
    """Return endpoint name, hostname/model slug, and shared Responses base URL."""

    region = options.routing_region
    if options.use_endpoint:
        host_match = ENDPOINT_HOST.fullmatch(options.use_endpoint)
        if host_match:
            name, region = host_match.groups()
            options.routing_region = region
        else:
            name = options.use_endpoint
            assert_endpoint_name(name)
    else:
        name = options.endpoint_name or generated_endpoint_name(display_model)
    host = f"{name}.{region}.modal.direct"
    return name, host, f"https://inference.{region}.modal.direct/v1"


BLOCKED_CODEX_COMMANDS = {
    "login",
    "cloud",
    "app",
    "app-server",
    "mcp-server",
    "exec-server",
    "remote-control",
    "update",
}
NO_INFERENCE_COMMANDS = {
    "help",
    "completion",
    "features",
    "doctor",
    "debug",
    "mcp",
    "plugin",
    "sandbox",
    "apply",
    "archive",
    "delete",
    "unarchive",
    "logout",
}


def _before_double_dash(arguments: list[str]) -> list[str]:
    try:
        return arguments[: arguments.index("--")]
    except ValueError:
        return arguments


def assert_isolated_codex_arguments(arguments: list[str]) -> None:
    visible = _before_double_dash(arguments)
    forbidden_long = (
        "--model",
        "--config",
        "--profile",
        "--oss",
        "--local-provider",
        "--search",
        "--enable",
        "--remote",
        "--remote-auth-token-env",
    )
    for argument in visible:
        if argument in forbidden_long or any(
            argument.startswith(f"{option}=") for option in forbidden_long
        ):
            raise ValueError(
                f"Codex option {argument!r} is blocked because it can escape the Modal-only "
                "provider/configuration. Use the corresponding --modal-* option instead."
            )
        if argument in {"-m", "-c", "-p"} or re.fullmatch(r"-[mcp].+", argument):
            raise ValueError(
                f"Codex option {argument!r} is blocked because it can override the isolated model."
            )
    command_name = first_codex_word(visible)
    if command_name in BLOCKED_CODEX_COMMANDS:
        raise ValueError(
            f"Codex command {command_name!r} is blocked because it leaves the local "
            "Modal-only inference path. Run the regular codex executable explicitly if intended."
        )


def first_codex_word(arguments: list[str]) -> str | None:
    options_with_values = {
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
        "-s",
        "--sandbox",
        "-i",
        "--image",
        "--disable",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if argument in options_with_values:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in options_with_values if option.startswith("--")):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return None


def codex_needs_inference(arguments: list[str]) -> bool:
    visible = _before_double_dash(arguments)
    if any(argument in {"-h", "--help", "-V", "--version"} for argument in visible):
        return False
    command_name = first_codex_word(arguments)
    return command_name not in NO_INFERENCE_COMMANDS
