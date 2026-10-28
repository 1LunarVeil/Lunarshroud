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
    [switch]$SkipDesktopShortcut,
    # 本机找不到签名证书时签名本来就是"跳过"，所以默认开启没有副作用；
    # 测试用假 exe（不是合法 PE）时必须显式跳过，否则签名会失败并中断部署。
    [switch]$SkipSigning,
    # 维护者明确知道应用已关闭时仍可覆盖；默认拒绝正在运行的安装。
    [switch]$AllowRunning
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

function Get-CodeSigningCertificate {
    # 本机自签证书（2026-10-22 建立）。刻意按 Subject 匹配而不是写死指纹：
    # 指纹会随证书重建而变化，写死之后换一次证书就要改脚本。
    # 找不到不是错误——别人 clone 下来本来就没有这张证书。
    try {
        return Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction Stop |
            Where-Object {
                $_.Subject -like '*CN=LunarShroud Local Build*' -and $_.HasPrivateKey
            } |
            Select-Object -First 1
    } catch {
        Write-Warning "could not query the code-signing certificate store: $($_.Exception.Message)"
        return $null
    }
}

function Protect-Artifact([string]$Path, $Certificate) {
    $result = Set-AuthenticodeSignature -FilePath $Path -Certificate $Certificate -HashAlgorithm SHA256
    if ($result.Status -ne 'Valid') {
        throw "code signing failed for $Path : $($result.Status) $($result.StatusMessage)"
    }
    Write-Host "Signed: $Path"
}

function Invoke-OptionalSigning([string]$LauncherPath, [string]$BackendPath) {
    # 签名发生在**复制之前**（签源文件）。这一点是刻意的：部署脚本全程靠
    # SHA-256 校验"暂存副本 == 源文件"，如果先校验再改字节，校验结论立刻就过期了。
    # 签源文件则暂存、部署、回滚三处的哈希始终一致。
    #
    # 只签外壳 exe，不签 backend.exe：backend 是外壳 spawn 的子进程，SmartScreen
    # 只对用户主动启动的可执行文件生效；而给 it 加签会改动它的哈希，徒增风险。
    if ($SkipSigning) {
        Write-Host 'Code signing skipped (-SkipSigning).'
        return
    }
    $cert = Get-CodeSigningCertificate
    if (-not $cert) {
        Write-Warning ('No local code-signing certificate found; deploying UNSIGNED. ' +
            'Windows SmartScreen will warn on first run for every recipient.')
        return
    }
    Write-Host "Code signing with: $($cert.Subject) [$($cert.Thumbprint)]"
    Protect-Artifact $LauncherPath $cert

    # 安装包是用户真正双击的那个文件，同样要签；签完必须重算校验和，否则就
    # 复现了"清单写于最终构建之前"那个缺陷（校验和与产物不一致）。
    $bundleDir = Join-Path (Split-Path -Parent $LauncherPath) 'bundle\nsis'
    if (-not (Test-Path -LiteralPath $bundleDir -PathType Container)) {
        return
    }
    $installers = @(Get-ChildItem -LiteralPath $bundleDir -Filter '*-setup.exe' -File -ErrorAction SilentlyContinue)
    if (-not $installers) { return }
    foreach ($installer in $installers) {
        Protect-Artifact $installer.FullName $cert
    }
    $sums = Join-Path $bundleDir 'SHA256SUMS.txt'
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Warning "python not found; regenerate $sums before publishing (it is now stale)."
        return
    }
    $regenerate = Join-Path $PSScriptRoot 'verify_checksums.py'
    foreach ($installer in $installers) {
        & $python.Source $regenerate --generate $bundleDir --artifact $installer.Name | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "could not regenerate $sums after signing $($installer.Name)"
        }
    }
    Write-Host "Regenerated $sums"
}

$launcher = Get-VerifiedPath $LauncherSource 'Launcher source'
$backend = Get-VerifiedPath $BackendSource 'Backend source'
$backendRuntimeSource = Split-Path -Parent $backend
$internalSource = Join-Path $backendRuntimeSource '_internal'
if (-not (Test-Path -LiteralPath $internalSource -PathType Container)) {
    throw "Backend runtime is incomplete: '$internalSource' is missing. Run PyInstaller backend.spec (onedir) before deploying."
}
if (@(Get-ChildItem -LiteralPath $internalSource -Recurse -File).Count -eq 0) {
    throw "Backend runtime is incomplete: '$internalSource' contains no files."
}
$destinationRoot = Resolve-UserPath $Destination
$launcherDestination = Join-Path $destinationRoot 'LunarShroud.exe'
$backendDestination = Join-Path $destinationRoot 'backend\backend.exe'
$internalDestination = Join-Path $destinationRoot 'backend\_internal'

if ($launcher -eq $launcherDestination -or $backend -eq $backendDestination) {
    throw 'Source and destination must be different paths.'
}

if (-not $AllowRunning) {
    $running = @(Get-Process -Name "LunarShroud", "backend" -ErrorAction SilentlyContinue)
    if ($running) {
        throw 'LunarShroud or backend.exe is running. Close it (including tray mode) before deploying, or pass -AllowRunning explicitly.'
    }
}

# 在暂存/校验之前签源文件，保证后续所有 SHA-256 比对比较的是同一份字节。
Invoke-OptionalSigning $launcher $backend

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

