# Changelog

Notable changes follow the Keep a Changelog structure. The project is currently pre-1.0.

## [Unreleased] - 2026-10-23

### Added

- Local knowledge bases: text/code, HTML, PDF, DOCX and scanned PDFs are indexed
  in SQLite with chunk vectors plus FTS. GLM-OCR runs through `llama-server`,
  Qwen embeddings are used for indexing and query retrieval, and an optional
  Qwen reranker rescues the fused candidate list.
- Chat knowledge mode: select a base in the toolbar, retrieve only for the
  current question, inject numbered excerpts server-side, and persist citations
  in assistant message stats so reload and Markdown export keep their sources.
- Knowledge management UI: create/edit bases, add files/folders/paths, live
  indexing progress, document preview, cancellation, retrieval testing, manual
  model unload, and bilingual localized failures.
- Navigation consolidation: model library/runtimes/presets now live in a
  **Model workspace**, logs/monitor/history in **Observability**, and knowledge
  bases have their own top-level page.
- Model cards show quantization, size, architecture, native context, recent-use
  state and quick launch; the dense list is now a real table (file / quant / size /
  architecture / context / actions) instead of a control that hid the grid.
- Knowledge indexing can be cancelled from the UI, and the confirmation names the
  base it is about to delete before anything is destroyed.
- Global backend reconnect/offline banner, browser core-path tests, axe-core
  accessibility gate and first-paint performance budget in CI.
- Two deeper browser suites: one clicks every previously unverified path (model
  detail, view toggle, quick-launch preflight, chunk preview, cancellation,
  settings persistence per category, English locale, dark theme, 800px layout)
  and sweeps every non-destructive control on tabbed pages for dead actions; the
  other forces failures — missing path, absent or broken model, embedding service
  killed mid-index, rerank misconfiguration — and requires the UI to admit each.

### Changed

- Indexing is staged: OCR runs and unloads before embedding starts; page and
  whole-document extraction results are cached by content hash.
- Dependency locks and the PyInstaller onedir build now include `pypdf`,
  `pypdfium2` and `numpy`; PDFium native binaries are collected explicitly.

### Fixed

- Knowledge-base quota failures, missing models, scanned PDFs without OCR and
  embedding-model mismatches return readable errors without deleting an existing
  index or silently answering as if retrieval succeeded.
- **Hybrid retrieval was dense-only.** `search_fts` quoted the whole question as
  one FTS5 phrase, which requires a verbatim substring of the user's wording and
  matched nothing on natural-language questions. Measured on a 377-chunk legal
  index: the fused ranking was identical to the dense ranking on 17 of 17
  questions. Queries are now split into clauses (including CJK 3-grams) so the
  lexical arm actually contributes — hit@1 35% → 52%, MRR 0.432 → 0.634, and
  76% / 0.843 with reranking. See `docs/KNOWLEDGE_BASE.md`.
- **The model-card 快速启动 button never fired.** Its click was swallowed by the
  card's own `[data-open-model]` delegate, so quick launch silently opened the
  detail pane instead. The more specific action is now matched first, and a
  browser test refuses to pass unless the click reaches preflight or issues the
  launch request.
- **The 卡片/列表 toggle was dead, and "list" would have blanked the library.**
  `workspace.js` hoists a child module's `.page-actions` into the shared toolbar,
  which moves those nodes outside the module's subtree — so the module's
  root-delegated listener never saw the click and `root.querySelectorAll` never
  found the buttons to sync their pressed state. List view had also never been
  implemented: selecting it only hid the grid. The control now holds its own
  nodes, and list view renders a real table with file, quant, size, architecture,
  context and actions columns.
- **A knowledge base could not be cancelled from the UI.** `/cancel` was wired
  end to end in the backend and in `api.js`, but no control ever called it, so an
  indexing run could only be waited out. The progress block now has a 取消索引
  button that leaves finished documents intact.
- **Chat overflowed the viewport at 800px.** `.chat-page-v2` pulled itself to the
  container edges with hardcoded `-20px -24px` margins, but the main padding
  drops to 14px on narrow layouts, so the page measured 820px in an 800px window
  and lost 10px on each side. The bleed is now derived from
  `--main-pad-block` / `--main-pad-inline`.
- **A named-but-missing source path was dropped silently.** `collect_paths`
  skipped any path that did not exist, so a typo produced "added" while the
  document never entered the index. Explicitly named missing paths now fail with
  the path in the message; walk-time disappearances and symlinks still skip.
- **The add-paths dialog closed on failure,** losing both the error context and
  the text the user had typed. It now stays open, which is what AGENTS §2.20
  already required.
- **`cargo tauri build` could no longer build the shell.** `frontend/` is Tauri's
  `frontendDist`, so the Node test dependencies added for the browser suites made
  it refuse with *"The configured frontendDist includes the `["node_modules"]`
  folder"* — and any scratch output under `frontend/tmp/` would have been embedded
  into the shipped binary. The JS package (`package.json`, lockfile,
  `node_modules`) now lives at the repository root, where Node still resolves it
  for `frontend/tests/**`; CI runs `npm ci` from the root.
  `backend/tests/test_web_root_hygiene.py` pins the web-root allowlist so this
  cannot come back.
- **A knowledge service could become permanently unreapable.** An index run took
  the embedding service with a bare `acquire()` that nobody released, and the
  idle reaper only started inside `lease()`; separately, `_reuse_running()`
  returned `owned=False`, and `release()` skips non-owned handles forever.
  Measured on a live machine: an embedding service stayed resident **22+ minutes
  and ~2 GB of VRAM** past the documented 10-minute unload. Indexing now uses a
  lease, `acquire()` starts the reaper, and a reused instance keeps its ownership
  when its name carries the `[KB] ` prefix (AGENTS §2.30).
- **`.grid-full` never applied.** The rule was written as a class selector while
  every call site passes `grid-full` as an attribute, so the dashboard's empty
  and debug states occupied a single 280px grid column instead of spanning.

### Added

- **Measured retrieval quality, reproducible.** `tools/eval_knowledge_retrieval.py`
  scores 21 labelled questions plus 10 deliberately unanswerable ones against a
  real 377-chunk legal index, and ablates dense / hybrid / +per-document cap /
  +rerank. Current shipped path: **hit@1 76%, hit@3 95%, MRR 0.841**.
  Numbers, the rejected options and the reproduction command live in
  `docs/KNOWLEDGE_BASE.md`.
- **Per-document cap in fusion** (`MAX_PER_DOCUMENT = 2`): one 161-chunk
  regulation no longer fills both recall arms. Without reranking this lifted
  hit@3 66% → 76% and hit@5 76% → 80% at no hit@1 cost.
- **Article labels on chunks.** Chunks keep their windowed boundaries — splitting
  on 第X条 measured *worse* (hybrid hit@1 52% → 35%) — but now carry the chapter
  or article they mostly contain, so citations read
  《个人信息保护法》第3页 · 第十七条.
- **OCR provenance is disclosed.** Documents indexed from scans show an `OCR`
  badge in the knowledge base and on every chat citation, with a tooltip saying
  the wording is best-effort. The user's legal PDFs turned out to be browser
  prints whose wrapped lines were clipped in the file itself, so no parser or
  OCR setting can recover the missing characters.
