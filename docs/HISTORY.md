# Project history

This concise record separates current verified facts from historical material.

Paths under `docs/archive/` and `tmp/` appear below because moving material
there *is* part of the history. They are maintainer-local and deliberately
absent from this repository, so treat every such mention as a record of what was
done, not as a location you can open. Nothing in the published tree depends on
them.

## 2026-10-21 (latest): packaged regression

Rebuilt the current PyInstaller onedir backend and an optimized Tauri binary
(no installer) and ran it as a standard-user process against isolated app data.
Windows UI Automation verified Chat search, attachment preview, prompt editing,
branching, message editing and process cleanup in the real WebView2; the
optimized release binary repeated the Chat/search/prompt checks and then ran
real Qwen3.5-4B (official b11698): streamed reply, packaged file-picker
attachment (`8642`), context meter, DB persistence and clean stop/close. WebView2 155's
CDP endpoint was unavailable despite the debug arguments (upstream regression),
so packaged interaction used UI Automation. The new release candidate remains
under `src-tauri/target/release`; the repo-root portable pair was not replaced.

## 2026-10-21 (latest): conversation search

The Chat sidebar gained local search across conversation titles, message text,
attachment names and attachment content (`GET /api/chats/search`). Saved prompts can be edited in
place, and Ctrl+F focuses the conversation search box. Queries and
results are bounded; SQLite LIKE wildcards are escaped and all text is passed as
a parameter. Results expose a snippet and match type, highlight matches, support
Enter/Arrow navigation, preserve input focus across list refreshes, and clear
with Escape. Verified in Chromium against the isolated backend with message and
attachment hits, no-match/clear states and conversation loading.

## 2026-10-21 (latest): Chat local text attachments

The native composer gained local text attachments: paperclip/file picker and
drag/drop read files with the browser File API, never upload them, enforce a
text-extension allow-list and bounds, and persist them in `chats.db`. Chips open
a preview/copy dialog; full attachment text is wrapped in explicit delimiters
only for the llama-server payload, so the visible message stays clean.
Attachment-only turns, automatic titles from file names, branch copying,
Markdown export and both backup paths are covered. Real Qwen3.5-4B verified a
file containing `7391` was read by the model, plus drag/drop, preview, branch,
reload persistence and unsupported-type handling.

## 2026-10-21 (latest): Chat advanced

Message editing now truncates later turns and regenerates the reply; editing
the first user turn updates an auto-generated conversation title but preserves a
manual name. Any saved message can branch into a new conversation that copies
the prefix, params, model binding and stats while leaving the source intact. The
composer gained a context-usage meter based on the latest assistant usage
(`total_tokens`, with prompt/completion counters as a fallback) and the
instance's `ctx_size`, plus a prompt library with six built-ins and locally
saved prompts inserted at the caret. Custom prompts live in `chats.db`, so both
backup paths carry them. Verified in Chromium against an isolated backend with
real Qwen3.5-4B: edit and regenerate, one-turn branch, prompt save/insert/delete,
2.1K/4.1K meter.

## 2026-10-21 (latest): model manifest editor

The model detail manifest card gained an editor for `model.yaml` /
`manifest.json`: raw text, YAML/JSON templates, validation preview, save and
delete. New backend helpers validate text with the scanning parser, require an
existing `.gguf`, reject creating a second format beside an existing manifest,
write atomically, and back up the old file under `DATA_DIR/manifest-backups/`.
The UI shows the parsed model key and warnings before saving. Real WebView2
checks covered template validation, save, overwrite backup, delete backup and
detail refresh.

## 2026-10-21 (latest): in-app backup and restore

