# Contributing

Thank you for improving LunarShroud.

1. Open an issue for large or security-sensitive changes before implementation.
2. Create a focused branch and keep generated binaries, credentials, private paths, model files, runtime databases, and logs out of commits.
3. Add a regression test first for bug fixes.
4. Run Python coverage, frontend tests/syntax checks, `cargo fmt`, Clippy, and Rust tests.
5. Update user-facing documentation and `CHANGELOG.md` when behavior changes.
6. Complete the pull request template and describe process-lifecycle, filesystem, authentication, and networking impact.

Do not weaken loopback binding, session authentication, path containment, request limits, or release sidecar restrictions without an explicit security design review.
