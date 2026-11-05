# Third-Party Notices

LunarShroud is MIT-licensed. It depends on third-party packages whose own licenses remain controlling for those components.

Major direct components include Tauri (and `tauri-plugin-dialog`), FastAPI,
Starlette (pinned deliberately, see `backend/requirements.txt`), Uvicorn,
Pydantic, psutil, HTTPX, `nvidia-ml-py`, Tokio, Reqwest, serde/serde_json,
getrandom, ring (HMAC-SHA256 for the readiness challenge) and windows-sys.
Build and test tooling includes PyInstaller, pytest, Pillow (icons only),
Node.js and Rust. See `backend/requirements*.txt`, `src-tauri/Cargo.toml`,
and `src-tauri/Cargo.lock` for the authoritative dependency set.

NSIS is **not** a dependency: the installer channel was removed on 2026-11-05
(see `AGENTS.md` §4), so nothing here builds or ships an NSIS package.

`llama-server.exe`, GGUF model files, WebView2, GPU drivers, and Python/Rust/Node toolchains are not distributed in this source tree. Users are responsible for their licenses and model terms.

Before publishing a binary release, review dependency license metadata and attach a machine-generated SBOM or equivalent inventory. Dependency security scans do not replace license review.

Known-advisory scan results and transitive maintenance warnings are tracked in `DEPENDENCY_AUDIT.md`.
