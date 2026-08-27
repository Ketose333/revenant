"""
===================================================================
Wayback Machine Archive & Site Restoration Toolkit
작성 목적: 인터넷 아카이브(Wayback Machine) 기반 폐쇄 사이트 복원, 
           미디어(이미지/플래시/문서) 전수 수집 및 로컬 뷰어 생성 도구
===================================================================
"""

import os
import sys
import json
import time
import shutil
import argparse
import base64
import collections
import hashlib
import urllib.request
import urllib.parse
import re
import gzip
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def clean_url(u):
    u = u.strip()
    u = re.sub(r":80(?=/|$)", "", u)
    return u

def decode_path_safe(raw_path):
    try:
        return urllib.parse.unquote(raw_path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        try:
            return urllib.parse.unquote(raw_path, encoding="cp949", errors="replace")
        except Exception:
            return urllib.parse.unquote(raw_path, errors="ignore")

def fetch_cdx(query_pattern, headers=DEFAULT_HEADERS, tries=5):
    url = f"https://web.archive.org/cdx/search/cdx?url={urllib.parse.quote(query_pattern)}&output=json"
    for attempt in range(tries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if len(data) > 1:
                    keys = data[0]
                    return [dict(zip(keys, row)) for row in data[1:]]
                return []
        except Exception as e:
            if attempt == tries - 1:
                print(f"[CDX 조회 오류] {query_pattern}: {e} (재시도 {tries}회 모두 실패)")
            else:
                time.sleep(3 * (attempt + 1))
    return []

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "policy.json"

COMMON_CRAWL_INDEXES = (
    "CC-MAIN-2012",
    "CC-MAIN-2009-2010",
    "CC-MAIN-2008-2009",
)

KIND_BY_SUFFIX = {
    ".htm": "page", ".html": "page", ".asp": "page", ".php": "page",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".bmp": "image",
    ".swf": "flash",
    ".css": "style",
    ".js": "script",
    ".pdf": "document", ".doc": "document", ".hwp": "document",
}

def load_policy():
    with open(POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)

def resolve_data_root(cli_value=None):
    """CLI, 환경변수, 저장소 기본값 순으로 데이터 루트를 결정한다."""
    value = cli_value if cli_value is not None else os.environ.get("ARCHIVES_DATA_ROOT")
    return Path(value or ROOT / "data").expanduser().resolve()


def is_link_or_reparse(path):
    """기존 심볼릭 링크와 Windows reparse point를 식별한다."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def site_paths(site_id, data_root=None):
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){0,2}", site_id):
        raise ValueError(f"올바르지 않은 site id: {site_id}")
    root = resolve_data_root(data_root)
    base = root / "sites" / site_id
    paths = {
        "base": base,
        "files": base / "files",
        "captures": base / "captures",
        "inventory": base / "inventory.json",
        "viewer": base / "viewer.html",
    }
    for path in paths.values():
        if is_link_or_reparse(path):
            raise ValueError(f"관리 경로의 링크 또는 reparse point는 허용하지 않는다: {path}")
    try:
        base.resolve().relative_to(root)
    except ValueError as error:
        raise ValueError(f"사이트 경로가 data root 밖을 가리킨다: {base}") from error
    return paths

def fetch_cdx_domain(domain, headers=DEFAULT_HEADERS, tries=5):
    """도메인 하나를 matchType=domain으로 한 번에 훑는다.

    스킴·www 변형을 따로 질의해도 같은 레코드가 돌아오므로 질의를 하나로 줄인다.
    """
    url = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={urllib.parse.quote(domain)}&matchType=domain&output=json"
    )
    for attempt in range(tries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if len(data) > 1:
                    keys = data[0]
                    return [dict(zip(keys, row)) for row in data[1:]], True
                return [], True
        except Exception as e:
            if attempt == tries - 1:
                print(f"[CDX 조회 실패] {domain}: {e} (재시도 {tries}회)")
                return [], False
            time.sleep(3 * (attempt + 1))
    return [], False

def classify_kind(path):
    return KIND_BY_SUFFIX.get(Path(path).suffix.lower(), "other")

def classify_recovery(statuses):
    if "200" in statuses:
        return "recoverable"
    if "403" in statuses:
        return "hotlink_blocked"
    if "404" in statuses:
        return "not_found"
    if statuses & {"301", "302"}:
        return "redirect_only"
    return "unknown"

def preserve_common_crawl_inventory(inventory, previous, files_root):
    """Wayback 재조사 때 이미 검증한 Common Crawl 메타와 경로를 합친다."""
    cc = previous.get("archives", {}).get("common_crawl")
    if not cc:
        return inventory
    assets = {asset["path"]: asset for asset in inventory.get("assets", [])}
    for saved in cc.get("assets", []):
        path = saved["path"]
        current = assets.get(path)
        if current is None:
            current = {
                "path": path,
                "kind": classify_kind(path),
                "recovery": "recoverable",
                "snapshots": 0,
                "first_seen": saved["first_seen"],
                "last_seen": saved["last_seen"],
                "local": "absent",
            }
            assets[path] = current
        current["snapshots"] += saved["snapshots"]
        current["first_seen"] = min(filter(None, (current.get("first_seen"), saved["first_seen"])))
        current["last_seen"] = max(filter(None, (current.get("last_seen"), saved["last_seen"])))
        current["recovery"] = "recoverable"
        try:
            rel = url_relative_path(path)
            if safe_destination(files_root, rel).is_file():
                current["local"] = "saved"
        except ValueError:
            pass

    inventory["assets"] = sorted(assets.values(), key=lambda asset: asset["path"])
    inventory["archives"] = previous["archives"]
    totals = inventory["totals"]
    totals["paths"] = len(inventory["assets"])
    totals["snapshots"] = sum(asset["snapshots"] for asset in inventory["assets"])
    for key in ("recoverable", "hotlink_blocked", "not_found", "redirect_only", "unknown"):
        totals[key] = sum(1 for asset in inventory["assets"] if asset["recovery"] == key)
    totals["saved"] = sum(1 for asset in inventory["assets"] if asset["local"] == "saved")
    return inventory

def survey_site(site_id, domains, data_root=None):
    """CDX 색인만 읽어 경로를 전수 조사한다. 바이트는 내려받지 않는다."""
    paths = site_paths(site_id, data_root=data_root)
    paths["base"].mkdir(parents=True, exist_ok=True)
    previous = {}
    if paths["inventory"].exists():
        with open(paths["inventory"], encoding="utf-8") as f:
            previous = json.load(f)

    print(f"[*] 전수조사 대상: {site_id} - {', '.join(domains)}")

    records = []
    reachable = True
    for d in domains:
        recs, ok = fetch_cdx_domain(d)
        reachable = reachable and ok
        print(f"  [cdx] {d} -> 스냅샷 {len(recs)}건")
        records.extend(recs)

    by_path = {}
    for r in records:
        original = clean_url(r.get("original", ""))
        raw = urllib.parse.urlparse(original).path
        p = decode_path_safe(raw) or "/"
        entry = by_path.setdefault(p, {"statuses": set(), "stamps": [], "count": 0})
        entry["statuses"].add(r.get("statuscode", ""))
        entry["count"] += 1
        ts = r.get("timestamp", "")
        if ts:
            entry["stamps"].append(ts)

    # 로컬 대조: 상대경로가 정확히 맞는 것부터 배정하고,
    # 남은 파일만 파일명으로 배정한다. 파일 하나는 경로 하나에만 배정된다.
    # (과거 실행이 일부 파일을 루트에 평평하게 저장해 파일명 대체가 필요하다)
    unclaimed = {}
    if paths["files"].exists():
        for f in paths["files"].rglob("*"):
            if f.is_file():
                unclaimed[f.relative_to(paths["files"]).as_posix()] = f.name

    def local_rel(p):
        rel = p.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = rel + "index.html"  # 다운로드가 디렉터리 경로에 붙이는 이름
        return rel

    ordered = sorted(by_path)
    claimed = set()
    for p in ordered:
        rel = local_rel(p)
        if rel in unclaimed:
            claimed.add(p)
            del unclaimed[rel]
    remaining_names = collections.Counter(unclaimed.values())
    for p in ordered:
        if p in claimed:
            continue
        name = Path(local_rel(p)).name
        if remaining_names.get(name):
            remaining_names[name] -= 1
            claimed.add(p)

    assets = []
    for p in ordered:
        e = by_path[p]
        stamps = sorted(e["stamps"])
        assets.append({
            "path": p,
            "kind": classify_kind(p),
            "recovery": classify_recovery(e["statuses"]),
            "snapshots": e["count"],
            "first_seen": stamps[0] if stamps else "",
            "last_seen": stamps[-1] if stamps else "",
            "local": "saved" if p in claimed else "absent",
        })

    totals = {"snapshots": len(records), "paths": len(assets)}
    for key in ("recoverable", "hotlink_blocked", "not_found", "redirect_only", "unknown"):
        totals[key] = sum(1 for a in assets if a["recovery"] == key)
    totals["saved"] = sum(1 for a in assets if a["local"] == "saved")

    inventory = {
        "site": site_id,
        "domains": sorted(domains),
        "survey_status": "surveyed" if assets else ("no_public_surface" if reachable else "unknown"),
        "surveyed_at": time.strftime("%Y-%m-%d"),
        "totals": totals,
        "assets": assets,
    }
    inventory = preserve_common_crawl_inventory(inventory, previous, paths["files"])
    totals = inventory["totals"]
    with open(paths["inventory"], "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n[+] 경로 {totals['paths']}건 / 스냅샷 {totals['snapshots']}건")
    print(f"    복구가능 {totals['recoverable']} · 핫링크차단 {totals['hotlink_blocked']} "
          f"· 삭제됨 {totals['not_found']} · 리다이렉트 {totals['redirect_only']} · 미상 {totals['unknown']}")
    print(f"    로컬보유 {totals['saved']} / 복구가능 {totals['recoverable']}")
    print(f"[+] 기록: {paths['inventory']}")
    return inventory

def payload_digest(path):
    """CDX가 기록하는 것과 같은 형식(SHA-1 base32)으로 로컬 파일 지문을 만든다."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b32encode(h.digest()).decode("ascii")

def bytes_digest(data):
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")

def url_relative_path(original):
    """URL 경로를 files/ 아래의 안전한 상대경로로 바꾼다."""
    parsed = urllib.parse.urlparse(clean_url(original))
    rel = decode_path_safe(parsed.path.lstrip("/"))
    if not rel or rel.endswith("/"):
        rel = rel + "index.html"
    parts = []
    reserved = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"상위 경로를 포함한 URL은 저장하지 않는다: {original}")
        if (":" in part or part.endswith((" ", ".")) or reserved.match(part)
                or any(ord(char) < 32 for char in part)):
            raise ValueError(f"Windows에서 안전하지 않은 URL 경로는 저장하지 않는다: {original}")
        parts.append(part)
    if not parts:
        return "index.html"
    return "/".join(parts)

def safe_destination(root, relative):
    """최종 저장 위치가 원본 루트 안인지 symlink까지 해석해 확인한다."""
    root_path = Path(root)
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError(f"절대경로에는 저장하지 않는다: {relative_path}")
    if is_link_or_reparse(root_path):
        raise ValueError(f"저장 루트의 링크 또는 reparse point는 허용하지 않는다: {root_path}")
    current = root_path
    for part in relative_path.parts:
        current /= part
        if is_link_or_reparse(current):
            raise ValueError(f"저장 경로의 링크 또는 reparse point는 허용하지 않는다: {current}")
    root = root_path.resolve()
    target = current.resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError(f"원본 루트 밖에는 저장하지 않는다: {target}") from e
    if target == root:
        raise ValueError(f"파일 저장 경로가 아니다: {target}")
    return target

def fetch_common_crawl_index(domain, index, headers=DEFAULT_HEADERS, tries=5):
    """Common Crawl 구형 CDXJ 색인에서 성공 응답만 읽는다."""
    query = urllib.parse.urlencode({
        "url": f"{domain}/*",
        "output": "json",
        "matchType": "domain",
        "filter": "status:200",
    })
    url = f"https://index.commoncrawl.org/{index}-index?{query}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                lines = resp.read().decode("utf-8").splitlines()
            return [json.loads(line) for line in lines if line.strip()], True
        except Exception as e:
            if attempt == tries - 1:
                print(f"[Common Crawl 색인 실패] {index} / {domain}: {e}")
                return [], False
            time.sleep(3 * (attempt + 1))
    return [], False

def fetch_common_crawl_records(domains, indexes=COMMON_CRAWL_INDEXES):
    records = []
    for index in indexes:
        for domain in domains:
            recs, ok = fetch_common_crawl_index(domain, index)
            if not ok:
                return None
            for rec in recs:
                rec["index"] = index
            print(f"  [cc] {index} / {domain} -> 스냅샷 {len(recs)}건")
            records.extend(recs)
    return records

def extract_arc_payload(compressed):
    """단일 ARC gzip member에서 HTTP payload 바이트만 꺼낸다."""
    raw = gzip.decompress(compressed)
    first_newline = raw.find(b"\n")
    if first_newline < 0:
        raise ValueError("ARC 메타데이터 행이 없다")
    metadata = raw[:first_newline].split()
    if not metadata or not metadata[-1].isdigit():
        raise ValueError("ARC 레코드 길이가 없다")
    record_length = int(metadata[-1])
    # 색인 길이가 레코드 구분용 마지막 개행까지 세는 ARC가 있어 실제 바이트를 넘길 수 있다.
    # 길이는 상한으로만 쓰고, 진짜 판정은 호출부의 지문 대조에 맡긴다.
    body = raw[first_newline + 1:]
    response = body[:record_length]
    split_at = response.find(b"\r\n\r\n")
    width = 4
    if split_at < 0:
        split_at = response.find(b"\n\n")
        width = 2
    if split_at < 0 or not response.startswith(b"HTTP/"):
        raise ValueError("ARC HTTP 응답 헤더가 올바르지 않다")
    return response[split_at + width:]

def fetch_common_crawl_payload(record, tries=4):
    """ARC range를 받아 색인 지문과 일치하는 HTTP payload만 반환한다."""
    offset = int(record["offset"])
    length = int(record["length"])
    url = f"https://data.commoncrawl.org/{record['filename']}"
    headers = dict(DEFAULT_HEADERS)
    headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                compressed = resp.read()
            payload = extract_arc_payload(compressed)
            got = bytes_digest(payload)
            if got != record["digest"] and payload.endswith(b"\n"):
                # 레코드 구분용 개행이 payload에 딸려온 경우만 한 바이트 떼고 다시 본다
                trimmed = payload[:-1]
                if bytes_digest(trimmed) == record["digest"]:
                    payload, got = trimmed, bytes_digest(trimmed)
            if got != record["digest"]:
                raise ValueError(f"지문 불일치: {got} != {record['digest']}")
            return payload
        except Exception as e:
            if attempt == tries - 1:
                print(f"    [ARC 수신 실패] {record.get('timestamp')} {record.get('url')}: {e}")
                return None
            time.sleep(2 * (attempt + 1))
    return None

def write_new_bytes(dest, data, root=None):
    """검증된 새 바이트만 기록한다. 기존 파일은 절대 덮어쓰지 않는다."""
    if root is not None:
        dest = safe_destination(root, Path(dest).relative_to(Path(root)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dest, "xb") as f:
            f.write(data)
        return True
    except FileExistsError:
        return dest.read_bytes() == data

def refetch_verified(path, snapshots, dest, tries=4):
    """아카이브에서 다시 받되, 지문이 기록과 맞을 때만 파일을 교체한다.

    맞지 않으면 아무것도 쓰지 않는다. 원본 폴더에 검증 안 된 바이트를 남기지 않기 위함이다.
    """
    for snap in snapshots:
        url = f"https://web.archive.org/web/{snap['timestamp']}id_/{snap['original']}"
        for attempt in range(tries):
            try:
                req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = resp.read()
            except Exception:
                time.sleep(2 * (attempt + 1))
                continue
            if not data:
                continue
            got = base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")
            if got != snap["digest"]:
                print(f"    [건너뜀] {snap['timestamp']} 지문 불일치 (기록 {snap['digest']} / 수신 {got})")
                break
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            return snap["timestamp"], len(data), got
    return None

def verify_site(site_id, domains, repair=False, data_root=None):
    """files/의 바이트가 아카이브 기록과 같은지 대조한다.

    repair=False면 파일을 건드리지 않는다. repair=True면 지문이 틀린 파일만
    다시 받아 교체하되, 받은 바이트의 지문이 기록과 맞을 때만 쓴다.
    """
    paths = site_paths(site_id, data_root=data_root)
    if not paths["files"].exists():
        print(f"[!] 원본 폴더가 없다: {paths['files']}")
        return None

    print(f"[*] 원본 대조: {site_id} - {', '.join(domains)}")

    records = []
    for d in domains:
        recs, ok = fetch_cdx_domain(d)
        if not ok:
            # 색인이 반쪽이면 멀쩡한 파일이 불일치로 보인다. 결과를 내지 않는다.
            print(f"[!] {d} 색인을 못 받아 대조를 중단한다. 나중에 다시 실행할 것")
            return None
        print(f"  [cdx] {d} -> 스냅샷 {len(recs)}건")
        records.extend(recs)

    # 경로별로 아카이브가 기록한 200 응답 지문을 모은다
    digests = {}
    snaps = collections.defaultdict(list)
    for r in records:
        if r.get("statuscode") != "200":
            continue
        raw = urllib.parse.urlparse(clean_url(r.get("original", ""))).path
        p = decode_path_safe(raw) or "/"
        digests.setdefault(p, set()).add(r.get("digest", ""))
        snaps[p].append(r)
    # 최신 스냅샷을 먼저 시도한다 (download와 같은 규칙)
    for p in snaps:
        snaps[p].sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    local = sorted(
        f for f in paths["files"].rglob("*") if f.is_file()
    )
    by_rel = {f.relative_to(paths["files"]).as_posix(): f for f in local}
    by_name = collections.defaultdict(list)
    for rel, f in by_rel.items():
        by_name[Path(rel).name].append(rel)

    # survey와 같은 배정 규칙: 정확한 상대경로 우선, 남은 것만 파일명으로
    assigned = {}
    taken = set()
    for p in sorted(digests):
        rel = p.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = rel + "index.html"  # 다운로드가 디렉터리 경로에 붙이는 이름
        if rel in by_rel and rel not in taken:
            assigned[rel] = p
            taken.add(rel)
    for p in sorted(digests):
        if p in assigned.values():
            continue
        for cand in by_name.get(Path(p).name, []):
            if cand not in taken:
                assigned[cand] = p
                taken.add(cand)
                break

    verified, mismatch, unmatched = [], [], []
    for rel, f in sorted(by_rel.items()):
        p = assigned.get(rel)
        if p is None:
            unmatched.append(rel)
            continue
        if payload_digest(f) in digests[p]:
            verified.append(rel)
        else:
            mismatch.append((rel, p))

    print(f"\n[+] 원본 일치 {len(verified)}건 / 불일치 {len(mismatch)}건 / 대조불가 {len(unmatched)}건")
    for rel, p in mismatch:
        print(f"  [불일치] {rel}  (아카이브 경로 {p})")
    for rel in unmatched:
        print(f"  [대조불가] {rel}")

    repaired, failed = [], []
    if repair and mismatch:
        print(f"\n[*] 불일치 {len(mismatch)}건 재수신 (지문이 맞을 때만 교체)")
        for rel, p in mismatch:
            print(f"  {rel}")
            got = refetch_verified(p, snaps.get(p, []), paths["files"] / rel)
            if got:
                ts, size, dg = got
                print(f"    [교체] {ts} {size} bytes 지문={dg}")
                repaired.append(rel)
            else:
                print(f"    [실패] 지문이 맞는 바이트를 못 받았다. 파일은 그대로 둔다")
                failed.append(rel)
        print(f"\n[+] 교체 {len(repaired)}건 / 실패 {len(failed)}건")

    return {
        "verified": verified, "mismatch": mismatch, "unmatched": unmatched,
        "repaired": repaired, "failed": failed,
    }

def update_common_crawl_inventory(site_id, domains, indexes, records, captures, data_root=None):
    """Common Crawl 조사·다운로드 결과를 inventory.json에 재현 가능하게 기록한다."""
    paths = site_paths(site_id, data_root=data_root)
    inventory = {}
    if paths["inventory"].exists():
        with open(paths["inventory"], encoding="utf-8") as f:
            inventory = json.load(f)

    old_cc = inventory.get("archives", {}).get("common_crawl", {})
    old_counts = {a["path"]: a["snapshots"] for a in old_cc.get("assets", [])}
    assets_by_path = {a["path"]: a for a in inventory.get("assets", [])}

    grouped = collections.defaultdict(list)
    for record in records:
        raw_path = urllib.parse.urlparse(clean_url(record["url"])).path
        path = decode_path_safe(raw_path) or "/"
        grouped[path].append(record)

    cc_assets = []
    for path in sorted(grouped):
        items = grouped[path]
        stamps = sorted(r["timestamp"] for r in items)
        cc_assets.append({
            "path": path,
            "snapshots": len(items),
            "first_seen": stamps[0],
            "last_seen": stamps[-1],
            "digests": sorted({r["digest"] for r in items}),
        })
        current = assets_by_path.get(path)
        if current is None:
            current = {
                "path": path,
                "kind": classify_kind(path),
                "recovery": "recoverable",
                "snapshots": 0,
                "first_seen": stamps[0],
                "last_seen": stamps[-1],
                "local": "absent",
            }
            assets_by_path[path] = current
        base_count = max(0, current["snapshots"] - old_counts.get(path, 0))
        current["snapshots"] = base_count + len(items)
        current["first_seen"] = min(filter(None, (current.get("first_seen"), stamps[0])))
        current["last_seen"] = max(filter(None, (current.get("last_seen"), stamps[-1])))
        current["recovery"] = "recoverable"

    # 자연 경로에 저장된 원본을 기준으로 local을 다시 계산한다.
    for path, asset in assets_by_path.items():
        try:
            rel = url_relative_path(path)
        except ValueError:
            continue
        if safe_destination(paths["files"], rel).is_file():
            asset["local"] = "saved"

    cc_capture_rows = sorted(captures, key=lambda c: (c["path"], c["timestamp"], c["local_path"]))
    archives = inventory.setdefault("archives", {})
    archives["common_crawl"] = {
        "indexes": list(indexes),
        "snapshots": len(records),
        "assets": cc_assets,
        "captures": cc_capture_rows,
    }
    inventory["site"] = site_id
    inventory["domains"] = sorted(set(inventory.get("domains", [])) | set(domains))
    inventory["survey_status"] = "surveyed" if assets_by_path else "no_public_surface"
    inventory["surveyed_at"] = time.strftime("%Y-%m-%d")
    inventory["assets"] = sorted(assets_by_path.values(), key=lambda a: a["path"])

    totals = inventory.setdefault("totals", {})
    totals["paths"] = len(inventory["assets"])
    totals["snapshots"] = sum(a["snapshots"] for a in inventory["assets"])
    for key in ("recoverable", "hotlink_blocked", "not_found", "redirect_only", "unknown"):
        totals[key] = sum(1 for a in inventory["assets"] if a["recovery"] == key)
    totals["saved"] = sum(1 for a in inventory["assets"] if a["local"] == "saved")

    with open(paths["inventory"], "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[+] Common Crawl 기록: {paths['inventory']}")
    return inventory

def restore_common_crawl(site_id, domains, indexes=COMMON_CRAWL_INDEXES, max_workers=3,
                         delay=0.15, data_root=None):
    """Common Crawl ARC에서 검증된 HTTP payload 원본을 복원한다."""
    paths = site_paths(site_id, data_root=data_root)
    paths["files"].mkdir(parents=True, exist_ok=True)
    paths["captures"].mkdir(parents=True, exist_ok=True)
    records = fetch_common_crawl_records(domains, indexes)
    if records is None:
        print("[!] 색인이 반쪽이므로 다운로드를 중단한다")
        return None

    targets = collections.defaultdict(list)
    for record in records:
        try:
            rel = url_relative_path(record["url"])
        except ValueError as e:
            print(f"  [건너뜀] {e}")
            continue
        targets[rel].append(record)
    for rel in targets:
        targets[rel].sort(key=lambda r: r["timestamp"], reverse=True)

    def download_one(item):
        rel, snapshots = item
        natural = safe_destination(paths["files"], rel)
        if natural.is_file():
            local_digest = payload_digest(natural)
            matching = next((r for r in snapshots if r["digest"] == local_digest), None)
            if matching:
                return rel, "existing", natural, matching, natural.stat().st_size
            record = snapshots[0]
            variant_rel = Path("common_crawl") / record["timestamp"] / rel
            variant = safe_destination(paths["captures"], variant_rel)
            if variant.is_file() and payload_digest(variant) == record["digest"]:
                return rel, "variant_existing", variant, record, variant.stat().st_size
            time.sleep(delay)
            data = fetch_common_crawl_payload(record)
            if data is None or not write_new_bytes(variant, data, root=paths["captures"]):
                return rel, "failed", None, record, 0
            return rel, "variant_saved", variant, record, len(data)

        for record in snapshots:
            time.sleep(delay)
            data = fetch_common_crawl_payload(record)
            if data is None:
                continue
            if write_new_bytes(natural, data, root=paths["files"]):
                return rel, "saved", natural, record, len(data)
        return rel, "failed", None, snapshots[0], 0

    print(f"[+] Common Crawl 복원 대상 고유 파일: {len(targets)}개")
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_one, item): item for item in targets.items()}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            rel, status, dest, record, size = result
            if dest:
                shown = dest.relative_to(paths["base"]).as_posix()
                print(f"  [OK] {rel} -> {shown} ({status}, {size} bytes)")
            else:
                print(f"  [FAIL] {rel}")

    captures = []
    for rel, status, dest, record, size in results:
        if dest is None:
            continue
        captures.append({
            "path": "/" if rel == "index.html" else "/" + rel,
            "local_path": dest.relative_to(paths["base"]).as_posix(),
            "timestamp": record["timestamp"],
            "digest": record["digest"],
            "bytes": size,
        })
    update_common_crawl_inventory(
        site_id, domains, indexes, records, captures, data_root=data_root
    )
    saved = sum(1 for _, status, _, _, _ in results if status in ("saved", "variant_saved"))
    existing = sum(1 for _, status, _, _, _ in results if "existing" in status)
    failed = sum(1 for _, status, _, _, _ in results if status == "failed")
    print(f"[+] Common Crawl 완료: 신규 {saved} / 기존확인 {existing} / 실패 {failed}")
    return {"saved": saved, "existing": existing, "failed": failed, "captures": captures}

