# LunarShroud

[简体中文](README.md) | **English**

LunarShroud is a **local AI chat application for Windows**: bring your own
llama.cpp build and GGUF models. No account, no network access, all data stays
on the machine. Built with Tauri, FastAPI and plain JavaScript. It ships
parameter editing, presets, multi-instance management, live logs, resource
monitoring, launch history, a native streaming Chat (with a llama-server WebUI
fallback), a local knowledge base / RAG, local backup and a tray lifecycle.

> Current version: `1.0.0`. The **only release channel is the portable zip**
> (`LunarShroud-1.0.0-portable-x64.zip`, unzip and run); no installer ships, and
> none is built. Artifacts are **not code-signed**; verify the SHA-256 from
> the release page before running. See [docs/RELEASE.md](docs/RELEASE.md).
>
> Positioning: `1.0.0` means **feature-complete and usable daily**, not an
> unqualified "production-grade stable". The **8-hour soak and the
> multi-hardware matrix are not done** (a 4-hour real-model soak passed), and
> the unsigned package still triggers SmartScreen's unknown-publisher prompt.
> Both are disclosed limits — see "Known limitations" below.

## Highlights

- **Visual editing of llama-server parameters.** The panel shows the common set
  and adapts to **your local build** — the `--help` probe is authoritative, not
  a hard-coded list. Long-tail flags go through "additional command-line
  arguments".
- **One-click "use case" recipes:** chat generation / embeddings / rerank /
  read-only tools / full agent. They write intent, not tuning numbers.
- **Local knowledge base / RAG.** Text, HTML, PDF, DOCX and scanned PDFs
  (GLM-OCR) are indexed on this machine. Chat can select a base and answers
  carry page citations. See [docs/RAG.md](docs/RAG.md) and
  [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md).
- **RAG building blocks included:** `/v1/embeddings`, `/rerank`, prompt caching,
  built-in tools and MCP.
- **Multi-instance** start, stop, restart, port allocation and status monitoring.
- **Bounded live logs** with search, filtering and download.
- **CPU, memory and NVIDIA GPU monitoring.**
- **Model library:** GGUF capability probing (native context, MTP, mmproj), VRAM
  estimation, companion-file detection, and an optional `model.yaml` /
  `manifest.json` editor.
- **Presets:** import, export, clone, compare, and launch history.
- **Native Chat:** local conversations (`chats.db`), SSE streaming,
  stop/regenerate, message editing and branching, context usage, local text
  attachments, prompt library, conversation search, Markdown export; the WebUI
  is available as a fallback.
- **Two-view chat inspector:** `Configuration` (instance, knowledge base, system
  prompt, sampling, reasoning, context) and `Integrations` (agent and tool
  status, session-level tool-root authorization, resident embedding/rerank/OCR
  services, local API gateway). Both resizable.
- **One-click stop** next to the model capsule in the title bar — no need to
  switch to the dashboard.
- **Local API gateway:** an OpenAI-compatible server on its own port with an
  on/off switch, port and Bearer key, origin allow-listing, and live request
  logs you can export.
- **Settings gained `Hardware` and `Developer` categories:** the former shows
  CPU/memory/GPU/VRAM/temperature and GPU processes, the latter shows version,
  platform, data and log paths, and runtime probe status.
- **Local backup / restore** for config, presets, history and chats (including
  attachments and prompts): SHA-256 manifest, restore applied on next startup.
- **Tray icon**, optional close-to-tray, explicit quit, and a Ctrl+K command
  palette.

## Stability and security boundaries

- Blocking process, disk and GPU work runs on a thread pool; logs, history,
  scans, request bodies and frontend DOM all have explicit bounds.
- A Windows Job Object manages the backend and `llama-server` subtrees, so the
  launcher exiting or crashing reclaims them.
- The packaged build generates its session token with the OS secure RNG.
  REST and WebSocket both require authentication; the management API listens on
  `127.0.0.1` only.
