# Third-Party Notices

LunarShroud is MIT-licensed. It depends on third-party packages whose own licenses remain
controlling for those components.

## No AGPL code

**This project contains no AGPL-licensed code.** Two projects were studied as design
references and neither was copied:

- **Unsloth Studio** (`studio/`) is licensed **AGPL-3.0-only**, with an SPDX header on
  every source file. Its *visual language* — a warm off-white page, a single accent
  colour, 8/12/16/20/24/28px radii, 15px body text — was studied and reimplemented from a
  written specification. No file, stylesheet, component or string was copied.
- **Cherry Studio** is **AGPL-3.0** with an additional term requiring a commercial licence
  for organisations above ten users. Its *architecture* — a server-side agent loop, a
  per-call approval gate, a per-tool renderer registry — was likewise reimplemented; no
  code was taken.

This matters because the product ships as a single static binary. Copying AGPL source
into it would make the whole distributed artifact AGPL. `docs/DESIGN_2.0.md` and
`docs/AGENT_WORKBENCH.md` record what was learned and what was built; the dependency
list below is what is actually linked.

## Runtime dependencies

Backend (Python): Tauri-sidecar FastAPI, Starlette (pinned deliberately — see
`backend/requirements.txt`), Uvicorn, Pydantic, psutil, HTTPX, `nvidia-ml-py`, pypdf,
pypdfium2, NumPy, PyYAML.

Shell (Rust): Tauri and `tauri-plugin-dialog`, Tokio, Reqwest, serde/serde_json, getrandom,
ring (HMAC-SHA256 for the readiness challenge), windows-sys.

Frontend (`ui/`, all MIT or Apache-2.0):

| Package | Licence | Used for |
|---|---|---|
| React / React DOM | MIT | UI runtime |
| Vite, `@vitejs/plugin-react` | MIT | build |
| Tailwind CSS, `@tailwindcss/vite` | MIT | styling |
| `@tanstack/react-query`, `@tanstack/react-virtual` | MIT | server state, list virtualization |
| zustand | MIT | UI state |
| `@radix-ui/react-*` | MIT | dialog, menu, tooltip, tabs primitives |
| `react-markdown`, `remark-gfm`, `remark-math`, `rehype-sanitize`, `rehype-katex`, KaTeX | MIT | model output rendering and sanitization |
| shiki | MIT | syntax highlighting |
| `lucide-react` | ISC | icons |
| sonner | MIT | toasts |
| `cmdk` | MIT | command palette |
| motion | MIT | animation |
| `clsx`, `tailwind-merge`, `class-variance-authority` | MIT | class composition |
| `@fontsource-variable/inter`, `@fontsource-variable/jetbrains-mono` | OFL-1.1 | bundled fonts |

Build and test tooling: PyInstaller, pytest, Pillow (icon generation only), Node.js, Rust,
Vitest, Playwright, axe-core. See `backend/requirements*.txt`, `src-tauri/Cargo.lock` and
`ui/package-lock.json` for the authoritative sets.

## Not distributed here

`llama-server.exe`, GGUF model files, WebView2, GPU drivers and the Python/Rust/Node
toolchains are not part of this source tree. Users are responsible for their licences and
for model terms. **NSIS is not a dependency**: the installer channel was removed on
2026-11-05 (`AGENTS.md` §4), so nothing here builds or ships an installer.

## Before publishing a binary

Review dependency licence metadata and attach a machine-generated SBOM or equivalent
inventory. A dependency security scan does not replace a licence review. Known-advisory
results are tracked in `DEPENDENCY_AUDIT.md`.