def verify_common_crawl(site_id, domains, indexes=COMMON_CRAWL_INDEXES, data_root=None):
    """Common Crawl 캡처 지문과 로컬 원본을 읽기 전용으로 대조한다."""
    paths = site_paths(site_id, data_root=data_root)
    records = fetch_common_crawl_records(domains, indexes)
    if records is None:
        print("[!] 색인이 반쪽이므로 대조를 중단한다")
        return None
    allowed = collections.defaultdict(set)
    for record in records:
        allowed[url_relative_path(record["url"])].add(record["digest"])

    verified, mismatch, absent = [], [], []
    for rel, digests in sorted(allowed.items()):
        natural = safe_destination(paths["files"], rel)
        candidates = [natural] if natural.is_file() else []
        variants = paths["captures"] / "common_crawl"
        if variants.exists():
            candidates.extend(p for p in variants.glob(f"*/{rel}") if p.is_file())
        if not candidates:
            absent.append(rel)
        elif any(payload_digest(candidate) in digests for candidate in candidates):
            verified.append(rel)
        else:
            mismatch.append(rel)
    print(f"[+] Common Crawl 원본 일치 {len(verified)} / 불일치 {len(mismatch)} / 없음 {len(absent)}")
    return {"verified": verified, "mismatch": mismatch, "absent": absent}

