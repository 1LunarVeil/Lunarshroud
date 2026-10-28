# Model manifests and runtime profiles

Status: **B1–B4 implemented**. Runtime profiles are selectable per launch;
model-manifest defaults are resolved with source tags; `runtime_id` is carried
into presets, instances and history; draft vocabulary and manifest minimum-build
checks run locally before launch; user model defaults can be saved/cleared;
`customFields` defaults feed `chat_template_kwargs`; `launch_guardrail` policy
and `model_key`/`variant_key` records are in place. Everything here is offline:
no downloader, catalog or remote fetch.

This document describes the design that keeps two things independent:

1. **what a model is** (identity, variants, companions, declared defaults), and
2. **which llama.cpp build runs it** (version, backend, capabilities).

The launcher still spawns a user-selected `llama-server.exe` with argv and talks
to it over loopback REST/WebSocket. Nothing here introduces LM Studio's engine
protocol, a downloader, or an IPC plugin. The **probe remains the authority** for
hard capabilities; manifests are optional overlays with provenance.

## Two registries + one resolver

```text
user-selected llama-server.exe + optional lunarshroud-runtime.json
        -> Runtime Registry (profiles, probe cache, preferences)
        -> chosen runtime_id selects the exe for schema / capabilities /
           command preview / preflight / launch

model.gguf + optional model.yaml / manifest.json
        -> Model Registry (identity, companions, declared defaults, warnings)

        -> Config Resolver (global defaults < manifest < user model defaults
                            < preset < launch overrides)
        -> param_builder -> argv -> llama-server.exe
```

The launch path stays the same: a user-selected executable plus argv and
loopback REST/WebSocket. No engine protocol, downloader or IPC plugin is added.
Runtime selection changes *which* local executable is spawned, not how it is
spoken to.

## Runtime selection

`runtime_id` may be supplied by the launch request, or pinned by a preset. The
backend resolves it in this order:

1. explicit `runtime_id` in the request;
2. `runtime_id` stored on the referenced preset;
3. `runtime-preferences.json` default for GGUF;
4. the `llama_server_path` in `config.json`.

A profile with `status != "ok"` or a missing executable is rejected with 400
rather than silently launching another build. Instances expose
`runtime_id`/`runtime_label`; history rows persist both (old databases are
migrated with idempotent `ALTER TABLE`), and history relaunch reuses the runtime
when it still exists.

## Model manifests

Accepted files, in priority order, beside the `.gguf`:

1. `model.yaml`
2. `model.yml`
3. `manifest.json`

YAML parsing uses the locked `PyYAML` dependency and is included in the
PyInstaller onedir (it is imported lazily, so the spec lists it explicitly).
Model manifests therefore work offline in packaged builds, not only from source.

