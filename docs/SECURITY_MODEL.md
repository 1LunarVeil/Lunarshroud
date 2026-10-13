# Security Model

## Trust boundaries

LunarShroud trusts the installed application files, the current Windows user, the user-selected `llama-server.exe`, and model files. It does not treat web content, imported presets, filesystem paths, REST bodies, WebSocket clients, or llama-server output as trusted.

"`llama-server.exe` is user-selected" is literal: the `llama_server_path` setting has **no default**. A non-empty default would let an implicit bare filename be resolved — and the first resolution candidate is the application directory, which is user-writable for both distribution modes (NSIS installs per-user under `%LOCALAPPDATA%`, the portable runtime lands wherever the user unpacked it). The launcher then probes that executable at every startup with no user action, so an unchosen default would silently become "execute whatever `llama-server.exe` is sitting in `<install>\backend`". With an empty default the launcher executes nothing until the user picks a path. Explicitly configured bare names are still resolved (application directory, then `PATH`), but that is then a real choice.

The launcher management API is always bound to `127.0.0.1`. Packaged sessions require a per-launch 256-bit random token. WebSockets carry the token as a subprotocol and check their Origin. Source-only unauthenticated development is explicit and cannot be enabled in a frozen backend.

llama-server may be bound beyond loopback only when an API key is supplied. That protects its HTTP API from unauthenticated network clients, but the key is necessarily passed to llama-server on its command line and may be visible to local processes running with the same Windows privileges.

## Residual risks

- Unsigned installers can be replaced or trigger SmartScreen warnings; use code signing for a trusted public release.
- A malicious or compromised user-selected llama-server executable has the user's privileges.
- Local administrators and same-user malware can inspect processes and application data.
- Dependency audits identify known advisories only; they cannot prove absence of vulnerabilities.
- Real model workloads may reveal resource-exhaustion behavior not covered by unit tests.
- The webview CSP allows `connect-src`/`frame-src` to any `127.0.0.1` port because instance ports are user-configurable; a compromised webview could therefore probe other loopback services.
- The management server does not cap concurrent connections or WebSocket count; a token-holding local client can attempt resource exhaustion.
- Request bodies are capped by declared `Content-Length` and, when absent, by a bounded stream read of the same limit; response sizes from llama-server itself are not capped by the launcher.