- **First-run dashboard.** Replaced the lone "暂无实例" icon with a two-card
  panel: three working actions (选择模型并启动 / 浏览模型库 / 打开知识库) and a
  live state card (llama-server configured, preset and knowledge-base counts).
- **Knowledge services are labelled as such** on the dashboard, so a system-started
  embedding server no longer looks like a model the user launched.

## [Unreleased] - 2026-10-27

### Added

- Multi-backend runtime support for the official llama.cpp Windows packages
  (CPU / Vulkan / OpenVINO / ROCm). All four ship the same version string and
  the same `--help` surface, so the launcher identifies the backend from the
  sibling accelerator DLL (`ggml-openvino.dll`, `ggml-hip.dll`,
  `ggml-vulkan.dll`) and — the authoritative signal — from the build's own
  `--list-devices` output, cached per path/mtime/size like the help probe.
- The Runtime page lists the devices each build actually reports on this
  machine (name, hardware, total/free memory) with per-backend empty-state
  guidance: a CPU build listing nothing is normal, a ROCm build with no
  supported AMD GPU says so, an OpenVINO build without runtime dependencies
  says so.
- A `--device` parameter so multi-GPU machines can pin inference to specific
  devices (e.g. `Vulkan0`). For OpenVINO builds the launcher translates the
  device name into the `GGML_OPENVINO_DEVICE` environment variable that
  backend actually requires, and refuses to start on an unknown device name
  instead of silently running on the wrong one.
- Setting the llama-server path in Settings now mirrors into the runtime
  registry's default slot (the reverse direction already existed), so the two
  views of "which build am I using" can no longer drift apart. Unregistered
  paths still save fine — sync never auto-registers a profile.
- Auto-generated runtime display names include the backend family
  (`VULKAN · 0.7.0-dev (build 11929…)`); the four official packages otherwise
  produced identical rows in the runtime list.
- A `warnings` field on running instances, rendered as an amber note (not a
  red error), used e.g. for the ROCm CPU fallback notice below.

### Fixed

- **CORS did not expose the chat citation headers.** The packaged page origin
  (`tauri.localhost`) fetches the loopback API cross-origin, and browsers only
  let JavaScript read `Access-Control-Expose-Headers` names. The list carried
  only `X-LunarShroud-Dropped-Turns`, so `X-LunarShroud-Sources` (knowledge
  citations) and `X-LunarShroud-Knowledge` (miss notice) read as `null` in the
  installed build: citations never rendered, never persisted, never exported,
  while the backend was in fact injecting the excerpts. Both headers are now
  exposed, pinned by a real-middleware test and a static contract test that
  reconciles every header the frontend reads against the CORS list.
- PowerShell 5.1 + UTF-8 Python environments crashed four portable-runtime
  acceptance tests (`UnicodeDecodeError` on GBK console output); all test
  subprocess reads now use `errors="replace"`.
- Knowledge-service reaping raced a concurrent `acquire()`: a retrieval could
  receive a handle to a service that was about to be stopped (a one-off 502).
  Acquire/lease/release on the same service key are now serialized, released
  services cannot be re-adopted mid-stop, and `release_kind` skips only the
  busy service instead of the whole kind.
- ROCm builds silently fall back to CPU inference when no supported AMD GPU
  is present (verified on b11929: the service starts normally at ~6.8 tok/s).
  The launcher now surfaces a warning on the instance card and in the log.
- Every audit-identifier comment (`独立审计 M1`, `audit §7`, …) was rewritten
  into plain technical statements so future audits can start blind.

### Changed

- Internal audit/remediation reports moved from the repository root into
  `archive/audit-2026/` (still excluded from the public mirror, now by
  directory rule instead of per-file names).


## [Unreleased] - 2026-10-21

### Added

- Native Chat as the default Chat mode: local `chats.db` conversations, backend SSE proxy (API keys stay server-side), Markdown/code copy, reasoning disclosure, stop/regenerate, token/speed stats, Markdown export and a sandboxed WebUI fallback.
- Chat advanced tools: user-message editing with truncate-and-regenerate, conversation branching, context usage meter tied to the instance `ctx_size`, and a prompt library (built-ins plus editable local prompts stored in `chats.db`).
- Local text attachments via paperclip, file picker or drag/drop. Files are read in the webview only; text extensions, NUL/UTF-8 and per-file/message/conversation bounds are enforced, attachments persist with messages and copy into branches.
- Local conversation search across titles, messages, attachment names and attachment content, with snippets, match-type labels, highlighting, keyboard navigation and focus preservation.
- In-app backup/restore for config, presets, history and chats (including attachments/prompts): SQLite snapshots, SHA-256 manifests, staged restore applied before DB init, a pre-restore backup and path/hash validation.
- Tauri tray icon with Show/Quit, opt-in `close_to_tray` (default off), runtime toggle and explicit quit; EnumWindows restore for a hidden second-instance launch.
- Model-detail manifest editor for `model.yaml`/`manifest.json` with validation preview, atomic save/delete and old-file backups under `DATA_DIR/manifest-backups`.
- Ctrl+K command palette for pages, new chat, model picker, parameter editor, theme and quit.

### Changed

- Packaged regression on 2026-10-21 rebuilt the PyInstaller onedir backend and optimized Tauri binary and verified real Qwen3.5-4B streaming plus packaged text attachments in WebView2; the repo-root portable pair was not replaced.
- Follow-up on 2026-10-22 **did** rebuild and redeploy the repo-root portable pair (it had been left at the 2026-10-18 build while the desktop shortcut pointed there, so the shipped UI looked unchanged despite later work). The native chat UI is embedded in the shell binary at compile time, so a stale `LunarShroud.exe` cannot show newer frontend files no matter what is on disk.

#### Chat reasoning trace (native Chat)

- The model's thinking process is now a first-class `messages.reasoning` column with its own bound, instead of being squeezed into `stats_json`. It previously had to be truncated to 4000 characters before the write because the whole stats blob is capped at 16 KiB, so **the tail of any long chain of thought was destroyed at save time**: users saw the full trace while streaming and a shortened one after a reload, with no indication. A lazy `ALTER TABLE` migration adds the column and backfills the legacy `stats.reasoning` value, so existing sessions keep what they already had rather than losing it on upgrade.
- The reasoning panel now expands while the model is thinking and collapses when the answer starts (default `auto`, selectable in Settings: auto / always expanded / always collapsed). Manual open/close is recorded on the message and survives the streaming re-render, and it **overrides** the preference for that message -- otherwise reading a collapsed trace would fight an automatic policy that snaps it shut on the next chunk.
- Copy offers `Copy` and `Copy with reasoning` when a reply has a trace instead of silently dropping half the message; Markdown export writes the trace under its own heading, since an export is an archive.
- Editing an assistant answer clears its now-inconsistent trace; `branch` copies the trace; an oversized trace is rejected with an error rather than quietly cut.

#### One source of truth for "is a build configured"

- `config.llama_server_path` and the runtime registry used to be two independent answers to the same question, and three UI sites looked only at the first. A user who registered a build under **运行环境 / Runtimes** could launch normally -- the backend accepts `server_path or cfg["llama_server_path"]` -- yet Settings refused to save *any* change (port, theme, tray were all blocked by that one empty field), the dashboard kept showing a "get llama-server" banner, and the Getting-started checklist reported an error.
- The startup now adopts a legacy bare path into the runtime registry (idempotent, never fatal, and it does not invent a profile for a missing file), choosing a default runtime mirrors the path back into the legacy field, and all three sites consult one shared predicate that accepts either. A registered build whose capability probe failed still counts: probing answers *which flags* exist, not whether the binary can launch.

