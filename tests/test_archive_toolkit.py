import base64
import gzip
import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import archive_toolkit as toolkit


def digest(data):
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


def make_directory_link(link, target, link_kind):
    if link_kind == "symlink":
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlink unavailable: {error}")
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip(f"junction unavailable: {result.stderr}")


def remove_directory_link(link):
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


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


@pytest.mark.parametrize("link_kind", ["symlink"] + (["junction"] if os.name == "nt" else []))
def test_verify_repair_link_blocked(link_kind, tmp_path, monkeypatch):
    root = tmp_path / "root"
    files = root / "sites" / "sample" / "files"
    external = tmp_path / "external"
    files.mkdir(parents=True)
    external.mkdir()
    outside = external / "asset.jpg"
    outside.write_bytes(b"outside")
    link = files / "linked"
    make_directory_link(link, external, link_kind)
    replacement = b"replacement"
    record = {
        "statuscode": "200",
        "original": "https://example.test/linked/asset.jpg",
        "timestamp": "20200101000000",
        "digest": digest(replacement),
    }
    monkeypatch.setattr(toolkit, "fetch_cdx_domain", lambda domain: ([record], True))
    monkeypatch.setattr(
        toolkit.urllib.request, "urlopen",
        lambda request, timeout: io.BytesIO(replacement),
    )
    monkeypatch.setattr(
        sys, "argv",
        [
            "archive_toolkit.py", "--data-root", str(root), "verify",
            "--site", "sample", "--domains", "example.test", "--repair",
        ],
    )
    try:
        toolkit.main()
        assert outside.read_bytes() == b"outside"
    finally:
        remove_directory_link(link)


@pytest.mark.parametrize("link_kind", ["symlink"] + (["junction"] if os.name == "nt" else []))
def test_viewer_link_blocked(link_kind, tmp_path, monkeypatch):
    root = tmp_path / "root"
    site = root / "sites" / "sample"
    files = site / "files"
    external = tmp_path / "external"
    files.mkdir(parents=True)
    external.mkdir()
    outside = external / "asset.png"
    outside.write_bytes(b"private-image")
    link = files / "linked"
    make_directory_link(link, external, link_kind)
    monkeypatch.setattr(
        sys, "argv",
        [
            "archive_toolkit.py", "--data-root", str(root), "view",
            "--site", "sample",
        ],
    )
    try:
        toolkit.main()
        rendered = (site / "viewer.html").read_text(encoding="utf-8")
        assert base64.b64encode(b"private-image").decode("ascii") not in rendered
    finally:
        remove_directory_link(link)


def make_swf(tags, compressed=False):
    """DefineBitsJPEG2 등을 담은 최소 SWF를 만든다."""
    import struct
    body = bytearray()
    body += bytes([0x00])          # RECT: nbits=0
    body += struct.pack("<HH", 0x0C00, 1)   # framerate, framecount
    for code, payload in tags:
        if len(payload) < 0x3F:
            body += struct.pack("<H", (code << 6) | len(payload))
        else:
            body += struct.pack("<H", (code << 6) | 0x3F)
            body += struct.pack("<I", len(payload))
        body += payload
    body += struct.pack("<H", 0)   # End tag
    body = bytes(body)
    if compressed:
        import zlib
        payload = zlib.compress(body)
        head = b"CWS\x06" + struct.pack("<I", 8 + len(body))
        return head + payload
    head = b"FWS\x06" + struct.pack("<I", 8 + len(body))
    return head + body


def jpeg_bytes(marker=b"\xff\xe0"):
    return b"\xff\xd8" + marker + b"\x00\x10JFIF" + b"\x00" * 2000 + b"\xff\xd9"


def test_swf_images_extracted(tmp_path):
    import struct
    image = jpeg_bytes()
    payload = struct.pack("<H", 1) + b"\xff\xd9\xff\xd8" + image
    source = tmp_path / "movie.swf"
    source.write_bytes(make_swf([(21, payload)]))
    out = tmp_path / "out"
    saved = toolkit.extract_swf_images(source, out)
    assert len(saved) == 1
    assert saved[0].read_bytes() == image


def test_swf_separator_stripped(tmp_path):
    """DefineBitsJPEG2의 FFD9FFD8 구분자에서 자르면 안 된다."""
    import struct
    image = jpeg_bytes()
    payload = struct.pack("<H", 1) + b"\xff\xd9\xff\xd8" + image
    source = tmp_path / "movie.swf"
    source.write_bytes(make_swf([(21, payload)]))
    saved = toolkit.extract_swf_images(source, tmp_path / "out")
    data = saved[0].read_bytes()
    assert data.startswith(b"\xff\xd8")
    assert data.count(b"\xff\xd9") == 1


def test_swf_compressed_read(tmp_path):
    import struct
    image = jpeg_bytes()
    payload = struct.pack("<H", 1) + image
    source = tmp_path / "movie.swf"
    source.write_bytes(make_swf([(21, payload)], compressed=True))
    saved = toolkit.extract_swf_images(source, tmp_path / "out")
    assert len(saved) == 1


def test_extract_output_scoped(tmp_path):
    """추출 결과는 원본 폴더 안에 쓰지 않는다."""
    files = tmp_path / "sites" / "sample" / "files"
    files.mkdir(parents=True)
    (files / "movie.swf").write_bytes(make_swf([]))
    with pytest.raises(ValueError):
        toolkit.extract_site_images("sample", out_dir=files / "out", data_root=tmp_path)


def test_extract_site_collected(tmp_path):
    """사이트의 SWF 전부에서 이미지를 모은다."""
    import struct
    files = tmp_path / "sites" / "sample" / "files"
    files.mkdir(parents=True)
    payload = struct.pack("<H", 1) + jpeg_bytes()
    (files / "a.swf").write_bytes(make_swf([(21, payload)]))
    (files / "b.swf").write_bytes(make_swf([(21, payload)]))
    saved = toolkit.extract_site_images("sample", data_root=tmp_path)
    assert len(saved) == 2
    assert all(path.parent.name == "extracted" for path in saved)


def png_bytes(size=(120, 90)):
    """min_bytes 필터를 넘도록 압축되지 않는 노이즈 이미지를 만든다."""
    Image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pixels = os.urandom(size[0] * size[1] * 3)
    Image.frombytes("RGB", size, pixels).save(buffer, format="PNG")
    return buffer.getvalue()


def test_sheet_pages_written(tmp_path):
    files = tmp_path / "sites" / "sample" / "files"
    files.mkdir(parents=True)
    for index in range(3):
        (files / f"shot{index}.png").write_bytes(png_bytes())
    sheets = toolkit.build_contact_sheets("sample", data_root=tmp_path, columns=2, rows=1)
    assert len(sheets) == 2          # 3장이 2×1 시트 두 장에 나뉜다
    assert all(path.is_file() for path in sheets)


def test_sheet_output_scoped(tmp_path):
    files = tmp_path / "sites" / "sample" / "files"
    files.mkdir(parents=True)
    (files / "shot.png").write_bytes(png_bytes())
    with pytest.raises(ValueError):
        toolkit.build_contact_sheets("sample", out_dir=files / "sheets", data_root=tmp_path)
