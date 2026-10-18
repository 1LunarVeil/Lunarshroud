# Current status

Last reviewed: 2026-10-18 (Asia/Shanghai)  
Current candidate: `0.9.0-beta.2`  
Release posture: Windows beta; do **not** describe as 1.0 or stable.

## Working tree has moved past the frozen candidates

The sections below are the historical record of the frozen `0.9.0-beta.1`
candidate (2026-08-23) and of the incremental redeployments up to 2026-10-17.
The current candidate is **`0.9.0-beta.2`**, rebuilt on 2026-10-17 after the
independent release audit summarized under *Thirteenth round* below; its
fingerprints are in *Currently deployed pair (0.9.0-beta.2)*.

Current working-tree checks (2026-10-18, after the post-takeover hardening):

```text
backend    python -m pytest -q                      533 passed
coverage   --cov=backend --cov-fail-under=75        88.01% (gate 75)
frontend   node --test frontend/tests/*.mjs          21 passed
syntax     node --check (17 files)                   clean
lint       python -m ruff check backend tools        clean
mirror     tools/check_release_mirror.py             CLEAN, 155 files
rust       cargo fmt/clippy/test                     clean, 22 passed
build      PyInstaller onedir, pinned env            backend.exe A1BAACAA…
```

## Post-takeover hardening (2026-10-18, deployed)

A read-only, three-way review of the beta.2 tree (backend lifecycle, Tauri
shell, silent failures/test gaps) re-derived the release state instead of
trusting the previous session's claims. It found no P1, but several P2
defects that could reproduce the reported instability. All were fixed,
tested red-green and rebuilt into the deployed pair:

- **Port reservation ownership.** A stale monitor from a stopped generation
  could release the *new* generation's reservation for the same reused port;
  the next auto-assigned instance could then bind the same port. Reservations
  are now released only when no other starting/running/stopping instance owns
  the key and the process is confirmed dead. A monitor that wakes while the
  stop path is still terminating no longer fabricates `STOPPED` or releases
  the port. Tests: `test_port_reservation_survives_the_old_generations_stale_release`,
  `test_release_port_keeps_reservation_while_the_process_is_alive`.
- **System tools never fall back to bare names.** Python and Rust now resolve
  `taskkill.exe`/`nvidia-smi.exe` from the real system directory
  (`GetSystemDirectoryW` in Rust) and disable the helper when it cannot be
  resolved; `which_on_path` skips relative `PATH` entries. Tests:
  `test_system_tools_are_disabled_without_systemroot`,
  `test_which_on_path_ignores_relative_path_entries`,
  `system_tools_are_resolved_inside_system32`, and the missing-tool Rust test.
- **Capability probes cannot execute CWD files.** `probe_server` resolves
  relative `llama-server` names only against the application directory/PATH;
  an unresolved name returns a failed probe instead of `Popen`-ing a same-named
  file in the current directory. `_warm_server_probe` skips unresolved paths.
  Test: `test_probe_server_never_executes_a_relative_name_from_cwd`.
- **Incomplete `--help` no longer claims unsupported flags.** Missing options
  are called unsupported only when the probe is complete; an unparsed
  `--spec-type` block reports unknown instead of "all values unsupported".
- **Shell/backend data directory alignment.** The launcher reproduces the
  backend's writable-candidate order (including `<launcher>\backend\data`)
  and strips a UTF-8 BOM, so a `launcher_port` saved in a restricted-environment
  fallback directory is honored. Verified end-to-end: with every normal user
  directory pointed at a file, the second start listened on the edited
  `17531`, not the default.
- **Tauri lifecycle/diagnostics.** Single-instance creation failure no longer
  masquerades as "already running"; rapid crash loops back off up to 30 s;
  WebView2 falls back from the exe directory to `%LOCALAPPDATA%`; the
  readiness response is capped at 8 KiB and does not follow redirects;
  shutdown waits briefly for the backend and always logs the exit; the child
  stderr log falls back to the runtime temp directory; launcher log writes are
  serialized.
- **Client/API robustness.** Non-idempotent requests are no longer retried on
  network failure (duplicate instance/preset creation); WS parse/handler
  errors are logged; log-feed drops are surfaced as `logs_dropped`; late
  stdout lines cannot reopen a finalized instance log; an unreadable config is
  never overwritten with defaults; `/api/models/vram-estimate` samples GPUs
  off the event loop; missing static assets return 404 instead of
  `index.html` (with an explicit favicon link, so the browser no longer
  requests a missing `/favicon.ico`); deleting an unknown instance returns
  404; NVML's `NVML_VALUE_NOT_AVAILABLE` sentinel is reported as unknown.
- **Release mirror gate.** Missing `github/` now fails closed (opt-in
  `--allow-missing-mirror` for the public mirror itself); `--apply` runs the
  privacy/forbidden checks *before* writing anything; runtime directories
  created beside the exe (`webview2-data/`, `logs/`, `runtime-temp/`,
  `appdata/`) and local AI session directories (`dsh-session-*`, `CLAUDE.md`)
  are excluded at any depth.
- **Portable deployer.** An incomplete onedir build (no `_internal`) is
  rejected before touching the destination; launcher, backend and runtime are
  swapped together and restored on failure.

