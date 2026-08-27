import base64
import gzip
import hashlib
import sys
from pathlib import Path

import pytest

import archive_toolkit as toolkit


def digest(data):
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


def test_arc_payload_extracted():
    payload = b"\x00original\xffbytes"
    arc = (
        b"http://example.test/a 127.0.0.1 20120101000000 text/plain 100\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
        + payload
    )
    assert toolkit.extract_arc_payload(gzip.compress(arc)) == payload
    assert digest(payload) == toolkit.bytes_digest(payload)


def test_url_path_safe():
    assert toolkit.url_relative_path("https://example.com/") == "index.html"
    assert toolkit.url_relative_path("https://example.com/high%20school.htm") == "high school.htm"
    with pytest.raises(ValueError):
        toolkit.url_relative_path("https://example.com/../outside.txt")


def test_existing_not_overwritten(tmp_path):
    target = Path(tmp_path) / "page.htm"
    target.write_bytes(b"wayback version")
    assert not toolkit.write_new_bytes(target, b"common crawl version")
    assert target.read_bytes() == b"wayback version"


def test_same_bytes_reused(tmp_path):
    target = Path(tmp_path) / "page.htm"
    target.write_bytes(b"same bytes")
    assert toolkit.write_new_bytes(target, b"same bytes")
    assert target.read_bytes() == b"same bytes"


def test_arc_trailing_newline_not_in_payload():
    """색인 길이가 레코드 구분 개행까지 셀 때 그 한 바이트는 원본이 아니다."""
    payload = b"<html>body</html>"
    arc = (
        b"http://example.test/a 127.0.0.1 20120101000000 text/html 200\n"
        b"HTTP/1.1 200 OK\r\n\r\n" + payload + b"\n"
    )
    got = toolkit.extract_arc_payload(gzip.compress(arc))
    assert got == payload + b"\n"
    assert got[:-1] == payload


def test_data_root_priority(tmp_path, monkeypatch):
    env_root = tmp_path / "environment"
    cli_root = tmp_path / "command"
    monkeypatch.setenv("ARCHIVES_DATA_ROOT", str(env_root))

    assert toolkit.resolve_data_root(cli_root) == cli_root.resolve()
    assert toolkit.resolve_data_root() == env_root.resolve()

    monkeypatch.delenv("ARCHIVES_DATA_ROOT")
    assert toolkit.resolve_data_root() == (toolkit.ROOT / "data").resolve()


def test_site_paths_scoped(tmp_path):
    paths = toolkit.site_paths("sample", data_root=tmp_path)

    assert paths["base"] == tmp_path.resolve() / "sites" / "sample"
    assert paths["files"] == tmp_path.resolve() / "sites" / "sample" / "files"
    assert paths["captures"] == tmp_path.resolve() / "sites" / "sample" / "captures"
    assert paths["inventory"] == tmp_path.resolve() / "sites" / "sample" / "inventory.json"
    assert paths["viewer"] == tmp_path.resolve() / "sites" / "sample" / "viewer.html"


@pytest.mark.parametrize("command", ["survey", "download", "view", "verify"])
def test_cli_root_forwarded(command, tmp_path, monkeypatch):
    received = {}
    root = tmp_path / "external"

    def record_paths(site_id, data_root=None):
        received["root"] = data_root
        base = Path(data_root) / "sites" / site_id
        return {
            "base": base,
            "files": base / "files",
            "captures": base / "captures",
            "inventory": base / "inventory.json",
            "viewer": base / "viewer.html",
        }

    monkeypatch.setattr(toolkit, "site_paths", record_paths)
    monkeypatch.setattr(toolkit, "survey_site", lambda *args, **kwargs: None)
    monkeypatch.setattr(toolkit, "restore_domain", lambda *args, **kwargs: None)
    monkeypatch.setattr(toolkit, "generate_viewer", lambda *args, **kwargs: None)
    monkeypatch.setattr(toolkit, "verify_site", lambda *args, **kwargs: None)

    args = ["archive_toolkit.py", "--data-root", str(root), command, "--site", "sample"]
    if command in {"survey", "download", "verify"}:
        args.extend(["--domains", "example.test"])
    monkeypatch.setattr(sys, "argv", args)

    toolkit.main()

    assert received["root"] == root.resolve()
