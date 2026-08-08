# codex-via-modal

Run the Codex CLI against a Hugging Face model served on Modal. The wrapper can use
Modal's optimized managed Endpoints or deploy its own generic SGLang app for weights
outside the endpoint catalog. It pins every Codex model path to that server and cleans
up only the exact resources it created when Codex exits.

The default preset is `deepseek-ai/DeepSeek-V4-Flash-0731`. Catalog-compatible
fine-tunes use Modal's managed recipe. Other SGLang-compatible Hugging Face models can
use `--modal-self-managed` with an explicit GPU allocation and engine arguments.

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

For this managed-Endpoint path, the Modal CLI accepts a private Hugging Face token as a
command argument. The wrapper never prints it and removes the selected environment
variable from the Codex child process, but a same-user process may be able to inspect
command arguments while endpoint creation is running. The self-managed path below
injects that value as an ephemeral Modal Secret instead.

### Models outside the Modal catalog

`--modal-self-managed` deploys the checked-in generic SGLang app rather than calling
`modal endpoint create`. GPU choice is deliberately required because the wrapper cannot
infer a safe or affordable topology for an arbitrary checkpoint. Deploys pin Modal's
`2025.06` image builder, matching the managed Endpoint source recipes.

Serve a complete non-catalog Hugging Face repository (PowerShell):

```powershell
.\codex-modal.ps1 `
  --modal-self-managed `
  --modal-model your-org/your-model `
  --modal-model-revision <exact-hugging-face-commit> `
  --modal-gpu H100:2 `
  --modal-sglang-arg "--trust-remote-code" `
  --modal-startup-timeout 7200
```

The staging function runs on one CPU container, downloads the pinned repository into a
uniquely named temporary Modal Volume, and only then lets the first readiness request
allocate the serving GPU. Staging is incremental and retry-safe: it preserves complete
files, repairs missing or wrong-sized files, and verifies every file from Hugging Face
metadata before returning. Add engine-specific flags as repeatable
`--modal-sglang-arg` values using `--flag` or `--flag=value`; for example, parsers,
quantization settings, MoE backends, or speculative-decoding options from the model
card. Override the container with `--modal-sglang-image` when the checkpoint requires
another SGLang build.

Cost guardrails are on by default: custom apps keep zero warm containers, are capped at
one serving container, and scale that container to zero after 300 idle seconds. Change
the idle delay with `--modal-scaledown-window`; lower values save more idle compute but
can force another long model cold start while a Codex session is still open. Normal
exit stops the app and deletes its temporary Volume, and the detached watchdog retries
the same cleanup if the local wrapper crashes. `--modal-keep-endpoint` deliberately
keeps the app and Volume, although its GPU still scales to zero when idle.

Attach interactive Codex to an already-deployed custom app without redeploying,
restaging weights, or taking ownership of it:

```powershell
.\codex-modal.ps1 `
  --modal-use-app <modal-app-name> `
  --modal-model your-org/your-model
```

The app name is the value passed to `modal deploy --name` (not the full
`*.modal.direct` hostname). Exiting this attached Codex session does not stop or delete
the app; its configured idle scale-down policy still applies.

Modal Server URLs put the workspace, app name, and `server` suffix in one DNS label.
The wrapper deterministically shortens long generated app names when needed so the full
label stays within the DNS limit; the Hugging Face served-model ID is unchanged.

For a fine-tune whose repository mostly references unchanged base shards, reuse a base
checkpoint already cached in a Modal Volume:

```powershell
.\codex-modal.ps1 `
  --modal-self-managed `
  --modal-model base-org/base-model `
  --modal-model-revision <exact-base-commit> `
  --modal-custom-hf-repo your-org/your-finetune `
  --modal-custom-hf-revision <exact-finetune-commit> `
  --modal-base-volume <existing-modal-volume-name> `
  --modal-base-volume-path /path/inside/volume/to/base-snapshot `
  --modal-gpu B200:2 `
  --modal-sglang-arg "--trust-remote-code" `
  --modal-startup-timeout 7200
```

You can find the mounted Volume name and model path in the **Source** panel of an
existing Modal endpoint. `--modal-base-volume-path` is the model path with that source
code's mount prefix removed. Before creating any symlink, the staging function compares
the Hugging Face content identity and size at both exact revisions and verifies that the
base file exists. Files that do not match are downloaded from the fine-tune repository.

