# Release Checklist

There is **one** distribution channel: the portable zip. The NSIS installer
channel was **removed** on 2026-11-05 (see `AGENTS.md` §4) — not parked, not
"optional per release". Nothing in the repository builds, packages, signs, or
publishes an installer any more, and the release workflow fails if one appears.

| Channel | Artifact | Audience |
|---|---|---|
| Portable | `LunarShroud-<version>-portable-x64.zip` | unzip and run, no install |

Why the installer went away: it targeted exactly the same audience as the
portable zip, but carried three extra chains — code signing, uninstall hooks,
and install-directory permissions under sandboxed/restricted accounts — while
the only acceptance that matters for it (install → start → uninstall on a clean
machine) was never doable here. Two distribution paths, one of them
unverifiable, is a net risk rather than a net feature.

The portable zip needs no install permission and works under sandboxed or
restricted accounts. It is produced by `.github/workflows/release.yml` from a
single job, and that job builds the launcher with `--no-bundle`.

1. Verify repository settings, enable private vulnerability reporting, and configure branch protection.
2. Run CI and dependency-security workflows from a clean checkout.
3. Run product-code coverage, frontend tests plus non-empty syntax enumeration, Rust format/Clippy/tests, and a real Windows start/close/uninstall smoke test on the portable build.
4. Perform at least one real llama-server start/chat/stop cycle. For a stable release, add 4–8 hour soak results across representative hardware.
5. Review `CHANGELOG.md`, licenses, dependency inventory/SBOM, and `SANITIZATION_REPORT.md`.
6. Build with the release workflow or the pinned commands in `BUILDING.md` (including the `RUSTFLAGS` path remap); never reuse an old `backend.exe`.
7. Byte-scan the built launcher and backend sidecar for builder identity — user names, `C:\Users` paths, local directories; both must scan clean before anything is published.
8. Compute and publish the SHA-256 checksum for the zip. Code-sign when a certificate is available. Until then, every download must be verified by the user before running:

   ```powershell
   Get-FileHash .\LunarShroud-1.0.0-portable-x64.zip -Algorithm SHA256
   ```

   Compare the printed value with `SHA256SUMS.txt` (published with the release) or the checksum in the release notes. A mismatch means the file is not the published artifact — do not run it.

   Do not retype hashes into reports or release notes; read them from the file. The repository ships a verifier that parses `SHA256SUMS.txt` and recomputes every digest (non-zero exit on any missing/corrupt artifact):

   ```powershell
   python tools\verify_checksums.py .\release-candidate-directory
   ```
9. Upload the zip, the checksum file, changelog/release notes, and the SBOM/inventory intended for users.

## Gates that run automatically

| Gate | Where | What it prevents |
|---|---|---|
| Version consistency | `test_regressions.py::test_release_version_is_consistent_across_manifests` | tauri / Cargo / Cargo.lock / CHANGELOG / README drifting apart |
| Packager keeps no private version copy | `test_packaging_version_source.py` | a zip whose name says 1.0.0 while it contains 0.9 |
| Packager copies runtime only | same file | source, `tests/`, `.mimosa/` or **user `data/`** entering the package |
| Finished zip is inspected | same file + packager self-check | any leak that survived the copy step (exit 5, zip deleted) |
| Portable zip is byte-reproducible | packager fixed entry timestamps | an unusable checksum because rebuilding changes bytes |
| Launcher is byte-reproducible | `release.yml` `-C link-arg=/Brepro` | a different shell hash on every build |
| Release mirror | `tools/check_release_mirror.py` | private paths or drift leaking into the public mirror |

## What the portable package may contain

Exactly this, nothing else:

```text
LunarShroud.exe
backend/backend.exe
backend/_internal/...        (the PyInstaller onedir runtime)
LICENSE
README.md
使用说明.txt
```

Anything else is a bug. The list is enforced by a **whitelist** on the copy step
and then re-checked by reading the finished zip back, because the copy step is a
promise and the finished archive is evidence.

## What is verified, and what is not

Verified for this release:

- The shell reports `FileVersion 1.0.0`, matching `tauri.conf.json`; the
  packager refuses to build when the binary's embedded version disagrees.
- **The launcher is byte-reproducible** when built with
  `-C link-arg=/Brepro`: `SOURCE_DATE_EPOCH` alone leaves the PE timestamp
  tracking the wall clock, so two builds then differ. Re-verify with two builds
  before claiming reproducibility for a new revision; record both digests in the
  release notes.
- The portable zip is byte-reproducible: two consecutive `package_portable.py`
  runs produced the identical digest (`5e7249ae…` at 1.0.0).
- The portable zip is self-contained — **771 entries**: `LunarShroud.exe`,
  `backend/backend.exe`, 766 files under `backend/_internal/`, plus `LICENSE`,
  `README.md` and `使用说明.txt`. No `tests/`, no `.mimosa`, no `__pycache__`,
  no user data.
- The zip was extracted and launched at 1.0.0: healthy start, `401` without a
  token, `GET /api/system/config` returning an empty `api_server_key`, and the
  data directory isolated inside the extraction root.
- All three shipped artifacts scan clean for builder identity (no user-profile
  path, no user name, no workspace name; ASCII and UTF-16LE both scanned).
- `verify_checksums.py` recomputes both published digests independently.

**Not verified, and this must not be described as verified:**

- Code signing: no certificate. Windows SmartScreen will warn on first run.
- Installer install / start / close / uninstall round trip on a clean machine.
- The 4–8 hour soak matrix across GPU vendors.

