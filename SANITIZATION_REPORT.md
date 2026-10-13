# Sanitization Report

Prepared on 2026-08-17 from an explicit source allowlist.

- Executables, installers, archives, logs, databases, caches, coverage artifacts, virtual environments, screenshots, and runtime data: none included.
- Private-key/API-token patterns and development-machine absolute paths: no matches in the final source tree.
- Included binary assets: one project logo and four Tauri icon files, documented in `ASSETS.md`.
- User-created presets: excluded; only the three built-in presets are included.
- Source parity: all copied files matched the reviewed working source by SHA-256 before final placement.
- The portable-runtime deployment utility is generic, accepts explicit source/destination paths, validates SHA-256 using the .NET runtime, and has a regression test that does not touch a real desktop.
- Repository-owner and unfinished-work placeholder scan: no markers remain.

The dependency-audit workflows run separately because a clean source tree is not proof that third-party packages have no known advisory. This report is not a Git-history audit: the supplied workspace contained no `.git` history.

The local dependency baseline is recorded in `DEPENDENCY_AUDIT.md`.

## 2026-08-23 revalidation

After current handover, status, history, and documentation-map files were
added, the public-source tree was rechecked. It contains 86 files (1,421,754
bytes); no executable, installer, archive, database, log, cache, build-output,
or temporary directory was present. Its only `data/` content is the three
reviewed built-in preset JSON files. Markdown links resolved, and the mirrored
current documents matched their reviewed source copies by SHA-256.

After the same-day backend-supervision and frontend-recovery changes, the
mirror was refreshed and rescanned again: still 86 files (1,447,125 bytes),
every file byte-identical to its reviewed working-tree source, and no new
forbidden file, local absolute path, or secret pattern introduced.

## 2026-09-19 revalidation

- Public-source mirror: 131 files, byte-identical to the reviewed working
  tree per `tools/check_release_mirror.py` (5-category gate: missing, drifted,
  stale, forbidden paths, private-path leakage). Note: built-in preset
  templates were removed since the 2026-08-23 report — the launcher no longer
  ships any preset, and the mirror contains no `data/` content at all. The
  "three built-in presets" statements above describe those earlier snapshots.
- Plugin state exclusion: the security-scan hook created `.mimosa/` session
  state (containing machine paths) inside the workspace during one session,
  and a mirror `--apply` briefly copied it into the staging tree. The gate's
  private-path check caught it immediately; `.mimosa` is now excluded by the
  gate's rule table and `.gitignore`, and the mirrored copy was removed.
- Built release artifacts byte-scanned for builder identity (user name,
  `C:\Users` paths, model directories, plugin state, workspace path):
  `backend\backend.exe` and the NSIS installer were clean; the launcher
  initially embedded 316 cargo-registry paths containing the build machine's
  user name via Rust panic location strings. Fixed by remapping
  `%USERPROFILE%\.cargo\registry\src` to a neutral prefix at build time
  (`RUSTFLAGS=--remap-path-prefix=...`), now encoded in the release workflow
  and build instructions. All three rebuilt artifacts scan clean.