Verification for this revision:

```text
pytest        533 passed, coverage 88.01%
frontend      21 passed, 17 JS files syntax-clean
rust          fmt/clippy clean, 22 tests
mirror        CLEAN, 155 files
packaged      tmp/release_smoke.py: 22/22 (isolated APPDATA, dead proxy,
              HMAC readiness, token rotation, Job reaping, clean-shutdown log)
broken TEMP   launcher + backend reach ready with TEMP/TMP pointing at a
              nonexistent path
single inst.  second launch exits 0; exactly one launcher/backend remain
fallback      all normal user-data env vars unusable: backend uses
              <app>\backend\data; edited launcher_port=17531 is honored
real model    deployed launcher + Qwen3.5-4B Q4_K_M: RUNNING, raw
              /completion text, /v1/chat/completions content "OK", stop,
              port release, history stopped, no residue; isolated APPDATA
              and the real user data byte-identical before/after
soak          partial 15-minute real-model run: 402/402 requests, 0 failures,
              min/avg/max 0.203/0.242/0.281 s, llama-server RSS ~3.18 GB
              flat for the whole run; the full 4–8 h soak remains open
```

A follow-up independent verification (R8, 2026-10-18) reproduced the deployed
pair and the gates, and flagged three issues that are now fixed: Rust tests
depending on inherited `%TEMP%` (now hermetic; 22/22 with `TEMP`/`TMP` pointing
at a nonexistent path), an external temp-file residue in `backend/`
(`jy4s_b4h`; removed, with a narrow gate rule for 8-character tempfile-style
residue), and the NVML device-memory sentinel call site (now both `used` and
`total` go through `_nvml_memory_mb`, pinned by a call-site test). See
`RISK_ASSESSMENT.md` for the follow-up section.

Not re-run this round: NSIS install/uninstall (the earlier onedir install
evidence stands), the 4–8 h soak, and packaged screenshots. The browser check
used the DevTools protocol (DOM snapshot + console) because screenshot capture
timed out in this headless session; the dashboard and the parameter-editor
modal rendered with zero console errors and zero 404s.

The fresh-environment row in the older block below is not a re-run in the
developer tree: a copy with no `data/`, `tmp/`, `.venv`, `backend/dist`,
`backend/build` or `src-tauri/target` was bootstrapped by `setup.ps1` under
**Windows PowerShell 5.1** earlier on 2026-10-17. The `0.9.0-beta.2` rebuild was
produced in this workspace with the pinned commands; see *Thirteenth round*.

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
zero failures. The release blockers below were re-reviewed on 2026-10-17.
[CHANGELOG.md](../CHANGELOG.md)
carries the user-visible change record and [HISTORY.md](HISTORY.md) the dated
incident timeline; between them they are the published record of the evolution.

## Thirteenth round: independent release audit (2026-10-17)

Before calling this tree open-source-ready, an independent audit re-derived the
release claims from the source and reproduced the failure modes instead of
trusting `SECURITY_MODEL.md`/this document. It found one still-open P1, one P2
and several P2/P3 defects that the previous rounds had not pinned; all of the
security/data-integrity ones were fixed and given red-green regression tests.

**P1 — the readiness "identity proof" was forgeable.** `/api/system/ready`
accepted any peer that echoed the `session_id` the shell had just sent in the
probe URL. A local process that binds `127.0.0.1:<launcher_port>` before the
backend can therefore answer the very first probe and receive the management
token from the WebView; the token allows `PUT /api/system/config` to repoint
`llama_server_path` → arbitrary code execution as the victim. Verified by
protocol analysis and a PoC replaying the exact client predicate. The probe is
now a challenge-response: the shell picks a fresh nonce and accepts only
`HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)`, which a squatter cannot
compute; the token never appears in the request or response
(`backend/main.py`, `src-tauri/src/lib.rs`).

**P1 — the capability probe handed the token to the user-selected
llama-server.** `server_probe._run` spawned `--help` / `--version` without
`env=`, so a configured `.bat`/third-party build inherited
`LUNARSHROUD_SESSION_TOKEN`. All subprocess paths (probe, `nvidia-smi`,
`taskkill`, instance launch) now use `sanitized_child_env()`.

**P1/P2 — restart token replay.** The supervisor restarted a crashed backend
with the *same* token and the frontend reconnected to the same port; a port
squatter could win the restart gap and capture a still-valid token. Each
generation now gets a new `session_id`+token, `get_backend_session` returns
nothing until the new generation passes the HMAC check, and the packaged
frontend no longer falls back to `127.0.0.1:17520` when no verified session
exists.

**P2 — stop could hang forever.** If `taskkill` was blocked (security software,
group policy, restricted sandbox), the return code was ignored and
`_finalize_process_resources` closed the stdout pipe while the reader thread
was blocked inside it: the stop request and monitor thread deadlocked. Reproduced
in this environment (first test-suite run hung; faulthandler stacks showed both
threads at `stream.close()`). `taskkill` failures now fall back to a psutil tree
kill, the reader owns closing, and finalize keeps a live handle.

