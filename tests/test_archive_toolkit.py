import base64
import gzip
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

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


@pytest.mark.parametrize("site_id", ["../outside", "four_part_site_id", "UPPER"])
def test_site_id_rejected(site_id, tmp_path):
    with pytest.raises(ValueError, match="site id"):
        toolkit.site_paths(site_id, data_root=tmp_path)


@pytest.mark.parametrize(
    "command,target,archive_args",
    [
        ("survey", "survey_site", []),
        ("verify", "verify_site", []),
        ("download", "restore_common_crawl", ["--archive", "common_crawl"]),
        ("verify", "verify_common_crawl", ["--archive", "common_crawl"]),
    ],
)
def test_cli_root_forwarded(command, target, archive_args, tmp_path, monkeypatch):
    root = tmp_path / "external"
    command_mock = Mock()
    monkeypatch.setattr(toolkit, target, command_mock)
    monkeypatch.setattr(toolkit, "generate_viewer", Mock())

    args = [
        "archive_toolkit.py", "--data-root", str(root), command,
        "--site", "sample", "--domains", "example.test", *archive_args,
    ]
    monkeypatch.setattr(sys, "argv", args)

    toolkit.main()

    assert command_mock.call_args.kwargs["data_root"] == root.resolve()


def test_download_root_forwarded(tmp_path, monkeypatch):
    root = tmp_path / "external"
    download_mock = Mock()
    viewer_mock = Mock()
    monkeypatch.setattr(toolkit, "restore_domain", download_mock)
    monkeypatch.setattr(toolkit, "generate_viewer", viewer_mock)
    monkeypatch.setattr(
        sys, "argv",
        [
            "archive_toolkit.py", "--data-root", str(root), "download",
            "--site", "sample", "--domains", "example.test",
        ],
    )

    toolkit.main()

    site_root = root.resolve() / "sites" / "sample"
    assert download_mock.call_args.args[1] == site_root / "files"
    assert viewer_mock.call_args.args[0] == site_root / "files"
    assert viewer_mock.call_args.kwargs["out_path"] == site_root / "viewer.html"


def test_view_root_forwarded(tmp_path, monkeypatch):
    root = tmp_path / "external"
    viewer_mock = Mock()
    monkeypatch.setattr(toolkit, "generate_viewer", viewer_mock)
    monkeypatch.setattr(
        sys, "argv",
        [
            "archive_toolkit.py", "--data-root", str(root), "view",
            "--site", "sample",
        ],
    )

    toolkit.main()

    site_root = root.resolve() / "sites" / "sample"
    assert viewer_mock.call_args.args[0] == site_root / "files"
    assert viewer_mock.call_args.kwargs["out_path"] == site_root / "viewer.html"


@pytest.mark.parametrize(
    "original",
    [
        "https://example.test/../outside.txt",
        "https://example.test/C:/outside.txt",
        "https://example.test/%2e%2e/outside.txt",
    ],
)
def test_wayback_path_rejected(original, tmp_path, monkeypatch):
    record = {
        "statuscode": "200",
        "original": original,
        "timestamp": "20200101000000",
        "digest": digest(b"unsafe"),
    }
    refetch_mock = Mock()
    monkeypatch.setattr(toolkit, "fetch_cdx_domain", lambda domain: ([record], True))
    monkeypatch.setattr(toolkit, "refetch_verified", refetch_mock)

    assert toolkit.restore_domain(["example.test"], tmp_path / "files", delay=0) == 0
    refetch_mock.assert_not_called()
    assert not (tmp_path / "outside.txt").exists()


def test_site_symlink_rejected(tmp_path):
    root = tmp_path / "root"
    external = tmp_path / "external"
    base = root / "sites" / "sample"
    base.mkdir(parents=True)
    external.mkdir()
    link = base / "files"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    with pytest.raises(ValueError, match="링크|reparse"):
        toolkit.site_paths("sample", data_root=root)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_site_junction_rejected(tmp_path):
    root = tmp_path / "root"
    external = tmp_path / "external"
    base = root / "sites" / "sample"
    base.mkdir(parents=True)
    external.mkdir()
    junction = base / "files"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip(f"junction unavailable: {result.stderr}")
    try:
        with pytest.raises(ValueError, match="링크|reparse"):
            toolkit.site_paths("sample", data_root=root)
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_destination_junction_rejected(tmp_path):
    root = tmp_path / "files"
    target = root / "target"
    root.mkdir()
    target.mkdir()
    junction = root / "linked"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip(f"junction unavailable: {result.stderr}")
    try:
        with pytest.raises(ValueError, match="링크|reparse"):
            toolkit.safe_destination(root, "linked/page.html")
    finally:
        junction.rmdir()


def test_viewer_paths_escaped(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "page#fragment&name.html").write_text("safe", encoding="utf-8")
    (files / "flash&name.swf").write_bytes(b"safe")
    viewer = tmp_path / "viewer.html"

    toolkit.generate_viewer(files, viewer)

    rendered = viewer.read_text(encoding="utf-8")
    assert 'href="page%23fragment%26name.html"' in rendered
    assert "page#fragment&amp;name.html" in rendered
    assert "flash&amp;name.swf" in rendered
    assert "flash&name.swf" not in rendered