The generic app cannot guarantee that every Transformers repository is runnable:
SGLang architecture support, GPU memory, tensor parallelism, chat/reasoning parsers,
remote code, and quantization still have to match the model. Start from the model card's
known-good SGLang command and translate each extra flag to `--modal-sglang-arg`.

For an arbitrary model, the wrapper uses conservative generic Codex metadata (131,072
tokens and high reasoning) unless you override it:

```powershell
.\codex-modal.ps1 `
  --modal-model org/model `
  --modal-context-window 262144 `
  --modal-reasoning-effort high `
  --modal-reasoning-levels low,high
```

## Resource lifecycle

On a managed-Endpoint launch the wrapper:

1. Validates an isolated Codex config and one-model catalog.
2. Recovers any stale resource with a wrapper-owned state record.
3. Creates a unique Modal endpoint and resolves its exact `ep-...` ID.
4. Starts an embedded detached watchdog via `python -m codex_modal __watchdog ...`.
5. Waits for Modal's `live` status, then selects either the shared Responses route or
   a capability-checked direct `/v1/responses` route.
6. Runs Codex in the directory from which you invoked the launcher.
7. Stops only that exact endpoint ID in `finally`; the watchdog retries after a crash.

On a self-managed launch it instead:

1. Deploys a uniquely named Modal app and resolves its exact `ap-...` ID.
2. Creates one wrapper-prefixed temporary Volume for changed/downloaded weights.
3. Runs pinned-revision, file-completeness staging in one CPU container; any reusable
   base Volume is content-identity checked, read-only, and never owned by this wrapper.
4. Starts the same detached watchdog before staging or GPU startup.
5. Capability-checks the app's direct `/v1/responses` route and runs Codex.
6. Permanently stops that exact app ID, then deletes only its temporary Volume. A crash
   leaves the ownership record for the watchdog or `cleanup` command to retry.

There is no `scripts/` lifecycle helper. Manual stale recovery is also a CLI command:

```powershell
.\codex-modal.ps1 cleanup
```

Attach to an already-running endpoint without taking ownership:

```powershell
.\codex-modal.ps1 --modal-use-endpoint my-endpoint
.\codex-modal.ps1 --modal-use-endpoint my-endpoint.us-east.modal.direct
```

Keep a newly created endpoint, or a custom app plus its model Volume, intentionally:

```powershell
.\codex-modal.ps1 --modal-keep-endpoint
```

The wrapper prints exact stop/delete commands on exit. Modal bills active compute;
scale-to-zero means idle compute is not billed, while `--modal-keep-endpoint` still
leaves the resource deployed.

## Codex isolation and telemetry

Codex runs with `CODEX_HOME` set to the git-ignored `.codex-modal/codex-home`, not the
user's normal `~/.codex`. Each concurrent run receives a unique profile and catalog.
The catalog contains exactly one model: the endpoint hostname on Modal's shared route,
or the Hugging Face repo ID when using the endpoint's direct route.

Codex 0.146.1 custom providers require the Responses wire API. For managed endpoints,
the wrapper prefers Modal's documented shared route. If registration is delayed or
missing, or when using a self-managed app, it checks the direct server's model list and
OpenAPI document for `/v1/responses`, then uses that route with the Hugging Face repo
ID. Direct SGLang Responses streaming currently rejects reasoning levels above `high`,
so the wrapper maps `max`, `xhigh`, or `ultra` to `high` only on a direct route.

The wrapper applies these controls at CLI precedence, above project config:

- Main model, review model, default subagent model, provider, and catalog are pinned.
- The provider is the selected Modal Responses URL with
  `requires_openai_auth = false`.
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
--modal-self-managed --modal-gpu H100:2
--modal-sglang-arg "--trust-remote-code"
--modal-no-wait
--modal-no-history
--modal-dry-run
```

Run `codex-modal --modal-help` for the complete wrapper surface. All unrecognized
arguments are passed to Codex, subject to the provider-isolation checks.

## Development

Offline verification never creates a paid Modal resource:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\codex-modal.ps1 --modal-dry-run
```

## References

- [Modal Endpoints](https://modal.com/docs/guide/endpoints)
- [Modal Endpoint integrations](https://modal.com/docs/guide/endpoint-integrations)
- [Modal Servers](https://modal.com/docs/guide/servers)
- [Modal Volumes](https://modal.com/docs/guide/volumes)
- [DeepSeek V4 Flash 0731 model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [Codex configuration](https://developers.openai.com/codex/config-basic)