Settings gained a “数据备份” section backed by `core/user_backup.py`. Backups
store config, sqlite snapshots of history/chats and presets under
`DATA_DIR/backups/<name>` with a SHA-256 manifest. Restore is verified, staged
in `restore-pending.json`, and applied by the next backend start before the
databases open; a pre-restore backup is created first, sidecars are cleared and
presets are replaced. The API refuses restore while instances run, rejects
path traversal, and the UI offers to quit immediately after staging. The
PowerShell tools remain compatible (same manifest format). Real WebView2 test:
backup → modify theme/preset → stage restore → quit/relaunch → old config and
preset restored, new preset removed, `restore-result.json` = restored.

## 2026-10-21 (latest): tray, background mode and quit

The Tauri shell gained a tray icon with Show/Quit and an opt-in
`close_to_tray` setting (default off, so existing close behavior is unchanged).
When enabled, closing the window hides it and keeps the backend and instances
running; Ctrl+K now offers an explicit “Quit LunarShroud”, and the tray menu
does the same. The setting is read from `config.json` and applied at runtime via
the `set_close_to_tray` command with its own Tauri ACL permission. The
second-instance path was changed from FindWindowW (unreliable here) to an
EnumWindows title scan, which successfully restores a hidden window. Real
WebView2 checks covered default close-exit, opt-in hide, runtime toggle, hidden
window restore and quit; Job cleanup left no orphan backend.

## 2026-10-21 (latest): native chat and command palette

The Chat page gained a native streaming mode as its default. `chats.db`
(SQLite) stores bounded local conversations and messages; `/api/chats*` exposes
CRUD and `/api/instances/{id}/chat/completions` proxies SSE through the backend
so the webview never talks to llama-server directly and API keys stay
server-side. The frontend adds Markdown rendering, code copy, stop/regenerate,
editable chat names, system prompt and sampling settings, token/speed stats,
reasoning disclosure, Markdown export and the original WebUI as a fallback. Stop and regenerate were
verified against a real Qwen3.5-4B instance, including persistence across
navigation.

Ctrl+K opens a command palette (pages, new chat, model picker, parameter editor,
settings, theme); the dashboard has a direct Chat action; model grouping now
falls back to folder names when no manifest exists; and the user-data backup
script includes `chats.db`. Source tests reached 592 backend tests at 85.85%
coverage and 34 frontend tests; the real-WebView2 audit had zero console
warnings/errors.

## 2026-10-21 (latest): fresh packaged-pair validation

The current source was rebuilt as a PyInstaller onedir plus a release Tauri
binary and validated in a fresh isolated environment (new `%APPDATA%`,
`%LOCALAPPDATA%`, `%TEMP%`, WebView2 profile). The packaged UI matched the debug
build: zero console errors, all routes, real-DPR 1.545, responsive drawer and
desktop layouts, Settings open/close without a false dirty prompt, a real
Qwen3.5-4B launch, Chat WebUI, live logs and history.

The fresh environment caught a packaged-only bug the source tests missed:
`backend.spec` excluded PyYAML although `model_manifest._load_yaml` imports it
lazily and `requirements.lock` pins it. In the frozen backend every `model.yaml`
became "not found", killing manifest grouping/details. The spec now collects
`yaml` submodules and no longer excludes PyYAML; `test_packaging_contracts.py`
guards the spec. After rebuilding, a real `model.yaml` beside hardlinked Qwen
files showed the `demo-org/qwen-demo` group, the manifest card, and history
identity `demo-org/qwen-demo · qwen-a`.

## 2026-10-21 (latest): real WebView2 audit

The current debug build was run in an isolated data directory under the real
WebView2 runtime (not Chromium emulation), with a CDP audit over the Tauri IPC
boundary. Baseline DPR was 1.545 at 1243x797; all routes rendered without
horizontal overflow, and the 800px/DPR2 drawer plus the 1280px/DPR2 desktop
layout both passed. The audit exposed two real defects:

- every `get_backend_session` invoke was blocked by CSP and silently fell back
  to `postMessage`, logging a CSP error each time. `connect-src` now includes
  Tauri's local `ipc:` / `http://ipc.localhost` bridge; the offline contract
  test's allowlist was narrowed accordingly.