- The released build executes only the bundled backend inside its install
  directory — it never falls back to Python or other programs found in the
  working directory or `PATH`.
- Preset IDs and static file paths are normalized and checked against directory
  boundaries; config and presets are written atomically.
- API keys are never written to presets, history, logs or API responses. Because
  `llama-server` takes command-line arguments, a runtime key can still be seen
  by other users with the same privileges via the process command line.

Full threat model: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md). Vulnerability
reporting: [SECURITY.md](SECURITY.md).

## Requirements

Running the prebuilt version: Windows 10/11, WebView2 Runtime, and a
`llama-server.exe` plus GGUF models you supply yourself. An NVIDIA GPU is only
needed for GPU metrics.

### First launch: get llama-server

> Full newcomer steps, common mistakes and a launch-failure troubleshooting
> table are in [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md). The in-app
> "Getting started" page reads your current configuration and produces a
> dynamic checklist.

This project **does not bundle** `llama-server.exe` (it is a separate llama.cpp
artifact and comes in per-GPU variants), so one step remains after installing
the launcher:

1. Open the llama.cpp releases page
   <https://github.com/ggml-org/llama.cpp/releases> and download the latest
   Windows package for your GPU:

   | Your GPU | Pick a file containing |
   |---|---|
   | NVIDIA (RTX 30/40/50 etc.) | `bin-win-cuda12.x-x64` (recommended, see step 3) |
   | AMD discrete, on the official ROCm list (RX 6000/7000 etc.) | `bin-win-rocm-*-x64` |
   | Other AMD, including integrated | `bin-win-vulkan-x64` |
   | Intel integrated / Arc / NPU | `bin-win-openvino-*-x64` |
   | CPU only | `bin-win-cpu-x64` |

   The ROCm package only works for GPUs on AMD's official support list
   (gfx9/gfx10). Integrated GPUs and unlisted models show an empty
   `--list-devices`; use the Vulkan package instead. The OpenVINO package uses
   Intel's CPU/GPU plugin and device names start with `OPENVINO` (the CPU device
   is usually listed first). After registering a build, the "Runtimes" page
   shows the devices it **actually enumerates on this machine**; the parameter
   panel's "compute device" (`--device`) takes names from that list.

2. Extract to a **fixed location** (e.g. `D:\llama`); it contains
   `llama-server.exe`.
3. With NVIDIA you need the CUDA runtime: install the CUDA Toolkit runtime
   component from NVIDIA, or copy the `cudart64_*.dll` / `cublas64_*.dll` that
   came with the release next to `llama-server.exe`.
4. Start LunarShroud → Settings → "llama-server path" → select the exe you just
   extracted.

> The launcher **deliberately ships no default** for this path. A default would
> mean executing "whatever file happens to carry that name in some install
> directory" on every start — and that directory is usually user-writable. See
> `docs/SECURITY_MODEL.md`. The cost is the one step above.

Building from source additionally needs Python 3.11+, Node.js 22, Rust `1.97.1`
MSVC, Tauri CLI `2.11.4`, and Visual Studio C++ Build Tools with the "Desktop
development with C++" workload. Details: [docs/BUILDING.md](docs/BUILDING.md).

## Quick start

```powershell
.\setup.ps1
```

To run only the backend as a source-mode development server you must explicitly
enable insecure dev mode; it still listens on loopback only:

```powershell
$env:LUNARSHROUD_ALLOW_INSECURE_DEV = '1'
.\.venv\Scripts\python.exe backend\main.py
# then open http://127.0.0.1:17520
```

