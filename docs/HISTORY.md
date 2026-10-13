# Project history

This concise record separates current verified facts from historical material.

Paths under `docs/archive/` and `tmp/` appear below because moving material
there *is* part of the history. They are maintainer-local and deliberately
absent from this repository, so treat every such mention as a record of what was
done, not as a location you can open. Nothing in the published tree depends on
them.

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
