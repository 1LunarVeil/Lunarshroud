$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $RepoRoot '.venv'

python -m venv $VenvPath
& (Join-Path $VenvPath 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $VenvPath 'Scripts\python.exe') -m pip install -r (Join-Path $RepoRoot 'backend\requirements-dev.txt')
rustup toolchain install 1.97.1 --profile minimal --component rustfmt,clippy
cargo install tauri-cli --version 2.11.4 --locked

Write-Host 'Development environment is ready. See README.md for test and run commands.'
