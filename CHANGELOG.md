# Changelog

Notable changes follow the [Keep a Changelog](https://keepachangelog.com/) structure.

Before `1.0.0` this file accumulated five separate `[Unreleased]` sections from
2026-10-21 through 2026-11-03, three of which were dated *before* the `1.0.0`
entry and sorted after it. Nothing was ever collapsed into a released version,
so the file read as an endless stream of iterations rather than as a history of
releases. `1.0.0` is the first version to own the whole body of work.

## [1.0.0] - 2026-11-03

First stable release. The product moved from "launcher for `llama-server`" to a
complete local AI chat application: bring your own llama.cpp build, bring your
own GGUF files, no account, no network, all data on this machine.

**Known limits at this version** — 8-hour soak and the multi-hardware matrix are
not done; artifacts are unsigned. The README's "已知限制" section states both.
This is a feature-complete release, not an unqualified "production-grade stable"
claim.

**How this release was qualified.** An earlier candidate was reported as
passing 903 tests, a clean release mirror and a bootable packaged backend — and
was still unusable. Using it surfaced four defects on real user paths: sending
after a model switch, creating a conversation, the chat inspector covering the
conversation, and quitting the application. None of them could be seen by the
existing gates, because every one of them lived past where the tests stopped:
`TestClient` never crosses the browser's CORS layer, doubles never return 500,
and asserting `querySelector('.chat-inspector') !== null` does not notice a
panel whose right edge sits 152 px outside its container. Those four are fixed
below under "Fixed after real use", and §6 of `AGENTS.md` now says plainly that
green tests do not mean the product works.

### Fixed after real use

- **Sending a message failed with `Failed to fetch` after switching models.**
  The real cause was three layers stacked on top of each other: the data
  directory had become unwritable, so `PATCH /api/chats/{id}` raised
  `sqlite3.OperationalError`; no handler caught it, so Starlette (outside
  `CORSMiddleware`) produced a **500 without `Access-Control-Allow-Origin`**;
  and the WebView could then only report `TypeError: Failed to fetch`. Backend
  logs showed `attempt to write a readonly database` **116 times** while the UI
  said "network request failed" every time. Now: storage errors become a
  `503` with a `code` and CORS headers, every unhandled exception gets CORS
  headers too, and startup probes each database with a **real write
  transaction** (`sqlite_recovery.probe_writable`) and logs an actionable ERROR
  instead of swallowing the failure. New `GET /api/system/storage` exposes the
  result so the UI can warn up front. Separately, a failed binding `PATCH` no
  longer aborts the send: conversation binding is metadata, the target instance
  is already decided, and the cross-model handoff confirmation runs before it.
- **"创建对话失败：知识库设置无效" appeared with no knowledge base in
  existence.** `DEFAULT_PARAMS.seed = null` was submitted verbatim, and
  `_normalize_params` rejected `None` for every numeric key with
  `seed must be a number`; the frontend's broad i18n regex
  `/must be a number|must be a string|.../` then translated *any* such message
  into a knowledge-base error. Fixed on both sides: `None` now means "unset"
  (matching `system`/`thinking`), the new-chat path strips null draft values,
  and the generic type-error pattern moved to `err.invalidValue`, which keeps
  the backend's original wording so the field name survives.
- **The chat inspector overflowed the content column.** Measured with real
  `getBoundingClientRect`: at a 900 px window with the panel dragged to 720 px
  the inspector's right edge reached **1052 px** while the container ended at
  900 px — the overflow was clipped, which reads as "the panel is covering the
  conversation". `.chat-inspector` had `flex: 0 0 auto`, so it could never
  shrink. It is now `flex: 0 1 auto` with `max-width: 100%`, and
  `.chat-column` keeps a 320 px readable minimum so the panel yields instead of
  the text being crushed.
- **The application could not be quit.** With `close_to_tray` enabled the window
  close button only hides the window, and the only real quit entry was inside
  the Ctrl+K command palette. There is now a **visible** `Quit application`
  button in Settings → Backups → Application lifecycle, and the first
  hide-to-tray writes an explicit hint to the boot log and sets the tray
  tooltip.

### Added (verification)

- `frontend/tests/browser/layout.test.mjs` — real geometry at six widths,
  including a panel dragged far beyond the window, asserting the inspector never
  leaves its container and the conversation column is never crushed.
- `frontend/tests/browser/user-paths.test.mjs` — three real user paths against a
  running backend: conversation creation with an unset parameter (through the
  real HTTP contract, not a button that silently no-ops without a model), a
  visible quit control with non-zero size, and no inspector overlap at four
  widths. Console errors and any 4xx/5xx fail the run.
- `AGENTS.md` §6 gained two rules with this incident as the worked example:
  "green tests do not mean the product works — doubles cannot prove usability",
  and "a storage failure must never be disguised as a network failure".



### Security

- **`GET /api/system/config` returned the local API gateway key in plaintext.**
  `api_server_admin.py` already redacted it and `settings.js` renders the field
  as `value=""` with the comment "the backend does not return it either", but
  this endpoint returned `get_config()` verbatim, so the same secret reached
  WebView JS memory. It now returns `""` for the key. No extra key is added to
  the response: the settings page renders from `DEFAULT_CONFIG`'s shape and a
  browser test asserts every config key has an editable field. "Is it set?"
  stays answered by `GET /api/api-server/status`.
- **`grep_search` / `file_glob_search` escaped the authorized tool root through
  a Windows junction.** `os.walk(followlinks=False)` does not stop junctions and
  `is_symlink()` returns `False` for them, so `read_file` (which goes through
  `resolve_inside`) was protected while the two search tools were not: a
  junction pointing outside the root made an outside secret readable. The walk
  now prunes reparse points (`FILE_ATTRIBUTE_REPARSE_POINT`) and re-checks every
  yielded path with `relative_to`.
- **Gateway auth turned non-ASCII credentials into 500s.**
  `hmac.compare_digest` raises `TypeError` for `str` containing non-ASCII
  characters instead of returning `False`, and `api_server_key` accepts
  non-ASCII values — so a non-ASCII key locked the user out of their own gateway
  with a 500 on every request. Comparison now runs on UTF-8 bytes.
- **Bundled binaries carried builder identity.** `RUSTFLAGS` remaps cargo
  registry paths (which embed the build machine's user name) to a neutral
  prefix; launcher, backend sidecar and installer are byte-scanned for user
  names, user-profile paths and workspace names (ASCII and UTF-16LE) before
  anything is published. `release.yml` applies the remap.
- **The runtime packager was shipping source, tests and the developer's real
  user data.** `tools/package_portable.py` called
  `shutil.copytree(backend.parent, ...)`, which copied the entire `backend/`
  source directory into the distributed zip: 102 test sources (including
  hard-coded test tokens), 13 files of the developer's own `chats.db` /
  `history.db` / `config.json` / 7 personal presets, 41 files of internal
  AI-tool state, a second nested copy of the backend build, plus build scratch
  and runtime logs — 136.7 MiB, most of it content that must never leave the
  machine. Packaging is now a whitelist (launcher, `backend.exe`,
  `backend/_internal/**`, `LICENSE`, `README.md`, `使用说明.txt`) and the
  finished archive is read back and inspected; a leak is exit 5 and the zip is
  deleted.
- **A `config.json` that is valid JSON but not valid UTF-8** (saved from Notepad
  as ANSI/GBK with a Chinese model path) no longer bricks the backend.
  `UnicodeDecodeError` is neither `json.JSONDecodeError` nor `OSError`, so it
  escaped the quarantine path entirely.
- Non-loopback management listeners are refused; API keys are scrubbed from
  preset/history/log/export surfaces by content, not by parameter name.

### Added

**Chat**

- Native Chat as the default mode with local `chats.db` conversations, a
  backend SSE proxy (API keys never reach the webview), Markdown/code copy,
  stop/regenerate, token and speed stats, Markdown export, and a sandboxed
  WebUI fallback.
- User-message editing (truncate and regenerate), conversation branching, a
  context-usage meter tied to the instance `ctx_size`, and a prompt library
  (built-ins plus editable local prompts in `chats.db`).
- Local text attachments via paperclip, file picker or drag/drop, read in the
  webview only, with text-extension / NUL / UTF-8 and per-file, per-message,
  per-conversation bounds; attachments persist and copy into branches.
- Local conversation search across titles, messages, attachment names and
  attachment content, with snippets, match-type labels, highlighting, keyboard
  navigation and focus preservation.
- Reasoning trace as a first-class `messages.reasoning` column with its own
  bound, expanding while the model thinks and collapsing when the answer starts
  (`auto` / always expanded / always collapsed). Manual open/close is recorded
  per message and overrides the preference. Copy offers `Copy` and
  `Copy with reasoning`; Markdown export writes the trace under its own heading.
- Image attachments with a vision preflight: the button is disabled unless the
  instance was started with `--mmproj`, because that is the only case where
  `llama-server` reports `modalities.vision`.

**Knowledge base / RAG**

- Local knowledge bases indexing text/code, HTML, PDF, DOCX and scanned PDFs
  into SQLite with chunk vectors plus FTS. GLM-OCR runs through `llama-server`,
  Qwen embeddings index and retrieve, and an optional Qwen reranker rescues the
  fused candidate list.
- Chat knowledge mode: pick a base in the toolbar, retrieve only for the current
  question, inject numbered excerpts server-side, and persist citations in
  assistant message stats so reload and Markdown export keep their sources.
- Knowledge management UI: create and edit bases, add files/folders/paths, live
  indexing progress, document preview, cancellation, retrieval testing, manual
  model unload, and localized failures.
- Indexing is staged: OCR runs and unloads before embedding starts, so an 8 GB
  GPU never holds vision, projector and embedding models at once. Page and
  whole-document extraction results are cached by content hash.
- Per-document cap in fusion (`MAX_PER_DOCUMENT = 2`) so one 161-chunk
  regulation cannot fill both recall arms; this lifted hit@3 66% → 76% and
  hit@5 76% → 80% without reranking, at no hit@1 cost.
- Article labels on chunks: chunk boundaries stay windowed (splitting on 第X条
  measured *worse*: hybrid hit@1 52% → 35%) but now carry the chapter or article
  they mostly contain, so citations read 《个人信息保护法》第3页 · 第十七条.
- OCR provenance is disclosed: documents indexed from scans show an `OCR` badge
  in the base and on every citation, with a tooltip that the wording is
  best-effort.
- Retrieval quality was measured on 21 labelled questions plus 10 deliberately
  unanswerable ones, against a real 377-chunk legal index, ablating dense /
  hybrid / per-document cap / rerank. Shipped path: **hit@1 76%, hit@3 95%,
  MRR 0.841**. The evaluation harness needs a local legal index and embedding and
  rerank models, so it is not part of the published tree; the method and the
  per-configuration numbers are in [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md).

**Runtime and models**

- Multi-backend support for the official llama.cpp Windows packages
  (CPU / Vulkan / OpenVINO / ROCm). All four ship the same version string and
  the same `--help` surface, so the backend is identified from the sibling
  accelerator DLL (`ggml-openvino.dll`, `ggml-hip.dll`, `ggml-vulkan.dll`) and —
  the authoritative signal — from the build's own `--list-devices`.
- OpenVINO ignores `--device` (measured: passing it exits), so the device is
  translated into `GGML_OPENVINO_DEVICE` (`OPENVINO1` → `GPU.0`, taken from the
  `GGML_OPENVINO_DEVICE=` prefix in `--list-devices`, `(selected)` stripped).
  An untranslatable or multi-device selection is refused rather than guessed.
- ROCm silently falls back to CPU when no supported AMD discrete GPU is present
  (measured ~6.8 tok/s, an order of magnitude slower than Vulkan); a ready
  instance now carries a `warnings` entry rather than an `error_message`.
- Model library with GGUF capability probing (native context, MTP, mmproj),
  VRAM estimation, companion-file detection, and an optional `model.yaml` /
  `manifest.json` editor with validation preview, atomic save/delete and
  backups under `DATA_DIR/manifest-backups`.
- "Use case" one-click recipes (chat / embeddings / rerank / read-only tools /
  full agent) that write intent, not tuning numbers.
- `model_key` / `variant_key` identity so history and instance grouping survive
  a file move, and chat conversations rebind by model identity rather than only
  by instance id.

**Application**

- Desktop shell rebuilt around a chat-first single-column layout: navigation,
  the current page's list and the footer actions all live in one sidebar
  column; content fills the rest. A header hamburger appears only when the
  column is hidden.
- Current-model capsule in the title bar with a one-click stop button; the
  empty state stays a neutral invitation rather than a colored button implying a
  loaded model.
- Chat inspector as a two-view panel (`Configuration` / `Integrations`).
  Integrations shows agent and tool status, the session's tool-root
  authorization (with revoke), resident embedding/rerank/OCR services, and the
  local API gateway.
- Settings with `Hardware` and `Developer` categories (read-only), backed by
  `GET /api/system/overview`, `GET /api/system/paths` and `GET /api/runtimes`.
- Local API gateway: a **separate** OpenAI-compatible FastAPI app on its own
  port with its own Bearer key, `/v1/models` and `/v1/chat/completions`, per-
  request logging, and origin allow-listing. External clients cannot hold the
  launcher session token, so the gateway must be a second app rather than a
  route on the launcher.
- In-app backup/restore for config, presets, history and chats (including
  attachments and prompts): SQLite snapshots, SHA-256 manifests, a staged
  restore applied before DB init, a `pre-restore-*` backup, and path/hash
  validation.
- Tauri tray icon with Show/Quit, opt-in `close_to_tray` (default off), a
  runtime toggle, explicit quit, and EnumWindows restore for a hidden
  second-instance launch.
- Ctrl+K command palette for pages, new chat, model picker, parameter editor,
  theme and quit.
- First-run dashboard: three working actions (选择模型并启动 / 浏览模型库 /
  打开知识库) plus a live state card, replacing a lone "暂无实例" icon.
- Knowledge services are labelled as such on the dashboard, so a
  system-started embedding server no longer looks like a model the user launched.

**Safety and observability**

- Windows Job Object manages the backend and `llama-server` subtrees, so the
  launcher exiting or crashing reclaims them.
- Bounded logs, history, scans, request bodies and frontend DOM; log buffers
  bounded by both line count and a byte budget.
- CPU, memory and NVIDIA GPU monitoring (NVML first, `nvidia-smi` fallback,
  disabled rather than executed by bare name when unavailable).
- Global backend reconnect/offline banner.
- `GET /api/knowledge/services/status` for resident embedding/rerank/OCR
  services, derived from the `[KB] ` managed instances (no second state store).
- `GET /api/system/paths` for data/log/config paths, app version, Python and
  platform; `app_version` comes from `LUNARSHROUD_APP_VERSION`, injected by the
  shell from `CARGO_PKG_VERSION`.

**Tests and gates**

- Browser suites: a core-path suite (navigation, axe accessibility, first-paint
  budget), a deep suite that clicks every previously unverified path (model
  detail, view toggle, quick-launch preflight, chunk preview, cancellation,
  settings persistence per category, English locale, dark theme, 800px layout)
  and sweeps every non-destructive control on tabbed pages for dead actions,
  and an adversarial suite that forces failures (missing path, absent or broken
  model, embedding service killed mid-index, rerank misconfiguration) and
  requires the UI to admit each.
- `backend/tests/test_web_root_hygiene.py` pins the web-root allowlist.
- `backend/tests/test_version_consistency.py` keeps the version, the README
  statement and the CHANGELOG entry from drifting apart.

### Changed

- **The layout is one sidebar plus content.** The second list column
  (`#context-panel`) and its resizer are gone; collapsing hides the whole
  column rather than shrinking to an icon rail.
- **Avatars are images only.** The emoji/initial text box is removed; unset
  avatars render a built-in SVG default. Legacy short-text values are still
  accepted by the backend so an old `config.json` does not 400 on the next save.
- **Model library is a detail pane plus the sidebar list.** The card grid and
  table view that duplicated the list are deleted; the runtime page gained the
  list it never had.
- Design token layer rewritten for a macOS-native feel: system color palette,
  Apple-then-Segoe font stack, radius scale collapsed from 9 near-identical
  values to 5, hue-tinted oklch shadows (`docs/DESIGN_1.0.md`).
- Navigation consolidated: model library / runtimes / presets live in a **Model
  workspace**, logs / monitor / history in **Observability**, and knowledge
  bases have their own top-level page.
- Page titles on pages that already have a list column are visually hidden
  (still present for screen readers) instead of duplicated.
- Sidebar and panel widths are editable numerically in Settings; both keys
  previously had no UI at all.
- Sample parameters `top_k`, `repeat_penalty` and `seed` are now actually sent.
  The backend accepted and forwarded them, but the frontend never transmitted
  them.
- Chat now binds conversations by **model identity** (`model_key`); a restart of
  the same model reconnects silently, a cross-model handoff asks first.
- Drag-resizable chat inspector; `chat_panel_width` previously had no runtime
  consumer at all.
- Chat length is admitted against the model's real context window in **tokens**
  (`--tokenize` on the instance), trimming oldest non-system turns until the
  prompt fits, instead of two limits that measured different units. The 2 MiB
  transport guard stays for every other endpoint; only the chat route is widened
  to 8 MiB so the token policy is what decides. Dropped turns are reported in
  `X-LunarShroud-Dropped-Turns` and shown in the UI.
- `config.llama_server_path` and the runtime registry were two independent
  answers to "is a build configured", and three UI sites looked only at the
  first. A user with a registered build could launch normally yet Settings
  refused to save *any* change, the dashboard kept showing a "get llama-server"
  banner and the Getting-started checklist reported an error. Startup now
  adopts a legacy bare path into the registry, a chosen default mirrors back
  into the legacy field, and all three sites consult one shared predicate.
- The JS package (`package.json`, lockfile, `node_modules`) lives at the
  repository root. `frontend/` is Tauri's `frontendDist`, and the test
  dependencies made `cargo tauri build` refuse with *"The configured
  frontendDist includes the `["node_modules"]` folder"* — any scratch output
  under `frontend/tmp/` would also have been embedded into the shipped binary.
- `tools/Publish-PortableRuntime.ps1` signs when a certificate is present and
  warns explicitly when none is; it signs the **source** artifact before staging
  because the script verifies "staged copy == source" by SHA-256, and it
  regenerates the installer directory's `SHA256SUMS.txt` after signing so
  signing cannot recreate a stale-checksum defect.
- Dependencies are hash-locked (`backend/requirements*.lock`); CI and
  `setup.ps1` install with `--require-hashes`.
- `pyyaml` is a declared runtime dependency. It was only in the lock file, so
  `pip install -r requirements.txt` silently disabled `model.yaml` support.

### Removed

- The 68 px icon rail. Icons without labels in a narrow strip were the ugliest
  state in the app; the header hamburger is the only way back.
- The "show/hide list" toggle, deleted along with the column it controlled.
- The card grid and table view that duplicated the model sidebar list.
- The emoji/initial avatar text box.
- The old chat settings modal; its fields live in the inspector's Configuration
  view.

### Fixed

**Silent correctness failures**

- **Chat could silently hand a conversation to a different model.** When the
  bound instance disappeared (stop, or restart with a new id) the page fell back
  to the first running instance with no notice, and the next send rewrote the
  conversation's `model` field — model A's history answered by model B.
- **Hybrid retrieval was dense-only.** `search_fts` quoted the whole question as
  one FTS5 phrase, which requires a verbatim substring and matched nothing on
  natural-language questions. Measured on a 377-chunk legal index: the fused
  ranking was identical to dense on 17 of 17 questions. Queries are now split
  into clauses (including CJK 3-grams) so the lexical arm contributes — hit@1
  35% → 52%, MRR 0.432 → 0.634, and 76% / 0.843 with reranking.
- **A knowledge service could become permanently unreapable.** An index run took
  the embedding service with a bare `acquire()` that nobody released, and the
  idle reaper only started inside `lease()`; separately `_reuse_running()`
  returned `owned=False` and `release()` skips non-owned handles forever.
  Measured: an embedding service stayed resident **22+ minutes and ~2 GB of
  VRAM** past the documented 10-minute unload. The OCR path had the same bare
  `acquire()` and would be reaped mid-job on any OCR run longer than 600 s.
- **Most `core.*` module logs never reached `backend.log`.** Handlers were
  attached to `lunarshroud.backend` with `propagate=False`, but `core/*` uses
  `getLogger(__name__)` → `core.chat_store`, which is not a descendant. Index
  failures, chat-db corruption, backup and preset errors were silently dropped.
- **`GET /v1/models` and gateway model routing were broken.** The gateway read
  `inst.model_path`, which `Instance` does not have. Every lookup returned `""`
  — empty model ids, 404 on any name hint, and with several instances it could
  pick the `[KB] Embedding` service as the default chat model.
- **Embedding instances appeared in the chat instance dropdown.** The frontend
  filtered on `item.params?.embeddings`, but `to_dict()` never returns `params`,
  so the test was always false and only the `[KB] ` prefix case was caught.
- **Client-side read-only tools were dead code.** `activeToolDeclarations()`
  filtered the tool list by an array that is empty on that branch, so it always
  returned `[]`; the approval UI, `POST /api/chat/tools/execute` and the backend
  executor were never reachable, and the tool-folder button was hidden so the
  prerequisite could not be created either.
- **`/api/system/capabilities` returned 400 when the default runtime was
  broken.** A registered toolchain whose probe had failed made
  `_runtime_for_launch(None)` raise, turning a read-only capability query into
  an error and making the getting-started page blame the current build.
- **The model-card 快速启动 button never fired.** Its click was swallowed by the
  card's own `[data-open-model]` delegate. The more specific action is matched
  first now.
- **The 卡片/列表 toggle was dead, and "list" would have blanked the library.**
  `workspace.js` hoists a child module's `.page-actions` into the shared
  toolbar, which moves those nodes outside the module's subtree, so the
  module's root-delegated listener never saw the click and
  `root.querySelectorAll` never found the buttons to sync pressed state. List
  view had also never been implemented: selecting it only hid the grid.
- **A knowledge base could not be cancelled from the UI.** `/cancel` was wired
  end to end in the backend and `api.js`, but no control ever called it.
- **`.grid-full` never applied.** The rule was written as a class selector while
  every call site passes `grid-full` as an attribute.
- **A named-but-missing source path was dropped silently.** `collect_paths`
  skipped any path that did not exist, so a typo produced "added" while the
  document never entered the index.
- **Saving any unrelated setting restarted the local API gateway.** The draft is
  the whole config, so `api_server_enabled` was always present in the update
  payload; the restart pushed the "saved" toast out by several seconds.
- **`PATCH /api/chats/{id}` returned the whole transcript.** Parameters save on a
  600 ms debounce, so every keystroke re-sent every message and every base64
  attachment (measured 187–500 KB per call).

**Data loss and budget violations**

- **The tail of any long reasoning trace was destroyed at save time.** It had to
  be truncated to 4000 characters because the whole stats blob is capped at
  16 KiB, so users saw the full trace while streaming and a shortened one after
  reload, with no indication.
- **Tool results could not be persisted and tool parameters blew the stats
  budget.** `chat_tools.MAX_READ_BYTES` (256 KiB) exceeded
  `chat_store.MAX_CONTENT_CHARS` (200,000), and `CHAT_MAX_TOOL_ARGS_CHARS`
  (64 KiB) exceeded `MAX_STATS_BYTES` (16 KiB) — a large `read_file` result or a
  large tool call made the whole assistant message fail to save. A large
  `grep_search` result hit the same wall: 100 × 2000 characters is exactly the
  content cap before the frontend's `path:line:` prefixes (measured 201,089).
- **Image attachments were capped far below what the backend accepts.** Only
  `/chat/completions` was exempted from the 2 MiB transport limit; attachments go
  through `/api/chats/*/messages`, so the effective ceiling was ~1.4 MiB base64
  (~1 MiB of image) versus the 4 MiB the frontend advertises.
- **Both image attachment caps were dead code.** The conversation-level check
  summed text and images against the 2 M **character text** budget, so the first
  4 MiB image was refused with a message about "attachments" and
  `MAX_IMAGE_ATTACHMENT_CHARS` / `MAX_MESSAGE_IMAGE_ATTACHMENT_CHARS` could never
  fire.
- **Tool messages could not survive export, import or branching.**
  `import_conversation` accepted only system/user/assistant although
  `export_conversation` emits `role="tool"`, and `branch_conversation` dropped
  `tool_call_id`. An existing test used `role="tool"` as its invalid-role case,
  pinning the defect as correct behaviour.
- **A non-finite number in `stats` bricked a conversation permanently.**
  `json.dumps` defaults to `allow_nan=True` while `JSONResponse.render` uses
  `allow_nan=False`, so one `{"pps": 1e999}` made every later GET/PATCH/export/
  branch return 500, with no UI route to delete the poisoned message.
- **A source file grown in place bypassed the knowledge size quota.** The budget
  used the stored size for known files and counted only new files, so a 100 MiB
  document replaced by a 900 MiB one was accepted under a 200 MiB cap.
- **Cancelling an index left the in-flight document stuck at `indexing`.**
  `_stage_document` upserts the row before extracting, then raises
  `IndexCancelled`; the row was never in `staged`, so it was skipped, and after a
  restart `mark_interrupted()` relabelled it with a false cause.
- **Multimodal token pre-check sent the entire base64 image to `/tokenize`.**
  A 4 MB image produced a 5.6 MB request taking 2.65 s and returning ~2.8M
  tokens, on every turn.

**Concurrency, resources and lifecycle**

- **The monitor thread could die mid-timeout and leave an orphan model.**
  `server_reported_listening` iterated the live log `deque` while the reader
  thread appended → `RuntimeError: deque mutated during iteration`, which
  escaped `_monitor_instance` and skipped `_terminate_process_tree`,
  `_release_port` and `_finalize_process_resources`: a live `llama-server`, a
  FAILED row exempt from pruning, and a permanently reserved port.
- **The per-instance log byte budget was not atomic.** Three threads append to
  the same instance; the unlocked read-modify-write made the byte counter drift
  by an order of magnitude (measured 357,650 counted vs 150,300 actual).
- **The packaged backend hung at "Waiting for application startup".** The first
  version of the logging fix iterated `logging.Logger.manager.loggerDict` and
  called `addHandler` per child, deadlocking against the logging module's
  internal lock. Only reproduced in the PyInstaller build.
- **The packaged backend died on `import config` when no temp directory was
  usable.** `tempfile.gettempdir()` raises `FileNotFoundError` when every
  candidate is unwritable, and `_frozen_data_dir()` calls it at module import
  time — so the process died before logging existed.

**Startup resilience**

- **One invalid value in `config.json` could brick the startup.** The
  `validate_config(repair=True)` contract is "never raise, replace a damaged
  value with its default", but most of the chat/appearance block raised
  unconditionally — and `main.py` calls `get_config()` at *import* time, so the
  packaged backend exited 3 before uvicorn and before any log line.
  `{"chat_background_opacity": 3.0}`, `{"chat_assistant_name": null}` and
  unhashable values like `{"theme": []}` (plain `value in {...}` → `TypeError`)
  all did it.
- **`chat_sidebar_width` / `chat_panel_width` ranges changed without a migration
  path.** Raising the minimum from 160 to 200 made a `config.json` holding `160`
  fail validation even under `repair=True`. Loading now clamps; runtime updates
  still reject.

**Input validation — well-formed JSON that produced 500 instead of 400**

- Non-string `model` in the manifest body routes (`Path(dict)` → `TypeError`).
- `max_tokens: 1e999` in the chat path and `10**400` in chat params
  (`int(inf)` / `math.isfinite` → `OverflowError`).
- `chunk_chars: Infinity` and `min_score: NaN` in knowledge routes; `NaN` was
  also silently written to SQLite where it poisoned every later retrieval's
  score floor.
- `overwrite: "false"` was string-truthy, so a client that serialises booleans
  as strings — and asked *not* to overwrite — silently replaced the manifest.

**Layout and visual**

- **The header's right-side buttons overlapped by 10 px on wide windows.**
  `.btn-icon` pinned every button to 30 px while `.btn-labelled` added text plus
  8 px padding at ≥1101 px; the 44 px of content overflowed a 14 px box and bled
  both ways. Measured: `#btn-theme` and `#btn-about` glyphs overlapped and the
  About label ran 7 px past the header's right edge.
- **Chat page left a 44 px empty band at the bottom.** The height formula
  subtracted the conversation header's own 44 px a second time.
- **Chat background images never appeared.** Not CSP, not saving:
  `.chat-native` painted an opaque `--color-surface` inside the `z-index: 1`
  shell, covering the `z-index: 0` background layer.
- **Chat overflowed the viewport at 800px.** `.chat-page-v2` used hardcoded
  `-20px -24px` margins, but main padding drops to 14px on narrow layouts, so
  the page measured 820px in an 800px window.
- **Inline `width` does not resize a flex child.** `.chat-inspector` used
  `flex: 0 0 var(--panel-width)`, which pins `flex-basis`; the CSS now uses
  longhand and the resizer writes both properties.
- **The add-paths dialog closed on failure,** losing both the error context and
  the text the user had typed.
- Accessibility: the resizable sidebars were `role="separator"` without
  `aria-valuenow/min/max` and lived outside any landmark; active sidebar text
  now meets 4.5:1 against its background.

**Release and build**

- **`tools/package_portable.py` read the wrong version source** and could emit a
  zip whose name contradicted its contents; version now comes from
  `tauri.conf.json` and is cross-checked against `Cargo.toml`.
- **`release.yml` built only the NSIS installer** while the docs promise the
  portable zip as the main channel — that zip was never produced by CI. The
  workflow now stages the runtime with `Publish-PortableRuntime.ps1`, packages
  it, verifies the zip actually contains `backend/_internal/`, and publishes
  checksums for both artifacts.
- **The portable zip was not byte-reproducible** before fixed entry timestamps,
  and the launcher needed `-C link-arg=/Brepro`: without it the COFF
  TimeDateStamp tracked the wall clock (measured `1793601298` vs `968523806`)
  even with `SOURCE_DATE_EPOCH` set. The earlier claim that
  `SOURCE_DATE_EPOCH` alone made the launcher reproducible was wrong.
- **`cargo tauri build` could no longer build the shell** once Node test
  dependencies lived under `frontend/`.

### Verification

Run at this release, on Windows 10/11 x64:

```text
backend    python -m pytest backend/tests -q        903 passed, 1 skipped
coverage   pytest --cov=backend --cov-fail-under=75  TOTAL 84.92%
frontend   node --test frontend/tests/*.test.mjs    117 passed
syntax     node --check on every js/*.js,*.mjs       all pass
lint       python -m ruff check backend tools       All checks passed
browser    core-paths (nav/a11y/FCP)                3 passed
browser    deep-paths (full)                        9 passed / 3 skipped
browser    adversarial                              2 passed / 5 skipped
mirror     tools/check_release_mirror.py            CLEAN, 269 files
rust       cargo test                               23 passed, 0 failed
```

The Rust suite needs `CARGO_TARGET_DIR` pointed at a short path outside the
repository; under `src-tauri/target` the `ring` C compile fails with an empty
stderr. The root cause is not narrowed to a line, so treat that setting as a
workaround rather than an explanation.

Release artifacts (portable channel only):

```text
LunarShroud-1.0.0-portable-x64.zip     40,898,399 bytes   6ce47552…
launcher LunarShroud.exe                                064f6895…
backend  backend.exe                                    7a03e8e4…
```

The zip embeds exactly the launcher/backend pair above (digests verified by
reading the archive back). It was extracted and launched: healthy start, `401`
without a token, gateway key returned empty, `GET /api/system/storage` reporting
all three databases writable, `POST /api/chats` accepting a `seed: null`
parameter, and error responses carrying CORS headers. No residual process or
listener after exit.

`LunarShroud_1.0.0_x64-setup.exe` was built and byte-scanned for builder
identity, but it is **not published**: this version has not been through an
install → start → uninstall round trip, and shipping an unvalidated installer
would be worse than shipping one channel.

### Not verified at this release

- 8-hour soak and the multi-hardware matrix. A 4-hour real-model soak passed
  (7,186/7,186 requests, 0 failures, p99 0.281 s, stable RSS).
- Code signing. SmartScreen will warn on first run.
- Installer install / start / close / uninstall round trip — which is why the
  installer is not published for this version.
- Build-provenance attestation.

## [0.9.0-beta.2] - 2026-10-17

### Added

- llama.cpp v0.7.0 alignment: use-case recipes (`/api/params/recipes` plus the
  recipe row in the parameter editor), nine capability-driven parameters
  (pooling, embedding normalization, prompt caching, router-mode directory and
  limit, thinking mode, reasoning format, chat-template kwargs, RPC),
  argument-position allowed-value parsing (`{a,b,c}` / `[a|b|c]`),
  build-aware select validation, the annotated numeric-default fix
  (`--moe-cache-mib` no longer parses as `0,disabled`), and crash translation for
  `GGML_ASSERT` lines.
- AI tools and MCP parameter group.

### Changed

- Backend is PyInstaller **onedir** rather than onefile. Onefile extracts
  ~20 MB to `%TEMP%` on every start; when the temp directory is redirected,
  read-only or scanned, the bootloader prints only "Could not create temporary
  directory!" and hangs, and a hard kill leaves extraction directories behind
  (measured: 167 directories, 2.2 GB). Onedir never touches temp.

### Fixed

- Probe-hang / thread-starvation (H1), restart launch-history (M1),
  instance-log rotation and cleanup (L1), BOM config (L2).
- Corrupt `history.db` and `config.json` are quarantined and rebuilt instead of
  blocking startup.

### Security

- Readiness probe authenticates with an HMAC challenge-response over a nonce
  rather than echoing `session_id`, which any local process that wins the port
  could replay.
- Each backend generation rotates both `session_id` and token; `get_backend_session`
  returns empty until the new generation passes the HMAC check.
- Single-instance mutex plus `ShowWindow`/`SetForegroundWindow` restore.
- WebView2 data directory is placed beside the executable instead of relying on
  the Tauri default under `%LOCALAPPDATA%`, which fails under a sandboxed or
  restricted token.

## [0.9.0-beta.1] - 2026-08-16

First packaged candidate. Launcher for `llama-server.exe` with parameter
editing, presets, multi-instance start/stop, bounded live logs, CPU/memory/GPU
monitoring, launch history and a tray lifecycle. Unsigned; beta because
real-model and hardware soak coverage was incomplete.

Known limitation carried forward: a `--cache-ram` default of 8192 enables
host-side prompt caching, and a 4B model was measured growing from 5.2 GB to
11.4 GB RSS over 10 minutes before plateauing. This is llama.cpp behaviour, not
a launcher leak.
