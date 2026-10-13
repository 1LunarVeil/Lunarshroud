# Current status

Last reviewed: 2026-09-25 (Asia/Shanghai)  
Current candidate: `0.9.0-beta.1`  
Release posture: Windows beta; do **not** describe as 1.0 or stable.

## Working tree has moved past this candidate

Everything else in this document describes the **frozen `0.9.0-beta.1`
candidate**. Newer locally deployed launcher/backend pairs were rebuilt and
hash-verified on 2026-09-18, 2026-09-19, 2026-09-23, 2026-09-24 and again on
2026-09-25; the pair currently deployed in this workspace is fingerprinted
under *Verified
state* below. The source tree has carried the full P0–P5 evolution (parameter
correctness and adaptivity, model capabilities, UX, release hygiene), so the
candidate-era counts in the rest of this document no longer describe the
working tree.

Current working-tree checks (2026-09-25):

```text
backend    py -m pytest -q                      352 passed   (candidate: 38)
frontend   node --test frontend/tests/*.mjs      17 passed   (candidate: 6)
lint       py -m ruff check backend              clean        (new)
css        tools/css_cascade_probe.js            NO_DUPLICATES (see note below)
mirror     tools/check_release_mirror.py         137 files, clean (candidate: 86)
rust       cargo fmt/clippy/test                 clean, 7 passed
```

The `css` row is not reproducible from a clean checkout on its own: the cascade
probe runs inside a page served by the backend, so it needs a running,
authenticated launcher (`tools/ui_preview.py --probe @tools/css_cascade_probe.js`).
Without a session token the backend answers 401, the stylesheet never loads, and
the probe reports `NO_SHEET` rather than a selector count. Treat `NO_SHEET` as
"the check did not run", not as a pass. The unique-selector count grows whenever
CSS is added, so read the first line of the probe output rather than memorizing a
number.

Modules added since the candidate: `core/gguf_probe.py`,
`core/capabilities.py`, `core/server_probe.py`, `core/vram_estimate.py`,
`core/formatting.py`; `frontend/js/i18n.mjs` with `locales/`, `models.js`,
`panel_views.mjs`, `model_picker.mjs`; `frontend/css/tokens.css`.

Endpoints added: `GET /api/system/capabilities`,
`GET /api/models/vram-estimate`, `GET /api/models/scan?with_capabilities=`.

Release hygiene added: `tools/check_release_mirror.py` gates the public mirror
(`github/`) against drift, forbidden paths and local-path leakage, and
`.gitignore` now covers `tmp/`, `.claude/` and `*.bak*`.

This section is informational only; test counts grow over time and the gate is
zero failures. The release blockers below are unchanged. [CHANGELOG.md](../CHANGELOG.md)
carries the user-visible change record and [HISTORY.md](HISTORY.md) the dated
incident timeline; between them they are the published record of the evolution.

## Verified state

### Currently deployed pair (2026-09-25)

Root `LunarShroud.exe` with the adjacent `backend\backend.exe`, deployed by
`tools/Publish-PortableRuntime.ps1` and confirmed byte-identical to
`src-tauri/target/release/lunarshroud.exe`:

```text
launcher  b5dbba8b231ca3e800b8fb5163b83ed7daf25a0a47d2c4263b1ae9d8b6da38ea
backend   5a71d9bc7735a375258e45c10a030ebb1b49e9b0e2bc54fc126f8284cf776454
installer 5c5d16b635d90eb7d9846c5ce699c79f07318e72eb3368421049b366413e716a
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.1_x64-setup.exe)
```

Both 2026-09-24 and 2026-09-25 rebuilds were triggered by icon work (transparent
rounded corners, then the `ICON_BIG` taskbar fix), so the backend hash is
carried forward unchanged: `backend.spec` sets `icon=None` and the sidecar
embeds no icon, therefore no backend rebuild was warranted. The launcher was
built with the `RUSTFLAGS` path remap and byte-scanned: 317 neutral
`cargo-registry` panic-location prefixes, and zero hits for the builder user
name, `C:\Users`, the workspace directory, or the machine name. The installer
scans clean entirely.

Verified at the symptom level, not just in the binary: with the deployed
launcher running, the Windows 11 taskbar button was captured by screen region
and now renders the rounded application icon (mean luminance 231.9 → 128.7,
dark pixels 0% → 27.1%) instead of the previous generic blank-page glyph.

