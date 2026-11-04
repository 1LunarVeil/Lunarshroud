# Release Checklist

There are two distribution channels. The **portable zip is required**; the NSIS
installer is optional per release (decided 2026-10-28, see `AGENTS.md` §4).
**1.0.0 ships the portable channel only.**

| Channel | Artifact | Audience | Shipped in 1.0.0 |
|---|---|---|---|
| Portable | `LunarShroud-<version>-portable-x64.zip` | unzip and run, no install | **Yes** |
| Installer | `LunarShroud_<version>_x64-setup.exe` | per-user NSIS install | No — buildable, not validated |

When both are produced they come out of `.github/workflows/release.yml` in the
same job from the same sources, so they can never be built from different
revisions. The portable zip is the **main** channel: it needs no install
permission and works under sandboxed or restricted accounts. The 1.0.0 installer
was built and byte-scanned but has **not** been through an install → start →
uninstall round trip, which is why it is not published.

1. Verify repository settings, enable private vulnerability reporting, and configure branch protection.
2. Run CI and dependency-security workflows from a clean checkout.
3. Run product-code coverage, frontend tests plus non-empty syntax enumeration, Rust format/Clippy/tests, and a real Windows install/start/close/uninstall smoke test.
4. Perform at least one real llama-server start/chat/stop cycle. For a stable release, add 4–8 hour soak results across representative hardware.
5. Review `CHANGELOG.md`, licenses, dependency inventory/SBOM, and `SANITIZATION_REPORT.md`.
6. Build with the release workflow or the pinned commands in `BUILDING.md` (including the `RUSTFLAGS` path remap); never reuse an old `backend.exe`.
7. Byte-scan the built launcher, backend sidecar, and installer for builder identity — user names, `C:\Users` paths, local directories; all three must scan clean before anything is published.
8. Compute and publish SHA-256 checksums for **both** artifacts. Code-sign when a certificate is available. Until then, every download must be verified by the user before running:

   ```powershell
   Get-FileHash .\LunarShroud-1.0.0-portable-x64.zip -Algorithm SHA256
   Get-FileHash .\LunarShroud_1.0.0_x64-setup.exe  -Algorithm SHA256
   ```

   Compare the printed values with `SHA256SUMS.txt` (published with the release) or the checksum in the release notes. A mismatch means the file is not the published artifact — do not run it.

   Do not retype hashes into reports or release notes; read them from the file. The repository ships a verifier that parses `SHA256SUMS.txt` and recomputes every digest (non-zero exit on any missing/corrupt artifact):

   ```powershell
   python tools\verify_checksums.py .\release-candidate-directory
   ```
9. Upload both artifacts, the checksum file, changelog/release notes, and the SBOM/inventory intended for users.

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

