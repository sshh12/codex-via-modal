# codex-via-modal

Run the Codex CLI against a Hugging Face model served by a Modal managed endpoint. The
wrapper starts an endpoint for a Codex session, pins every Codex model path to that
endpoint, and stops the exact endpoint ID when Codex exits.

The default preset is `deepseek-ai/DeepSeek-V4-Flash-0731`. Arbitrary models and
fine-tunes are supported when Modal has a compatible endpoint recipe for the base
architecture.

## Quick start

Requirements: Python 3.10+, `codex-cli 0.146.1`, a Modal account, and the `codex`
executable on `PATH`. The launchers create `.venv` and install the pinned Modal client
on first use.

PowerShell:

```powershell
.\codex-modal.ps1 setup
.\codex-modal.ps1 --modal-dry-run
.\codex-modal.ps1
```

macOS/Linux:

```sh
./codex-modal.sh setup
./codex-modal.sh --modal-dry-run
./codex-modal.sh
```

`setup` is a Python CLI subcommand, not a separate script. It performs `modal setup`
when login is missing, creates a workspace proxy token, and stores that token without
printing its secret. Pass an environment when Modal RBAC requires an explicit token
association:

```powershell
.\codex-modal.ps1 setup --modal-env prod
```

The token lookup order is:

1. `MODAL_PROXY_TOKEN` or the separate ID/secret environment variables.
2. The operating-system keyring.
3. A warned, owner-only, git-ignored `.codex-modal/credentials.json` fallback when no
   keyring backend exists.

## Models

Use the DeepSeek V4 preset:

```powershell
.\codex-modal.ps1 --modal-preset deepseek-v4-flash-0731
```

List or interactively choose presets:

```powershell
.\codex-modal.ps1 --modal-list
.\codex-modal.ps1 --modal-pick
```

Use another Modal catalog model:

```powershell
.\codex-modal.ps1 --modal-model Qwen/Qwen3.6-27B
```

Serve custom Hugging Face weights. `--modal-model` is the supported catalog/base
architecture; `--modal-custom-hf-repo` is the fine-tuned checkpoint:

```powershell
$env:HF_PRIVATE_TOKEN = "hf_..."
.\codex-modal.ps1 `
  --modal-model Qwen/Qwen3.6-27B `
  --modal-custom-hf-repo your-org/your-finetune `
  --modal-custom-hf-revision main `
  --modal-hf-token-env HF_PRIVATE_TOKEN
```

The Modal CLI currently accepts a private Hugging Face token as a command argument.
The wrapper never prints it and removes the selected environment variable from the
Codex child process, but a same-user process may be able to inspect command arguments
while endpoint creation is running.

For an arbitrary model, the wrapper uses conservative generic Codex metadata (131,072
tokens and high reasoning) unless you override it:

```powershell
.\codex-modal.ps1 `
  --modal-model org/model `
  --modal-context-window 262144 `
  --modal-reasoning-effort high `
  --modal-reasoning-levels low,high
```

## Endpoint lifecycle

On a normal inference launch the wrapper:

1. Validates an isolated Codex config and one-model catalog.
2. Recovers any stale endpoint with a wrapper-owned state record.
3. Creates a unique Modal endpoint and resolves its exact `ep-...` ID.
4. Starts an embedded detached watchdog via `python -m codex_modal __watchdog ...`.
5. Waits for both Modal status and the shared Responses route.
6. Runs Codex in the directory from which you invoked the launcher.
7. Stops only that exact endpoint ID in `finally`; the watchdog retries after a crash.

There is no `scripts/` lifecycle helper. Manual stale recovery is also a CLI command:

```powershell
.\codex-modal.ps1 cleanup
```

Attach to an already-running endpoint without taking ownership:

```powershell
.\codex-modal.ps1 --modal-use-endpoint my-endpoint
.\codex-modal.ps1 --modal-use-endpoint my-endpoint.us-east.modal.direct
```

Keep a newly created endpoint intentionally:

```powershell
.\codex-modal.ps1 --modal-keep-endpoint
```

The wrapper prints the exact stop command on exit. Modal bills active endpoint compute;
scale-to-zero means idle compute is not billed, while `--modal-keep-endpoint` still
leaves the endpoint resource deployed.

## Codex isolation and telemetry

Codex runs with `CODEX_HOME` set to the git-ignored `.codex-modal/codex-home`, not the
user's normal `~/.codex`. Each concurrent run receives a unique profile and catalog.
The catalog contains exactly one model: the Modal endpoint hostname used by the shared
Responses API.

Modal also exposes each endpoint's direct server URL with
`/v1/chat/completions`, where the request model is the Hugging Face repo ID. That route
is useful for `curl` diagnostics, but Codex 0.146.1 custom providers require the
Responses wire API. The wrapper therefore uses Modal's shared `/v1/responses` adapter,
not the direct Chat Completions URL.

The wrapper applies these controls at CLI precedence, above project config:

- Main model, review model, default subagent model, provider, and catalog are pinned.
- The provider is the Modal shared Responses URL with `requires_openai_auth = false`.
- OpenAI/Codex API credentials and base-URL overrides are removed from the Codex child
  environment.
- Analytics and feedback are disabled. OpenTelemetry log, metric, and trace exporters
  are explicitly `none`, and prompt logging is disabled.
- Auto-review/Guardian, agents, memories, apps/plugins, web search, image/browser tools,
  tool suggestions, and remote compaction are disabled.
- Model/provider/profile/config override flags and Codex commands that leave this local
  inference path are rejected by the wrapper.

Project instructions still load normally. Inference commands use `--strict-config`;
Codex 0.146.1 utility commands that explicitly reject profiles or strict mode run from
the isolated base config without making inference requests.

Session transcripts are stored only under the isolated home by default. Use
`--modal-no-history` to disable transcript persistence.

## Useful options

```text
--modal-routing-region us-east
--modal-compute-region us-west       # repeatable
--modal-colocate-compute
--modal-env prod
--modal-startup-timeout 2700
--modal-no-wait
--modal-no-history
--modal-dry-run
```

Run `codex-modal --modal-help` for the complete wrapper surface. All unrecognized
arguments are passed to Codex, subject to the provider-isolation checks.

## Development

Offline verification never creates a paid endpoint:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\codex-modal.ps1 --modal-dry-run
```

## References

- [Modal Endpoints](https://modal.com/docs/guide/endpoints)
- [Modal Endpoint integrations](https://modal.com/docs/guide/endpoint-integrations)
- [DeepSeek V4 Flash 0731 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [Codex configuration](https://developers.openai.com/codex/config-basic)
