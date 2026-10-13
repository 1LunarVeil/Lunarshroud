# Building on Windows

Install Python 3.11+, Node.js 22, WebView2, Visual Studio Build Tools with “Desktop development with C++”, and Rust MSVC. The repository pins Rust in `rust-toolchain.toml`; install Tauri CLI `2.11.4` with Cargo.

```powershell
.\setup.ps1
Set-Location backend
..\.venv\Scripts\python.exe -m PyInstaller backend.spec --clean -y
Set-Location ..\src-tauri
# Registry crate paths (which contain the build machine's user name) end up in
# the binary via Rust panic location strings; remap them to a neutral prefix so
# the public artifact carries no builder identity. Always build with this set.
$env:RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE\.cargo\registry\src=cargo-registry"
cargo tauri build --ci -- --locked
```

Release builds require `backend/dist/backend.exe`. Tauri copies it into the bundle and aborts when it is absent. Do not copy a previously built backend across source revisions.

Before publishing, byte-scan the built launcher, backend sidecar, and installer for builder identity (user names, `C:\Users` paths, local directories). The remap above covers the known source of such strings; the scan is the verification.

For reproducible review, build from a clean checkout, retain the source commit/tag and `Cargo.lock`, capture tool versions, and publish SHA-256 checksums. Python direct dependencies are pinned, but transitive resolution is not yet hash-locked; treat build date and audit output as part of release provenance.

For a local portable runtime, run `tools/Publish-PortableRuntime.ps1` after both builds. It copies the exact release launcher and its matching PyInstaller backend, verifies SHA-256 for both files, and rewrites the Desktop shortcut. Do not point a shortcut at an old standalone EXE.
