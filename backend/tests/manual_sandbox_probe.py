"""Adversarial probe: try to escape the agent path sandbox.

Every case here is a Windows-specific shape that a string-prefix check would let
through. This is a probe, not a test — it prints what happens so the result can be read
before deciding whether a test is warranted. Anything that escapes is a real finding.

Run:  python backend/tests/manual_sandbox_probe.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import chat_tools  # noqa: E402

root = Path(tempfile.mkdtemp(prefix="sandbox-root-"))
outside = Path(tempfile.mkdtemp(prefix="sandbox-outside-"))
(root / "inside.txt").write_text("inside", encoding="utf-8")
(outside / "secret.txt").write_text("secret", encoding="utf-8")

print(f"root    = {root}")
print(f"outside = {outside}\n")

# A junction inside the root that points outside it. `mklink /J` needs no admin rights
# on Windows, which is why it matters: an attacker does not need symlink privilege.
junction = root / "escape"
junction_made = False
try:
    import subprocess

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        shell=False,
    )
    junction_made = result.returncode == 0
except OSError:
    junction_made = False
print(f"junction created: {junction_made}")

CASES = [
    ("plain inside", "inside.txt"),
    ("dot-dot escape", "../secret.txt"),
    ("nested dot-dot", "a/../../secret.txt"),
    ("absolute posix", "/etc/passwd"),
    ("absolute windows", "C:\\Windows\\win.ini"),
    ("drive relative", "D:foo"),
    ("UNC", "\\\\server\\share\\x"),
    ("forward UNC", "//server/share/x"),
    ("via junction", "escape/secret.txt"),
    ("trailing dot", "..\\secret.txt."),
    ("trailing space", "..\\secret.txt "),
    ("ntfs ads", "inside.txt:evil"),
    ("8.3 short name", "inside~1.txt"),
    ("device name", "CON"),
    ("device name path", "NUL"),
    ("uppercase escape", "..\\SECRET.TXT"),
    ("mixed separators", "..\\/secret.txt"),
    ("double dot no sep", "....//secret.txt"),
    ("percent-encoded", "..%2Fsecret.txt"),
    ("uri form", "file:///C:/Windows/win.ini"),
    ("extended path", "\\\\?\\C:\\Windows\\win.ini"),
    ("device namespace", "\\\\.\\C:\\Windows\\win.ini"),
    ("dot only", "."),
    ("empty", ""),
]

print("\n--- resolve verdict (wrapped as read_file would) ---")
escaped: list[str] = []
for label, candidate in CASES:
    try:
        target = chat_tools.resolve_inside(root, candidate)
        inside = True
        try:
            target.relative_to(root.resolve())
        except ValueError:
            inside = False
        verdict = "INSIDE" if inside else "ESCAPED"
        if not inside:
            escaped.append(label)
        print(f"{verdict:8} {label:22} -> {target}")
    except chat_tools.ToolError as error:
        print(f"REJECT   {label:22} -> {error}")

print("\n--- read attempt through the junction, if it exists ---")
if junction_made:
    try:
        result = chat_tools.read_file(root, "escape/secret.txt")
        print("ESCAPED: read_file returned the outside file:", result["content"][:20])
        escaped.append("read_file via junction")
    except chat_tools.ToolError as error:
        print("REJECT  read_file via junction ->", error)

print("\n--- write attempt through the junction, if it exists ---")
if junction_made:
    from core import agent_writes

    try:
        result = agent_writes.write_file(root, "escape/written.txt", "pwned")
        print("ESCAPED: write_file wrote outside:", result)
        escaped.append("write_file via junction")
    except agent_writes.AgentToolError as error:
        print("REJECT  write_file via junction ->", error)

print(f"\nescaped cases: {escaped if escaped else 'none'}")
