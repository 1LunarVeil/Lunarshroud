# Building on Windows

Install Python 3.11+, Node.js 22, WebView2, Visual Studio Build Tools with “Desktop development with C++”, and Rust MSVC. The repository pins Rust in `rust-toolchain.toml`; install Tauri CLI `2.11.4` with Cargo.

```powershell
.\setup.ps1
# Fixed metadata makes the backend byte-reproducible; without these two
# variables consecutive PyInstaller builds differ.
$env:SOURCE_DATE_EPOCH = '1790000000'
$env:PYTHONHASHSEED = '0'
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
# Two things in RUSTFLAGS, both required:
#  1. Registry crate paths (which contain the build machine's user name) end up
#     in the binary via Rust panic location strings; remap them to a neutral
#     prefix so the public artifact carries no builder identity.
#  2. `/Brepro` tells the MSVC linker to write a content-derived PE timestamp
#     instead of the current time. WITHOUT IT the launcher hash changes on every
#     build even with SOURCE_DATE_EPOCH set: measured, the COFF TimeDateStamp was
#     `1793601298` (the wall clock) on one build and `968523806` after adding
#     /Brepro. Two /Brepro builds on unchanged sources then produced the
#     identical hash 7eae1d96…. SOURCE_DATE_EPOCH alone does NOT fix this.
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry\src=cargo-registry -C link-arg=/Brepro"
cargo tauri build --ci -- --locked
```

PyInstaller writes an **onedir** backend to `backend/dist/backend/`: `backend.exe`
plus the `_internal/` runtime. They are one deployment unit; `src-tauri/build.rs`
refuses a release build without them and stages the whole directory for the
bundle. Do not copy the exe alone.

With the fixed environment (including `/Brepro`), two consecutive builds on
unchanged sources produce an identical `lunarshroud.exe` (verified 2026-11-02:
`7eae1d96…` twice at that revision; the 1.0.0 release launcher is `064f6895…`).
Earlier claims that `SOURCE_DATE_EPOCH` alone made the launcher reproducible
were wrong — the PE timestamp kept tracking the wall clock, which is exactly the
kind of claim that only a second build disproves.
The 2026-10-18 onedir backend also reproduced `08C8FAFC…` across two builds.
(An NSIS installer was also measured at one point; that channel has since been
removed — see `AGENTS.md` §4 — so it is no longer built or verified.)

Release builds require the `backend/dist/backend/` directory (`backend.exe` plus its `_internal/` runtime). `src-tauri/build.rs` stages the whole directory beside the compiled launcher and aborts a release build when it is absent; never copy a previously built backend across source revisions, and never copy only the exe. The sidecar is **onedir** so startup does not extract to `%TEMP%` — see `backend/backend.spec` for the rationale (a redirectable or read-only temp directory makes a onefile bootloader hang with no usable error).

Before publishing, byte-scan the built launcher and backend sidecar for builder identity (user names, `C:\Users` paths, local directories). The remap above covers the known source of such strings; the scan is the verification.

For reproducible review, build from a clean checkout, retain the source commit/tag and `Cargo.lock`, capture tool versions, and publish SHA-256 checksums. Python runtime and dev dependencies are hash-locked (`backend/requirements.lock` / `requirements-dev.lock`) and CI plus `setup.ps1` install with `--require-hashes`; when a pin changes, regenerate both the `.txt` and the corresponding lock (see the lock header for the exact commands).

For a local portable runtime, run `tools/Publish-PortableRuntime.ps1` after both builds. It copies the exact release launcher and its matching PyInstaller backend, verifies SHA-256 for both files, and rewrites the Desktop shortcut. Do not point a shortcut at an old standalone EXE.

## Running the browser gate locally

The gate is what CI runs; run it the same way or it will report failures that
do not exist (or miss ones that do).

```bash
# 1. backend on the port the gate expects, with an ISOLATED data directory
LUNARSHROUD_ALLOW_INSECURE_DEV=1 LUNARSHROUD_LAUNCHER_PORT=17520 \
  python tools/dev_backend.py

# 2. browser runtime — playwright-core ships no browser binary, and
#    `npx playwright install` fetches the *latest* Playwright, whose revision
#    does not match the pinned one (measured: npx pulls 1248, the pinned
#    playwright-core@1.57 wants 1200). Use the pinned CLI.
node node_modules/playwright-core/cli.js install chromium

# 3. the gate
npm run test:browser          # core paths, axe accessibility, first paint
npm run test:browser:diagnose # same boot probe, dumps console/DOM on failure
```

Two things that produce **false reds** if skipped:

- `LUNARSHROUD_LAUNCHER_PORT` must equal the port the tests use.
  `trusted_origins()` derives the WebSocket allow-list from the *configured*
  port (AGENTS §2.7), so a mismatch returns HTTP 200 for REST but
  `403 Forbidden` for every WebSocket handshake.
- Use `tools/dev_backend.py`, not `python -m uvicorn`: it points `DATA_DIR` at
  a temp directory, so the suite cannot rewrite your real chats/history.

`LUNARSHROUD_BROWSER_CHANNEL=msedge` runs against the installed Edge instead of
the bundled Chromium, which is useful for checking WebView-adjacent behaviour.

### Branches your machine cannot reach

Some defects only appear in states a normal developer box never enters — no GPU
(therefore the empty hardware-chip branch), light colour scheme (headless
Chromium reports `prefers-color-scheme: light` while most dev machines are
dark), a clean database, or a machine without the registry entries that give
`.mjs` a MIME type. CI is the only place those states occur naturally, which is
why a suite can be green locally for weeks and red on every run.

Construct the state explicitly instead of waiting for CI:

```js
await page.evaluate(() => {
  document.body.className = 'theme-light';
  document.querySelectorAll('.hardware-chip').forEach((c) => c.classList.add('is-empty'));
});
```

When a gate fails, make it say *which* node and *what* numbers — a message like
`color-contrast: 2 node(s)` costs a debugging round; `#6d6d76 on #eeeef1 =
4.42:1 (needs 4.5)` identifies the cause immediately. `describeViolations()` in
`frontend/tests/browser/helpers.mjs` does this for axe results.

And after a fix, prove the gate can still fail: inject a real defect and watch
it go red. "It turned green" alone cannot distinguish a fix from a loosened
assertion.
