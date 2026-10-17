# Release Checklist

1. Verify repository settings, enable private vulnerability reporting, and configure branch protection.
2. Run CI and dependency-security workflows from a clean checkout.
3. Run product-code coverage, frontend tests plus non-empty syntax enumeration, Rust format/Clippy/tests, and a real Windows install/start/close/uninstall smoke test.
4. Perform at least one real llama-server start/chat/stop cycle. For a stable release, add 4–8 hour soak results across representative hardware.
5. Review `CHANGELOG.md`, licenses, dependency inventory/SBOM, and `SANITIZATION_REPORT.md`.
6. Build with the release workflow or the pinned commands in `BUILDING.md` (including the `RUSTFLAGS` path remap); never reuse an old `backend.exe`.
7. Byte-scan the built launcher, backend sidecar, and installer for builder identity — user names, `C:\Users` paths, local directories; all three must scan clean before anything is published.
8. Compute and publish SHA-256 checksums. Code-sign production installers when a certificate is available. Until a certificate exists, every download must be verified by the user before running:

   ```powershell
   Get-FileHash .\LunarShroud_0.9.0-beta.2_x64-setup.exe -Algorithm SHA256
   ```

   Compare the printed value with `SHA256SUMS.txt` (published with the release) or the checksum in the release notes. A mismatch means the file is not the published artifact — do not install it.

   Do not retype hashes into reports or release notes; read them from the file. The
   repository ships a verifier that parses `SHA256SUMS.txt` and recomputes every
   digest (non-zero exit on any missing/corrupt artifact):

   ```powershell
   python tools\verify_checksums.py .\release-candidate-directory
   ```
9. Upload only the installer, checksum, changelog/release notes, SBOM/inventory, and signature material intended for users.

The current beta is not code-signed and has not completed the full hardware/soak matrix; do not describe it as 1.0-stable.
