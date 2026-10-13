# Changelog

Notable changes follow the Keep a Changelog structure. The project is currently pre-1.0.

## [Unreleased]

### Added

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

### Changed

- Backend error messages are now English (international convention and greppable logs); the frontend localizes the common ones per user language.
- The monitor page subtitle now shows the actual configured sampling interval instead of a hardcoded (incorrect) "2 seconds".
- The backend honors `LUNARSHROUD_LAUNCHER_PORT` over `config.json`'s `launcher_port` (in memory only; invalid values are ignored with a warning), so the launching process can pin the listener port without rewriting the user's config file. The Tauri shell sets that variable from the same persisted config before spawning the sidecar and probes readiness on the same port, so the shell and the backend can no longer read different config locations across dev and packaged layouts.
- `n_gpu_layers` now defaults to `-1`, which leaves `-ngl` out of the command line and lets llama-server choose. The previous default of `99` forced every layer onto the GPU, and llama.cpp treats an explicitly-set value as an instruction it must not override: when VRAM falls short it logs `n_gpu_layers already set by user to 99, abort` and stops adapting, turning an ordinary memory shortage into a hard crash. The new default still tries full offload but spills the overflowing layers to CPU instead. Set a number to pin it.

- The application icon and the in-app logo now have genuine transparent rounded corners (radius 17.2% of the edge; the arc is a plain circle, not a squircle — fitted error 1.84 px vs 8.4 px against the reference). Every shipped asset previously had alpha 255 on all four corners: the artwork *drew* a rounded square but filled outside it with opaque dark navy, so Windows composited a full square tile and the launcher looked boxy on the desktop. `src-tauri/icons/artwork.png` is now the single opaque master and all sizes derive from `tools/gen_icons.py`, which also zooms 1.16× to push the artwork's own dark frame off-canvas — masking alone left a band along the rounded edge that read as a dirty border on light backgrounds. `backend/tests/test_icons.py` rejects any asset that regresses to opaque corners; `Pillow` became a dev-only dependency to make the generator reproducible.

### Removed

- Built-in preset templates: they drifted with every llama.cpp release (speculative decoding names, VRAM tiers), so presets are now entirely user-created and all presets can be deleted.

### Fixed

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
- The Tauri shell now supervises the backend sidecar: an unexpected exit closes that generation's Job Object (reclaiming its llama-server children) and restarts the backend with the same session token, so the UI recovers instead of dying until app restart. Launcher-side lifecycle events are appended to `%APPDATA%/LunarShroud/logs/launcher.log`.
- The frontend now survives backend restarts: REST calls retry once after refreshing the session (401 or network failure), system-metrics and instance-log WebSockets reconnect with exponential backoff and de-duplicate replayed history, and the header shows an explicit "backend disconnected" state instead of silently going stale.
- Instance lifecycle transitions (launch, ready, stop, timeout, abnormal exit) are written to the backend file log for post-mortem diagnosis.

## [0.9.0-beta.1] - 2026-08-16

- First beta candidate prepared for extended Windows validation.