#### Chat length is measured in tokens, not bytes

- The chat send path is admitted against the model's real context window in **tokens** (`--tokenize` on the instance), trimming oldest non-system turns until the prompt fits, instead of two limits that measured different units. `MAX_REQUEST_BYTES` (2 MiB, checked as body bytes by middleware) used to fire long before `CHAT_MAX_TOTAL_CHARS` (2,000,000 characters): one Chinese character is 3 UTF-8 bytes, so a Chinese conversation was rejected at roughly 699,000 characters -- about 35% of the documented budget -- with a generic `413 request body too large` that mentions neither chat nor length, while an English conversation of the same size reached the intended `conversation is too long` message.
- The 2 MiB transport guard is kept for every other endpoint (dropping it would let one request exhaust memory); only the chat route is widened to 8 MiB so the token policy is what actually decides, and it must be, because the middleware runs *before* trimming could happen.
- When turns are dropped the count is returned in `X-LunarShroud-Dropped-Turns` and the UI says so -- silently losing history is the same failure class this whole pass set out to remove. The header had to be added to CORS `expose_headers`: the webview is cross-origin to the backend, so without it the notice would ship and never display.
- If even the system prompt plus the current question cannot fit, the request fails with the measured token count and the configured window instead of trimming the user's input away.

Verified against the real local build: a 3.10 MiB Chinese conversation (121 turns) against a 1024-token window now returns 200 with 120 turns trimmed and a normal reply, while a 3 MiB body to a non-chat endpoint still gets 413.

### Fixed

- `tools/Publish-PortableRuntime.ps1` now signs the launcher and the NSIS installer with a
  local code-signing certificate when one is present, and skips with an explicit warning when
  none is (so a plain clone still deploys). It signs the **source** artifact *before* staging,
  because the whole script verifies "staged copy == source" by SHA-256 and signing after that
  check would invalidate it. When it signs an installer it also regenerates that directory's
  `SHA256SUMS.txt` — otherwise signing would recreate the stale-checksum defect fixed earlier in
  this release. A signing failure aborts the deploy instead of silently shipping an unsigned
  binary, and `-SkipSigning` exists for tests that use non-PE fixtures.
  Local trust only: the certificate lives in `CurrentUser\My`, trusted by
  `CurrentUser\Root` + `CurrentUser\TrustedPublisher`, and does nothing for other machines.

Independence note: every item below was re-verified against the current source
with an isolated reproduction **before** being changed; the 2026-10-21 external
audit report was used as a lead, not as evidence. Each fix ships a red-green
test (it fails against the pre-fix code).

- A `config.json` that is valid JSON but not valid UTF-8 (saved from Notepad as
  ANSI/GBK with a Chinese model path) no longer bricks the backend.
  `UnicodeDecodeError` is neither `json.JSONDecodeError` nor `OSError`, so it
  escaped `_load_config_from_file` and reached `main.py`'s **import-time**
  `get_config()` — uvicorn never bound a port and the user only saw "backend
  failed to start", with the config neither quarantined nor recoverable.
  Non-decodable files are now quarantined to `config.json.corrupt` and startup
  continues on defaults; a genuine UTF-16 (BOM) config is *read*, not discarded.
- A corrupt `chats.db` is now quarantined and rebuilt like `history.db`.
  Previously the app started, but every `/api/chats` and `/api/prompts` call
  re-ran `CREATE TABLE`, re-raised `DatabaseError`, and the chat page, search,
  prompt library and attachments stayed 500 forever with no in-app repair path.
  The recovery rules now live in one module (`core/sqlite_recovery.py`) shared by
  both databases: only SQLite-reported corruption is quarantined, lock and
  permission errors still propagate, and `-wal`/`-shm` move with the main file.
- `Publish-PortableRuntime.ps1` no longer deletes a working install when it
  fails *before* swapping. The staging hash checks ran inside `try` while the
  `catch` deleted all three destinations unconditionally — and because
  `.rollback` is only created after those checks, there was nothing to restore.
  Reproduced against a fixture: the target directory was left containing only
  `.next` files. Rollback is now per-slot (a slot with no `.rollback` was never
  touched), stale `.next` leftovers are cleared before staging so one crash
  cannot wedge every later deploy, and the PS 5.1 `Remove-Item`
  `NullReferenceException` in `finally` is gone.
- Branching a conversation at the 500-conversation cap no longer destroys the
  source thread. The prune deleted the oldest row without excluding the
  conversation being branched; verified both outcomes, including the more common
  one where an *unrelated* conversation was silently evicted instead.
- `extra_args` can no longer override `--model` / `--cors-origins`. Managed-flag
  enforcement covered `host`/`api_key`/`port`/`path`/`log_file`, but the same
  last-wins mechanism applies to identity: validation, preflight, history and
  VRAM estimates all read `params["model"]` while `extra_args` lands at the end
  of argv. The blacklist is now scoped to `config_resolver.MANAGED_KEYS` and
  covers each upstream spelling (`-m`, `--model`, `-mu`/`--model-url`), and is
  enforced at preset import as well as launch. `--mmproj` deliberately stays
  allowed.
- Launch now probes the configured build instead of promising to. Preflight said
  "flag support will be verified at launch" while `_validate_start` only *read*
  the cache and `DefaultGate.supported` passed unknown flags through, so a cold
  cache produced a launch that died in llama-server argument parsing with an
  unhelpful `invalid argument`. The launch path now warms the probe (bounded,
  single-flight, degrades on failure) and a cold cache is reported as a warning
  rather than an informational line.
- Python `system_tool()` resolves the real system directory through
  `GetSystemDirectoryW` instead of trusting `SystemRoot`/`windir`. A forged
  `SystemRoot` previously produced an *absolute* path into an attacker-writable
  directory, which is exactly what the absolute-path rule was meant to prevent;
  the Tauri shell already used the API. When the system directory cannot be
  determined the tool is disabled rather than falling back to a bare name.
- Presets saved with a UTF-8 BOM are no longer invisible. `preset_store` read
  with `utf-8` while config and manifests use `utf-8-sig`, so `json.load` threw
  and `list_presets` silently skipped the file — the preset stayed on disk but
  vanished from the UI. Skipped presets are now logged, and the paste-to-import
  path strips a leading BOM. The in-app backup manifest reader was aligned too.
- `tools/verify_checksums.py` gained `--generate` and now fails when the
  directory contains a release-shaped file the checksum list does not cover.
  The shipped `SHA256SUMS.txt` (written 2026-10-18) did not match the
  `0.9.0-beta.2` installer (rebuilt 2026-10-20); checksums were regenerated for
  the current artifact. `LunarShroud_0.9.0-beta.1_x64-setup.exe` is still sitting
  in that bundle directory and is now reported as uncovered — decide whether to
  remove it before treating this directory as a release candidate.
- Rust tests no longer write the real `%APPDATA%\LunarShroud\logs\launcher.log`.
  `request_shutdown()` logs, and the candidate chain puts `%APPDATA%` first, so
  `cargo test` both polluted user data and failed under restricted tokens
  (AGENTS §6.5). Test runs now log beside the test binary; verified by
  comparing file mtimes before and after `cargo test`.
