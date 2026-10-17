# Maintenance tools

The routine deployment tool is `Publish-PortableRuntime.ps1`. It copies and
hash-verifies the launcher and backend sidecar, then updates the Desktop
shortcut. Relative `-Destination` / `-BackupDirectory` values are resolved
against PowerShell's current location, not .NET's current directory — the two
can differ after `Set-Location`, and `-Destination .` once deployed into
`src-tauri\` by mistake.

### User-data backup and restore

Close LunarShroud first. Both scripts run under Windows PowerShell 5.1:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Backup-UserData.ps1
# => .\LunarShroud-backup-<timestamp>\ with SHA256SUMS.txt

powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\Restore-UserData.ps1 `
    -BackupDirectory .\LunarShroud-backup-20261017-203000
```

Backup copies `config.json`, `history.db` (plus WAL/SHM sidecars) and
`presets\`; logs are optional via `-IncludeLogs`. Restore verifies the manifest
before touching anything, renames the current data directory to
`LunarShroud.pre-restore-<timestamp>`, then copies and re-verifies. Do not use
`Get-FileHash` in these scripts: PowerShell 5.1 launched from a PowerShell 7
environment can inherit a `PSModulePath` where the Utility module fails to
auto-load; the tools use .NET SHA-256 instead.

### Release mirror gate (`check_release_mirror.py`)

Maintainer-facing: this gate compares the working tree against the staging tree
that the public repository is published from. Both trees have to exist, so in a
plain clone it exits with an error and there is nothing for a contributor to run.

The staging tree is hand-maintained, and it has already drifted once (it sat at a
pre-i18n snapshot while the workspace moved on). This gate turns "what the
published tree must contain" into one reproducible command:

```powershell
py -X utf8 tools\check_release_mirror.py          # exit code 1 if anything is off
py -X utf8 tools\check_release_mirror.py --apply  # refresh the mirror, same rules
```

It fails on five kinds of problems: a worktree file missing from the mirror, a
content hash mismatch, a stale mirror-only file, a forbidden path inside the
mirror (`data/` in its entirety, `*.bak*`, `*.exe`, `tmp/`, `__pycache__/`, …)
and a local absolute path leaking into mirrored text.

Pure standard library. The check and `--apply` share one rule table on purpose:
two rule sets would eventually disagree ("gate says clean, diff says otherwise").

Two properties of that rule table matter more than the rest, because both were
real leaks rather than hypothetical ones:

- Runtime data is excluded by **whole directory**, not by enumerated filename.
  SQLite in WAL mode produces `history.db-wal`/`-shm` beside `history.db`, and a
  damaged config is rebuilt into `config.json.corrupt` containing the user's
  absolute model paths. Derived filenames cannot be enumerated, so any
  per-file allowlist eventually misses one — and `--apply` then *copies* it into
  the published tree while still reporting clean.
- The local-path scan reads every mirrored file rather than a fixed suffix
  allowlist. A suffix allowlist silently skips anything it does not recognize
  (`.corrupt`, `.db-wal`), which is exactly the class of file that carries
  private paths. Genuine binaries are skipped by the UTF-8 decode failure
  instead, so the allowlist added no safety — only a blind spot.

If you add a new kind of local artifact, extend the rule table. Do not rely on
the scan to notice: it only judges files that already reached the staging tree.

`smoke_test.py` and `lifecycle_test.py` are repeatable backend/API and
process-lifecycle diagnostic helpers. The other Python tools support manual UI,
CDP, window, dialog, and iframe diagnosis and are not part of the automated
release gate.

`ui_preview.py` is the frontend visual check: it drives a headless Chrome/Edge
over CDP, opens a page, clicks through a few steps, captures a screenshot and
reports console errors. Use it before and after any UI change so the result can
be reviewed instead of imagined:

```powershell
.\.venv\Scripts\python.exe backend\main.py            # 前端由同一个进程静态托管
python tools\ui_preview.py --out tmp\shots\spec.png `
    --page param_editor --expand speculative --full-page
```

It needs no Tauri window: `frontend/js/api.js` already degrades to
`location.origin` outside Tauri, so a plain Chromium can exercise the whole UI.

Screenshots alone are not enough — a downscaled thumbnail hides clipping and
contrast problems. Pair them with `--probe` to read computed styles or
`scrollHeight`, and treat the numbers as authoritative. Windows PowerShell
mangles embedded double quotes in native-command arguments, so run the tool
with `py --%` when a probe contains quotes.

### CSS cascade audit (`css_cascade_probe.js`)

Screenshots also cannot verify `:hover`, `:focus`, toasts or modals — precisely
the states a CSS de-duplication refactor breaks. Use the cascade probe instead:
it dumps every selector's *effective* declarations plus a duplicate-selector
report. The output must be byte-identical before and after such a refactor.

```powershell
# 1) 改动前留基线
py --% -X utf8 tools/ui_preview.py --page param_editor `
    --probe @tools/css_cascade_probe.js --probe-out tmp/cascade_before.txt
# 2) 改动后对比（第一行必须是 NO_DUPLICATES）
py --% -X utf8 tools/ui_preview.py --page param_editor `
    --probe @tools/css_cascade_probe.js --probe-out tmp/cascade_after.txt
Compare-Object (Get-Content tmp/cascade_before.txt) (Get-Content tmp/cascade_after.txt)
```

One-off scripts and icon-investigation tools from earlier debugging are kept by
the maintainer outside this repository, so they stay recoverable locally without
obscuring the supported maintenance path documented here.