These three values go stale on **every** rebuild. Any change to this section
must be produced by re-running the commands in [RELEASE.md](RELEASE.md) steps
6–8 (build, byte-scan for builder identity, recompute checksums) and regenerating
`SHA256SUMS.txt` beside the installer — not by copying the previous block
forward. Do not record a fingerprint for a pair that was not deployed and
start/close-verified together.

### Frozen `0.9.0-beta.1` candidate (2026-08-23)

The candidate this document was originally written against:

```text
launcher  4475C4F60DC26FDFCB9450E874168DE2CC50CAE3FCB9F8715961E6B559027925
backend   8479F8A6EEB3E8183811C898F7DD031F785464597556D5E8F96F7C6DAC4B46EF
```

On 2026-08-22 this pair opened a `LunarShroud` window, bound the management
service on port 17520, returned HTTP 401 to an unauthenticated request, exited
with code 0 after window close, and released the port. The Desktop shortcut was
rewritten to target this root launcher. (Pair refreshed twice on 2026-08-23;
see HISTORY.md for the resilience and security changes each rebuild carried.)

Current source checks passed: 38 Python tests with 76.85% product-code
coverage (75% gate), 6 frontend runtime tests, syntax checks for 11 JavaScript
modules, Rust formatting, Clippy with warnings denied except linker messages,
and 7 Rust tests. The public-source scan found 86 reviewed files with no
forbidden files/directories, source-parity mismatch, local-path match, or
secret-pattern match at the time of the check.

On 2026-08-23, frequent terminal flashes were traced to Windows child-process
creation for `nvidia-smi` polling and model-server launches. Both now receive
the no-console creation flag. The rebuilt packaged backend passed all 36 Python
tests (76.67% coverage) and an actual launcher start/close check with no
residual process or listener.

Later on 2026-08-23, backend-crash resilience was added end to end: the Tauri
shell supervises the sidecar and restarts it with the same session token
(reclaiming the crashed generation's llama-server children via its Job
Object); the frontend refreshes the session and retries once on 401/network
failure, reconnects both WebSockets with exponential backoff, de-duplicates
replayed log history, and shows an explicit "backend disconnected" header
state; instance lifecycle transitions are now written to the backend file log,
and launcher supervision events to `launcher.log` beside it.

The rebuilt matched pair (launcher SHA-256 `4475C4F60DC2…`, backend
`8479F8A6EEB3…`) was verified live: healthy start with window, port 17520
listener, and HTTP 401 without a token; force-killing the entire backend
process tree was followed by an automatic restart within ~1s (port re-bound,
auth intact, exit+restart recorded in `launcher.log`); a graceful window close
exited the launcher with no residual process or listener. After the
security-audit fixes, the full 22-probe attack battery was re-run against the
rebuilt packaged backend: 22/22 passed, including the chunked-transfer body
now returning 413.

## Remaining release blockers

- No code-signing certificate; SmartScreen may warn and users cannot
  independently establish installer authenticity.
- No completed 4–8 hour real-workload soak test or representative GPU/model
  hardware matrix.
- Python transitive dependencies are version-pinned but not hash-locked.
- FastAPI test-client/httpx deprecation warning remains for the next dependency
  update; it is not a known packaged-runtime defect.
- `cargo audit` has no vulnerability finding, but reports five unmaintained
  `unic-*` crates inherited through Tauri/urlpattern; track upstream updates.

## Deployment rule

Never copy only one executable into a portable runtime. Deploy the matching
launcher/backend pair with:

```powershell
.\tools\Publish-PortableRuntime.ps1 -Destination . -BackupDirectory .\tmp\portable-backups
```

The script verifies both copied SHA-256 values and then updates the Desktop
shortcut. It is the canonical repair for a stale root EXE or shortcut.

## Deliberate no-touch areas

- `data\` is local runtime and user data in its entirety — `config.json`,
  `history.db` (plus its `-wal`/`-shm` sidecars while the backend runs),
  `logs\`, `presets\`, and any `config.json.corrupt` written when a damaged
  config is rebuilt. None of it is source or a release input. Do not publish,
  delete, or overwrite it during source cleanup.
- `src-tauri\target\` and `backend\dist\` are local build output. The
  beta installer remains in `src-tauri\target\release\bundle\nsis\`.
- `tmp\` is maintainer-local: debug scripts, screenshots, probe output, and
  recoverable archives moved out of the root on 2026-08-23. It is not part of
  the published tree, so nothing in it may be cited as evidence or instruction
  by a document that ships. `.claude\` and `.mimosa\` are likewise local tool
  state and record machine-specific absolute paths.

See [HANDOVER.md](HANDOVER.md) for commands and [HISTORY.md](HISTORY.md) for
the incident timeline.
