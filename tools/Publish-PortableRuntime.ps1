[CmdletBinding()]
param(
    # 刻意**不在参数默认值里**用 $PSScriptRoot：Windows PowerShell 5.1 在求值
    # param() 默认值时该变量还是空的（只有 PowerShell 7 里有值），Join-Path 会收到
    # 空字符串并立刻抛 ParameterArgumentValidationErrorEmptyStringNotAllowed ——
    # 也就是脚本第一步就死。CI 用的是 shell: pwsh 所以从没暴露，但在任何只有系统
    # 自带 PowerShell 的机器上手动跑都会失败。改成进 body 后再解析，两种 shell 一致。
    [string]$LauncherSource,
    [string]$BackendSource,
    [Parameter(Mandatory)]
    [string]$Destination,
    [string]$BackupDirectory,
    [switch]$SkipDesktopShortcut
)

if (-not $LauncherSource) {
    $LauncherSource = Join-Path $PSScriptRoot '..\src-tauri\target\release\lunarshroud.exe'
}
if (-not $BackendSource) {
    # onedir 构建：backend.exe 与同目录的 _internal/ 是一个整体，不能只复制 exe。
    $BackendSource = Join-Path $PSScriptRoot '..\backend\dist\backend\backend.exe'
}

$ErrorActionPreference = 'Stop'

function Resolve-UserPath([string]$Path) {
    # Do not use [IO.Path]::GetFullPath($Path) alone for relative paths: .NET's
    # current directory can differ from PowerShell's current location after
    # Set-Location, so "-Destination ." once deployed into src-tauri instead of
    # the workspace root. Resolve relative paths against Get-Location.
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Path))
}

function Get-VerifiedPath([string]$Path, [string]$Label) {
    $resolved = Resolve-UserPath $Path
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

function Assert-SameTree([string]$Source, [string]$Destination, [string]$Label) {
    $root = [System.IO.Path]::GetFullPath($Source)
    foreach ($file in Get-ChildItem -LiteralPath $Source -Recurse -File) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/')
        $target = Join-Path $Destination $relative
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "$Label is missing $relative"
        }
        Assert-SameHash $file.FullName $target "$Label/$relative"
    }
}

function Copy-TreeVerified([string]$Source, [string]$Destination, [string]$Label) {
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
    Assert-SameTree $Source $Destination $Label
}

$launcher = Get-VerifiedPath $LauncherSource 'Launcher source'
$backend = Get-VerifiedPath $BackendSource 'Backend source'
$backendRuntimeSource = Split-Path -Parent $backend
$internalSource = Join-Path $backendRuntimeSource '_internal'
$destinationRoot = Resolve-UserPath $Destination
$launcherDestination = Join-Path $destinationRoot 'LunarShroud.exe'
$backendDestination = Join-Path $destinationRoot 'backend\backend.exe'
$internalDestination = Join-Path $destinationRoot 'backend\_internal'

if ($launcher -eq $launcherDestination -or $backend -eq $backendDestination) {
    throw 'Source and destination must be different paths.'
}

New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $backendDestination) -Force | Out-Null

if ($BackupDirectory -and ((Test-Path -LiteralPath $launcherDestination) -or (Test-Path -LiteralPath $backendDestination))) {
    $backupRoot = Join-Path (Resolve-UserPath $BackupDirectory) (Get-Date -Format 'yyyyMMdd-HHmmss')
    New-Item -ItemType Directory -Path (Join-Path $backupRoot 'backend') -Force | Out-Null
    if (Test-Path -LiteralPath $launcherDestination) {
        Copy-Item -LiteralPath $launcherDestination -Destination (Join-Path $backupRoot 'LunarShroud.exe') -Force
    }
    if (Test-Path -LiteralPath $backendDestination) {
        Copy-Item -LiteralPath $backendDestination -Destination (Join-Path $backupRoot 'backend\backend.exe') -Force
    }
    if (Test-Path -LiteralPath $internalDestination) {
        Copy-Item -LiteralPath $internalDestination -Destination (Join-Path $backupRoot 'backend\_internal') -Recurse -Force
    }
}

$launcherStage = "$launcherDestination.next"
$backendStage = "$backendDestination.next"
$internalStage = "$internalDestination.next"
Copy-Item -LiteralPath $launcher -Destination $launcherStage -Force
Copy-Item -LiteralPath $backend -Destination $backendStage -Force
if (Test-Path -LiteralPath $internalSource -PathType Container) {
    Copy-TreeVerified $internalSource $internalStage 'Backend runtime staging'
} elseif (Test-Path -LiteralPath $internalStage) {
    Remove-Item -LiteralPath $internalStage -Recurse -Force
}

try {
    Assert-SameHash $launcher $launcherStage 'Launcher staging'
    Assert-SameHash $backend $backendStage 'Backend staging'
    Copy-Item -LiteralPath $launcherStage -Destination $launcherDestination -Force
    Copy-Item -LiteralPath $backendStage -Destination $backendDestination -Force
    if (Test-Path -LiteralPath $internalStage -PathType Container) {
        if (Test-Path -LiteralPath $internalDestination) {
            Remove-Item -LiteralPath $internalDestination -Recurse -Force
        }
        Move-Item -LiteralPath $internalStage -Destination $internalDestination
        Assert-SameTree $internalSource $internalDestination 'Backend runtime deployment'
    } elseif (Test-Path -LiteralPath $internalDestination) {
        # 切换回 onefile 时清掉旧 onedir 运行时，避免留下不匹配的 _internal。
        Remove-Item -LiteralPath $internalDestination -Recurse -Force
    }
    Assert-SameHash $launcher $launcherDestination 'Launcher deployment'
    Assert-SameHash $backend $backendDestination 'Backend deployment'
}
finally {
    Remove-Item -LiteralPath $launcherStage,$backendStage -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $internalStage -Recurse -Force -ErrorAction SilentlyContinue
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