- History rows render `port` and `exit_code` through `escapeHtml`. Normal writes
  are integers, but the column is not type-constrained, so a hand-built or
  restored SQLite backup could carry a markup payload into the history detail
  view.


## [0.9.0-beta.2] - 2026-10-17

### Added

- `tools/Backup-UserData.ps1` and `tools/Restore-UserData.ps1`: backup `config.json`, `history.db` (plus WAL/SHM) and presets into a timestamped folder with `SHA256SUMS.txt`; restore verifies the manifest before touching current data, rejects tampered backups and manifest path traversal, and keeps a `LunarShroud.pre-restore-<timestamp>` snapshot. Logs are opt-in via `-IncludeLogs`.
- In-app **Getting started** page (sidebar → 新手指南 / Getting started): five-step setup, GPU build table, model requirements, the most common wrong operations, and a symptom→cause→fix troubleshooting table. Its checklist reads the current config, capability probe, model scan and history, so it shows exactly which setup step is still missing. A setup banner on the dashboard links to it whenever `llama_server_path` or the model folder is unset.
- **Pre-launch checks** (`POST /api/diagnose`) run before every launch from the dashboard, model library and parameter editor. Blocking errors (missing server/model, non-GGUF or truncated file, mmproj selected as the main model, forbidden `extra_args`, occupied port, non-loopback without an API key) stop the launch; warnings (VRAM over/tight, context above the model native limit, embedding model without the embeddings endpoint, unsupported parameters) can be acknowledged and overridden.
- **Failure attribution** for failed instances (`GET /api/instances/{id}/diagnosis`, also embedded in the instance payload): classifies exit codes (`0xC0000135` missing DLL, `0xC0000005`, `0xC0000409`, …) and log patterns (CUDA runtime missing, out of memory, model load failure, GGML assertion, draft vocabulary mismatch, port conflict, bad arguments, permission denied, timeout). The dashboard adds a “Why did it fail?” action that shows the localized cause and fix for each item.
- GGUF encoder detection: `<arch>.attention.causal=false` or a declared `<arch>.pooling_type` marks embedding/encoder models (e.g. nomic-bert), and a reranker name hint marks rerankers. The model library shows 嵌入模型 / 重排模型 badges; direct launch automatically enables `--embeddings` (and `--rerank` when detected) instead of walking into the upstream single-slot embedding assertion.
- The pre-launch dialog only warns about suppressed parameters the user actually changed; a stock launch is no longer blocked by schema defaults such as `--mmap` that the local build lacks.
- `docs/GETTING_STARTED.md`: the same first-install and troubleshooting guidance for users who read the repository instead of the app.
- `docs/RISK_ASSESSMENT.md`: a probability/impact/evidence register for the remaining release risks. This round measured `--cache-ram` host-memory behavior (default 8 GiB cache grew llama-server RSS to 11.4 GB; `cache_ram: 0` stayed at 3.21 GB), verified packaged supervisor crash-restart and clean shutdown, scanned isolated logs for the session token (zero hits), and boot-tested the rollback backup pair. The guide now recommends lowering `--cache-ram` on small-memory machines and setting `api_key` on shared machines. The sixth-round independent review's documentation findings were then fixed: the `EVOLUTION.md` v0.7.0 snapshot now records that NSIS validation was completed on 2026-10-17, R8's attribution was corrected, and a full silent install → installed-app smoke → silent uninstall replay was retained with raw installer output, installed hashes, registry snapshots and before/after user-data hashes.

- Self-adapting parameter system: the backend parses `llama-server --help` (cached per binary mtime/size) so every parameter carries `supported` and `upstream_options` for the current build; flags the build does not recognize are dropped from the command line and surfaced to the UI as "suppressed" chips, and default-value omission is adjudicated per build (three-state) instead of trusting a hardcoded snapshot.
- Model capabilities: GGUF header probing (native context length, KV geometry, layer count, MTP/NextN weights, mmproj detection with metadata-name pairing), a model library page with capability badges and companion-file tables, a top-bar model picker (Ctrl+L), per-model VRAM estimation keyed to the current parameter values, and speculative-decoding recommendations with one-click apply.
- Free-form `extra_args` CLI field appended verbatim to the end of the command (quote-aware splitting, unclosed quotes rejected at launch), plus human-readable hints for Windows crash exit codes (e.g. `0xC0000005` with draft-model vocabulary guidance).
- Bilingual UI (Simplified Chinese / English): zero-dependency i18n module with per-locale dictionaries (423 keys each), automatic language detection from the browser, header and Settings language switchers, and localized mapping of known backend errors. The parameter schema serves `label_en`/`help_en`/`name_en`/`description_en` alongside the Chinese originals; backend API errors are English-first.
- Real-time instance status push: a new `/ws/instances` broadcast replaces the five independent UI pollers (dashboard, header, Chat, logs sidebar). A single app-level feed drives all pages and falls back to 5-second polling only while the WebSocket is disconnected, making status changes instant instead of up to 3 seconds stale.
- Launch history auto-pruning via the new `history_max_records` setting (default 1000); the oldest records are trimmed on write so `history.db` can no longer grow unbounded.
- Instance log persistence via the new `persist_instance_logs` setting (default on): each instance's stream is written to `data/logs/instances/<id>.log` for post-crash diagnosis, with per-instance write-failure isolation.
- Restart lineage: restarted instances carry `restarted_from`, so the UI and history can follow the chain; restart no longer sleeps a fixed second — the same port is reused immediately after the old child exits.
- GPU monitoring now prefers NVML (`nvidia-ml-py`) over spawning `nvidia-smi` every sample, with automatic fallback when the library or driver is unavailable; per-process VRAM is reliable where the driver reports `[N/A]`.
- End-to-end smoke tests with a real child process (launch → health probe → running → real taskkill tree stop → history/log persistence → restart lineage → concurrent auto port assignment), plus frontend unit tests for i18n, parameter-enable logic, and port auto-assignment.
- Ruff lint gate in CI (E/W/F/I/B/UP) with all existing violations fixed.
- AI tools & MCP parameter group: `--agent`, `--tools`, `--tools-runtime`, `--ui-mcp-proxy`, `--mcp-servers-config` and `--rerank` now render in the parameter editor for llama.cpp builds that support them (capability-driven: older builds gray them out and drop them from the command line). This covers the built-in agent tools (`read_file`, `grep_search`, `exec_shell_command`, …), external MCP servers and the reranking endpoint used by RAG-style pipelines.
- MoE, draft, video and lazy-loading parameters aligned with the official llama.cpp build (0.6.0-dev / build 11630): `--cpu-moe` / `--n-cpu-moe` / `--n-cpu-ffn` / `--moe-cache-mib`, `--spec-draft-n-ubatch` / `--spec-draft-sampling`, `--video-fps` / `--video-ffmpeg-dir` / `--video-timestamp-interval` and `--lazy-mode` join the parameter panel, likewise capability-driven.
- The inline `--mcp-servers-json` counterpart of `--mcp-servers-config` is exposed as a first-class parameter too (capability-driven like its sibling).
- Use-case recipes in the parameter editor — chat, embeddings (RAG retrieval), reranking (RAG rescoring), read-only tools and full agent — served by a new `/api/params/recipes`. A recipe sets **intent only**: no context sizes, layer counts, VRAM tiers and no model path, because tuned numbers are exactly what drifts with every llama.cpp release (the reason built-in presets were removed). Values are filtered against the schema and the local build, and a recipe whose flags the build lacks is disabled with the reason in its tooltip. `docs/RAG.md` documents the building blocks, a measured two-instance layout for 8 GB cards, and the v0.7.0 pitfalls.
- RAG- and v0.7.0-facing parameters, all capability-driven: `--pooling`, `--embd-normalize`, `--cache-prompt`, the router-mode pair `--models-dir` / `--models-max` (v0.7.0 multi-model serving and its Web UI model manager), the thinking controls `--reasoning` / `--reasoning-format` / `--chat-template-kwargs`, and `--rpc` (RPC protocol v8, tensor-parallel).
- Allowed values declared in the **argument position** are now parsed: `--pooling {none,mean,cls,last,rank}` and `--reasoning [on|off|auto]` style hints feed `upstream_options`, so those dropdowns track the local build instead of a static snapshot (previously only `allowed values:` text and the `--spec-type` special case were understood).
- `GGML_ASSERT` crash lines are now recognized as the fatal cause and, when the assertion is one we have reproduced, translated into an actionable workaround. The first entry is v0.7.0's `GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max)`, which fires when an embedding model is loaded with a single slot and no `--embeddings`; both workarounds (enable the embeddings endpoint, or use 2+ parallel slots) were verified end to end through the launcher.

