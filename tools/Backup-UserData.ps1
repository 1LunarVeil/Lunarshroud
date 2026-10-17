#requires -Version 5.1
<#
.SYNOPSIS
Back up LunarShroud user data (config, history, presets) with a hash manifest.

.DESCRIPTION
Copies config.json, history.db (and WAL/SHM sidecars if present) and the
presets directory into a timestamped folder, then writes SHA256SUMS.txt.
The launcher must be closed first; a running LunarShroud process is refused
unless -Force is supplied.

.PARAMETER DataDirectory
LunarShroud data directory. Defaults to %APPDATA%\LunarShroud.

.PARAMETER Destination
Backup folder. Defaults to .\LunarShroud-backup-<timestamp>.

.PARAMETER IncludeLogs
Also copy the logs directory. Logs are not needed to restore settings/history
and can be large, so they are excluded by default.

.PARAMETER Force
Overwrite an existing destination, or back up while LunarShroud is running.
#>
param(
    [string]$DataDirectory = "",
    [string]$Destination = "",
    [switch]$IncludeLogs,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Resolve-DataDirectory {
    param([string]$Requested)
    if ($Requested) {
        return [System.IO.Path]::GetFullPath($Requested)
    }
    if (-not $env:APPDATA) {
        throw "APPDATA is not set; pass -DataDirectory explicitly."
    }
    return [System.IO.Path]::GetFullPath((Join-Path $env:APPDATA "LunarShroud"))
}

function Assert-NotRunning {
    $running = Get-Process -Name "LunarShroud" -ErrorAction SilentlyContinue
    if ($running -and -not $Force) {
        throw "LunarShroud is running. Close it first, or pass -Force (not recommended)."
    }
}

function Get-RelativePath {
    param([string]$Root, [string]$FullPath)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $full = [System.IO.Path]::GetFullPath($FullPath)
    if (-not $full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "path escaped the backup root: $FullPath"
    }
    return $full.Substring($rootFull.Length).TrimStart('\', '/')
}

function Get-Sha256 {
    param([string]$Path)
    # Do not use Get-FileHash: a PowerShell 7 PSModulePath inherited by
    # powershell.exe 5.1 can make the Utility module fail to auto-load.
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

$data = Resolve-DataDirectory -Requested $DataDirectory
if (-not (Test-Path -LiteralPath $data -PathType Container)) {
    throw "data directory not found: $data"
}
Assert-NotRunning

if (-not $Destination) {
    $Destination = Join-Path (Get-Location).Path ("LunarShroud-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$dest = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $dest) {
    if (-not $Force) {
        throw "destination already exists: $dest (use -Force to overwrite)"
    }
    Remove-Item -LiteralPath $dest -Recurse -Force
}
New-Item -ItemType Directory -Path $dest | Out-Null

$copied = New-Object System.Collections.Generic.List[string]
foreach ($name in @("config.json", "history.db", "history.db-wal", "history.db-shm")) {
    $source = Join-Path $data $name
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $dest $name)
        $copied.Add($name)
    }
}
$presets = Join-Path $data "presets"
if (Test-Path -LiteralPath $presets -PathType Container) {
    Copy-Item -LiteralPath $presets -Destination (Join-Path $dest "presets") -Recurse
    $copied.Add("presets\")
}
if ($IncludeLogs) {
    $logs = Join-Path $data "logs"
    if (Test-Path -LiteralPath $logs -PathType Container) {
        Copy-Item -LiteralPath $logs -Destination (Join-Path $dest "logs") -Recurse
        $copied.Add("logs\")
    }
}

$manifest = Join-Path $dest "SHA256SUMS.txt"
$lines = New-Object System.Collections.Generic.List[string]
$files = Get-ChildItem -LiteralPath $dest -Recurse -File | Sort-Object FullName
foreach ($file in $files) {
    $relative = (Get-RelativePath -Root $dest -FullPath $file.FullName).Replace('\', '/')
    if ($relative -eq "SHA256SUMS.txt") { continue }
    $hash = Get-Sha256 -Path $file.FullName
    $lines.Add("$hash *$relative")
}
if ($lines.Count -eq 0) {
    throw "nothing to back up in $data"
}
$lines | Set-Content -LiteralPath $manifest -Encoding ASCII

# Verify the manifest before reporting success (fail closed).
foreach ($line in Get-Content -LiteralPath $manifest) {
    if ($line -notmatch '^([0-9a-fA-F]{64}) \*(.+)$') {
        throw "invalid manifest line: $line"
    }
    $expected = $Matches[1].ToLowerInvariant()
    $relative = $Matches[2]
    $target = Join-Path $dest ($relative.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "manifest references a missing file: $relative"
    }
    $actual = Get-Sha256 -Path $target
    if ($actual -ne $expected) {
        throw "checksum mismatch after backup: $relative"
    }
}

Write-Output "backup=$dest"
Write-Output ("files=" + ($lines.Count))
Write-Output ("items=" + ($copied -join ","))
