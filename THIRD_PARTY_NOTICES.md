# Third-Party Notices

LunarShroud is MIT-licensed. It depends on third-party packages whose own licenses remain controlling for those components.

Major direct components include Tauri, FastAPI, Uvicorn, Pydantic, psutil, HTTPX, Tokio, Reqwest, serde, and windows-sys. Build and test tooling includes PyInstaller, pytest, Node.js, Rust, and NSIS. See `backend/requirements*.txt`, `src-tauri/Cargo.toml`, and `src-tauri/Cargo.lock` for the authoritative dependency set.

`llama-server.exe`, GGUF model files, WebView2, GPU drivers, and Python/Rust/Node toolchains are not distributed in this source tree. Users are responsible for their licenses and model terms.

Before publishing a binary release, review dependency license metadata and attach a machine-generated SBOM or equivalent inventory. Dependency security scans do not replace license review.

Known-advisory scan results and transitive maintenance warnings are tracked in `DEPENDENCY_AUDIT.md`.