def restore_domain(domains, output_dir, max_workers=3, delay=0.15):
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    
    print(f"\n=======================================================")
    print(f"[*] 대상 도메인: {domains}")
    print(f"[*] 저장 대상 폴더: {out_path.resolve()}")
    print(f"[*] 동시 다운로드 스레드: {max_workers}")
    print(f"=======================================================\n")
    
    # 1. CDX 색인 수집 (도메인당 질의 하나)
    all_cdx = []
    for d in domains:
        recs, ok = fetch_cdx_domain(d.strip())
        if not ok:
            # 반쪽 색인으로 받으면 어떤 경로가 남았는지 알 수 없다
            print(f"[!] {d} 색인을 못 받아 중단한다. 나중에 다시 실행할 것")
            return 0
        print(f"[+] {d} -> 스냅샷 {len(recs)}건")
        all_cdx.extend(recs)

    print(f"\n[+] 총 {len(all_cdx)}개의 아카이브 스냅샷 수집 완료")

    # 2. 경로별 스냅샷 매핑
    #    200만 대상으로 삼는다. 301·302·revisit을 받으면 본문이 아닌
    #    리다이렉트 바이트가 원본 자리에 들어앉는다.
    targets_map = {}
    for r in all_cdx:
        if r.get("statuscode", "") != "200":
            continue
        try:
            rel = url_relative_path(r.get("original", ""))
        except ValueError as error:
            print(f"  [건너뜀] {error}")
            continue
        targets_map.setdefault(rel, []).append(r)

    for rel in targets_map:
        targets_map[rel].sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    print(f"[+] 복원 대상 고유 파일: {len(targets_map)}개")

    # 3. 다운로드 함수 — 지문이 기록과 맞을 때만 기록한다
    def download_file(item):
        rel, snaps = item
        dest_file = safe_destination(out_path, rel)
        if dest_file.exists() and dest_file.stat().st_size > 0:
            return rel, True, "기존 파일 존재", dest_file.stat().st_size

        time.sleep(delay)
        got = refetch_verified(rel, snaps, dest_file, tries=3)
        if got:
            ts, size, _ = got
            return rel, True, f"성공 ({ts})", size
        return rel, False, "지문이 맞는 바이트를 못 받음", 0

    print(f"\n[*] 병렬 다운로드 시작 (스레드: {max_workers})...")
    success_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(download_file, it): it for it in targets_map.items()}
        for f in as_completed(futures):
            rel, ok, msg, sz = f.result()
            if ok:
                success_count += 1
                print(f"  [OK] {rel} ({sz} bytes)")
            else:
                print(f"  [FAIL] {rel} ({msg})")
    
    print(f"\n[+] 다운로드 완료: {success_count} / {len(targets_map)} 파일 복원 성공")
    return success_count

