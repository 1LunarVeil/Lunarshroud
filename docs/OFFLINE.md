# Offline contract

LunarShroud is a **local launcher**: once installed, every launcher feature must
work with no internet connection. This is a product invariant, not a best-effort
goal. The automated guard is `backend/tests/test_offline_contract.py`.

## What never needs the network

- Frontend assets: HTML, CSS, ES modules, icons and fonts are all shipped under
  `frontend/`. There are no CDN scripts, remote fonts or remote `@import`s.
- Backend startup, config, presets, history, model scanning, GGUF probing,
  runtime registration and capability probing are pure local operations.
- The UI talks to the backend over loopback (`127.0.0.1`); the backend talks to
  `llama-server` over loopback. The Tauri shell probes the backend readiness
  endpoint over loopback with redirects disabled and proxy environment ignored.
- No update checker, telemetry uploader, model downloader, or remote catalog is
  present. The launcher deliberately does not ship or fetch `llama-server`.
- Draft-model compatibility (B3) reads local GGUF metadata only: total
  vocabulary size plus the first 256 token strings, cached by path/mtime/size.
  A missing/unreadable signature is reported as `unknown`, never as a match.
- Native chat stores conversations in local `chats.db` and streams through the
  backend's loopback proxy. Markdown rendering is a shipped ES module; there is
  no remote chat, analytics or sync service.
- Backups are local files under `DATA_DIR/backups/`; restore is verified and
  applied at the next startup. No cloud backup or remote account is involved.

## The one external interaction

`frontend/js/help.js` contains links to llama.cpp releases and related public
documentation. They are ordinary user-clicked `<a target="_blank">` links that
open in the system browser. The app does not fetch them, and the UI remains
fully usable when they are unavailable.

## Local prerequisites (not network dependencies)

The user still needs:

- Windows WebView2 Runtime (usually present on Windows 11);
- a local `llama-server.exe` build matching the GPU;
- local GGUF model/draft/mmproj files;
- GPU drivers and CUDA/Vulkan runtime DLLs as required by that build.

These are files on the machine. The launcher's job is to detect missing files
and explain the problem locally, not to download them.

## Network policy in the Tauri shell

`src-tauri/tauri.conf.json` restricts runtime connections:

```text
connect-src 'self' ipc: http://ipc.localhost http://127.0.0.1:* ws://127.0.0.1:*
frame-src   'self' asset: https://asset.localhost http://127.0.0.1:*
```

`ipc:` / `http://ipc.localhost` are Tauri's in-process bridge, not a network
endpoint: the webview uses them only to call `get_backend_session`. Without
them WebView2 logs a CSP error on every invoke and Tauri silently falls back to
`postMessage`; the bridge is faster and keeps the console clean.

The only `reqwest` use in the Rust shell is the loopback readiness challenge;
proxy environment variables are ignored and redirects are disabled. The Python
backend health probe uses `httpx` with `trust_env=False` for the same reason.
The CSP and these calls are pinned by the offline contract test.

## If a future feature needs the network

It must be **opt-in, off by default, and never required for launch**. Any new
remote asset or import should fail the offline contract test first; update the
test and this document together only when the product decision is explicit.
