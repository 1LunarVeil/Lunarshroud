# Security Policy

## Supported versions

Security fixes currently target the latest source revision and the latest beta release only.

## Reporting a vulnerability

Please use GitHub's private security advisory form after the repository is created. Do not open a public issue for a suspected vulnerability and do not include API keys, private paths, models, or user data in reports.

Include the affected version, impact, reproduction steps, and a minimal proof of concept. Maintainers should acknowledge a report within 7 days and provide a remediation plan or status update within 30 days. These are response targets, not a guarantee.

## Security posture

- The launcher management API is loopback-only and authenticated in packaged builds.
- Insecure source development requires the explicit `LUNARSHROUD_ALLOW_INSECURE_DEV=1` opt-in.
- Release builds fail if their bundled backend is missing; release fallback to CWD/PATH is disabled.
- Secrets are redacted before logging, persistence, and API display.

See `docs/SECURITY_MODEL.md` for residual risks and trust boundaries.