The accepted subset follows the open [model.yaml](https://modelyaml.org/)
draft plus an optional `x-lunarshroud` extension.

```yaml
model: example-org/example-model
base:
  - key: example-org/base-gguf
    sources:
      - type: huggingface
        user: example-org
        repo: base
config:
  operation:
    fields:
      - key: llm.prediction.temperature
        value: 0.7
  load:
    fields:
      - key: llm.load.contextLength
        value: {checked: true, value: 32768}
metadataOverrides:
  architectures: [qwen35]
  contextLengths: [32768]
  vision: true
  trainedForToolUse: true
  minMemoryUsageBytes: 7400000000
customFields:
  - key: enableThinking
    displayName: Enable Thinking
    type: boolean
    defaultValue: true
    effects:
      - type: setJinjaVariable
        variable: enable_thinking
x-lunarshroud:
  companions:
    mmproj: mmproj-model-f16.gguf
    draft: model-draft-Q4_K_M.gguf
  requires:
    min_llama_cpp_build: 11698
    backends: [cuda12, vulkan]
    min_driver_version: "12040"
```

Rules:

- `owner/name` must match lowercase `owner/name` with dots/dashes/underscores.
- `metadataOverrides` is **declarative**. The UI shows it next to GGUF-probed
  facts and says which one wins (probe). It never silently replaces probe data.
- Companion paths are **relative to the model directory**; absolute paths and
  `..` traversal are rejected with a warning.
- `customFields` supports `boolean` and `string` with `setJinjaVariable` effects.
  Boolean/string defaults become `chat_template_kwargs` manifest defaults;
  unknown types/effects are preserved as warnings and never applied.
- Unknown fields are ignored. A broken manifest never breaks scanning: the
  model stays visible, and the API returns `{"found": false}` or warnings.
- `manifest.json` (artifact manifest) supports `type`, `owner`, `name`,
  `dependencies` (`baseModel` / `draftModel` / `custom`) and `tags`.

### Limits (untrusted input)

| Limit | Value |
|---|---|
| file size | 512 KiB |
| nesting depth | 32 |
| mapping keys | 2000 |
| list items | 500 |
| string length | 16 KiB |
| YAML anchors/aliases | rejected |
| remote fetching | never |

## Runtime sidecar

Place `lunarshroud-runtime.json` beside the selected executable. All fields are
optional; auto-probe fills the rest. The sidecar **cannot change the executable
path**; a mismatched `executable_relative_path` is reported and ignored.

```json
{
  "schema_version": 1,
  "name": "llama.cpp-win-x86_64-nvidia-cuda12-avx2",
  "engine": "llama.cpp",
  "runtime_kind": "llama-server",
  "executable_relative_path": "llama-server.exe",
  "version": "2.60.0",
  "build": 11698,
  "domains": ["llm", "embedding"],
  "supported_model_formats": ["gguf"],
  "cpu": { "architecture": "x86_64", "instruction_set_extensions": ["AVX2"] },
  "gpu": {
    "make": "Nvidia",
    "framework": "CUDA",
    "targets": ["7.5", "8.0", "8.9", "9.0", "10.0", "12.0"],
    "minimum_driver_version": "12040"
  },
  "display": {
    "zh-CN": { "displayName": "CUDA 12 llama.cpp", "description": "..." },
    "en": { "displayName": "CUDA 12 llama.cpp", "description": "..." }
  }
}
```

Profile fields stored under `DATA_DIR/runtimes/<id>.json`:

`id`, `display_name`, `executable_path`, `manifest_path`, `engine`,
`runtime_kind`, `version`, `build`, `build_text`, `backend` (family/framework/
targets/driver), `domains`, `supported_model_formats`, `target_libraries`,
`manifest` summary, `warnings`, `probe` (probed/complete/option_count/error/
probed_at), `status`, `artifacts` (size/mtime only), `created_at`, `updated_at`.

`DATA_DIR/runtime-preferences.json` stores default runtime ids per format
(`gguf`, `embedding`, `rerank`, `llm`). A runtime whose probe failed cannot be
set as default.

Backend inference uses, in order: the sidecar, the path/version text, then
sibling DLLs (`ggml-cuda.dll`, `ggml-vulkan.dll`, `ggml-hip.dll`,
`ggml-rocm.dll`, `ggml-metal.dll`). The DLL check matters for neutral release
directories such as `build\bin`, which otherwise look like `unknown`.

## Defaults precedence (B2)

| Priority | Source | Storage |
|---|---|---|
| 1 (lowest) | global `/api/params/defaults` | `param_schema.py` snapshot |
| 2 | model manifest `config.operation/load` | `model.yaml` / `manifest.json` |
| 3 | user model defaults | `DATA_DIR/model-defaults/md_<hash>.json` |
| 4 | preset | `PRESETS_DIR/*.json` |
| 5 (highest) | launch overrides | request body / `param_overrides` |

`core/config_resolver.py` applies only an explicit field map; unknown manifest
fields are reported as `unknown_fields` and never become argv. Values from
`{checked: false, ...}` are treated as “not set”. Every key in the resolved
response carries a source tag (`global` / `manifest` / `user`), and the
parameter editor shows the per-key provenance before applying.

| Manifest field | Parameter |
|---|---|
| `llm.load.contextLength` | `ctx_size` |
| `llm.load.numParallelSessions` | `parallel` |
| `llm.load.llama.kCacheQuantizationType` | `cache_type_k` |
| `llm.load.llama.vCacheQuantizationType` | `cache_type_v` |
| `llm.load.llama.flashAttention` | `flash_attn` |
| `llm.prediction.temperature` | `temperature` |
| `llm.prediction.topKSampling` | `top_k` |
| `llm.prediction.topPSampling` | `top_p` |
| `llm.prediction.repeatPenalty` | `repeat_penalty` |

Only field paths observed in real LM Studio configurations are mapped.
Speculative-decoding and seed paths have unverified semantics, so they are
reported as `unknown_fields` rather than guessed into argv.

User defaults are validated against the schema; managed keys (`model`,
`model_dir`, `host`, `port`, `api_key`, `cors_origins`, `path`, `log_file`,
`extra_args`) are rejected so a model-scoped file cannot redirect the model or
bypass security boundaries. Applying defaults is always an explicit button, never a silent
overwrite. Probe recommendations (`ctx_size`, `spec_type`, `mmproj`) remain a
separate explicit action.

## Local draft compatibility (B3)

`core/gguf_probe.read_vocab_prefix` reads only the local GGUF metadata needed to
identify a tokenizer: `tokenizer.ggml.model`, the total token count, and the
first 256 token strings. It never reads tensor data or weights, never fetches
anything, and caches by path/mtime/size. `core/draft_compat.py` compares a main
model with a draft model:

- `mismatch` — different tokenizer type, vocabulary size, or first-N token
  strings; preflight reports `draft_vocab_mismatch` and blocks.
- `unknown` — one signature is missing/unreadable; preflight reports
  `draft_vocab_unverified` as info and does not pretend it matched.
- `match` — vocabulary size and the prefix hash agree; this is evidence, not a
  guarantee, so no success message is fabricated.

The same preflight pass checks `x-lunarshroud.requires.min_llama_cpp_build` from
the model manifest against the selected runtime's version line and reports
`runtime_too_old` when the build is older.

## Guardrails and identity (B4)

`launch_guardrail` is a global setting:

- `off` — only errors block; warning-only preflight is skipped.
- `warn` (default) — errors block; warnings show a confirmation.
- `strict` — warnings block too.

Errors always block, regardless of policy. The setting is validated by the
backend and exposed on the Settings page.

Instances and history rows also carry `model_key` (manifest `model:` key, or
`local:<resolved path>`) and `variant_key` (filename stem, bounded). Old SQLite
databases gain the columns through the same idempotent migration as
`runtime_id`/`runtime_label`. History detail shows both keys.

On builds that advertise `--load-mode`, the default memory-map intent
(`mmap=true`, `mlock=false`) is emitted as `--load-mode mmap` rather than the
deprecated `--mmap`; an explicit `load_mode` wins and suppresses the legacy
switches.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/runtimes` | profiles + preferences |
| POST | `/api/runtimes` | register/probe `{path, manifest_path?, display_name?}` |
| POST | `/api/runtimes/{id}/probe` | re-probe and update |
| PUT | `/api/runtimes/{id}` | rename |
| DELETE | `/api/runtimes/{id}` | delete registration (not the exe) |
| GET/PUT | `/api/runtimes/preferences` | default runtime ids |
| GET | `/api/models/manifest?model=` | normalized manifest summary |
| GET | `/api/models/scan?with_manifest=true` | attach manifest summaries (cap 500) |
| GET | `/api/models/defaults?model=` | resolved `{params, sources, unknown_fields, warnings}` |
| PUT | `/api/models/defaults` | save user defaults `{model, params}` |
| DELETE | `/api/models/defaults?model=` | remove user defaults |
| GET | `/api/params/schema?runtime_id=` | schema for the selected build |
| GET | `/api/params/recipes?runtime_id=` | recipes marked unsupported by that build |
| POST | `/api/params/build-command` | `{params, runtime_id?}` → argv preview |
| GET | `/api/system/capabilities?model=&runtime_id=` | build × model capabilities |
| POST | `/api/diagnose` | preflight with `runtime_id` / preset-pinned runtime |
| POST | `/api/instances` | launch with optional `runtime_id` |

All routes are behind the existing session-token middleware. Runtime paths must
be absolute; missing/relative paths are rejected with 400. Corrupt profile files
are skipped, not fatal.

## UI surfaces (B1 + B2)

- **Runtimes** page: table of profiles (name/version/backend/status/actions),
  add via native file dialog (Tauri) or path prompt (browser), re-probe, set
  default, remove, detail card with warnings/manifest/probe info.
- **Models** detail: optional “Model manifest” card with model key, source,
  declared metadata, companions, runtime requirements and warnings. When no
  manifest exists, it says so and keeps using GGUF probe data.
- **Parameter editor**: runtime selector (default/preference or a specific
  profile). Switching reloads schema, capabilities, recipes and the command
  preview for that build while preserving compatible values. The model-capability
  card shows the manifest/user defaults with per-key source labels and an
  explicit “Apply model defaults” button.
- **Dashboard launch dialog**: optional runtime selector; empty means “preset
  runtime, then default preference, then configured path”.
- **Presets / history / instance cards**: presets can pin `runtime_id` (saved by
  the parameter editor); history and instance cards show the runtime label.

## Security boundaries

- Manifests are untrusted input; parsing is bounded and never fetches sources.
- Companion files must resolve inside the model directory.
- Runtime sidecars cannot override the selected executable path.
- Manifest fields never become argv; managed flags (`host`/`port`/`api_key`/
  `path`/`log_file`) remain forbidden in `extra_args`.
- Probe subprocesses still use the sanitized environment and absolute paths.

## Not implemented yet

- Logical model grouping across files (manifest-declared only; `model_key`
  groups identity, but the Library still lists files).
- A full per-customField editor (manifest defaults are applied; individual
  toggles are not rendered yet).
