# Building on Windows

Install Python 3.11+, Node.js 22, WebView2, Visual Studio Build Tools with “Desktop development with C++”, and Rust MSVC. The repository pins Rust in `rust-toolchain.toml`; install Tauri CLI `2.11.4` with Cargo.

```powershell
.\setup.ps1
# Fixed metadata makes the backend and launcher byte-reproducible; without
# these two variables consecutive PyInstaller builds differ.
$env:SOURCE_DATE_EPOCH = '1790000000'
$env:PYTHONHASHSEED = '0'
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
# Registry crate paths (which contain the build machine's user name) end up in
# the binary via Rust panic location strings; remap them to a neutral prefix so
# the public artifact carries no builder identity. Always build with this set.
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry\src=cargo-registry"
cargo tauri build --ci -- --locked
```

PyInstaller writes an **onedir** backend to `backend/dist/backend/`: `backend.exe`
plus the `_internal/` runtime. They are one deployment unit; `src-tauri/build.rs`
refuses a release build without them and stages the whole directory for the
bundle. Do not copy the exe alone.

With the fixed environment, two consecutive builds produced identical
`backend.exe`/`lunarshroud.exe` hashes (verified 2026-10-17; the 2026-10-18
onedir backend also reproduced `08C8FAFC…` across two builds). The NSIS
installer still differs between builds because makensis writes its own
metadata; verify the installed `lunarshroud.exe`/`backend.exe` hashes and record
the installer hash for each release instead of assuming installer reproducibility.

Release builds require the `backend/dist/backend/` directory (`backend.exe` plus its `_internal/` runtime). `src-tauri/build.rs` copies the whole directory into the bundle and aborts a release build when it is absent; never copy a previously built backend across source revisions, and never copy only the exe. The sidecar is **onedir** so startup does not extract to `%TEMP%` (see `backend/backend.spec` and the temporary-directory incident in `docs/STATUS.md`).

Before publishing, byte-scan the built launcher, backend sidecar, and installer for builder identity (user names, `C:\Users` paths, local directories). The remap above covers the known source of such strings; the scan is the verification.

For reproducible review, build from a clean checkout, retain the source commit/tag and `Cargo.lock`, capture tool versions, and publish SHA-256 checksums. Python runtime and dev dependencies are hash-locked (`backend/requirements.lock` / `requirements-dev.lock`) and CI plus `setup.ps1` install with `--require-hashes`; when a pin changes, regenerate both the `.txt` and the corresponding lock (see the lock header for the exact commands).

For a local portable runtime, run `tools/Publish-PortableRuntime.ps1` after both builds. It copies the exact release launcher and its matching PyInstaller backend, verifies SHA-256 for both files, and rewrites the Desktop shortcut. Do not point a shortcut at an old standalone EXE.