IMG_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".bmp": "image/bmp",
}

def _b64_data_uri(path, max_dim=None):
    """이미지를 data URI로 만든다.

    max_dim이 주어지고 사진이 그보다 크면 웹 표시용으로만 줄인다.
    원본 파일은 건드리지 않는다. GIF는 애니메이션이 깨지므로 그대로 둔다.
    """
    mime = IMG_MIME.get(path.suffix.lower())
    if not mime:
        return None
    if max_dim and mime in ("image/jpeg", "image/png"):
        try:
            from PIL import Image
            import io
            with Image.open(path) as im:
                if max(im.size) > max_dim:
                    im = im.convert("RGB")
                    im.thumbnail((max_dim, max_dim), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=85, optimize=True)
                    enc = base64.b64encode(buf.getvalue()).decode("ascii")
                    return f"data:image/jpeg;base64,{enc}"
        except Exception:
            pass  # 줄이기 실패하면 원본 그대로 싣는다
    try:
        data = path.read_bytes()
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None

def generate_viewer(target_dir, out_path, max_dim=None):
    """target_dir의 원본을 읽어 자체완결 뷰어를 out_path에 쓴다.

    원본 폴더는 읽기만 한다. 생성물을 그 안에 쓰지 않는다.
    """
    import html as html_mod
    p_dir = Path(target_dir)
    if not p_dir.exists():
        print(f"[!] 디렉터리가 존재하지 않습니다: {target_dir}")
        return

    all_files = [f for f in p_dir.glob("**/*.*") if f.name != "archive_viewer.html"]
    images = [f for f in all_files if f.suffix.lower() in IMG_MIME]
    flash = [f for f in all_files if f.suffix.lower() == ".swf"]
    pages = [f for f in all_files if f.suffix.lower() in [".htm", ".html"]]

    images.sort(key=lambda x: x.name)
    flash.sort(key=lambda x: x.name)
    pages.sort(key=lambda x: x.name)

    # group images by their parent folder, relative to the restored root
    groups = {}
    for img in images:
        rel = img.relative_to(p_dir)
        cat = rel.parent.as_posix() if rel.parent != Path(".") else "(루트)"
        groups.setdefault(cat, []).append(img)
    cats = sorted(groups.keys(), key=lambda c: (c == "(루트)", c))

    def render_grid(items):
        cells = []
        for img in items:
            uri = _b64_data_uri(img, max_dim=max_dim)
            if uri is None:
                continue
            name = html_mod.escape(img.name)
            cells.append(
                f'<button class="cell" type="button" onclick="openLightbox(this)" '
                f'aria-label="{name} 확대 보기">'
                f'<img src="{uri}" alt="{name}" loading="lazy"></button>'
            )
        return "\n".join(cells)

    sections = []
    for cat in cats:
        items = groups[cat]
        sections.append(f"""
    <section class="folder">
      <div class="folder-head">
        <h2>{html_mod.escape(cat)}</h2>
        <span class="folder-desc">{len(items)}장</span>
      </div>
      <div class="grid">
        {render_grid(items)}
      </div>
    </section>""")
    sections_html = "\n".join(sections)

    page_rows = []
    for page in pages:
        relative = page.relative_to(p_dir).as_posix()
        href = urllib.parse.quote(relative, safe="/")
        shown = html_mod.escape(relative)
        page_rows.append(
            f'      <a class="page-item" href="{href}" target="_blank" rel="noopener noreferrer">'
            f'<span class="page-title">{html_mod.escape(page.name)}</span>'
            f'<span class="page-path">{shown} ({page.stat().st_size/1024:.1f} KB)</span></a>'
        )
    pages_html = "\n".join(page_rows)

    flash_rows = []
    for item in flash:
        relative = item.relative_to(p_dir).as_posix()
        flash_rows.append(
            f'      <div class="page-item flash-item">'
            f'<span class="page-title">{html_mod.escape(item.name)}</span>'
            f'<span class="page-path">{html_mod.escape(relative)} · '
            f'Flash(.swf), 브라우저 미리보기 불가</span></div>'
        )
    flash_html = "\n".join(flash_rows)

    viewer_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>복원 아카이브 뷰어 · {html_mod.escape(p_dir.name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@500;700&family=Noto+Sans+KR:wght@400;500;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #17140f; --surface: #221d16; --surface-2: #2b241a; --border: #3c3323;
    --ink: #ece5d8; --muted: #a89b87; --accent: #d4674f;
    --font-display: "Noto Serif KR", serif; --font-body: "Noto Sans KR", sans-serif;
    --font-mono: "JetBrains Mono", monospace;
  }}
  :root[data-theme="light"] {{
    --bg: #f2ede4; --surface: #ffffff; --surface-2: #ece4d5; --border: #ddd0ba;
    --ink: #241d15; --muted: #7a6c57; --accent: #a8402f;
  }}
  @media (prefers-color-scheme: light) {{
    :root:not([data-theme="dark"]) {{
      --bg: #f2ede4; --surface: #ffffff; --surface-2: #ece4d5; --border: #ddd0ba;
      --ink: #241d15; --muted: #7a6c57; --accent: #a8402f;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--ink); font-family: var(--font-body); line-height: 1.6; -webkit-font-smoothing: antialiased; }}
  a {{ color: var(--accent); }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 28px 16px 64px; }}
  header.hero {{ border: 1px solid var(--border); background: var(--surface); border-radius: 14px; padding: 28px 22px; margin-bottom: 28px; }}
  .eyebrow {{ font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent); display: block; margin-bottom: 10px; }}
  h1 {{ font-family: var(--font-display); font-weight: 700; font-size: clamp(22px, 5vw, 30px); margin: 0 0 8px; text-wrap: balance; }}
  .lede {{ color: var(--muted); font-size: 14px; max-width: 70ch; margin: 0 0 20px; word-break: break-all; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }}
  .stat {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
  .stat .n {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 20px; font-weight: 500; color: var(--ink); display: block; }}
  .stat .l {{ font-size: 11.5px; color: var(--muted); }}
  .section, .folder {{ margin-bottom: 30px; }}
  .folder-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px; }}
  .folder-head h2 {{ font-family: var(--font-display); font-size: 16px; margin: 0; word-break: break-all; }}
  .folder-desc {{ font-size: 12px; color: var(--muted); font-family: var(--font-mono); white-space: nowrap; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
  @media (min-width: 640px) {{ .grid {{ grid-template-columns: repeat(4, 1fr); }} }}
  @media (min-width: 960px) {{ .grid {{ grid-template-columns: repeat(6, 1fr); }} }}
  .cell {{ border: 1px solid var(--border); border-radius: 8px; overflow: hidden; padding: 0; background: var(--surface-2); cursor: zoom-in; aspect-ratio: 1 / 1; display: block; }}
  .cell img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .page-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }}
  .page-item {{ background: var(--surface-2); border: 1px solid var(--border); padding: 12px; border-radius: 8px; text-decoration: none; display: flex; flex-direction: column; gap: 4px; }}
  .page-title {{ font-weight: 500; font-size: 13.5px; color: var(--ink); word-break: break-all; }}
  .page-path {{ font-size: 11px; color: var(--muted); font-family: var(--font-mono); word-break: break-all; }}
  .flash-item {{ opacity: 0.7; }}
  footer {{ margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12.5px; }}
  #lightbox {{ position: fixed; inset: 0; background: rgba(10,8,6,0.92); display: none; align-items: center; justify-content: center; padding: 20px; z-index: 50; }}
  #lightbox.open {{ display: flex; }}
  #lightbox img {{ max-width: 100%; max-height: 100%; border-radius: 6px; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <span class="eyebrow">Wayback Machine 복원 아카이브</span>
    <h1>{html_mod.escape(p_dir.name)}</h1>
    <p class="lede">폴더 위치: {html_mod.escape(str(p_dir.resolve()))}</p>
    <div class="stats">
      <div class="stat"><span class="n">{len(images)}</span><span class="l">이미지</span></div>
      <div class="stat"><span class="n">{len(flash)}</span><span class="l">Flash(.swf)</span></div>
      <div class="stat"><span class="n">{len(pages)}</span><span class="l">웹페이지</span></div>
      <div class="stat"><span class="n">{len(cats)}</span><span class="l">폴더</span></div>
    </div>
  </header>

  <section class="section">
    <div class="folder-head"><h2>웹페이지 ({len(pages)}개)</h2></div>
    <div class="page-grid">
{pages_html}
{flash_html}
    </div>
  </section>