Do not use that switch on a shared machine, a remote host or in production. The
desktop package has the Tauri shell inject a private session token and does not
need it.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=backend --cov-config=.coveragerc --cov-report=term-missing --cov-fail-under=75
Push-Location frontend
node --test tests\*.test.mjs
Get-ChildItem js -File | Where-Object Extension -in '.js', '.mjs' | ForEach-Object { node --check $_.FullName }
# Browser core paths, axe accessibility and first-paint budget (backend must be running)
npm ci
npm run test:browser
Pop-Location
cargo test --manifest-path src-tauri\Cargo.toml --locked
```

Baseline at the 2026-11-05 working tree: **929 Python tests (928 passed, 1
skipped)**, 117 frontend unit tests, all JavaScript syntax checks passing, and
**84.75%** product-code coverage (gate ≥ 75%). A clean checkout has no
prebuilt llama.cpp packages under `study/`, so the 6 multi-backend cases that
need them skip (**922 passed, 7 skipped**); that is expected. The Rust suite is
**23 passed,
0 failed**, but `CARGO_TARGET_DIR` must point at a short path outside the
repository: under the default `src-tauri/target/` the `ring` C compile fails on
a path-location issue, unrelated to the MSVC install, the SDK or the source.
Test counts grow with regressions — the gate is always **0 failures + coverage
≥ 75%**, measured by actually running it. Tests do not replace real-model and
long-duration stress runs.

## Building the portable package

There is **one** distribution channel: the portable zip. The NSIS installer
channel was removed on 2026-11-05 (see [docs/RELEASE.md](docs/RELEASE.md) and
[AGENTS.md](AGENTS.md) §4).

```powershell
# Fixed build metadata: otherwise two consecutive PyInstaller builds differ.
$env:SOURCE_DATE_EPOCH = '1790000000'
$env:PYTHONHASHSEED = '0'
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
# Remap the local cargo registry path (it contains the build machine's user
# name) to a neutral prefix so the public artifact carries no builder identity.
# This RUSTFLAGS is required on every release build.
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry\src=cargo-registry"
# --no-bundle: produce only the launcher binary, no installer of any kind.
cargo tauri build --ci --no-bundle -- --locked
```

The artifact is `src-tauri/target/release/lunarshroud.exe`. Packaging it into a
zip and producing the checksum file is covered in
[docs/RELEASE.md](docs/RELEASE.md).

To run inside the build workspace, do not reuse an old root-launcher EXE. After
building, use the publisher to produce a matching launcher/sidecar pair and
update the desktop shortcut:

```powershell
.\tools\Publish-PortableRuntime.ps1 -Destination . -BackupDirectory .\tmp\portable-backups
```

## Project layout

```text
backend/        FastAPI API, process management, storage and monitoring
frontend/       Plain-JavaScript UI
src-tauri/      Tauri main process, session handshake and Windows Job Object
backend/tests/  Backend unit and API integration tests
frontend/tests/ Frontend runtime unit tests
docs/           Architecture, build, release and security notes
```

Documentation map: [docs/README.md](docs/README.md).

## Known limitations

- Windows 10/11 only; macOS and Linux are not supported.
- The portable package is not code-signed; Windows SmartScreen may warn about an
  unknown publisher.
- **No installer is provided.** The NSIS channel was removed on 2026-11-05, so
  the portable zip is the only distribution form. `cargo tauri build` will not
  produce one either: `bundle.active` is off.
- GPU monitoring prefers NVML (`nvidia-ml-py`) and falls back to `nvidia-smi`
  when the library or driver is unavailable.
- llama.cpp parameters change between versions; re-check the generated command
  after an upgrade.
- llama.cpp's default `--cache-ram 8192` enables host-side prompt caching.
  Measured: a 4B model grew llama-server RSS from 5.2 GB to 11.4 GB over 10
  minutes before plateauing. This is not a launcher leak. On smaller machines
  lower `--cache-ram` or set it to 0 (at the cost of prompt reuse).
- Instance ports have no API key by default. Windows loopback ports are not
  isolated per user, so other local processes on a shared machine can call the
  model directly; set `api_key` in the parameters on shared machines.
- The instance port range (default 8080–8090, 11 ports) is **shared** between
  model instances and the knowledge base's embedding/rerank/OCR services:
  running several instances while indexing can exhaust it ("no available port in
  range"); widen the range in Settings.
- OpenVINO builds do not use `--device` but the `GGML_OPENVINO_DEVICE`
  environment variable (the launcher translates automatically; one device at a
  time). Measured: on some GPUs its GPU plugin crashes after loading a model
  (0xC0000409) — switch to `OPENVINO0` (CPU) or use a Vulkan/CUDA build.
- ROCm builds on machines without a supported AMD discrete GPU **silently fall
  back to CPU** and still start (measured an order of magnitude slower); the
  launcher shows a warning on the instance card but does not block the launch.
- On Windows, model/draft/mmproj paths whose total length exceeds 260 characters
  cannot be opened by llama.cpp; the pre-launch check reports `path_too_long` —
  move the files to a shorter directory.
- llama.cpp v0.7.0: an embedding model in **single-slot mode with the embedding
  endpoint disabled** trips an upstream assertion and crashes
  (`GGML_ASSERT(n_outputs_max ...)`). Enable the embedding endpoint or set
  parallel slots to 2 or more; the launcher translates that assertion into an
  actionable hint.
- Chat defaults to native mode. WebUI mode embeds the llama-server Web UI in a
  sandboxed iframe (scripts, forms, same-origin); the sandbox has no download
  permission, so for v0.7.0's model-manager downloads use the toolbar's "open in
  browser".
- Chats, messages per conversation and the prompt library **refuse new writes**
  at their fixed cap (500) rather than deleting old content; export or delete
  manually. Launch history still keeps the most recent records per
  `history_max_records`.
- The command-line backup/restore tools require the app and `backend.exe` to be
  closed; the backup manifest is UTF-8. In-app backup can run while the app is
  open via SQLite snapshots.
- A `pre-restore-*` snapshot is taken before restoring; if the restore itself
  fails, this version requires manual recovery from that snapshot — automatic
  rollback is not implemented.
- Launch history keeps the most recent records per `history_max_records`;
  exceeding it trims the oldest diagnostic records without touching chats or
  presets.
- A 4-hour real-model soak passed; the **8-hour soak and the multi-hardware
  matrix are not done**, so `1.0.0` does not claim "production-grade stable".
  Both are disclosed before release.

## Contributing and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
before submitting changes. The project is under the [MIT License](LICENSE);
third-party notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The dependency security baseline is in [DEPENDENCY_AUDIT.md](DEPENDENCY_AUDIT.md).

## Acknowledgements and provenance

### AI-assisted development

Development of this project was assisted end to end by an AI coding agent,
**DeepSeek-V4.1-Flash** (`deepseek-v4.1-flash-10-17`): defect localization,
regression-test authoring, documentation consolidation and release verification
all involved human–agent collaboration. Every conclusion the agent proposed was
reproduced before being accepted — that rule is written down in `AGENTS.md` §6
("verification discipline"), and it is why `CHANGELOG.md` marks entries as
*measured*.

The code itself makes no networked inference calls at runtime: no account, no
telemetry, no network access.

### Frontend aesthetics

The design tokens, spacing scale, hierarchy and contrast rules follow the
**frontend-aesthetics** plugin:

<https://github.com/1LunarVeil/frontend-aesthetics.git>

That plugin supplies **rules and method** (tokens first, contrast thresholds, a
visual-hierarchy checklist), not a runtime dependency, and it is not part of the
shipped artifacts. The concrete implementation is in `frontend/css/tokens.css`
and [docs/DESIGN_1.0.md](docs/DESIGN_1.0.md).

### Open source

Thanks to the [llama.cpp](https://github.com/ggml-org/llama.cpp),
[Tauri](https://tauri.app/) and [FastAPI](https://fastapi.tiangolo.com/)
communities.
