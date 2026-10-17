$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $RepoRoot '.venv'

# rustup 把工具链装到 %USERPROFILE%\.cargo\bin，但已经打开的终端不会看到更新后的
# PATH（实测：装好之后 `cargo --version` 仍然是“命令不存在”）。这里显式补一次，
# 否则下面的 rustup / cargo 在全新机器上会直接失败。
$CargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
if (
    (Test-Path (Join-Path $CargoBin 'rustup.exe')) -and
    (($env:PATH -split ';') -notcontains $CargoBin)
) {
    $env:PATH = "$CargoBin;$env:PATH"
}

python -m venv $VenvPath
& (Join-Path $VenvPath 'Scripts\python.exe') -m pip install --upgrade pip
# 用哈希锁而不是 requirements-dev.txt：CI 与发布构建装的是同一份闭包，
# 全新 checkout 不会因为解析到不同的传递依赖而与发布产物产生偏差。
& (Join-Path $VenvPath 'Scripts\python.exe') -m pip install --require-hashes -r (Join-Path $RepoRoot 'backend\requirements-dev.lock')
rustup toolchain install 1.97.1 --profile minimal --component rustfmt,clippy
cargo install tauri-cli --version 2.11.4 --locked

Write-Host 'Development environment is ready. See README.md for test and run commands.'
