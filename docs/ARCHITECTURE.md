# Architecture

## Component map

```text
Tauri desktop shell (src-tauri/)
  -> creates a per-launch session token and owns the backend Job Object
  -> starts only the bundled backend in packaged builds
  -> supervises the backend: crash -> close that generation's Job Object
     (reclaiming llama-server children) -> restart with the same session token
  -> exposes a narrow session/dialog bridge to the webview

FastAPI management backend (backend/)
  -> authenticates management REST/WebSocket traffic
  -> ProcessManager starts and stops llama-server process trees
  -> probes `llama-server --help` and GGUF headers (both cached per file
     identity) so parameters, defaults, and speculative-decoding options
     adapt to the selected build and model instead of a hardcoded snapshot
  -> writes instance lifecycle transitions to the rotating backend file log
  -> bounded history/log/config/preset/model-scan services persist local state

Static frontend (frontend/)
  -> authenticated API and WebSocket client
  -> refreshes the session and retries once on 401/network failure
  -> reconnects WebSockets with exponential backoff; bounded polling/rendering
  -> safe output display

llama-server.exe (user selected)
  -> model inference service, controlled through validated argv only
```

The Tauri process creates a random session ID and token, launches the bundled FastAPI backend, waits for a matching readiness response, and owns the backend process through a Windows Job Object. A supervisor thread reuses the same session when restarting a crashed backend, so the frontend can recover by refreshing its cached session and reconnecting; launcher-side restart events are appended to `%APPDATA%/LunarShroud/logs/launcher.log`. The browser UI obtains the session through a narrow Tauri command and authenticates REST requests and WebSockets.

FastAPI validates requests and delegates llama-server lifecycle work to `ProcessManager`. Each instance stores bounded metadata and logs, uses argv with `shell=False`, reserves its port during startup, and is terminated as a process tree. SQLite stores redacted launch history; JSON presets and configuration use atomic replacement.

Command construction never trusts a snapshot: `core/server_probe.py` parses the selected llama-server's own `--help`/`--version` output (cached by path, mtime and size), `core/gguf_probe.py` reads GGUF headers for model capabilities, and `core/capabilities.py` merges both into per-parameter support flags, upstream value lists, and default-omission decisions. Probing results are served read-only from cache on hot paths; a cold cache is warmed in a background thread and degrades gracefully.

This design is what makes non-official llama.cpp builds work without a per-fork compatibility matrix. Measured against a vendor fork shipped to run one specific model family: the fork parsed cleanly (408 options, `complete`, version and commit read correctly), flag support was resolved correctly including a real divergence from upstream (`--mmap` still present but deprecated in the fork, removed outright in the upstream build the same machine had), and the fork's custom-format model header was read with full geometry at `kv_source=declared`. The launcher emitted no flag the fork rejected.

What that does **not** cover is the readiness contract: the launcher decides "started" solely from `GET /health == 200`. Any build that serves a different health semantic, or binds differently than the probe assumes, will look like a startup timeout here. Probe attempts are recorded on the instance and included in the timeout message precisely so that class of mismatch is diagnosable rather than guessed at.

The frontend is static HTML/CSS/JavaScript. It uses bounded polling and rendering, validates chat URLs, and treats backend-provided text as untrusted. Tauri exposes only dialog and backend-session commands to the main window.

## Ownership boundaries

- `src-tauri/` owns desktop lifecycle, trusted packaged-backend selection, and
  Windows Job Object cleanup.
- `backend/` owns API contracts, lifecycle state, persistence, and command
  construction; it must not expose secrets in stored or displayed values.
- `frontend/` owns presentation only; it must not turn backend-provided text
  into HTML or bypass the session-authentication contract.
- `data/` belongs to the local user/runtime and is not source or a release
  input. `github/` is a separate curated source mirror.

For runtime placement and recovery see [HANDOVER.md](HANDOVER.md).
