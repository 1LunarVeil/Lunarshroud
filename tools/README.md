# Tools

Every script here runs from a plain clone with nothing but Python (or Windows
PowerShell 5.1) — no local GUI automation, no debug port, no machine-specific
paths.

## Deploy a portable runtime

`Publish-PortableRuntime.ps1` is the routine deployment tool. It copies and
hash-verifies the launcher and the backend sidecar, then updates the Desktop
shortcut.

Relative `-Destination` / `-BackupDirectory` values are resolved against
PowerShell's current *location*, not .NET's current directory — the two can
differ after `Set-Location`, and `-Destination .` once deployed into
`src-tauri\` by mistake.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Publish-PortableRuntime.ps1 `
    -Destination . -BackupDirectory .\tmp\portable-backups
```

## Package the portable zip

`package_portable.py` stages the launcher, `backend/backend.exe` and
`backend/_internal/**` into `release/`, zips it, then reads the finished archive
back and inspects it. The copy step is a whitelist and the finished zip is
evidence — anything that survives both is reported and the zip is deleted
(exit 5). It refuses to run when `tauri.conf.json` and `Cargo.toml` disagree on
the version.

```powershell
python tools\package_portable.py
```

## Verify published checksums

```powershell
python tools\verify_checksums.py .\release
```

Parses `SHA256SUMS.txt` and recomputes every digest. Non-zero exit on a missing
or corrupt artifact. Give users this command; do not retype hashes into release
notes.

## User-data backup and restore

Close LunarShroud first. Both scripts run under Windows PowerShell 5.1:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Backup-UserData.ps1
# => .\LunarShroud-backup-<timestamp>\ with SHA256SUMS.txt

powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Restore-UserData.ps1 `
    -BackupDirectory .\LunarShroud-backup-20261017-203000
```

Backup copies `config.json`, `history.db`, `chats.db` (messages, local text
attachments, saved prompts, plus their WAL/SHM sidecars) and `presets\`; logs are
optional via `-IncludeLogs`.

The in-app Settings → data backup panel uses the same manifest format and stores
snapshots under `DATA_DIR\backups\`; it snapshots SQLite through its backup API
and applies the restore at the next backend start. Logs are not restored by the
in-app path. Restore verifies the manifest before touching anything, renames the
current data directory to `LunarShroud.pre-restore-<timestamp>`, then copies and
re-verifies.

## Run the backend from source with isolated data

`dev_backend.py` points `DATA_DIR` at a fresh temp directory before importing the
backend, so a source-run instance never touches your real chats, history or
config. Use it instead of `python backend/main.py` whenever you are testing:

```bash
LUNARSHROUD_ALLOW_INSECURE_DEV=1 LUNARSHROUD_LAUNCHER_PORT=17591 \
    python tools/dev_backend.py
```

## Regenerate application icons

`gen_icons.py` renders the launcher icon set from `src-tauri/icons/artwork.png`.

## Release mirror gate (maintainer)

`check_release_mirror.py` compares the working tree against the staging tree the
public repository is published from. Both trees have to exist, so in a plain
clone it exits with an error and there is nothing for a contributor to run.

```powershell
py -X utf8 tools\check_release_mirror.py          # exit code 1 if anything is off
py -X utf8 tools\check_release_mirror.py --apply  # refresh the mirror, same rules
```

It fails on five kinds of problems: a worktree file missing from the mirror, a
content hash mismatch, a stale mirror-only file, a forbidden path inside the
mirror, and a local absolute path leaking into mirrored text.

Pure standard library. The check and `--apply` share one rule table on purpose:
two rule sets would eventually disagree.

Two properties of that rule table matter more than the rest, because both were
real leaks rather than hypothetical ones:

- Runtime data is excluded by **whole directory**, not by enumerated filename.
  SQLite in WAL mode produces `history.db-wal`/`-shm` beside `history.db`, and a
  damaged config is rebuilt into `config.json.corrupt` containing absolute model
  paths. Derived filenames cannot be enumerated, so a per-file allowlist
  eventually misses one — and `--apply` then *copies* it while reporting clean.
- The local-path scan reads every mirrored file rather than a fixed suffix
  allowlist. A suffix allowlist silently skips anything it does not recognize
  (`.corrupt`, `.db-wal`), which is exactly the class of file that carries
  private paths.

If you add a new kind of local artifact, extend the rule table. Do not rely on
the scan to notice: it only judges files that already reached the staging tree.

## Not published here

GUI, window, dialog, iframe and WebView2 debugging scripts stay in the
maintainer's tree. They need pyautogui, a specific window title, or a running
WebView2 debug port, so they do not run on another machine. The automated
equivalents are `backend/tests/` and `frontend/tests/browser/`.
