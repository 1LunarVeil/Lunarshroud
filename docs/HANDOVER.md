# Maintainer handover

Last updated: 2026-10-17 (Asia/Shanghai)

## Read this first

LunarShroud is a Windows desktop launcher for `llama-server.exe`: Tauri shell,
FastAPI management backend, and static JavaScript frontend. The verified
candidate is `0.9.0-beta.2`, still beta because real model/hardware soak
coverage is incomplete. [STATUS.md](STATUS.md) owns current facts and blockers;
this file owns maintenance and recovery steps.

## Repository layout

```text
backend/       FastAPI, lifecycle manager, stores, and tests
frontend/      Static UI and frontend runtime tests
src-tauri/     Desktop shell, trusted backend startup, Windows Job Objects
docs/          Current docs (this directory)
tools/         Maintenance helpers; see tools/README.md
data/          Created at runtime; local user data, never published
```

The maintainer's working copy additionally holds paths that are deliberately
absent from this repository: `tmp/` (local debug scripts, screenshots, probe
output, and recoverable archives), `github/` (the staging mirror this
repository was published from), and `.claude/` / `.mimosa/` (local tool state,
which records machine-specific absolute paths). `.gitignore` excludes all of
them. Do not cite anything under those paths from a document that ships.

## Run and test

For source-mode backend development, the explicit insecure switch remains
loopback-only:

```powershell
.\setup.ps1
$env:LUNARSHROUD_ALLOW_INSECURE_DEV = '1'
.\.venv\Scripts\python.exe backend\main.py
```

Run the release checks from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=backend --cov-config=.coveragerc --cov-report=term-missing --cov-fail-under=75
node --test frontend\tests\runtime.test.mjs frontend\tests\i18n.test.mjs
Get-ChildItem frontend\js -File | Where-Object Extension -in '.js', '.mjs' | ForEach-Object { node --check $_.FullName }
cargo fmt --manifest-path src-tauri\Cargo.toml -- --check
cargo clippy --manifest-path src-tauri\Cargo.toml --locked -- -D warnings -A linker-messages
cargo test --manifest-path src-tauri\Cargo.toml --locked
```

Test counts grow over time; the gate is zero failures plus the coverage floor, so run the commands rather than memorizing numbers.

Tests are necessary but insufficient. Before promotion, run real
`llama-server` start/chat/stop coverage plus the 4–8 hour soak matrix in
[RELEASE.md](RELEASE.md).

## Build and deploy

Build in this order; never reuse a backend executable from a different source
revision:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
cargo tauri build --ci -- --locked
Set-Location ..
```

The beta package artifacts are:

```text
src-tauri\target\release\lunarshroud.exe
src-tauri\target\release\bundle\nsis\LunarShroud_0.9.0-beta.1_x64-setup.exe
src-tauri\target\release\bundle\nsis\SHA256SUMS.txt
```

To refresh the root portable runtime and Desktop shortcut, deploy a matched
pair:

```powershell
.\tools\Publish-PortableRuntime.ps1 -Destination . -BackupDirectory .\tmp\portable-backups
```

The script verifies both copied hashes before changing the shortcut. Do not
restore the legacy root EXE from `tmp/legacy-root-launcher-backups/` by itself.

## Startup and shutdown diagnosis

1. Confirm the shortcut targets the project-root `LunarShroud.exe`, not an old
   installer extraction or standalone copy.
2. Confirm adjacent `backend\\backend.exe` exists and belongs to the same
   deployment.
3. A healthy start creates a `LunarShroud` window and a listener on
   `127.0.0.1:17520`; unauthenticated management requests must return HTTP 401.
4. Close the main window and verify the launcher exits and port 17520 is no
   longer listening. If a child remains, capture its command line and process
   tree before force-killing it.
5. If either file is uncertain, rebuild both and rerun
   `Publish-PortableRuntime.ps1`; do not manually replace one EXE.

Post-mortem logs live in `%APPDATA%/LunarShroud/logs/`: `backend.log` records
backend startup plus every instance lifecycle transition (launch, ready, stop,
timeout, abnormal exit), and `launcher.log` records shell-side supervision
events (backend ready, exit, restart, readiness failure, shutdown). The shell
restarts a crashed backend automatically with the same session token; the
frontend recovers by refreshing its session and reconnecting, so a single
restart pair in `launcher.log` with no matching frontend complaint is normal.

## Security invariants

- Packaged sessions fail closed if a random token cannot be created; do not add
  an unauthenticated production fallback.
- The management API remains loopback-only. Non-loopback model binding requires
  an API key, which can still be visible to same-user local processes through
  the model-server command line.
- Do not restore release fallback to the working directory, PATH, or source
  Python file for backend execution.
- Keep path containment, secret redaction, request limits, and bounded UI/log
  state covered by tests when changing related code.

## Public-source workflow

This section is maintainer-facing. It describes how the published tree is
produced; a clone of the published repository has nothing to run here.

The maintainer keeps a staging tree beside the working copy and never
synchronizes it by hand. `tools/check_release_mirror.py` is the single gate and
refresher: it fails on missing, drifted, stale, or forbidden files in the
staging tree and on any local absolute path or user name inside mirrored text,
and `--apply` refreshes it with the same rule table. Both trees must be present,
so the gate exits with an error in a plain clone:

```powershell
py -X utf8 tools\check_release_mirror.py --apply   # refresh, then re-verify
py -X utf8 tools\check_release_mirror.py           # must report CLEAN
```

The rule table is the authority on what ships: it excludes build output, runtime
data (`data/` in its entirety, including the SQLite `-wal`/`-shm` sidecars and
any `config.json.corrupt`), local tool state, editor backups, and the internal
planning documents that record machine-specific absolute paths. When a new kind
of local artifact appears, add it to that table rather than relying on the
privacy scan to notice — the scan only judges files that already reached the
staging tree.

Never synchronize the whole workspace. Initialize Git only after final review:
the original workspace carried no `.git` history, so no historical secret scan
was possible.

## Historical material and recovery

- [README.md](README.md) maps current documentation.
- Superseded handover and test plans are retained by the maintainer outside the
  published tree. They are evidence only; they do not authorize any old build or
  release procedure, and nothing here depends on them.
- The same applies to local archives of replaced executables, screenshots, and
  one-off debug scripts. Recover from them by inspection, never by copying them
  into a deployment or a commit.
