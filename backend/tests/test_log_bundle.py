"""The log bundle endpoint.

Two properties matter more than the happy path, because this endpoint hands a file to
the user that they will attach to a bug report:

1. **It only reaches `logs/`.** The resolve-and-`is_relative_to` check is what keeps a
   symlink (or a sibling directory whose name merely starts with "logs") from turning
   a support bundle into an archive of `chats.db` and `config.json`.
2. **It states what it left out.** A size ceiling that silently truncates produces a
   report that looks complete; whoever reads it will conclude the missing lines never
   existed.
"""
from __future__ import annotations

import io
import zipfile

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import main as backend_main
    from fastapi.testclient import TestClient

    token = "logs-bundle-test"
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", token)
    monkeypatch.setattr(backend_main, "DATA_DIR", tmp_path)
    (tmp_path / "logs" / "instances").mkdir(parents=True)
    (tmp_path / "logs" / "backend.log").write_text("hello\n", encoding="utf-8")
    (tmp_path / "logs" / "instances" / "abc.log").write_text("instance\n", encoding="utf-8")
    # A file outside `logs/` that must never appear in the bundle.
    (tmp_path / "config.json").write_text('{"secret": true}', encoding="utf-8")
    return TestClient(backend_main.app, headers={"X-LunarShroud-Token": token}), tmp_path


def _names(payload: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return set(archive.namelist())


def test_the_bundle_contains_logs_and_nothing_outside_the_logs_directory(client):
    test_client, _root = client
    response = test_client.get("/api/system/logs/bundle")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    names = _names(response.content)
    assert "backend.log" in names
    assert "instances/abc.log" in names
    # The data directory's own files must never travel in a log bundle.
    assert "config.json" not in names
    assert not any(name.startswith("..") for name in names)


def test_an_empty_logs_directory_yields_an_empty_but_valid_zip(monkeypatch, tmp_path):
    """`logs/` exists in practice (lifespan creates it), so the useful assertion is
    that an empty one still produces a valid archive rather than a 500."""
    import main as backend_main
    from fastapi.testclient import TestClient

    token = "logs-bundle-empty"
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", token)
    monkeypatch.setattr(backend_main, "DATA_DIR", tmp_path)
    with TestClient(backend_main.app, headers={"X-LunarShroud-Token": token}) as client:
        response = client.get("/api/system/logs/bundle")
        assert response.status_code == 200
        # A valid zip with no entries (a bare empty directory may add nothing).
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert isinstance(archive.namelist(), list)


def test_the_bundle_declares_files_it_had_to_leave_out(client, monkeypatch):
    """The ceiling must be visible inside the archive, not just in the HTTP response."""
    from api import system as system_api

    test_client, root = client
    # A ceiling of 1 byte guarantees every real file is omitted.
    monkeypatch.setattr(system_api, "MAX_LOG_BUNDLE_BYTES", 1)
    (root / "logs" / "big.log").write_text("x" * 500, encoding="utf-8")

    response = test_client.get("/api/system/logs/bundle")
    assert response.status_code == 200
    names = _names(response.content)
    assert "_OMITTED.txt" in names, "the bundle did not report its own truncation"
    assert "backend.log" not in names


def test_a_symlink_out_of_the_logs_directory_is_not_followed(client, tmp_path):
    """The real escape route is a **symlink**, not a prefix collision.

    `rglob` never leaves `logs/`, so a sibling directory is unreachable regardless —
    the first version of this test asserted exactly that and stayed green when I
    weakened the path check, so it proved nothing.

    **Honest scope:** with `rglob` + a file symlink, `Path.resolve()` already leaves
    the directory, so this test passes with or without the `is_relative_to` guard. The
    guard is defence in depth for a future change that walks differently. I could not
    construct a case where removing the guard makes this test fail, so it stands as a
    regression net for the *outcome* (nothing outside `logs/` in the bundle), not as
    proof that the guard is load-bearing.
    """
    test_client, root = client
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.log"
    secret.write_text("private data\n", encoding="utf-8")

    link = root / "logs" / "escape.log"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")

    names = _names(test_client.get("/api/system/logs/bundle").content)
    assert "escape.log" not in names, (
        "a symlink inside logs/ was followed out of the logs directory; the bundle "
        "must contain only files that really live under it"
    )