- opening Settings and closing it immediately showed “unsaved changes” even
  when nothing was edited: the dirty check compared the backend config with
  rendered values, and the theme radio comes from the frontend preference
  (`system`) while the backend default is `dark`. Settings now records a
  per-group baseline after the first render, so only actual edits are dirty.

## 2026-10-21 (later): B3 draft compatibility and the offline contract

The user made offline operation a hard product requirement. An audit found no
remote runtime assets: the frontend has no CDN scripts/fonts/imports, Tauri's
CSP allows only `self` and loopback, the Rust readiness probe disables proxies
and redirects, and the Python health probe uses `trust_env=False`. The only
external URLs are user-clicked documentation links in the help page. A new
`docs/OFFLINE.md` records the contract, and `test_offline_contract.py` now fails
if a remote asset, remote ES import, or non-loopback CSP entry is introduced.

B3 adds a pure-local draft-model check. `gguf_probe.read_vocab_prefix` reads the
tokenizer type, total vocabulary count, and the first 256 token strings (never
tensors; bounded and cached by path/mtime/size). `compare_draft_vocab` reports
`match` / `mismatch` / `unknown`; preflight blocks a definite mismatch with
`draft_vocab_mismatch` and merely informs on an unreadable signature. Manifest
`requires.min_llama_cpp_build` is compared with the selected runtime's version
line before launch (`runtime_too_old`). All checks are local and offline.

B4 followed: the parameter editor can save only the current differences from
global defaults as user model defaults, and clear them; managed keys are
filtered in the UI and rejected by the API. Manifest `customFields` with
boolean/string defaults and `setJinjaVariable` effects become
`chat_template_kwargs` manifest defaults. `launch_guardrail` adds
`off`/`warn`/`strict` warning policy (errors always block), instances and
history persist `model_key` + `variant_key` (idempotent SQLite migration), and
new builds receive `--load-mode mmap` instead of the deprecated `--mmap` for
the default memory-map intent. The launcher also defaults `--cors-origins
localhost` so upstream's `*` cannot expose local model output to arbitrary
websites; `cors_origins` is a managed key for model defaults.

Real validation followed on the same machine: official llama.cpp `b11698`,
prism `b10709` and prism `b10777` (all CUDA 13.3) probed successfully; a real
Qwen3.5-4B Q4_K_M launched on both prism and official builds, served health and
chat completions, and rendered the real llama-server WebUI in the Chat iframe.
Gemma-4-E4B plus its MTP draft launched with the draft loaded. Real vocabulary
comparison returned a mismatch for Qwen+gemma-MTP and a match for
Gemma+MTP. A proxy-poisoned run (`HTTP(S)_PROXY` pointing at a dead port) still
probed and launched, confirming the local path does not depend on a working
proxy. This audit also found and fixed neutral `build\bin` runtime detection
(now uses sibling `ggml-cuda.dll` etc.) and the Logs page not auto-selecting an
already-running instance.

M1 polish followed: 23 dead CSS classes were removed after a static usage scan;
raw px in `style.css` dropped from 529 to 241, with padding/margin/gap/
font-size/border-radius fully tokenized and a new design-token test. Settings
now intercepts X/Esc/backdrop when the draft differs and offers “keep editing”
or “discard”; Logs auto-selects a running instance, shows a live shown/total
line count, and no longer leaves the empty state above the first incoming line;
Chat shows the runtime label and an `aria-busy` loading state. A narrow-window
audit found the secondary context panel was `display:none` below 900px, making
model/preset/log instance lists unreachable; it is now a focus-managed left
drawer with automatic close on selection, ESC/backdrop, and inert cleanup when
the viewport widens. Lighthouse remained 100
(Accessibility/Best Practices/SEO/Agentic) on the parameter editor in light and
dark.

