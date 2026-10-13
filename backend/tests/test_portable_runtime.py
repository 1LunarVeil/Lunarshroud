from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def test_portable_runtime_deployer_stages_matching_launcher_and_backend(tmp_path):
    """A desktop shortcut must never point at a launcher without its sidecar."""
    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    launcher_source = tmp_path / "release-launcher.exe"
    backend_source = tmp_path / "release-backend.exe"
    destination = tmp_path / "portable"
    launcher_source.write_bytes(b"new launcher")
    backend_source.write_bytes(b"new backend")

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-LauncherSource",
            str(launcher_source),
            "-BackendSource",
            str(backend_source),
            "-Destination",
            str(destination),
            "-SkipDesktopShortcut",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "LunarShroud.exe").read_bytes() == launcher_source.read_bytes()
    assert (destination / "backend" / "backend.exe").read_bytes() == backend_source.read_bytes()
    assert hashlib.sha256((destination / "LunarShroud.exe").read_bytes()).hexdigest() == hashlib.sha256(launcher_source.read_bytes()).hexdigest()
