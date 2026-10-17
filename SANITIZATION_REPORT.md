# Sanitization Report

Last reviewed: 2026-10-17 (Asia/Shanghai), release candidate `0.9.0-beta.2`.

This report describes the **current** public-source mirror. Earlier sections of
this file described snapshots with 86 and 131 files and, at one point, built-in
preset templates; those snapshots are historical and no longer describe the
tree.

## Current state

- The public tree is `github/`, a curated mirror — not a copy of the private
  working directory. `tools/check_release_mirror.py` is the gate and the only
  supported way to refresh it (`--apply`), and it is fail-closed: missing,
  drifted, stale, forbidden and privacy-unscanned files all fail the check.
- At this revision the mirror contains 154 source and documentation files. It
  contains no executables, installers, archives, logs, databases, coverage
  artifacts, virtual environments, screenshots, build outputs or runtime data.
- `data/` is excluded as an entire directory: `config.json`, `history.db` (and
  its `-wal`/`-shm` sidecars), `logs/` and `presets/`. No preset ships with the
  launcher; presets are created by the user.
- Internal audit documents, maintainer-local history, AI coding-tool state and
  ignore files are deliberately excluded; the reasons live in
  `CURATED_EXCLUDES` in the gate, not in this document.
- The privacy scan covers raw bytes decoded as UTF-8, UTF-16LE, UTF-16BE and
  latin-1, plus gzip/zlib containers. The current mirror and the work-tree files
  that would be published both scan clean for the maintainer's workspace/user
  markers and for `C:\Users` paths.
- The public Git repository was initialized from this final tree. Unreachable
  objects from preparation (including a pre-sanitization draft) were removed
  with `git gc --prune=now`; `git fsck --unreachable` reports nothing before
  publication.
- Binary assets are the project logo and the Tauri icon set, documented in
  `ASSETS.md`; they are generated/checked by `tools/gen_icons.py` and
  `backend/tests/test_icons.py`.

## Scope limits

This is a source-tree sanitization report, not a dependency audit. Known
advisory results are in `DEPENDENCY_AUDIT.md`; licensing notes are in
`THIRD_PARTY_NOTICES.md`. It is not a substitute for reviewing the final commit
that is actually published.