The shell was then split along state/render boundaries: `modal.mjs` (overlay
stack, focus trap, confirm), `html.mjs` (escaping) and
`param_editor_view.mjs` (pure parameter/control/group HTML) were extracted from
`app.js`/`param_editor.js` with the same appCtx/DOM contract. The model library
now scans manifests and offers a persisted “group by model” view keyed by
`manifest.model_key`; files without a manifest stay under an explicit
“no manifest” heading, so directories without manifests behave exactly as
before. A pure `groupModelsByKey` helper has unit coverage.

Verification: 581 backend tests, coverage 86.07% (gate 75), 26 frontend tests,
`node --check` clean, ruff clean.

## 2026-10-21: B2 — runtime selection and resolved model defaults

B1’s registries were wired into the real launch path without changing the
transport. `runtime_id` now flows through `/api/params/schema`,
`/api/params/recipes`, `/api/params/build-command`, `/api/system/capabilities`,
`/api/diagnose` and `POST /api/instances`; presets can pin a runtime, and
instances/history persist `runtime_id` + `runtime_label` (old SQLite databases
migrate with idempotent `ALTER TABLE`). History relaunch reuses the recorded
runtime when it still exists; a preset’s pinned runtime applies when the launch
request does not name one.

`core/config_resolver.py` adds the global < manifest < user model-defaults
precedence with a small explicit field map. It stores user overrides under
`DATA_DIR/model-defaults/`, rejects managed keys (`host`/`port`/`api_key`/
`path`/`log_file`/`extra_args`) and unknown keys, treats `{checked:false}` as
unset, and returns a per-key source tag. New routes: `GET/PUT/DELETE
/api/models/defaults`. The parameter editor gained a runtime selector (schema/
capabilities/recipes/command preview reload per build), a provenance line for
manifest/user defaults, and an explicit “apply model defaults” button. The
dashboard launch dialog gained an optional runtime selector; history shows the
runtime column.

Verification: 554 backend tests, coverage 86.27% (gate 75), 22 frontend tests,
`node --check` clean, ruff clean. In an isolated audit backend (port 17641,
`tmp/` data only) the browser showed runtime switches changing schema/capability
requests and the final argv, model defaults `清单 2 项 · 用户 1 项` applied as
`-c 16384 --temp 0.6`, a preset restoring its pinned Vulkan runtime, and the
history runtime column. Launching the deliberately fake 4-byte runtime returned
WinError 216, which is the expected failure-path proof; a real llama-server
successful launch remains unverified in this environment.

## 2026-10-20: UI/A11y pass and read-only manifest/runtime registries

The release-hardening phase was paused by the user. A frontend audit found three
functional defects: numeric/float inputs were normalised on every keystroke
(typing `0.75` became `750`, `-1` became `1`), the pre-flight dialog destroyed
the host launch form, and navigation/list rows were non-focusable `div`s. All
three were fixed and verified in a real browser. The dialog gained a bounded
overlay stack (`{stack:true}`), focus trap, background `inert`, initial/restore
focus and stack-aware Escape; form controls across the parameter editor,
settings, launch dialog, logs and Chat now have programmatic labels. Contrast
tokens were recalculated for both themes (Lighthouse accessibility 100 on the
dashboard, model library, Runtimes page and parameter editor). The icon rail
became an expandable grouped navigation with labels, `aria-current`, a portal
tooltip, live-region toasts and a `system` theme with first-paint boot.

The first manifest/runtime layer was added as **read-only registries**: optional
`model.yaml` / `manifest.json` beside GGUF files (bounded JSON/YAML parsing,
relative-only companions, provenance warnings) and `lunarshroud-runtime.json`
profiles for user-selected `llama-server.exe` builds (version/build/backend,
probe status, defaults). New routes: `/api/models/manifest`,
`/api/models/scan?with_manifest=true`, `/api/runtimes*`. The launcher still
spawns the user-selected executable with argv and loopback REST; no engine
protocol, downloader or bundling was introduced. Per-instance runtime selection,
defaults precedence, draft compatibility and loading guardrails remain B2+.