function Clear-StalePath([string]$Path) {
    # 只用于 .next / .rollback 这种"本脚本自己造的暂存件"。上一次崩溃留下的残留
    # 会让 Copy-Item -Destination <目录> 把文件复制进目录里，于是本次哈希校验又
    # 失败 —— 一次故障变成永久故障。清理失败只警告：它不是本次部署的错因，
    # 不该掩盖真正的异常。
    if (Test-Path -LiteralPath $Path) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force
        } catch {
            Write-Warning "could not clear stale path $Path : $($_.Exception.Message)"
        }
    }
}

function Restore-RollbackSlot([string]$Rollback, [string]$Destination) {
    # 按**每一格**独立恢复。三段是逐个 Move-Item 的，中途失败时可能只有 launcher
    # 进了 .rollback，而 backend / _internal 的目的地仍是完好的旧文件。
    # 旧实现在 catch 里无条件删掉三个目的地，把从没动过的那两格也一起删了；
    # 而 staging 校验失败发生在旧文件移动**之前**，.rollback 根本不存在，
    # 于是"新的没装上、旧的也没了"（实测目标目录只剩 .next 残留）。
    # .rollback 不存在 = 这一格从没被动过 = 什么都不许碰。
    if (-not (Test-Path -LiteralPath $Rollback)) {
        return
    }
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $Rollback -Destination $Destination -ErrorAction SilentlyContinue
}

$launcherRollback = "$launcherDestination.rollback"
$backendRollback = "$backendDestination.rollback"
$internalRollback = "$internalDestination.rollback"

function Recover-InterruptedSwap([string]$Rollback, [string]$Destination) {
    # A previous run may have moved the only good copy into .rollback and then
    # crashed before installing the new one. Recover it before staging; never
    # clear a rollback slot that is the only surviving copy.
    if (-not (Test-Path -LiteralPath $Rollback)) { return }
    if (Test-Path -LiteralPath $Destination) {
        Clear-StalePath $Rollback
        return
    }
    Move-Item -LiteralPath $Rollback -Destination $Destination -ErrorAction Stop
    Write-Host "Recovered interrupted deployment slot: $Destination"
}
foreach ($pair in @(
    @($launcherRollback, $launcherDestination),
    @($backendRollback, $backendDestination),
    @($internalRollback, $internalDestination)
)) {
    Recover-InterruptedSwap -Rollback $pair[0] -Destination $pair[1]
}

$launcherStage = "$launcherDestination.next"
$backendStage = "$backendDestination.next"
$internalStage = "$internalDestination.next"
foreach ($path in @($launcherStage, $backendStage, $internalStage)) {
    Clear-StalePath $path
}
Copy-Item -LiteralPath $launcher -Destination $launcherStage -Force
Copy-Item -LiteralPath $backend -Destination $backendStage -Force
Copy-TreeVerified $internalSource $internalStage 'Backend runtime staging'

$swapped = $false

try {
    Assert-SameHash $launcher $launcherStage 'Launcher staging'
    Assert-SameHash $backend $backendStage 'Backend staging'

    # 三段一起换入：任何一步失败都把旧的三段整体恢复。只换 _internal 而留下
    # 新 launcher/backend 会产生版本不匹配；没有回滚则可能得到"新 exe + 缺
    # 运行时"的不可启动组合。运行中的 exe 无法改名，先让用户关闭应用。
    foreach ($path in @($launcherRollback, $backendRollback, $internalRollback)) {
        Clear-StalePath $path
    }
    if (Test-Path -LiteralPath $launcherDestination) {
        Move-Item -LiteralPath $launcherDestination -Destination $launcherRollback
    }
    if (Test-Path -LiteralPath $backendDestination) {
        Move-Item -LiteralPath $backendDestination -Destination $backendRollback
    }
    if (Test-Path -LiteralPath $internalDestination) {
        Move-Item -LiteralPath $internalDestination -Destination $internalRollback
    }

    Move-Item -LiteralPath $launcherStage -Destination $launcherDestination
    Move-Item -LiteralPath $backendStage -Destination $backendDestination
    Move-Item -LiteralPath $internalStage -Destination $internalDestination

    Assert-SameHash $launcher $launcherDestination 'Launcher deployment'
    Assert-SameHash $backend $backendDestination 'Backend deployment'
    Assert-SameTree $internalSource $internalDestination 'Backend runtime deployment'
    $swapped = $true
}
catch {
    # 逐格恢复旧对。恢复本身失败也不吞掉、不覆盖原始异常：那些 .rollback 文件
    # 仍在原地，用户还能手动搬回去。
    Restore-RollbackSlot $launcherRollback $launcherDestination
    Restore-RollbackSlot $backendRollback $backendDestination
    Restore-RollbackSlot $internalRollback $internalDestination
    throw
}
finally {
    if ($swapped) {
        # 只有换入并逐文件校验通过，旧的三段才成为冗余。失败路径上它们是用户
        # 唯一的原件，绝不允许在这里被删。
        foreach ($path in @($launcherRollback, $backendRollback, $internalRollback)) {
            Clear-StalePath $path
        }
    }
    foreach ($path in @($launcherStage, $backendStage, $internalStage)) {
        Clear-StalePath $path
    }
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
