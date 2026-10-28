#!/usr/bin/env python3
"""Verify the SHA-256 checksums shipped next to a release.

Usage::

    python tools/verify_checksums.py tmp/release-candidate-20261017
    python tools/verify_checksums.py path/to/SHA256SUMS.txt
    python tools/verify_checksums.py --generate tmp/release-candidate-20261017

Fail-closed: a missing file, an unreadable file, or a malformed line is a
failure. Empty checksum files are also a failure (nothing verified). So is a
release-shaped file in the directory that the checksum file does not cover --
that is how a stale previous build gets published by mistake.

Why this exists
---------------
Hash values are easy to transcribe wrong. The release checklist requires users
to compare the printed digest with ``SHA256SUMS.txt``; a report that retypes the
digest by hand can therefore send a user chasing a mismatch that does not exist
(round 5 verification found exactly that class of error). This tool reads the
file and the artifacts directly, so neither the report nor the user has to
retype anything.

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


def release_artifacts(base: Path) -> list[Path]:
    """该目录里"长得像发布物"的文件。用于发现没被清单覆盖的产物。"""
    suffixes = {".exe", ".msi", ".zip", ".7z", ".dmg", ".AppImage", ".deb", ".rpm"}
    return sorted(
        path
        for path in base.iterdir()
        if path.is_file()
        and path.suffix.lower() in suffixes
        and path.name.upper() != "SHA256SUMS.TXT"
    )


def generate(base: Path, names: list[str]) -> int:
    """按目录里**实际存在**的产物写 SHA256SUMS.txt。

    为什么要有这个子命令：校验和是手抄/手改的，而安装包会在
    之后被重新构建 —— 实测 ``SHA256SUMS.txt`` 是 10-18 21:20 写的，beta.2 的
    setup.exe 是 10-20 19:46 重建的，两者从此不再一致。按发布清单核对的用户
    会得到"文件被篡改"的假警报，发布者也可能带着错误的校验和发布。
    生成必须发生在**最后一次构建之后**，所以它得是一条命令，而不是一段流程说明。
    """
    if names:
        artifacts = []
        for name in names:
            path = base / name
            if not path.is_file():
                print(f"FAIL: artifact not found: {path}", file=sys.stderr)
                return 1
            artifacts.append(path)
    else:
        artifacts = release_artifacts(base)
    if not artifacts:
        print(f"FAIL: no release artifacts under {base}", file=sys.stderr)
        return 1

    lines = [f"{sha256_file(path)}  {path.name}" for path in artifacts]
    target = base / "SHA256SUMS.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    for line in lines:
        print(f"OK    wrote {line}")
    print(f"\nWrote {len(lines)} entry(s) to {target}")
    return 0


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
    parsed_entries: list[tuple[str, str]] = []
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
        # 连"文件读不到"的条目也要算已登记，否则下面的未覆盖检查会把
        # 一个清单里有、但产物被删掉的目录重复报两次。
        parsed_entries.append((expected, name))
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

    # 目录里有产物没被清单覆盖，同样是失败：
    # 清单只校验"列出来的"，于是上一个 beta 的 setup.exe 留在同一个目录里，
    # 核对全绿、发布时却上传了错的那一个。
    listed = {name for _, name in parsed_entries}
    unlisted = [p.name for p in release_artifacts(base) if p.name not in listed]
    if unlisted:
        print(
            "FAIL: artifact(s) present but not covered by "
            f"{sums_path.name}: {', '.join(unlisted)}",
            file=sys.stderr,
        )
        print(
            "      regenerate the checksum file after the final build, or remove "
            "the stale artifact from the release directory",
            file=sys.stderr,
        )
        failures += 1

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
    parser.add_argument(
        "--generate",
        action="store_true",
        help="write SHA256SUMS.txt from the artifacts actually present, instead of "
        "verifying. Run this after the FINAL build (see --artifact to pick which).",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME",
        help="with --generate: include only this artifact (repeatable). "
        "Default is every release-shaped file in the directory.",
    )
    args = parser.parse_args(argv)
    target = Path(args.target)
    if args.generate:
        base = target if target.is_dir() else target.parent
        return generate(base, args.artifact)
    return verify(target)


if __name__ == "__main__":
    raise SystemExit(main())
