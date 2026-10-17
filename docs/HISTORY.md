# Project history

This concise record separates current verified facts from historical material.

Paths under `docs/archive/` and `tmp/` appear below because moving material
there *is* part of the history. They are maintainer-local and deliberately
absent from this repository, so treat every such mention as a record of what was
done, not as a location you can open. Nothing in the published tree depends on
them.

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
  the sidecar and restarts it with the same session token (closing the crashed
  generation's Job Object first, reclaiming its llama-server children); REST
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