**P2 — data-page corruption bypassed the history quarantine.** A valid DB with
one corrupted 4 KiB data page passed `CREATE TABLE`, then killed the lifespan
(`mark_unfinished_as_interrupted`) or made `/api/history` return 500 forever.
`init_db` now runs `PRAGMA quick_check` before caching; a red-green test corrupts
page 2 of a real 40-row DB.

**P2 — external links were dead in the packaged app.** WebView2/wry deny
unhandled `window.open`/`target="_blank"` requests, so the Getting-started
llama.cpp link and Chat's "open in browser" did nothing. The main window now
handles new-window requests and opens only `http`/`https` URLs through
`ShellExecuteW`; other schemes are refused and logged.

**P3 fixes:** failed termination keeps the live handle/port and can be retried;
`OSError` from `Popen` returns 400 instead of a bare 500; huge
`ctx_size`/`parallel` are rejected by bounds; non-ASCII tokens/session values are
compared as bytes (401/404, not 500); `0.0.0.0:P` and `127.0.0.1:P` reservations
conflict; a single oversized log line is truncated; loopback detection requires
a full IPv4 literal; double-escaped toast messages were corrected; the runtime
lock now includes `colorama`.

Verification evidence for this revision:

```text
backend    python -m pytest -q                         508 passed
coverage   --cov=backend --cov-fail-under=75           88.41%
frontend   node frontend/tests/runtime.test.mjs         13 passed
           node frontend/tests/i18n.test.mjs            7 passed
syntax     node --check (17 files)                      clean
lint       python -m ruff check backend tools           clean
rust       cargo fmt --check / clippy / test            14 passed
build      PyInstaller x2 (SOURCE_DATE_EPOCH/PYTHONHASHSEED)
           backend.exe                                854F95A7… (identical)
```

Red-green for the new regressions: reverting the quick-check makes
`test_data_page_corruption_is_quarantined_not_just_non_sqlite_files` fail;
reverting the finalize handle guard makes
`test_finalize_never_closes_a_pipe_owned_by_the_reader_thread` fail; reverting
`taskkill` return-code handling makes
`test_taskkill_nonzero_exit_falls_back_and_kills_the_whole_tree` fail (2 failed
on the reverted tree, 2 passed after restore).

Not verified in this round: a real-browser click-through of the new external-link
handler (headless WebView2 automation is unavailable here; the scheme allowlist
has a Rust unit test), a 4–8 hour soak, multi-GPU/Windows 10 coverage, and
packaged-mode screenshot evidence. Those remain outstanding as before.

### Deployed `0.9.0-beta.2` pair (2026-10-18)

Built in this workspace with `SOURCE_DATE_EPOCH=1790000000`,
`PYTHONHASHSEED=0` and the `RUSTFLAGS` cargo-registry remap, then deployed with
`tools/Publish-PortableRuntime.ps1 -Destination . -SkipDesktopShortcut`:

```text
launcher  0E778DD0C7AFDB1C0DAA17A7D24FFEC3498DB1BE0E61B75AD9FA820627DA3DD8
backend   A1BAACAA21FD6CDC49CBF8E7DA79556AA21D44056630627E87BF1A59ABD93B5E
installer 4C43948374E8F6F14BE2ECB323D0BA4181E2ACE390576A6C4DF9774649AC0657
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.2_x64-setup.exe)
```

The earlier `854F95A7` build was run twice and produced the same `backend.exe`
hash both times. The 2026-10-18 hardening rebuild (`A1BAACAA…`) was built once;
byte-reproducibility of this round's launcher is not claimed. The NSIS installer
is still not byte-reproducible (makensis metadata).

**NSIS install/uninstall re-verified (2026-10-18, authorized).** Silent install
into a temporary directory exited 0; the installed pair contained the matching
backend (`A1BAACAA…`), 757 `_internal` entries and an installer-patched launcher.
The installed app reached `backend ready`, answered 401/404 and the HMAC
readiness proof, accepted the token, closed via WM_CLOSE, and left no
backend/port. Silent uninstall exited 0; the new `NSIS_HOOK_PREUNINSTALL`
(`src-tauri/nsis/hooks.nsh`) removed the app-created `webview2-data/`,
`runtime-temp/`, `logs/`, `launcher-boot.log`, `debug.log` and
`backend/backend-child.log`, so the install directory was removed. User data in
`%APPDATA%`/`%LOCALAPPDATA%` was byte-identical before/after, and the
desktop/Start Menu shortcuts were restored.

The Add/Remove Programs registry entry could **not** be observed in this
session: a minimal NSIS installer also fails to write `HKCU` here while a Python
probe in the same task succeeds, so this is an environment restriction rather
than a confirmed product defect. The 2026-10-17 round observed the entry on a
normal desktop; verify it once more in a normal interactive session before
release.

**The sidecar is now PyInstaller `onedir` (2026-10-18).** The first beta.2
deployment still failed on the user's machine with PyInstaller's
"Could not create temporary directory!" even though the same pair started from
the maintainer shell; the onefile bootloader extracts ~20 MB to `%TEMP%` before
any application code runs, so the launcher could not attribute the failure. The
backend is now built as `backend/dist/backend/backend.exe` + `_internal/`
(no per-launch extraction). `src-tauri/build.rs`, `tauri.conf.json` resources
and `Publish-PortableRuntime.ps1` copy/verify the whole directory; deploying
only the exe is a broken pair. Evidence:

- rebuilding the onedir backend twice produced the same `backend.exe`
  (`854F95A7…`);
- the packaged pair reaches `backend ready` even with inherited
  `TEMP`/`TMP` pointing at a nonexistent path;
- the same onedir backend also starts under the beta.1 launcher with a broken
  `TEMP`, so the fix does not depend on the user having the newest shell;
- the generated NSIS script contains `backend\_internal` file entries;
- a deployer regression test copies a nested `_internal/` tree with real
  PowerShell 5.1 and verifies every file hash;
- the rebuilt NSIS installer was silently installed into a temp directory
  (`/S /D=…`): exit 0, installed launcher, `backend\backend.exe` (hash
  `854F95A7…`, matches the build) and **716 `_internal` files**; the installed
  app pair is therefore complete. Silent uninstall removed the install
  directory, uninstall entry and shortcuts; the desktop shortcut was restored
  to the workspace root afterwards, and the real `%APPDATA%\LunarShroud` data
  hashes/timestamps were unchanged.

The earlier dedicated-temp hardening remains (it protects any future
onefile/child process and cleans stale `_MEI*`); it also removed 167 stale
directories (~2.2 GB) that forced-kill tests had left in the real `%TEMP%`.

**Restricted-environment data-dir fallback (2026-10-18).** A user run with the
new launcher produced `exit code: 3` *and left neither `launcher.log` nor
`backend.log` updated*. The real `%APPDATA%\LunarShroud\logs` ACL carried an
inherited `LUNARVEIL\CodexSandboxUsers:(RX)` entry: a process started through
that sandbox can read but not write the normal data directory, so
`configure_file_logging` / the history DB fail before the API is ready. Both
sides now treat persistence as best-effort and relocate it:

- frozen backend: probe `%APPDATA%` → `%LOCALAPPDATA%` → `%USERPROFILE%` → the
  application directory for writability, use the first writable one, and do not
  abort the lifespan if only the log file cannot be opened;
- launcher: `launcher.log` falls back from `%APPDATA%` to `exe\logs\`, backend
  temp falls back to `exe\runtime-temp\`, and a dependency-free
  `launcher-boot.log` (version, pid, APPDATA/LOCALAPPDATA/TEMP/USERPROFILE) is
  written next to the exe;
- the backend's stderr is captured into `backend/backend-child.log` (1 MB
  rotation) so uvicorn's startup traceback survives even when application
  logging cannot be created.

A probe that sets `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `TEMP` and `TMP` all
to a **file path** (every normal directory unusable) still reaches
`backend ready`, writes `app\logs\launcher.log`, `app\backend\data\config.json`
and the boot/child logs — verified with a copy of the portable pair. A normal
desktop launch with writable `%APPDATA%` keeps using the existing user data.

**Duplicate launches produced the "exit code: 3" dialog (2026-10-18).** The
user's own `launcher-boot.log` showed two launches 26 seconds apart. The first
one succeeded (`backend ready pid=21856 port=17520`, the first app window was
created); the second one's `backend/backend-child.log` contains
`[Errno 10048] ... bind ... 17520` and exits 3. The error dialog the user saw
was therefore the *second* instance, not a broken installation. The launcher now
takes a per-session named mutex (`Local\LunarShroud-SingleInstance`); a second
launch calls `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` on the existing
window and exits instead of spawning another backend. Window creation and
visibility are logged in `launcher-boot.log` (`hwnd=... visible_before/after`),
and `setup` reports a failed window build instead of leaving an invisible
process in the event loop. Verified with the packaged pair: first launch logs
`setup: main window created`; a second launch leaves exactly one
`LunarShroud.exe`/`backend.exe` pair and no port-17520 error. (Window visibility
cannot be asserted through `EnumWindows` from this headless DSH session, hence
the in-app log line.)

