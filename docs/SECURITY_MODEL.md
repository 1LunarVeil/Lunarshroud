# Security Model

## Trust boundaries

LunarShroud trusts the installed application files, the current Windows user, the user-selected `llama-server.exe`, and model files. It does not treat web content, imported presets, filesystem paths, REST bodies, WebSocket clients, or llama-server output as trusted.

"`llama-server.exe` is user-selected" is literal: the `llama_server_path` setting has **no default**. A non-empty default would let an implicit bare filename be resolved — and the first resolution candidate is the application directory, which is user-writable for both distribution modes (NSIS installs per-user under `%LOCALAPPDATA%`, the portable runtime lands wherever the user unpacked it). The launcher then probes that executable at every startup with no user action, so an unchosen default would silently become "execute whatever `llama-server.exe` is sitting in `<install>\backend`". With an empty default the launcher executes nothing until the user picks a path. Explicitly configured bare names are still resolved (application directory, then `PATH`), but that is then a real choice.

The launcher management API is always bound to `127.0.0.1`. Packaged sessions require a per-launch 256-bit random token. WebSockets carry the token as a subprotocol and check their Origin. Source-only unauthenticated development is explicit and cannot be enabled in a frozen backend.

llama-server may be bound beyond loopback only when an API key is supplied. That protects its HTTP API from unauthenticated network clients, but the key is necessarily passed to llama-server on its command line and may be visible to local processes running with the same Windows privileges.

The launcher sends `--cors-origins localhost` by default on builds that support it. Upstream's default is `*`, which lets an arbitrary website read responses from the local model API; the restricted value reflects only local Origins. Model defaults cannot override `cors_origins` (it is a managed key); the user can still change it explicitly in the parameter editor. Builds without `--cors-origins` keep the upstream default, which is reported by the capability schema.

Native chat does not talk to llama-server from the webview. It calls `POST /api/instances/{id}/chat/completions` on the authenticated management API; the backend injects the instance API key (params, or the last `--api-key` in the effective argv) and relays SSE. The key therefore never reaches the page, and the webview does not need a CORS exception.

Conversation text, local text attachments and saved prompts are stored locally in `chats.db` in plaintext under the user's data directory. Attachments are read by the webview from a user-selected file via the File API and are never uploaded to a service; the backend re-validates the name/text (NUL bytes rejected), recomputes the byte size and enforces per-file, per-message and per-conversation character bounds. Editing a message and branching a conversation are local SQLite operations; a branch is an independent copy that keeps the source thread and its attachments. This data is trusted no further than the current Windows user; users should treat exported Markdown and backups as sensitive if they paste secrets into chat or attach secret files.

Model manifests are optional files beside a user-selected GGUF. The in-app editor treats its input as untrusted, validates it with the normal bounded parser, writes atomically, and backs up the old file under `DATA_DIR/manifest-backups/`. It requires the model path to be an existing `.gguf` and refuses to create a second manifest format beside an existing one, so it cannot be used as an arbitrary-path file writer.

In-app backups live under `DATA_DIR/backups/` and contain config, history/chat snapshots and presets. They are ordinary local files with no encryption. Restore manifests are treated as untrusted: names are restricted, manifest paths are resolved and rejected if they escape the backup directory, every file hash is verified, and restore is applied only at the next backend start before SQLite opens. A pre-restore backup is created first; a failure leaves a `.failed.json` marker and `restore-result.json` for diagnosis.

## Residual risks

- Unsigned installers can be replaced or trigger SmartScreen warnings; use code signing for a trusted public release.
- A malicious or compromised user-selected llama-server executable has the user's privileges.
- Local administrators and same-user malware can inspect processes and application data.
- Dependency audits identify known advisories only; they cannot prove absence of vulnerabilities.
- Real model workloads may reveal resource-exhaustion behavior not covered by unit tests.
- The webview CSP allows `connect-src`/`frame-src` to any `127.0.0.1` port because instance ports are user-configurable; a compromised webview could therefore probe other loopback services.
- The management server does not cap concurrent connections or WebSocket count; a token-holding local client can attempt resource exhaustion.
- Request bodies are capped by declared `Content-Length` and, when absent, by a bounded stream read of the same limit; response sizes from llama-server itself are not capped by the launcher.
- Chat transcripts and exported Markdown are not encrypted; same-user malware and backups can read them.
