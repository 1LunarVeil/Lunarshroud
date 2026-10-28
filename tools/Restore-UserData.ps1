#requires -Version 5.1
<#
.SYNOPSIS
Restore LunarShroud user data from a Backup-UserData.ps1 folder.

.DESCRIPTION
Verifies SHA256SUMS.txt first (fail closed), moves the current data directory
aside as LunarShroud.pre-restore-<timestamp>, then copies the backup into
place and verifies the restored files. The launcher must be closed first.

.PARAMETER BackupDirectory
Folder created by Backup-UserData.ps1 (must contain SHA256SUMS.txt).

.PARAMETER DataDirectory
LunarShroud data directory. Defaults to %APPDATA%\LunarShroud.

.PARAMETER Force
Allow restore while LunarShroud is running (not recommended; file locks will
usually make the rename fail anyway).
#>
param(
    [string]$BackupDirectory = "",
    [string]$DataDirectory = "",
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
    $running = @(Get-Process -Name "LunarShroud", "backend" -ErrorAction SilentlyContinue)
    if ($running -and -not $Force) {
        throw "LunarShroud or backend.exe is running. Close the app first, or pass -Force (not recommended)."
    }
}

function Resolve-BackupRelativePath {
    param([string]$Root, [string]$Relative)
    $normalized = $Relative.Replace('/', '\')
    $full = [System.IO.Path]::GetFullPath((Join-Path $Root $normalized))
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + '\'
    if (-not $full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "backup manifest path escaped the backup root: $Relative"
    }
    return $full
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

if (-not $BackupDirectory) {
    throw "pass -BackupDirectory (the folder created by Backup-UserData.ps1)."
}
$backup = [System.IO.Path]::GetFullPath($BackupDirectory)
$manifest = Join-Path $backup "SHA256SUMS.txt"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "SHA256SUMS.txt not found in $backup"
}
$data = Resolve-DataDirectory -Requested $DataDirectory
Assert-NotRunning

$entries = New-Object System.Collections.Generic.List[object]
foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
    if ($line -notmatch '^([0-9a-fA-F]{64}) \*(.+)$') {
        throw "invalid manifest line: $line"
    }
    $expected = $Matches[1].ToLowerInvariant()
    $relative = $Matches[2]
    $source = Resolve-BackupRelativePath -Root $backup -Relative $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "backup is missing a file listed in the manifest: $relative"
    }
    $actual = Get-Sha256 -Path $source
    if ($actual -ne $expected) {
        throw "checksum mismatch in backup: $relative"
    }
    $entries.Add([pscustomobject]@{ Relative = $relative; Hash = $expected })
}
if ($entries.Count -eq 0) {
    throw "backup manifest contains no files"
}

$preRestore = ""
if (Test-Path -LiteralPath $data) {
    $parent = Split-Path -Parent $data
    $leaf = Split-Path -Leaf $data
    $preRestore = Join-Path $parent ($leaf + ".pre-restore-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    if (Test-Path -LiteralPath $preRestore) {
        $preRestore = $preRestore + "-" + ([guid]::NewGuid().ToString("N").Substring(0, 6))
    }
    Rename-Item -LiteralPath $data -NewName (Split-Path -Leaf $preRestore)
}
New-Item -ItemType Directory -Path $data | Out-Null

Get-ChildItem -LiteralPath $backup -Force |
    Where-Object { $_.Name -ne "SHA256SUMS.txt" } |
    ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $data -Recurse -Force
    }

foreach ($entry in $entries) {
    $target = Resolve-BackupRelativePath -Root $data -Relative $entry.Relative
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "restore did not produce: $($entry.Relative)"
    }
    $actual = Get-Sha256 -Path $target
    if ($actual -ne $entry.Hash) {
        throw "checksum mismatch after restore: $($entry.Relative)"
    }
}

Write-Output "restored=$data"
Write-Output "backup=$backup"
Write-Output ("files=" + $entries.Count)
if ($preRestore) {
    Write-Output "pre_restore=$preRestore"
}