Verification: 543 backend tests, 22 frontend tests, `node --check` clean, ruff
clean, Lighthouse 100 accessibility on all audited pages in light and dark.

## 2026-10-17: seventh-round hardening — corrupt data, long paths, logs, deployer

Fault injection against the packaged backend found a startup-killing defect:
placing non-SQLite bytes in `history.db` made `init_db()` raise
`sqlite3.DatabaseError: file is not a database`, and the process exited with
code 3 before serving anything. The store now quarantines the main DB and its
WAL/SHM sidecars as `history.db.corrupt*` and rebuilds the schema, while lock
and permission errors are still propagated.

A 315-character valid GGUF path passed preflight, then llama-server failed with
`failed to open GGUF ... (No such file or directory)`; the diagnosis blamed
format/download/architecture. New `path_too_long` (>=260) and
`path_near_limit` (>=240) checks cover the main model, draft model and mmproj,
with bilingual titles/fixes. The model-list summary also exposes
`truncated`/`stopped_early`, and the library renders a warning badge.

The startup log no longer writes the per-launch `session_id`; it records only
`backend started pid=…`. `tools/Backup-UserData.ps1` and
`tools/Restore-UserData.ps1` add hash-verified user-data backup/restore with a
pre-restore snapshot and reject tampered manifests/traversal. Testing them
exposed a PowerShell 5.1 trap: an inherited PowerShell 7 `PSModulePath` can
prevent `Get-FileHash` from auto-loading, so the tools use .NET SHA-256.

