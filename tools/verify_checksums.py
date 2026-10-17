#!/usr/bin/env python3
"""Verify the SHA-256 checksums shipped next to a release.

Usage::

    python tools/verify_checksums.py tmp/release-candidate-20261017
    python tools/verify_checksums.py path/to/SHA256SUMS.txt

Why this exists
---------------
Hash values are easy to transcribe wrong. The release checklist requires users
to compare the printed digest with ``SHA256SUMS.txt``; a report that retypes the
digest by hand can therefore send a user chasing a mismatch that does not exist
(round 5 verification found exactly that class of error). This tool reads the
file and the artifacts directly, so neither the report nor the user has to
retype anything.

Fail-closed: a missing file, an unreadable file, or a malformed line is a
failure. Empty checksum files are also a failure (nothing verified).

Pure standard library.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

CHUNK = 1024 * 1024


def parse_line(line: str) -> tuple[str, str] | None:
    """Parse ``<hex> *<name>`` (binary mode) or ``<hex>  <name>`` (text)."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"malformed checksum line: {line!r}")
    digest, name = parts[0].lower(), parts[1].strip()
    if name.startswith("*"):
        name = name[1:]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"not a SHA-256 digest: {parts[0]!r}")
    if not name:
        raise ValueError(f"checksum line has no filename: {line!r}")
    return digest, name


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_sums_path(target: Path) -> Path:
    if target.is_file():
        return target
    candidate = target / "SHA256SUMS.txt"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"no SHA256SUMS.txt under {target}")


def verify(target: Path) -> int:
    try:
        sums_path = resolve_sums_path(target)
        text = sums_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"FAIL: cannot read checksum file: {exc}", file=sys.stderr)
        return 1

    base = sums_path.parent
    checked = 0
    failures = 0
    for line in text.splitlines():
        try:
            parsed = parse_line(line)
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            failures += 1
            continue
        if parsed is None:
            continue
        expected, name = parsed
        artifact = base / name
        try:
            actual = sha256_file(artifact)
        except OSError as exc:
            print(f"FAIL  {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        checked += 1
        if actual == expected:
            print(f"OK    {name}")
        else:
            failures += 1
            print(f"FAIL  {name}\n      expected {expected}\n      actual   {actual}")

    if checked == 0:
        print("FAIL: checksum file contained no verifiable entries", file=sys.stderr)
        return 1
    if failures:
        print(f"\n{failures} failure(s); do not publish/install these artifacts", file=sys.stderr)
        return 1
    print(f"\nAll {checked} artifact(s) match {sums_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="directory containing SHA256SUMS.txt, or the checksum file itself",
    )
    args = parser.parse_args(argv)
    return verify(Path(args.target))


if __name__ == "__main__":
    raise SystemExit(main())
