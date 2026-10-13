[CmdletBinding()]
param(
    [string]$LauncherSource = (Join-Path $PSScriptRoot '..\src-tauri\target\release\lunarshroud.exe'),
    [string]$BackendSource = (Join-Path $PSScriptRoot '..\backend\dist\backend.exe'),
    [Parameter(Mandatory)]
    [string]$Destination,
    [string]$BackupDirectory,
    [switch]$SkipDesktopShortcut
)

$ErrorActionPreference = 'Stop'

function Get-VerifiedPath([string]$Path, [string]$Label) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label does not exist: $resolved"
    }
    return $resolved
}

function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '')
    }
    finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

function Assert-SameHash([string]$Expected, [string]$Actual, [string]$Label) {
    $expectedHash = Get-Sha256 $Expected
    $actualHash = Get-Sha256 $Actual
    if ($expectedHash -ne $actualHash) {
        throw "$Label hash verification failed."
    }
}

$launcher = Get-VerifiedPath $LauncherSource 'Launcher source'
$backend = Get-VerifiedPath $BackendSource 'Backend source'
$destinationRoot = [System.IO.Path]::GetFullPath($Destination)
$launcherDestination = Join-Path $destinationRoot 'LunarShroud.exe'
$backendDestination = Join-Path $destinationRoot 'backend\backend.exe'

if ($launcher -eq $launcherDestination -or $backend -eq $backendDestination) {
    throw 'Source and destination must be different paths.'
}

New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $backendDestination) -Force | Out-Null

if ($BackupDirectory -and ((Test-Path -LiteralPath $launcherDestination) -or (Test-Path -LiteralPath $backendDestination))) {
    $backupRoot = Join-Path ([System.IO.Path]::GetFullPath($BackupDirectory)) (Get-Date -Format 'yyyyMMdd-HHmmss')
    New-Item -ItemType Directory -Path (Join-Path $backupRoot 'backend') -Force | Out-Null
    if (Test-Path -LiteralPath $launcherDestination) {
        Copy-Item -LiteralPath $launcherDestination -Destination (Join-Path $backupRoot 'LunarShroud.exe') -Force
    }
    if (Test-Path -LiteralPath $backendDestination) {
        Copy-Item -LiteralPath $backendDestination -Destination (Join-Path $backupRoot 'backend\backend.exe') -Force
    }
}

$launcherStage = "$launcherDestination.next"
$backendStage = "$backendDestination.next"
Copy-Item -LiteralPath $launcher -Destination $launcherStage -Force
Copy-Item -LiteralPath $backend -Destination $backendStage -Force

try {
    Assert-SameHash $launcher $launcherStage 'Launcher staging'
    Assert-SameHash $backend $backendStage 'Backend staging'
    Copy-Item -LiteralPath $launcherStage -Destination $launcherDestination -Force
    Copy-Item -LiteralPath $backendStage -Destination $backendDestination -Force
    Assert-SameHash $launcher $launcherDestination 'Launcher deployment'
    Assert-SameHash $backend $backendDestination 'Backend deployment'
}
finally {
    Remove-Item -LiteralPath $launcherStage,$backendStage -Force -ErrorAction SilentlyContinue
}

if (-not $SkipDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktop 'LunarShroud.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $launcherDestination
    $shortcut.WorkingDirectory = $destinationRoot
    $shortcut.Description = 'LunarShroud llama.cpp desktop launcher'
    $shortcut.IconLocation = "$launcherDestination,0"
    $shortcut.Save()
}

Write-Host "Portable runtime deployed: $launcherDestination"
