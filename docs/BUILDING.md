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