{sections_html}

  <footer>이미지를 탭하면 확대됩니다 · 원본 파일 그대로 수록(무압축)</footer>
</div>

<div id="lightbox" onclick="closeLightbox()">
  <img id="lightbox-img" src="" alt="">
</div>
<script>
  function openLightbox(btn) {{
    var img = btn.querySelector('img');
    document.getElementById('lightbox-img').src = img.src;
    document.getElementById('lightbox-img').alt = img.alt;
    document.getElementById('lightbox').classList.add('open');
  }}
  function closeLightbox() {{ document.getElementById('lightbox').classList.remove('open'); }}
</script>
</body>
</html>
"""
    out_file = Path(out_path)
    if p_dir.resolve() in out_file.resolve().parents:
        raise ValueError(f"뷰어를 원본 폴더 안에 쓸 수 없다: {out_file}")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(viewer_html)
    print(f"[+] 뷰어 파일 생성 완료: {out_file.resolve()} ({out_file.stat().st_size/1024/1024:.2f} MB)")

def main():
    parser = argparse.ArgumentParser(
        description="Wayback Machine 아카이브 전수조사·복원 툴킷 (sites/<id>/ 단위로 동작)"
    )
    parser.add_argument(
        "--data-root", type=Path,
        help="조사 데이터 루트 (기본: ARCHIVES_DATA_ROOT 또는 저장소의 data 폴더)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    survey_parser = subparsers.add_parser(
        "survey", help="CDX 색인만 읽어 경로를 전수조사한다 (다운로드 없음)"
    )
    survey_parser.add_argument("--site", "-s", required=True, help="사이트 id (sites/<id>)")
    survey_parser.add_argument("--domains", "-d", nargs="+", required=True, help="조사할 도메인 목록")

    down_parser = subparsers.add_parser("download", help="아카이브에서 실제 바이트를 내려받아 복원한다")
    down_parser.add_argument("--site", "-s", required=True, help="사이트 id (sites/<id>/files 에 저장)")
    down_parser.add_argument("--domains", "-d", nargs="+", required=True, help="복원할 도메인 목록")
    down_parser.add_argument("--threads", "-t", type=int, default=3, help="병렬 다운로드 스레드 수 (기본값: 3)")
    down_parser.add_argument("--archive", choices=("wayback", "common_crawl"), default="wayback",
                             help="원본 아카이브 (기본값: wayback)")
    down_parser.add_argument("--cc-indexes", nargs="+", default=list(COMMON_CRAWL_INDEXES),
                             help="Common Crawl 인덱스 목록")

    view_parser = subparsers.add_parser("view", help="사이트의 자체완결 뷰어 HTML을 재생성한다")
    view_parser.add_argument("--site", "-s", required=True, help="사이트 id (sites/<id>/viewer.html 생성)")
    view_parser.add_argument("--max-dim", type=int, default=None,
                             help="이 픽셀보다 큰 사진만 웹 표시용으로 줄인다 (원본 파일은 불변)")

    verify_parser = subparsers.add_parser(
        "verify", help="files/의 바이트가 아카이브 기록과 같은지 대조한다 (읽기 전용)"
    )
    verify_parser.add_argument("--site", "-s", required=True, help="사이트 id")
    verify_parser.add_argument("--domains", "-d", nargs="+", required=True, help="대조할 도메인 목록")
    verify_parser.add_argument("--repair", action="store_true",
                               help="지문이 틀린 파일을 다시 받아 교체한다 (지문이 맞을 때만 씀)")
    verify_parser.add_argument("--archive", choices=("wayback", "common_crawl"), default="wayback",
                               help="대조할 원본 아카이브 (기본값: wayback)")
    verify_parser.add_argument("--cc-indexes", nargs="+", default=list(COMMON_CRAWL_INDEXES),
                               help="Common Crawl 인덱스 목록")

    args = parser.parse_args()
    data_root = resolve_data_root(args.data_root)
    paths = site_paths(args.site, data_root=data_root)

    if args.command == "survey":
        survey_site(args.site, args.domains, data_root=data_root)
    elif args.command == "download":
        if args.archive == "common_crawl":
            restore_common_crawl(args.site, args.domains, indexes=args.cc_indexes,
                                 max_workers=args.threads, data_root=data_root)
        else:
            restore_domain(args.domains, paths["files"], max_workers=args.threads)
        generate_viewer(paths["files"], out_path=paths["viewer"])
    elif args.command == "view":
        generate_viewer(paths["files"], out_path=paths["viewer"], max_dim=args.max_dim)
    elif args.command == "verify":
        if args.archive == "common_crawl":
            if args.repair:
                parser.error("Common Crawl verify는 --repair를 지원하지 않는다. download를 다시 실행할 것")
            verify_common_crawl(
                args.site, args.domains, indexes=args.cc_indexes, data_root=data_root
            )
        else:
            verify_site(args.site, args.domains, repair=args.repair, data_root=data_root)

if __name__ == "__main__":
    main()