**Root cause of the invisible window: WebView2 data directory not writable
(2026-10-18).** A limited-token run (Windows Task Scheduler `/RL LIMITED`, the
same kind of token the user's desktop shell hands to launched apps) reproduced
the failure exactly: the backend reached `backend ready`, but `setup` logged
`window creation failed: 拒绝访问 (os error 5)` because Tauri's default WebView2
user-data folder (`%LOCALAPPDATA%\<identifier>`) could not be created by the
restricted token. The process stayed in the event loop with **no window**, so the
user double-clicked again and hit the second-instance port conflict described
above. The window builder now passes an explicit
`WebviewWindowBuilder::data_directory(exe_dir/webview2-data)`; under the same
limited token the boot log records:

```
setup: creating main window
setup: main window created
setup: hwnd=0xe1294 visible_before=true visible_after=true
```

and the backend child log shows the frontend actually connecting
(`WebSocket /ws/instances [accepted]`). The frozen backend also seeds
`config.json`/`presets/` from the real `%APPDATA%` into the writable fallback
directory, so a restricted run keeps the user's settings instead of showing an
empty configuration.

Packaged-pair smoke (`tmp/release_smoke.py`, isolated `%APPDATA%` and
`%LOCALAPPDATA%`, dead `HTTP_PROXY`/`HTTPS_PROXY` pointing at
`127.0.0.1:1`) — all checks passed:

- launcher reached `backend ready`, created the dedicated backend temp dir and
  bound the management port despite the dead proxy (the `.no_proxy()` path);
- unauthenticated `/api/system/config` → 401, wrong readiness session → 404;
- `/api/system/ready?session&nonce` returned the expected
  `HMAC-SHA256(token, "lunarshroud-ready-v1:"+nonce)` and did not contain the
  token;
- hard-killing the backend generation made the supervisor restart it; the new
  generation had a different token, the old token got 401 and the new one 200;
- no file under the isolated `%APPDATA%` contained the token;
- after the launcher was terminated, the Job Object reaped both backend
  processes and the port was released.

Not exercisable in this session: the graceful window-close path. The packaged
app runs in a headless session where WebView2 never creates a titled top-level
window (`EnumWindows` only showed tao's message windows), so `WM_CLOSE` had no
target; the launcher was force-terminated and the Job Object cleanup was verified
instead. The maintainer's desktop run in the earlier rounds remains the only
window-close evidence.

The public mirror was refreshed with `tools/check_release_mirror.py --apply`,
re-checked **CLEAN (154 files, 0 problems)**, committed as the mirror's current
`HEAD`, and its
unreachable preparation objects were purged (`git gc --prune=now`;
`git fsck --unreachable` empty).

## Verified state

### Clean-room build validated this round (2026-10-17, not deployed)

Built from the fresh copy described above with the pinned commands from
`BUILDING.md` (`PyInstaller backend.spec --clean -y`, then
`cargo tauri build --ci -- --locked` with the `RUSTFLAGS` path remap):

```text
launcher  00a64ad81c623a839e058a8e7faf7f0e74b668ac62b0765567b7b5f24d982b0c
backend   c0d1d0f6cc79e948a88d7b1444aabc0daade682611306849f558ddecec83fafd
installer 850a1af2570a30c6068983d4f506289ea22f8ff7dfd19bbaea57177b9f708a5b
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.1_x64-setup.exe)
```

These are the **final** artifacts: they include the stop/monitor exit-code race
fix and were built after the Linux-only `rustls` 0.23.46 security bump.
`cargo tree --target x86_64-pc-windows-msvc -e normal,build,dev -i rustls` (and
`-i glib`) returns nothing, so neither crate is part of the Windows graph.
The portable deployment copies were byte-identical to the two build outputs
(`Publish-PortableRuntime.ps1` verifies both hashes). Start/close checks used an
isolated `%APPDATA%`, so the
maintainer's real configuration was not touched:

- With a deliberately dead `HTTP_PROXY`/`HTTPS_PROXY` (`127.0.0.1:1`, no
  `NO_PROXY`), the packaged pair bound the management port within 1 s. Without
  the `.no_proxy()` fix the shell's readiness probe would have gone through the
  dead proxy and reported a timeout.
- Unauthenticated `/api/system/health` and `/api/system/config` returned 401;
  `/api/system/ready` without the session id (and with a wrong one) returned
  404.
- Closing the window exited the launcher and both PyInstaller processes and
  released the port; `launcher.log` recorded `backend ready` then
  `backend exited during shutdown`.
- Artifact scan: raw ASCII/UTF-16LE search **plus** decompression of the
  PyInstaller archive (1,112 modules, `co_filename` and constants) found zero
  hits for the user name, workspace name, machine name or local model paths.
  The launcher carries the expected 317 neutral `cargo-registry` prefixes.
- Real model through the launcher's own API: llama.cpp `0.7.0-dev (build 11698)`
  with `nomic-embed-text-v1.5.Q4_K_M.gguf`; `/v1/embeddings` returned a
  768-dimensional vector; stop and history persistence worked. The stop response
  and the history row both carried `exit_code=1`, exercising the stop/monitor
  race fix end to end. The backend process ran with the dead proxy in its
  environment, exercising the `trust_env=False` fix end to end.
- Short stability check (not a substitute for the 4–8 h soak): 120 + 60
  embedding requests, zero failures; launcher backend RSS stayed flat at
  83.0 MB across the first run. The second run measured 3 ms min / 9.6 ms avg /
  22 ms max. The first run contained one ~6-minute server-side stall that
  coincided with a parallel Rust release build on the same machine; it did not
  reproduce in the quiet run.
- Browser (headless Chrome over CDP, no console errors on any page): dashboard,
  parameter editor with the embedding recipe applied (preview contained
  `--embeddings`), model library with GGUF capability values, history with the
  new 已中断 badge after a hard-killed backend, monitor (RTX 5060 Laptop GPU),
  settings, and the embedded llama-server WebUI iframe rendered.

Not done in the clean-room round: NSIS install/uninstall (done later on
2026-10-17 after authorization; see the onboarding section above and the raw
sixth-round replay), a packaged-mode screenshot (foreground capture was
unreliable on the shared desktop), the 4–8 h soak and the multi-GPU/model
matrix.

### Onboarding and diagnostics rebuild (2026-10-17, workspace build)

A later build the same day added the in-app Getting started page, pre-launch
checks and failure attribution. It was produced in the workspace with the pinned
commands after the clean-room build above, then **deployed to the workspace root
on 2026-10-17** after the fifth-round verification. The NSIS installer was also
installed silently, started, closed, and uninstalled; the install directory,
HKCU uninstall entry and Start Menu/Desktop shortcuts were removed.

```text
launcher  85c5cc57abd61c4cfc238109fae7eb477bca576f6fdc2662134da6cc499c1c49
backend   6e94321bfeaf167df8a211fc6857246949bb77f8d7c15a79147b17253189c809
installer b38443868466ae3f9ad90a1b908b8810abf907cd0a17dbfcd734965ae4af08f5
```

- Packaged smoke with an isolated `%APPDATA%` and a dead `HTTP_PROXY`: listener
  on 18091, unauthenticated `/health` → 401, wrong session → 404, graceful
  window close exited the launcher and released the port.
- Browser validation (headless Chrome/CDP, no console errors):
  - Getting started page: 7 sections, 5 steps, 15 troubleshooting rows, dynamic
    checklist (missing server/model → red/amber; after configuring the real
    llama.cpp build and model folder → green).
  - Dashboard setup banner linked to the guide.
  - Model library showed the 嵌入模型 badge; direct launch of
    nomic-embed-text-v1.5 produced a command containing `--embeddings`
    automatically and reached RUNNING.
  - A deliberately failing server (`CUDA error: out of memory`) produced
    `out_of_memory` diagnosis; the dashboard's “为什么没启动？” modal showed the
    localized cause and fix.
  - Launching with a nonexistent model showed the blocking pre-flight modal
    (`model_missing`) with 关闭 / 查看新手指南 and did not create an instance.
- NSIS installer end-to-end (authorized 2026-10-17): with the stale audit install
  key removed, a silent install placed `lunarshroud.exe`, `backend\backend.exe`
  and `uninstall.exe` under `%LOCALAPPDATA%\LunarShroud` and registered the
  uninstall entry; the installed app started, returned 401/404, and closed
  cleanly; the silent uninstaller removed the directory, uninstall entry and
  Start Menu/Desktop shortcuts. The installer patches the launcher binary with
  bundle metadata, so the installed launcher hash differs from the portable
  candidate while `backend.exe` matches. A stale
  `HKCU\Software\lunarshroud\LunarShroud` install-location key is preserved by
  design when “delete app data” is not selected; it makes a reinstall reuse the
  previous directory (removed manually for the clean-registry test).
- Sixth-round replay (2026-10-17) captured the raw NSIS evidence that the
  previous round only narrated. Silent install exited 0 in 2.55 s and produced
  three files (24,997,085 bytes): installed launcher `076577a5…` (bundle-patched,
  differs from the portable candidate), `backend.exe` `6e94321b…` (matches), and
  `uninstall.exe` `069d6145…`. The installed app bound 17520, returned 401 for an
  unauthenticated config request and 404 for a wrong readiness session, then
  closed cleanly with no installed process left. Silent uninstall exited 0 in
  1.36 s and removed the install directory, uninstall entry and Start
  Menu/Desktop shortcuts; the install-location key remained by design and was
  removed to restore the pre-test state. The real `%APPDATA%\LunarShroud`
  config/history SHA-256 values were unchanged before and after. Raw logs and
  registry snapshots are in maintainer-local
  `tmp/risk_assessment_20261017/nsis/` (not published).
- Partial soak (not a replacement for the 4–8 h gate): the deployed packaged
  backend served a real Qwen3.5-4B Q4_K_M chat model for 610 s. 99 of 100
  sequential chat requests succeeded (the last failed only because cleanup
  stopped the backend mid-request); average 992 ms, max 2.0 s, 6,336 completion
  tokens. Backend RSS stayed ~64 MB. llama-server RSS grew from ~5.2 GB to
  ~11.4 GB and then plateaued, consistent with the default 8 GiB `--cache-ram`
  prompt cache rather than an unbounded leak; GPU use was ~2.98 GB of 8 GB.
  A full 4–8 h soak is still required.
- `tools/verify_checksums.py` now verifies a whole `SHA256SUMS.txt` in one
  command (fail-closed); release reports should quote its output instead of
  retyping digests (fifth-round P1).

### Remaining risk assessment (2026-10-17)

The full register, with probability/impact/evidence/residual risk, is in
[RISK_ASSESSMENT.md](RISK_ASSESSMENT.md). This round added four real-process
checks that were still open:

- **`cache_ram` memory behavior:** with the default `--cache-ram 8192`, the real
  4B chat run grew llama-server RSS from 5.2 GB to 11.4 GB and then plateaued;
  with `cache_ram: 0` the same model stayed at 3.21 GB over 40 requests (214 s),
  backend RSS 65.2 MB, GPU 2980/8151 MiB. The growth is llama.cpp's host-side
  prompt cache, not a launcher leak. Guidance added to GETTING_STARTED: lower
  the value or set 0 on 16 GB machines, at the cost of prompt-cache reuse.
- **Supervisor crash restart:** killing the packaged backend tree produced
  `backend exited unexpectedly … restarting` followed by `backend restarted
  pid=21476` on the same 17520 port in about 2 s; the window stayed alive, and a
  normal window close exited the launcher, released the port and left no new
  backend. The first generation's bootloader/child PIDs were 40104/35536; the
  restarted pair was 21476/39932.
- **Credential scan:** across 19 isolated `%APPDATA%` files (ASCII + UTF-16LE),
  the session token had zero hits. The per-launch `session_id` appears once in
  `backend.log`; it is not the management token and is invalidated on restart.
- **Rollback backup boots:** the previous pair in
  `tmp/portable-backups/20261017-174210` (`83634d…` / `e4fae5…`) started in an
  isolated `%APPDATA%` on 17520 and closed cleanly, so the backup is a real
  rollback point rather than a hash-only record.
- Repeated risk checks kept passing: management API bound only to
  `127.0.0.1:18097`; a non-ASCII/space model path reached RUNNING and returned
  768-dimensional embeddings; 10 consecutive launch/stop cycles reused port 8080
  with zero failures and no leftover `llama-server`.

The 4–8 h soak, multi-hardware matrix, code signing and build provenance remain
open; the risk document states that beta publication is acceptable with those
disclosed, but 1.0 is not.

### Seventh-round hardening (2026-10-17, deployed)

A rebuild after the sixth-round review carries four backend/frontend fixes and
two operational tools:

```text
launcher  67ab9f22b4a7a3de659ffb70aa8320fbff6c13e24e21f68552a4f5700c6c4aab
backend   30d908756d233560fdea54f2c6918899f7f86bcb40c025621b7d70bcb631f9c6
installer bba806827f4bb7344c2ddde4edea28469843b604a02e4e4327fdf2ce9f7c61f5
```

- **Corrupt `history.db` no longer kills startup.** Fault injection produced
  `sqlite3.DatabaseError: file is not a database` and exit 3. `init_db()` now
  quarantines the main DB plus WAL/SHM as `history.db.corrupt*` and rebuilds;
  lock/permission errors are still re-raised rather than misclassified.
- **>260-character paths are blocked before launch.** A valid 315-character
  GGUF path was accepted by preflight, then llama-server failed with
  `failed to open GGUF (No such file or directory)` and the diagnosis blamed
  format/download/architecture. New `path_too_long` / `path_near_limit` checks
  cover model, draft and mmproj paths, with bilingual title/fix text.
- **Truncated GGUF metadata is visible in scan summaries.** The model list now
  returns `truncated`/`stopped_early`; the library shows a `元数据截断` warning
  badge. Malformed probes still degrade to `probed: false` rather than 500.
- **Startup logs no longer contain `session_id`.** `backend.log` now records
  only `backend started pid=…`; a regression test asserts the session value is
  absent.
- **User-data backup/restore tools** (`tools/Backup-UserData.ps1`,
  `tools/Restore-UserData.ps1`): SHA-256 manifest, tamper/traversal rejection,
  pre-restore snapshot. During testing, PowerShell 5.1 launched from a
  PowerShell 7 environment could not load `Get-FileHash` because of the
  inherited `PSModulePath`; the tools now use .NET SHA-256.
- **Deployer relative-path fix:** `-Destination .` resolved through .NET's
  current directory and once deployed into `src-tauri\` while the workspace root
  kept the old pair. `Publish-PortableRuntime.ps1` now resolves relative
  `-Destination`/`-BackupDirectory` against `Get-Location`, with a regression
  test where PowerShell location and process cwd differ.
- **Reproducible builds:** with `SOURCE_DATE_EPOCH=1790000000` and
  `PYTHONHASHSEED=0`, two consecutive builds produced identical `backend.exe`
  (`30d90875…`) and `lunarshroud.exe` (`67ab9f22…`) hashes. The NSIS installer
  still changes between builds because makensis writes its own metadata
  (`bba80682…` for this release); installed backend/launcher hashes are verified
  separately.
- **Fault/security probe:** 34/34 against the rebuilt deployed backend
  (authentication, CORS/WS, traversal, `extra_args`, command injection,
  redaction, 413, malformed GGUF, child env without `LUNARSHROUD_*`, logs
  without token).
- **Short model matrix:** Qwen3.5-4B, Qwen3.5-9B IQ4_NL, Gemma-4-E4B, MiMo-9B,
  nomic-embed (768-dim) and Qwen3.5-4B CPU-only all reached RUNNING and answered
  one request; no llama-server left after each stop.
- **Concurrency:** 8 instances started/stopped concurrently; 8/8 running,
  ports 8080–8087 unique, 8/8 embedding requests succeeded, no leftover process.
- **NSIS replay (r3):** silent install exit 0; installed launcher `1cb283d2…`
  (bundle-patched), backend `30d90875…` (matches candidate); installed app bound
  17520, returned 401/404, closed with no residue; silent uninstall removed the
  directory, uninstall entry and shortcuts. Raw logs are in maintainer-local
  `tmp/risk_assessment_20261017/nsis-r3/`.

Gates at this revision: 497 pytest, 88.53% coverage, ruff clean, 19 frontend
tests, 17 JavaScript syntax checks, 10 Rust tests, mirror CLEAN.

### Currently deployed pair (2026-10-17)

Root `LunarShroud.exe` with the adjacent `backend\backend.exe`, deployed by
`tools/Publish-PortableRuntime.ps1` and confirmed byte-identical to
`src-tauri/target/release/lunarshroud.exe` / `backend/dist/backend.exe`:

```text
launcher  67ab9f22b4a7a3de659ffb70aa8320fbff6c13e24e21f68552a4f5700c6c4aab
backend   30d908756d233560fdea54f2c6918899f7f86bcb40c025621b7d70bcb631f9c6
installer bba806827f4bb7344c2ddde4edea28469843b604a02e4e4327fdf2ce9f7c61f5
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.1_x64-setup.exe)
```

This pair carries the llama.cpp v0.7.0 alignment round on top of everything in
the 2026-10-14/16 pairs: use-case recipes (`/api/params/recipes` plus the recipe
row in the parameter editor), nine new capability-driven parameters (pooling,
embedding normalization, prompt caching, router-mode directory and limit,
thinking mode, reasoning format, chat-template kwargs, RPC), argument-position
allowed-value parsing (`{a,b,c}` / `[a|b|c]`), build-aware select validation, the
annotated numeric-default fix (`--moe-cache-mib` no longer parses as
`0,disabled`), and crash translation for `GGML_ASSERT` lines.

Verified at the symptom level: window start with the backend bound to 17520,
HTTP 401 for unauthenticated requests (including the new recipes route), HTTP 404
for `/api/system/ready` without a session, and a **graceful window close** that
reclaimed the whole backend process tree and released the port (no residue in
`tasklist`/`netstat`, no orphaned `llama-server`). All three artifacts passed the
builder-identity byte scan (ASCII + UTF-16LE: zero hits for the user name, the
workspace-directory name, the machine name, and the user-profile path prefix; the
launcher carries the expected 317 neutral `cargo-registry` panic-location
prefixes). `SHA256SUMS.txt` beside the installer was regenerated from this build.

Backend gates at this revision: 464 pytest, ruff clean, 19 frontend node tests,
10 cargo tests, release mirror CLEAN at 145 files. Real-model evidence against
llama.cpp `0.7.0-dev (build 11698)`, all through the launcher's own API: an
embedding instance serving 768-dimensional `/v1/embeddings`; a 4 B chat instance
with `--reasoning off` and the read-only tool set answering in 0.2 s with
`/tools` reporting `write: false` for all four tools; router mode
(`--models-dir` + `--models-max 1`) reaching RUNNING with the router-shaped
`/v1/models`; and the upstream single-slot embedding assertion reproduced,
bisected and worked around both ways. What was **not** done at this revision: a
real-browser click-through of the recipe row and of the embedded v0.7.0 Web UI
(browser automation was not authorized this round), packaged NSIS
install/uninstall, and the 4–8 h soak.

### 上一对部署（historical, 2026-10-14）

```text
launcher  5bff4affbce90a23ac80530b30f11ffb9fe99ddf610b79c6196a5d16ec3dc114
backend   4149e2bc96a9701b76c65a5503b2a084cb848cde70fa87b3164ac9045b185cbc
installer b3112d83398864fbdf0d468fc9aad9a2a29bee6f927063f0e73419b107519f50
```

That pair carried the independent-audit fixes (probe-hang/thread-starvation H1,
restart launch-history M1, instance-log rotation/cleanup L1, BOM config L2), the
AI tools & MCP parameter group, the official-build (11630) alignment batch, and
the second-verification fixes N1/N2/N3.

### 上一对部署（historical, 2026-09-25）

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

The probability/impact/evidence view of these blockers and the accepted beta
risks is [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md).

- No code-signing certificate; SmartScreen may warn and users cannot
  independently establish installer authenticity.
- No completed 4–8 hour real-workload soak test or representative GPU/model
  hardware matrix. A 610 s real 4B chat-model soak ran (99/100 requests, see
  above), but the full gate remains open.
- No GitHub build-provenance attestation (audit F16 residual). Releases publish
  `SHA256SUMS.txt` and put the digest in the release notes, but do not emit an
  `actions/attest-build-provenance` statement.
- Auto-assigned instance ports still have a probe → release → child-bind TOCTOU
  window: an unrelated process can take the port between selection and
  `llama-server` binding. The launch then fails with a clear bind error and a
  retry assigns a different port. The test double now retries binding briefly;
  the product-side race is documented, not eliminated.
- FastAPI test-client/httpx deprecation warning remains for the next dependency
  update; it is not a known packaged-runtime defect.
- `cargo audit` reports zero vulnerabilities after the 2026-10-17 `rustls`
  0.23.46 bump; seven warnings remain — five unmaintained `unic-*` crates
  inherited through Tauri/urlpattern, unmaintained `proc-macro-error`, and the
  Linux-only `glib` unsoundness advisory. Neither `rustls` nor `glib` is in the
  Windows dependency graph; track upstream Tauri/urlpattern updates.

Resolved since the previous list: Python dependencies are now hash-locked for
the tested CPython 3.11/3.12 Win64 wheels (`backend/requirements*.lock`) and
both CI and `setup.ps1` install from the lock with `--require-hashes`.

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
