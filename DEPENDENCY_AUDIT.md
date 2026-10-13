# Dependency Audit Baseline

Audit date: 2026-08-17 (baseline) · revalidated: 2026-09-19.

## Python

`pip-audit 2.10.1 -r backend/requirements.txt` reported no known vulnerabilities for the resolved runtime dependency set.

Revalidated 2026-09-19 against the same pinned set: no known vulnerabilities.

Dev-only dependencies in `backend/requirements-dev.txt` (`pytest`, `pytest-cov`, `pyinstaller`, `ruff`, and since 2026-09-24 `Pillow` for `tools/gen_icons.py`) are outside this baseline: the scheduled audit runs `-r backend/requirements.txt` only. `Pillow` is deliberately not a runtime dependency and `backend.spec` excludes it from the packaged backend.

## Rust

`cargo-audit 0.22.2` scanned 468 locked crate dependencies against 1,216 RustSec advisories and reported no vulnerability findings. It reported 17 allowed warnings:

- GTK3/glib and `proc-macro-error` maintenance/unsoundness advisories are not present in the `x86_64-pc-windows-msvc` dependency tree; they remain in the cross-platform lockfile.
- Five unmaintained `unic-* 0.9.0` crates are present on Windows through Tauri's `urlpattern` dependency. No patched direct version is under this project's control; track upstream Tauri/urlpattern updates and re-audit on every dependency change.

Revalidated 2026-09-19: the refreshed advisory database reported two advisories that postdate the 2026-08-17 baseline — `h2` unbounded empty DATA frames (RUSTSEC-2026-0258) and a `rustls` TLS 1.3 handshake issue (RUSTSEC-2026-0285). Neither is reachable from this launcher (it only speaks plain HTTP to loopback addresses; no TLS and no HTTP/2 connections are made), but the lockfile was updated to the patched versions anyway: `h2 0.4.19`, `rustls 0.23.45`, `rustls-webpki 0.103.15`. A re-audit of the updated lockfile reports zero vulnerabilities and 7 allowed maintenance warnings (`proc-macro-error` unmaintained plus the `unic-*` family).

The two warning counts above are not comparable and are both correct: 17 is the whole-lockfile figure from the 2026-08-17 baseline, which includes GTK3/glib advisories that only exist in the cross-platform part of the lockfile, while 7 is what remains for the `x86_64-pc-windows-msvc` tree this project actually ships. Both numbers move whenever the advisory database or the lockfile moves, so read them off the audit output at the time of writing rather than copying the previous figure forward. `cargo audit --file src-tauri/Cargo.lock` is the command; the scheduled workflow runs it weekly.

These results are time-bound checks of known advisories, not a proof of security. The scheduled GitHub workflow refreshes both databases weekly.
