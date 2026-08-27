# AGENTS.md

이 폴더는 폐쇄된 웹사이트를 Wayback Machine·Common Crawl 색인 기준으로 전수조사하고 복원한 결과를 관리한다.

## 구조

```text
archive_toolkit.py     # 유일한 실행 진입점 (survey / download / view)
policy.json            # 판정·상태값 단일 정책

sites/
└── <site_id>/
    ├── inventory.json # 전수조사·아카이브 출처·캡처 지문 (자동 생성물)
    ├── files/         # 아카이브에서 받은 원본 바이트 (원본 디렉터리 구조 그대로)
    ├── captures/      # 같은 경로의 다른 시점 원본 (files/ 구조를 더럽히지 않게 분리)
    └── viewer.html    # 자체완결 뷰어 (자동 생성물)

docs/
└── STATUS.md          # 진행 상태와 다음 작업 단일 관리처
```

원격 없이 로컬에서만 굴린다.

## 원칙

- `inventory.json`과 `viewer.html`은 자동 생성물이다. 직접 편집하지 않는다.
- 판정과 상태 enum은 `policy.json`을 단일 기준으로 쓴다. 스크립트에 값을 박지 않는다.
- 사이트를 새로 열 때는 `sites/<site_id>/`를 만들고 `survey`부터 돌린다. 스크립트는 고치지 않는다.
- `site_id`와 enum 값은 밑줄로 끊었을 때 세 토막을 넘지 않는다(`^[a-z0-9]+(_[a-z0-9]+){0,2}$`).
- 레코드에 산문 필드를 두지 않는다. 사람이 읽는 설명은 enum과 연결 구조로 표현한다.
- 조사 경위·막힌 경로·판단 근거는 레코드가 아니라 `docs/STATUS.md`에만 남긴다.
- 집계 수치는 문서에 적지 않는다. `inventory.json`의 `totals`가 단일 기준이다.
- **`files/`는 원본 전용이다.** `download`만 이 안에 쓴다. `survey`와 `view`는 읽기만 한다.
  생성물(`inventory.json`·`viewer.html`)은 사이트 폴더 루트에 두며 `files/` 안에 섞지 않는다.
  `generate_viewer`는 출력 경로가 `files/` 안이면 거부한다.
- 원본은 재인코딩·리사이즈하지 않고 받은 바이트 그대로 보관한다.
- **`files/`는 원본 사이트의 디렉터리 구조를 그대로 따른다.** 툴킷이 만든 보조 폴더를
  이 안에 두지 않는다. 같은 URL 경로의 아카이브별 바이트가 다르면 기존 파일을 덮어쓰지 않고
  `files/` 밖 `sites/<site_id>/captures/<archive>/<timestamp>/`에 원본을 분리 보관한다.

## 조사 기준

`survey`는 CDX 색인만 읽고 바이트는 내려받지 않는다. 도메인당 질의는 하나다
(`matchType=domain`). 스킴·`www` 변형을 따로 질의해도 같은 레코드가 돌아오므로 나누지 않는다.

경로별 `recovery` 판정은 그 경로에 기록된 상태코드 집합에서 정한다. 우선순위는
`200` → `403` → `404` → `301`/`302` 순이며, 하나라도 `200`이 있으면 복구 가능으로 본다.

- `recoverable`: 실제 바이트를 받을 수 있음
- `hotlink_blocked`: 원본 서버가 핫링크를 막아 아카이브 봇도 403만 받아감. **아카이브에 바이트 자체가 없어 영구 복구 불가**
- `not_found`: 수집 시점에 이미 삭제된 경로
- `redirect_only`: 본문 바이트 없이 리다이렉트만 기록됨

`local`은 `files/` 안의 실제 파일과 대조해 정한다. 상대경로가 정확히 맞는 것부터 배정하고
남은 파일만 파일명으로 배정한다. 파일 하나는 경로 하나에만 배정된다.

`survey_status`가 `no_public_surface`이면 아카이브 색인과 실서비스 양쪽에 조사할 표면이 없다는 뜻이다.
CDX가 빈 응답을 준 것과 조회 자체가 실패한 것은 다르므로, 조회 실패는 `unknown`으로 남기고 재조사한다.

## 실행

```powershell
python archive_toolkit.py survey   --site <id> --domains <도메인...>
python archive_toolkit.py download --site <id> --domains <도메인...>
python archive_toolkit.py download --site <id> --domains <도메인...> --archive common_crawl
python archive_toolkit.py view     --site <id>
python archive_toolkit.py verify   --site <id> --domains <도메인...>
python archive_toolkit.py verify   --site <id> --domains <도메인...> --archive common_crawl
```

콘솔이 cp949면 `PYTHONIOENCODING=utf-8`을 앞에 붙인다.

`survey`는 언제 돌려도 안전하다. `download`는 이미 있는 파일을 건너뛰므로 중단 후 이어받기가 된다.

`verify`는 CDX가 스냅샷마다 기록한 페이로드 SHA-1(base32)과 로컬 파일의 지문을 대조한다.
**색인을 하나라도 못 받으면 결과를 내지 않고 중단한다.** 반쪽 색인으로 대조하면 멀쩡한 파일이
무더기로 불일치로 보이기 때문이다. 지문이 안 맞는 파일은 지우지 말고 먼저 그 바이트가
아카이브의 다른 경로에서 온 것인지부터 확인한다.

Common Crawl 다운로드는 `policy.json`에 등록된 출처값을 사용하고 기본 구형 인덱스 세 개
(`CC-MAIN-2012`, `CC-MAIN-2009-2010`, `CC-MAIN-2008-2009`)의 ARC range를 받는다.
gzip을 푼 뒤 ARC·HTTP 헤더를 제거한 payload만 색인 지문과 대조해 기록한다.

## 알려진 제약

- web.archive.org는 시간대에 따라 503·타임아웃이 잦다. `survey`는 도메인당 질의가 하나뿐이라
  영향이 작지만, `download`는 경로마다 요청이 필요해 혼잡할 때 크게 느려진다.
- 다운로드가 실패한 경로와 `hotlink_blocked` 경로는 뜻이 다르다. 전자는 재시도로 풀리고
  후자는 아카이브에 원본이 없어 재시도해도 풀리지 않는다. `inventory.json`으로 둘을 구분한다.