### Fixed (post-takeover hardening, 2026-10-18)

- Restart/stop races no longer release a port that a newer generation is still using; a process that has not exited keeps its handle and reservation. This removes a duplicate-port class behind "port already in use" launch failures.
- System helpers (`taskkill.exe`, `nvidia-smi.exe`) are resolved from the actual system directory and disabled if that fails; relative `PATH` entries are ignored. A relative `llama-server` name can no longer execute a same-named file from the current directory during capability probing.
- Capability results from an incomplete `--help` no longer report supported flags as unsupported.
- In restricted environments the launcher and backend now choose the same writable data directory, so a saved `launcher_port` is honored instead of being overridden by the old `%APPDATA%` value.
- Single-instance mutex failures no longer show "already running"; crash-loop restarts back off to 30 s; WebView2 falls back to `%LOCALAPPDATA%` when the exe directory is read-only; the readiness probe caps the response body and refuses redirects; a normal close always logs the backend exit.
- Non-idempotent API calls are no longer automatically retried on network errors (which could create duplicate instances/presets); missing static assets return 404 (an explicit favicon link keeps the console clean); an unreadable config is never overwritten with defaults; GPU sampling for VRAM estimates runs off the event loop.
- The release mirror gate fails closed when `github/` is missing, checks privacy/forbidden rules before `--apply` writes, and excludes runtime directories created beside the exe.
- The portable deployer rejects an incomplete onedir build and swaps launcher/backend/runtime together with rollback.
- The NSIS uninstaller now removes the WebView2 profile, runtime temp, logs and diagnostic files the app creates beside the exe; user data in `%APPDATA%`/`%LOCALAPPDATA%` is untouched. Silent install → installed-app start → silent uninstall was re-run against the built pair.

### Changed

- Release builds now pin `SOURCE_DATE_EPOCH=1790000000` and `PYTHONHASHSEED=0`. Without them, consecutive PyInstaller builds embedded different timestamps/hash iteration order; with them, two consecutive builds produced byte-identical `backend.exe` and `lunarshroud.exe`. The NSIS installer still varies because makensis writes its own metadata, so release verification checks the installed binary hashes.
- The release workflow now reruns the full gate set before producing an installer — ruff, frontend unit tests and syntax checks, Rust formatting/Clippy/tests — instead of only pytest. A new regression test enforces that the version in `tauri.conf.json`, `Cargo.toml` and `Cargo.lock` agrees and appears in `CHANGELOG.md` and `README.md` (audit RR6).
- Backend error messages are now English (international convention and greppable logs); the frontend localizes the common ones per user language.
- The monitor page subtitle now shows the actual configured sampling interval instead of a hardcoded (incorrect) "2 seconds".
- The backend honors `LUNARSHROUD_LAUNCHER_PORT` over `config.json`'s `launcher_port` (in memory only; invalid values are ignored with a warning), so the launching process can pin the listener port without rewriting the user's config file. The Tauri shell sets that variable from the same persisted config before spawning the sidecar and probes readiness on the same port, so the shell and the backend can no longer read different config locations across dev and packaged layouts.
- `n_gpu_layers` now defaults to `-1`, which leaves `-ngl` out of the command line and lets llama-server choose. The previous default of `99` forced every layer onto the GPU, and llama.cpp treats an explicitly-set value as an instruction it must not override: when VRAM falls short it logs `n_gpu_layers already set by user to 99, abort` and stops adapting, turning an ordinary memory shortage into a hard crash. The new default still tries full offload but spills the overflowing layers to CPU instead. Set a number to pin it.
- Relaunching from the launch history now auto-assigns a fresh port instead of replaying the recorded one, so relaunching a still-running generation no longer fails with "port already in use" (direct launches with an explicit port are unchanged).
- Select-value validation now accepts the values **the local build declares**, not just the schema snapshot. The dropdowns already merged upstream values in, so a value the UI offered could still be rejected with a 400 at launch — llama.cpp does add values between builds (`--spec-type` gained `draft-dspark` in build 10960). Without probe data the snapshot still applies; nothing is admitted on a guess.
- The `--port` and `--cache-ram` help texts now match v0.7.0 reality: the upstream default port moved from 8080 to 9931 (the launcher always passes `--port` explicitly, so instance identity, health checks and the embedded Chat page are unaffected), and upstream now recommends `--cache-ram 0` for decision/scoring models.

- The application icon and the in-app logo now have genuine transparent rounded corners (radius 17.2% of the edge; the arc is a plain circle, not a squircle — fitted error 1.84 px vs 8.4 px against the reference). Every shipped asset previously had alpha 255 on all four corners: the artwork *drew* a rounded square but filled outside it with opaque dark navy, so Windows composited a full square tile and the launcher looked boxy on the desktop. `src-tauri/icons/artwork.png` is now the single opaque master and all sizes derive from `tools/gen_icons.py`, which also zooms 1.16× to push the artwork's own dark frame off-canvas — masking alone left a band along the rounded edge that read as a dirty border on light backgrounds. `backend/tests/test_icons.py` rejects any asset that regresses to opaque corners; `Pillow` became a dev-only dependency to make the generator reproducible.

### Removed

- Built-in preset templates: they drifted with every llama.cpp release (speculative decoding names, VRAM tiers), so presets are now entirely user-created and all presets can be deleted.

### Fixed

