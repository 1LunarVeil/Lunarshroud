# Source Preparation Report

This directory is prepared as the canonical public source package, not as a dump
of the development workspace.

- Included: application source, tests, lockfiles, icons, CI/release workflows,
  portable-runtime deployment and user-data backup/restore tooling, license,
  security policy, contribution guidance, and build documentation.
- Excluded: executables/installers, caches, coverage databases, screenshots,
  runtime configuration/history/logs, temporary environments, local helper
  scripts with machine paths, internal audit documents, and model files. The
  exclusions are enforced by `tools/check_release_mirror.py`, not by memory.
- Upstream relationship: no upstream fork provenance was identified in the
  available workspace. If the project is later based on another repository,
  record the upstream URL and commit here before publishing.
- Git history: the public tree is its own repository, initialized from the final
  mirror. Preparation-time unreachable objects were purged with
  `git gc --prune=now`; the tree has been scanned for credentials and local
  identity by the release-mirror gate and by an independent scan.