The deployment script had a location bug: after `Set-Location`, `-Destination .`
was resolved through .NET's current directory and copied the new pair into
`src-tauri\`, leaving the workspace root on the old pair and pointing the
Desktop shortcut at the wrong copy. Relative paths now resolve against
`Get-Location`, with a regression test that deliberately makes the two
locations differ. The rebuilt pair (`67ab9f22…` / `30d90875…`) was redeployed;
the NSIS r3 install/uninstall replay confirmed the installed backend hash and
401/404/clean-shutdown behavior. Release builds now pin
`SOURCE_DATE_EPOCH=1790000000` and `PYTHONHASHSEED=0`: consecutive builds
produced byte-identical launcher and backend hashes, while the NSIS installer
itself still varies and is verified through its installed files.

## 2026-10-17: llama.cpp v0.7.0 alignment, RAG recipes, and an upstream assert made actionable

The local llama.cpp moved to `0.7.0-dev (build 11698)`. Re-diffing its `--help`
against build 11630 showed the option surface is unchanged (258 distinct
options, one wording tweak in `--cache-ram`), so the previous alignment still
holds; the real deltas are behavioural — upstream's default port is now 9931,
the chat session API is restructured into parts, and the Web UI gained a model
manager plus router mode. The Chat page embeds llama.cpp's own Web UI in an
iframe and parses no chat JSON itself, so the API restructure does not reach our
code; the framing headers on that UI (`COEP: require-corp`, `COOP: same-origin`)
date from 2026-06-13 and were already present in the build this page was
verified against.

- Coverage gaps closed with capability-driven parameters: `--pooling`,
  `--embd-normalize` and `--cache-prompt` for the retrieval side, `--models-dir`
  / `--models-max` for router mode, `--reasoning` / `--reasoning-format` /
  `--chat-template-kwargs` for thinking control, and `--rpc` for distributed
  offload.
- Two capability-layer fixes: allowed values declared in the **argument
  position** (`{a,b,c}`, `[a|b|c]`) are now parsed, and select validation
  accepts the values the local build declares — the dropdowns already offered
  them and the backend then rejected them with a 400 at launch.
- RAG productization took the shape of **use-case recipes** instead of built-in
  presets, because presets were removed on purpose (tuned numbers drift with
  every release). Recipes carry intent only, are filtered against the schema and
  the local build, and are documented in `docs/RAG.md` together with a measured
  two-instance layout on an 8 GB card: an 86 MB Q4_K_M embedding model serving
  768-dimensional `/v1/embeddings` while a 4 B Q4_K_M chat model with read-only
  tools ran at `-c 8192` on a second instance, both healthy at the same time.
- A crash found while smoke-testing router mode turned out to be upstream, not
  ours: an embedding model with one slot and no `--embeddings` aborts on
  `GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max)` in v0.7.0. Bisecting the
  flags showed `-np 1` alone is sufficient and that `--kv-unified`,
  `--flash-attn` and `--models-dir` are irrelevant. Both workarounds were
  verified end to end through the launcher, and the assertion is now recognized
  as the fatal cause and translated into those workarounds instead of leaving a
  bare exit code — the line previously matched neither `assertion` nor
  `ggml_abort`, so nothing was shown at all.
- Published-tree hygiene: `docs/EVOLUTION.md` hyperlinked two maintainer-local
  paths that are deliberately absent from the mirror, i.e. dead links in the
  public docs. They are now plain-text mentions carrying the same "record of
  what was done, not a location you can open" caveat this file already uses.

## 2026-10-16: second-round verification — N2/N3 closed

A second independent verification round (its own isolated experiments, including
a real browser click-through that the first round had left undone) confirmed
H1/M1/L1/L2 as genuinely fixed and the deployed pair byte-exact. It reported
three low-risk residuals; two were closed with red-green tests:

- History-page relaunch no longer replays the recorded port — it auto-assigns,
  so relaunching a still-running generation no longer fails with "port already
  in use" (direct launches with an explicit port are unchanged).
- The inline `--mcp-servers-json` counterpart of `--mcp-servers-config` is now a
  first-class, capability-driven parameter.

The remaining residual (N1: transient worker-thread queueing during a
hung-target cold-probe window; self-heals within the follower deadline) was
closed the same day with a cheap concurrency gate (per-target waiter cap with a
clear fast-fail), red-green tested. A real-model smoke test also ran on this
machine against the official build 11630: a real GGUF instance launched with
`--agent` (flags accepted by the real server, `/tools` flipped from 403 to 200
with the built-in tool list), and a real chat completion returned in 1.3 s. The
inline-JSON parameter and relaunch changes below shipped in a fresh verified
pair.

## 2026-10-14: independent audit verification, hang/starvation fix, feature alignment, redeploy

An independent blind audit (its own experiments, no access to earlier conclusions)
confirmed the security invariants held under live testing and found four defects,
all fixed with red-green regression tests:

- **H1**: capability probing against a `.bat`/`.cmd` wrapper had no real timeout
  on Windows (`subprocess.run(timeout=)` only kills the direct child; grandchildren
  holding the inherited pipe made the call hang for 585 s in measurement), leaving
  orphan processes and — through the single-flight follower loop without a total
  deadline — leaking worker threads until the `asyncio.to_thread` pool starved and
  instance management stopped responding. Probes now recycle the whole process
  tree (`taskkill /F /T`, the same System32 discipline as instance lifecycle) and
  followers exit at a bounded deadline.
- **M1**: `restart` and history-page relaunch never wrote a launch-history row.
  All three launch paths now share one recorder.
- **L1**: instance log files rotated at 5 MB with a single `.log.1` backup and are
  deleted with their instance (including terminal pruning).
- **L2**: BOM-prefixed `config.json` (Notepad / `Set-Content -Encoding utf8`) is
  read instead of being treated as corrupt and reset.
- Hygiene: `.bak-20260917` sources and an orphan `.pyc` were moved to
  `tmp/deep-clean-20261014/`.

Feature alignment with the user's official llama.cpp build (0.6.0-dev / build
11630; an earlier probe snapshot came from a prism-ml fork, b10709 — both carry
the same agent/MCP flags) added an **AI tools & MCP** parameter group
(`--agent`, `--tools`, `--tools-runtime`, `--ui-mcp-proxy`,
`--mcp-servers-config`) plus `--rerank`, and a second batch aligned to the
official build: the MoE family (`--cpu-moe`, `--n-cpu-moe`, `--n-cpu-ffn`,
`--moe-cache-mib`), draft knobs (`--spec-draft-n-ubatch`,
`--spec-draft-sampling`), video input (`--video-fps`, `--video-ffmpeg-dir`,
`--video-timestamp-interval`) and `--lazy-mode`. Everything is capability-driven:
older builds gray the parameters out and never emit them. The packaged launcher,
backend and NSIS installer were rebuilt from this source and redeployed as a
verified pair; `docs/EVOLUTION.md` was added as the single entry point for the
project's evolution history.

## 2026-09-16 to 2026-09-19: adaptive parameters, model capabilities, and UX

The parameter system stopped trusting a hardcoded llama.cpp snapshot: the
backend now parses the selected `llama-server`'s own `--help`/`--version`
output (cached per binary identity) so every parameter reports whether the
current build supports it, which values it declares, and whether its schema
default matches the upstream default before it may be omitted. Flags the build
does not recognize are dropped from the command line and surfaced in the UI
instead of failing at launch. A GGUF header prober (`core/gguf_probe.py`)
powers a model library page, a top-bar model picker (Ctrl+L), per-model VRAM
estimation keyed to the current parameter values, and speculative-decoding
recommendations with companion-file detection (MTP heads, mmproj pairing by
metadata name). The frontend gained bilingual i18n (423 keys per locale), a
modal parameter editor with inline launch confirmation, push-based instance
status, unified empty states, and honest VRAM-estimate labeling. Robustness
work in the same window: `extra_args` free-form CLI field with launch-time
quote validation, crash-exit-code hints, instance-log persistence with
per-instance failure isolation, and history auto-pruning.

Release hygiene: a five-category gate (`tools/check_release_mirror.py`) now
enforces the public-source tree against drift, forbidden paths, and private
path leakage; `.gitignore` covers local debug state and editor backups.

Audited findings closed in this window:

- 401/413 responses now carry CORS headers (the CORS middleware is registered
  outermost), so the frontend's 401 session-recovery branch actually works.
- The shell pins the backend listener port via `LUNARSHROUD_LAUNCHER_PORT`
  (honored in memory only), so the readiness probe and the backend can never
  read different config locations across dev and packaged layouts.
- Switch parameters sent as integers from imported presets now behave exactly
  like booleans instead of being dropped or inverted.
- Restart and history-relaunch endpoints report port exhaustion as 409.

## 2026-09-19: release artifact privacy and dependency refresh

A byte-scan of the built launcher found 316 cargo-registry source paths
containing the build machine's user name, embedded via Rust panic location
strings. Builds now remap `%USERPROFILE%\.cargo\registry\src` to a neutral
prefix through `RUSTFLAGS`; the release workflow and `BUILDING.md` carry the
same setting, the release checklist gained a mandatory builder-identity scan,
and the rebuilt launcher, backend sidecar, and NSIS installer all scan clean.
The same pass bumped `h2` and `rustls` in the lockfile for two RustSec
advisories that postdated the 2026-08-17 audit (unreachable from this
loopback-only HTTP launcher) and refreshed `DEPENDENCY_AUDIT.md` and
`SANITIZATION_REPORT.md`.

## 2026-08-23: documentation and workspace normalization

- Replaced the stale root handover as the current authority with
  `docs/HANDOVER.md`, `docs/STATUS.md`, and this history.
- Moved the 2026-08-02 handover, release-verification plan, and earlier TDD
  evidence to `docs/archive/`; they remain evidence, not current instructions.
- Moved loose historical screenshots, logs, generated JSON, downloaded
  `rustup-init.exe`, and one-off debugging scripts into
  `tmp/archive/20260823-workspace-cleanup/` without touching user runtime data.
- Diagnosed frequent terminal-window flashes after launcher start: high-frequency
  `nvidia-smi` polling and llama-server child launches lacked Windows
  `CREATE_NO_WINDOW`. Added the flag, regression coverage, rebuilt the packaged
  backend, and verified a full launch/close cycle with zero residual process or
  port listener.
- Added backend-crash resilience after a full-source audit found the frontend
  could never recover from a backend restart: the Tauri shell now supervises
  the sidecar and restarts it (closing the crashed generation's Job Object
  first, reclaiming its llama-server children). That original implementation
  reused the session token; current builds rotate session ID/token each
  generation. REST
  calls refresh the session and retry once on 401/network failure; both
  WebSockets reconnect with exponential backoff and de-duplicate replayed
  history; the header shows an explicit backend-disconnected state. Instance
  lifecycle transitions now reach the backend file log, and launcher
  supervision events reach `launcher.log`. Coverage: 37 Python / 6 frontend /
  7 Rust tests.
- Removed leftover empty `backend/api` package, stale root `__pycache__`, and
  an empty root `target/` directory from the workspace.
- Regenerated `SHA256SUMS.txt` for the rebuilt `0.9.0-beta.1` setup bundle
  (the previous file still described the 2026-08-17 build) and moved the stale
  August 2 `LunarShroud_1.0.0_x64-setup.exe` out of the bundle directory into
  the workspace-cleanup archive.
- Ran an authorized offensive audit (probe scripts kept in `tmp/`): 22 live
  attacks against a standalone packaged backend (auth matrix, WebSocket
  origin/token, eight path-traversal encodings, argv injection, key redaction,
  config validation) plus a full frontend XSS sweep. All core defenses held.
  Fixed the two findings: chunked-transfer bodies now honor the 2 MiB request
  cap via a bounded stream read (regression test added), and the two remaining
  unescaped preset-ID dropdown render sites were escaped. Residual risks
  (loopback-wide CSP targets, unbounded local concurrency) were recorded in
  `SECURITY_MODEL.md`.

## 2026-08-22: desktop startup incident repaired

The Desktop shortcut targeted a stale root `LunarShroud.exe` built as version
1.0.0 in July, while the current package was `0.9.0-beta.1`; this caused the
reported shortcut/startup failure.

- Replaced the root launcher with the current release launcher and deployed its
  matching backend sidecar.
- Preserved the replaced executable in
  `tmp/legacy-root-launcher-backups/20260822-223716/`.
- Rewrote the shortcut and verified window creation, authenticated API behavior,
  clean exit, and port release.
- Added `tools/Publish-PortableRuntime.ps1` and a backend regression test so a
  future portable deployment copies and verifies both files before changing the
  shortcut.

## 2026-08-16 to 2026-08-17: stability, security, and OSS preparation

After reports of UI stalls and processes that would not close, the source was
hardened for child-process cleanup, Job Object ownership, bounded
logs/history/work queues, packaged authentication, loopback-only management,
path containment, request/import/model-scan bounds, API-key redaction, and
trusted bundled-backend startup. CI, dependency audit, regression, release, and
source-sanitization support were also added.

The published tree is a curated public-source mirror, not a binary release
directory; it intentionally excludes local data, build output, executables,
logs, screenshots, and caches.

## Before 2026-08-16: early implementation record

The early FastAPI, frontend, Tauri, packaging, and UI-test work is described by a
2026-08-02 handover that the maintainer keeps locally; it is not part of this
repository. Its version labels, paths, test counts, and release conclusions were
already stale when it was superseded, so re-verify everything against the current
source before acting on any of it.