- **Readiness is now authenticated by a challenge-response, not by echoing a public value.** The shell used to accept any local process that returned the `session_id` it had just sent in the probe URL; a process that bound `127.0.0.1:<port>` first could impersonate the backend and receive the management token from the WebView (then `PUT /api/system/config` → arbitrary executable). The backend now answers `HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)` for a fresh shell-chosen nonce; the token never appears in the request or response.
- **Session credentials rotate on every backend generation.** The supervisor used to restart a crashed backend with the same token and the WebView kept reconnecting to the same port; an attacker could win the restart gap and capture the token. Each generation now gets a new `session_id`/token, `get_backend_session` returns nothing until the new generation passes the HMAC check, and the packaged frontend no longer falls back to the hardcoded port when no verified session exists.
- A capability probe could hand the session token to the user-selected `llama-server` build: `server_probe._run` spawned `--help`/`--version` without an `env`, so `LUNARSHROUD_SESSION_TOKEN`/`SESSION_ID` were inherited. Probes, `nvidia-smi` sampling and `taskkill` now all run with `LUNARSHROUD_*` stripped (`sanitized_child_env`).
- Stopping an instance could hang forever when `taskkill` was blocked (security software, group policy, restricted sandbox): the return code was ignored, and `_finalize_process_resources` closed the stdout pipe while the reader thread was blocked inside it, deadlocking the stop request and the monitor thread. `taskkill` failures now fall back to a psutil tree kill, the pipe is closed only by its reader, and finalization keeps a live process handle instead of dropping it.
- Data-page corruption in an otherwise valid `history.db` bypassed the quarantine: `CREATE TABLE` succeeded, then `mark_unfinished_as_interrupted` killed the lifespan (exit 3) or `/api/history` returned 500 forever. `init_db` now runs `PRAGMA quick_check` before caching the path, so page-level damage is quarantined and rebuilt at startup.
- If every termination path failed, the stop path still released the port and dropped the live process handle, leaving an orphan llama-server with no API able to stop it and a port that could be reassigned. The handle and reservation are now kept until the child is confirmed dead, and a second stop retries termination.
- Unhandled `OSError` from `Popen` no longer turns `POST /api/instances` / `restart` into a bare 500; the readable launch error is returned as a 400. Huge `ctx_size`/`parallel` query values are rejected by bounds instead of overflowing `float()` into a 500, and non-ASCII tokens/session values are compared as bytes (401/404) instead of raising `TypeError`.
- Automatic port allocation now treats `0.0.0.0:P` and `127.0.0.1:P` reservations as conflicting, closing the window where two concurrent launches were assigned the same port.
- `backend/requirements.lock` now includes `colorama` (click's Windows dependency), so `pip install --require-hashes -r backend/requirements.lock` can actually install.
- `README.md` had a broken code span (`\``) and a doubled backslash in the GPU build table; `docs/BUILDING.md` no longer claims the Python closure is not hash-locked.
- The Tauri shell now gives the PyInstaller backend its own `TMP`/`TEMP` (`%LOCALAPPDATA%\LunarShroud\runtime-temp`) instead of inheriting the user's `%TEMP%`, recreates it before every launch, and removes `_MEI*` leftovers older than an hour. This fixes the observed "Could not create temporary directory!" startup failure on a machine whose inherited temp state was unusable, and stops forced-kill tests from accumulating gigabytes of extraction directories in the user's Temp.
- **The backend sidecar is now PyInstaller `onedir`** (`backend/dist/backend/backend.exe` plus `_internal/`) instead of onefile. Startup no longer extracts ~20 MB to a temporary directory, which removes the "Could not create temporary directory!" failure class entirely (reproduced with `TEMP` pointing at a nonexistent path: the onedir backend still starts). `src-tauri/build.rs`, `tauri.conf.json`, `Publish-PortableRuntime.ps1` and the NSIS bundle now copy and verify the whole runtime directory; deploying only `backend.exe` is a broken pair and is rejected by tests.
- **Running under a restricted token/sandbox no longer turns into an unexplained `exit code: 3`.** The frozen backend probes its data directory for writability and falls back `%APPDATA%` → `%LOCALAPPDATA%` → `%USERPROFILE%` → the application directory; a failure to open the log file no longer aborts the lifespan. The launcher does the same for its logs and backend temp directory, writes a `launcher-boot.log` with the inherited environment next to the exe, and captures the backend's stderr into `backend/backend-child.log`. This addresses the observed case where `%APPDATA%\LunarShroud` was readable but not writable (`CodexSandboxUsers:(RX)`), so neither the launcher log nor `backend.log` was written and all the user saw was "后端进程启动失败（exit code: 3）".
- **A second double-click no longer starts a second backend.** The launcher holds a per-session named mutex and, on a duplicate launch, restores/foregrounds the existing window (`ShowWindow`/`SetForegroundWindow`) and exits. Previously the duplicate backend failed to bind port 17520 and uvicorn exited with code 3, which surfaced as the misleading "后端进程启动失败" dialog while the first instance was already running. Window creation and visibility are now recorded in `launcher-boot.log`; a failed window build shows an explicit error instead of leaving an invisible process running.
- **The main window now works when the desktop token cannot write `%LOCALAPPDATA%`.** Tauri's WebView2 user-data folder was the real reason the app could be alive with no visible window in restricted sessions: creating `%LOCALAPPDATA%\<identifier>` failed with `os error 5`, Tauri logged a window-creation failure, and the process idled invisibly. The window is now built with an explicit `data_directory(exe_dir/webview2-data)`; a limited-token scheduled-task probe records `setup: main window created`, `visible_before/after=true`, and the frontend's `WebSocket /ws/instances [accepted]`. When the data directory has to fall back, existing `config.json`/`presets/` are seeded from the real `%APPDATA%` so settings are preserved.
- A corrupt `history.db` no longer prevents the backend from starting. Fault injection produced `sqlite3.DatabaseError: file is not a database` and exit 3; `init_db()` now quarantines the main DB plus WAL/SHM as `history.db.corrupt*` and rebuilds a fresh schema. Lock and permission errors are still raised rather than misclassified as corruption.
- Model, draft and mmproj paths at or beyond the Windows 260-character `MAX_PATH` limit are blocked by preflight with `path_too_long` (or warned at 240+). A valid 315-character GGUF path previously reached llama-server, which failed with `failed to open GGUF (No such file or directory)` while the diagnosis blamed format, download or architecture.
- The model-list capability summary now exposes `truncated` / `stopped_early`, and the library shows a warning badge for truncated GGUF metadata. Malformed files still degrade to `probed: false` instead of failing the scan.
- Startup logs no longer include the per-launch `session_id`; `backend.log` records only `backend started pid=…`. A regression test asserts the session value is absent.
- `Publish-PortableRuntime.ps1` resolves relative `-Destination` / `-BackupDirectory` against PowerShell's `Get-Location`, not .NET's current directory. `-Destination .` once deployed the new pair into `src-tauri\` while the workspace root kept the old pair; the regression test deliberately makes the two locations differ.
- Backup/restore scripts no longer use `Get-FileHash`: Windows PowerShell 5.1 launched from a PowerShell 7 environment can inherit a `PSModulePath` where the Utility module fails to auto-load. They use .NET SHA-256 and pass the same test under that environment.
- `tools/verify_checksums.py` verifies a whole `SHA256SUMS.txt` against the artifacts in one command (fail-closed on missing/corrupt entries). Release reports should quote the file via this tool instead of retyping digests — the fifth-round verification found transcription drift in a hand-written report.
- The E2E fake server retries binding the auto-assigned port for two seconds, so a third-party process winning the test-side probe→bind race no longer shows up as a product failure. The corresponding product-side race (external process takes the port between selection and `llama-server` binding) is documented in STATUS as a known low-probability residual; a retry assigns a new port.
- The Getting started guide now explains that `--n-cpu-moe 0` / `--n-cpu-ffn 0` in the preview are explicit “do not offload” values, not launcher noise (upstream declares no default for them).
- Launch history no longer lies after a launcher exit. Rows left in `starting`/`running`/`stopping` by a previous backend process are marked `interrupted` at startup: the Tauri shell closes the backend's Job Object, so the FastAPI lifespan shutdown hook never runs, and the shipped database contained rows from weeks earlier still marked "running". `stopped_at` and duration stay NULL because the true stop time is unknown. The history page has a dedicated 已中断 / Interrupted badge and a localized explanation.
- Loopback probes now bypass HTTP proxies. `httpx` reads `HTTP_PROXY`/`HTTPS_PROXY` and, on Windows, the registry proxy; with a dead system proxy (Clash/V2Ray configured but not started), every request to `127.0.0.1:<port>/health` timed out and a healthy llama-server was reported as "startup timed out". The backend probe now uses `trust_env=False`; the shell's readiness probe uses `.no_proxy()`.
- Stopping an instance no longer loses its exit code when the monitor thread finalizes the process first. The stop path snapshotted the handle *after* termination; if the monitor had already waited and cleared `instance.process`, there was nothing left to read and history showed `exit=None`. The handle is now captured before termination, and a deterministic race regression test pins it.
- `nvidia-smi` is never invoked by bare name. When neither System32 nor PATH resolves it, GPU metrics are disabled instead of executing a same-named file from the user-writable application directory on every sampling tick (AGENTS §2.1).
- Saving a new `launcher_port` no longer breaks realtime updates before a restart. WebSocket trusted origins now include both the configured port and the port this process is actually listening on; previously every `/ws/*` handshake from the still-served old port got 403 while HTTP same-origin looked healthy (the same failure shape as the earlier R9 bug).
- `setup.ps1` installs the hash-locked dev closure (`requirements-dev.lock` with `--require-hashes`) instead of resolving `requirements-dev.txt`, and prepends `%USERPROFILE%\.cargo\bin` to PATH for the current session so `rustup`/`cargo` work on a fresh machine where the installer's PATH change has not reached the open shell.

- The taskbar button now shows the application icon instead of Windows' generic blank-page glyph. Tauri only ever sets `WM_SETICON(ICON_SMALL)`: tao keeps the `ICON_BIG` slot the taskbar reads in a separate `taskbar_icon` field that is reachable only through the Windows-only extension trait `WindowBuilderExtWindows::with_taskbar_icon`, which neither Tauri nor tauri-runtime-wry calls — so tao explicitly *clears* `ICON_BIG` on every window and the taskbar falls back to resolving the main-module icon through the shell's icon cache. That fallback is cache-mediated and fragile: it rendered the generic glyph once the cache was rebuilt. The shell now sets `ICON_BIG` itself from the exe's own multi-size icon resource at the system icon size, which also stops the window icon being pinned to the ICO's first frame (16px).
- 401/413 responses now carry CORS headers: the CORS middleware is registered as the outermost layer, so the WebView can actually read the status code and the frontend's 401 session-recovery branch works instead of surfacing as a generic network error.
- A startup timeout now says **what the readiness probe actually observed**: how many attempts were made, the URL, and the last status code or exception — and whether llama-server had already printed `listening`. Previously it reported only "startup timed out (not ready within 60s)" after roughly a hundred silent probe failures, which collapsed three unrelated causes (port already taken, probe pointed at the wrong address, model genuinely slow to load) into one undiagnosable message and sent users looking in the wrong place.
- The VRAM estimate now accounts for layer count as well as context length. A 64-layer model was scored as fitting inside 7.96 GiB with 0.02 GiB to spare when it does not; the estimate now matches what llama.cpp itself reports for the same model.
- Configurations that fit with under 5% headroom are now flagged `tight` instead of `ok`. On an idle GPU "free" equals "total", so the previous check let a model with essentially zero margin pass with no warning — and it then died at first inference, while the task manager still showed about 1 GB spare.
- An instance that never passed the readiness check no longer displays a "running for" duration. It reports how long the *process* existed, so a launch that timed out after a minute read as a minute of successful runtime.
- An instance that crashes **after** becoming ready now reports the same detail as one that crashes during startup: the hex exception code plus the last fatal line from the server's own output. The translation was previously wired only into the startup path, so the most common real failure — model loads, readiness passes, first request kills the process — left nothing but a bare decimal like `exit=3221226505` while `CUDA error: device not ready` sat one line above it in the log. Both paths now share one implementation so they cannot drift apart again.
- Speculative "check your draft model / mmproj" guidance is suppressed when the log already names a concrete cause; attributing a VRAM failure to a vocabulary mismatch points users the wrong way.
- The VRAM estimate is now compared against the machine's actual GPU memory and graded (`ok` / `tight` / `over`) instead of rendering a number with nothing to judge it against. `over` predicts the load-then-die pattern described above. This is a warning, never a block: the estimate is accurate only to order of magnitude, and gating on it would reject configurations that do run.
- The compute-buffer term in the VRAM estimate now grows with context length. It was a flat 0.5 GiB, which under-reported large contexts — and that buffer is allocated at first inference rather than at load, so the under-report sat exactly where the crash happens. Recalibrated against a real 8 GB laptop GPU running a 9B Q4_K_M model, where 8K and 16K contexts work and 32K crashes; the estimate now classifies all three correctly.
- The release mirror gate now excludes `data/` by whole directory and scans every mirrored file for local paths. Previously runtime data was excluded by enumerated filename and the privacy scan by a suffix allowlist, so derived files — the SQLite `history.db-wal`/`-shm` sidecars and the `config.json.corrupt` that holds absolute model paths — slipped through both, and `--apply` would copy them into the published tree while reporting clean. Added regression tests for the rule table, which had none.
- `min`/`max`/`options` are now enforced by the backend. They were declared in the schema but only applied by frontend HTML attributes, so a direct API call or an imported preset could put `-c 1000000000000`, `--top-k -5` or `-fa banana` on the command line, surfacing as an unexplained llama.cpp crash instead of a clear 400.
- The model library and model picker no longer mislabel quantization: `Q2_K`, `Q3_K`, `Q4_K` and `Q6_K` were truncated to `Q2`/`Q3`/`Q4`/`Q6` because a bare `Q\d` alternative preceded them in the regex alternation.
- The GPU table no longer silently empties itself on Chinese Windows when `nvidia-smi` prints non-ASCII process paths. Those calls inherited the locale encoding (cp936), and the resulting `UnicodeDecodeError` was swallowed into an empty list with nothing logged.
- `q5_1` KV cache is no longer underestimated by ~9%: the block size omitted ggml's `min` field (24 bytes per 32 elements, not 22).
- A concurrent reader could observe an empty configuration while `PUT /api/system/config` applied an update, because the shared singleton was cleared and refilled in place. The validated config is now built first and the module global rebound, and the file is persisted before the swap.
- Stopping an instance while it was still launching could orphan a live `llama-server`: the stop set its event between `_stop_event.clear()` and `Popen`, so it terminated a process handle that did not exist yet, and the monitor thread then returned on the stop event without reaping. Launch now holds the same operation lock stop does.
- Startup timeout now terminates the process tree instead of a single process, matching the user-stop path, so grandchildren spawned by llama-server no longer survive.
- `os.scandir()` in directory browsing is now closed deterministically; the lingering handle blocked renaming or deleting the browsed directory on Windows.
- A just-started instance shows `0s` uptime instead of `-`; `formatDuration` treated 0 as absent.
- Log timestamps follow the selected language instead of always formatting as `zh-CN`.
- User-facing strings that bypassed i18n now go through it: the sidebar toggle's tooltip and accessible name, and the Tauri-unavailable, request-timeout and network-error messages.
- Empty states in the dashboard and model library use the shared line-icon component instead of ad-hoc emoji.
- Monitor chart data is no longer poisoned by a `NaN` timestamp when the server sends an unparseable one.
- Chat's "reload last model" shortcut now keys off the empty-state element as the frontend contract requires, instead of a WebSocket cache that lags behind a stop.
- Preset clone, history fetch/delete/relaunch now URL-encode their identifiers consistently with every other endpoint.

- Restart and history-relaunch endpoints report port exhaustion as HTTP 409 (matching instance creation) instead of an internal 500.
- Switch parameters sent as integers (`1`/`0`) from imported or hand-edited presets now behave exactly like booleans; previously a truthy `1` could be silently dropped or, for reverse flags, emitted inverted (`--no-…`).
- GGUF probe cache entries are keyed by probe options as well as file identity, so fast-scan and full-scan results can no longer stand in for each other.
- A failed process launch now triggers terminal-instance pruning like every other terminal path, instead of relying on a later unrelated instance event.
- Windows graceful stop was a no-op: `taskkill` without `/F` cannot close `CREATE_NO_WINDOW` console children, so every stop waited out the full 10-second timeout before force-killing. The process tree is now terminated directly, bringing stop latency from ~10s to well under a second.
- `log_buffer_lines` and `monitor_history_seconds` were defined, validated, and editable in Settings but never consumed; both now drive the instance log ring buffer and monitor history depth.
- Launching from the parameter editor always sent the default port explicitly, so a second concurrent instance failed with "port already in use"; the schema-default port is now treated as unspecified and auto-assigned, matching the dashboard launch dialog.
- Launch history no longer full-scans and re-parses every row on each read/write; the secret-scrub migration runs once at backend startup instead.
- Fast-exiting instances could beat history creation and stay "starting" forever; launch history is now recorded via a pre-monitor callback, before the monitor thread can observe an exit.
- Generated command strings used a cmd.exe-only `^` line continuation while claiming PowerShell compatibility; commands are now rendered as a single copy-pasteable line for cmd.exe, PowerShell, and POSIX shells.
- A failed argv build leaked its reserved port for the lifetime of the backend, shrinking the auto-assignment range and causing spurious "port in use" errors.
- Snapshot process handles in the monitor/stop/reader threads, closing a race where finalizing an instance could crash a reader with `AttributeError`.
- Instance IDs now use the full UUID; the previous 8-character form could silently replace an existing instance on collision.
- `/health` probes against wildcard (`0.0.0.0`) listeners now target loopback, consistent with the chat endpoint.
- SQLite history connections are explicitly closed and run in WAL mode; a per-field parse fallback keeps partial `nvidia-smi` output (`[N/A]`) from dropping an entire GPU row, and the GPU table renders such values as "N/A".
- Stale preset buttons no longer throw after a list refresh; log search keywords containing HTML-special characters now match escaped log text.
- `PUT /api/system/config` rejects unknown keys instead of persisting them; corrupt `config.json` is backed up as `config.json.corrupt` and rebuilt from defaults rather than silently reverting on every start.
- Capability probing no longer hangs on wrapper scripts: a `.bat`/`.cmd` target whose children inherit stdout used to block the nominal 15 s timeout indefinitely (585 s measured) and leak its process tree; probes are now bounded by real wall-clock, recycle the whole tree (`taskkill /F /T`), and the single-flight follower loop has a total deadline — a stuck probe can no longer starve the worker-thread pool and take instance management down with it.
- Restarted generations now appear in launch history: `restart` and history-page relaunch previously skipped the launch recorder, so the running generation was invisible while its stopped predecessor kept the only row. All launch paths share one recorder now.
- Instance log files rotate at 5 MB (single `.log.1` backup) and are deleted together with their instance when it is removed or pruned, so `data/logs/instances/` can no longer grow without bound.
- A `config.json` saved with a UTF-8 BOM (Notepad, `Set-Content -Encoding utf8`) is read normally instead of being treated as corrupt and silently reset to defaults.
- A burst of concurrent capability requests against a hung wrapper target can no longer occupy the whole worker-thread pool: waiters beyond a small per-target cap fail fast with a clear message instead of queueing (probe single-flight and process-tree recycling are unchanged).
- Annotated numeric defaults such as `(default: 0, disabled)` — the wording llama.cpp v0.7.0 uses for `--moe-cache-mib` — were parsed as a two-item value list, producing `0,disabled`. Two consequences: the panel reported "default is not a comparable number" for a parameter whose default matches upstream exactly, and `skip_when_default` could never fire, so every command carried a redundant `--moe-cache-mib 0`. Numeric lists (`0.1,0.2,0.3`) and word lists (`ngram-mod,ngram-cache`) are unaffected.

### Security

- Restricted preset IDs and static file resolution to their intended directories.
- Made packaged REST/WebSocket authentication fail closed and launcher management loopback-only.
- Removed release execution fallbacks to the working directory, PATH Python, and source files.
- Redacted API keys from persistence, logs, generated display commands, and API responses.
- Added request, history, import, model-scan, and configuration bounds.
- Removed unused Tauri shell permission and tightened CSP.
- Enforced the request-body size cap on the actual byte stream when `Content-Length` is absent, closing a chunked-transfer bypass found by a local security probe.
- Escaped preset IDs in all preset dropdowns, removing the last unescaped render sites found by a frontend XSS sweep.
- Release artifacts no longer embed the build machine's cargo-registry paths (which include the builder's user name) via Rust panic location strings; builds remap them to a neutral prefix, and the built launcher/backend/installer are byte-scanned for builder identity.
- Bumped `h2` to 0.4.19 and `rustls` to 0.23.45 in the lockfile for two new RustSec advisories (neither reachable from this loopback-only HTTP launcher, but patched anyway).

### Reliability

- Hardened child-process shutdown, Job Object cleanup, logging queues, UI polling, and handle release.
- Prevented console flashes from frequent NVIDIA monitoring and launched llama-server child processes on Windows.
- Added CI, dependency-audit, release-build, regression, and coverage gates.
- Added a hash-verifying portable-runtime publisher after a stale desktop shortcut/root launcher incident; deployment refreshes the launcher and backend sidecar as a pair.
- The Tauri shell now supervises the backend sidecar: an unexpected exit closes that generation's Job Object (reclaiming its llama-server children) and restarts the backend with **freshly generated per-generation session credentials**, so the UI recovers instead of dying until app restart. Launcher-side lifecycle events are appended to `%APPDATA%/LunarShroud/logs/launcher.log`.
- The frontend now survives backend restarts: REST calls retry once after refreshing the session (401 or network failure), system-metrics and instance-log WebSockets reconnect with exponential backoff and de-duplicate replayed history, and the header shows an explicit "backend disconnected" state instead of silently going stale.
- Instance lifecycle transitions (launch, ready, stop, timeout, abnormal exit) are written to the backend file log for post-mortem diagnosis.

## [0.9.0-beta.1] - 2026-08-16

- First beta candidate prepared for extended Windows validation.
