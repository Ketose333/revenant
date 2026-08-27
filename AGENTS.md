# AGENTS.md

이 저장소는 Wayback Machine과 Common Crawl을 이용해 폐쇄된 웹사이트를 조사하고 복원하는 공개 도구를 관리한다. 실제 조사 데이터는 저장소와 분리한다.

## 구조

```text
archive_toolkit.py          # survey / download / view / verify 실행 진입점
policy.json                 # 판정·상태값 단일 정책
tests/
└── test_archive_toolkit.py # 경로·원본 검증 단위 테스트

<data-root>/
└── sites/
    └── <site_id>/
        ├── inventory.json  # 조사 결과와 아카이브 메타데이터
        ├── files/          # 내려받은 원본 바이트
        ├── captures/       # 동일 경로의 다른 시점 원본
        └── viewer.html     # 자체완결 뷰어
```

## 원칙

- 실제 조사 데이터, 원본 바이트, 인벤토리, 뷰어, 조사 상태 문서는 커밋하지 않는다.
- 데이터 경로는 `--data-root`, `ARCHIVES_DATA_ROOT`, 저장소 내부 `data` 순으로 결정한다.
- 모든 명령은 선택된 데이터 루트 아래 `sites/<site_id>`에서만 조사 데이터를 읽고 쓴다.
- `inventory.json`과 `viewer.html`은 자동 생성물이다. 직접 편집하지 않는다.
- 판정과 상태 enum은 `policy.json`을 단일 기준으로 쓴다.
- `site_id`와 enum 값은 밑줄로 끊었을 때 세 토막을 넘지 않는다(`^[a-z0-9]+(_[a-z0-9]+){0,2}$`).
- 레코드에 산문 필드를 두지 않는다. 설명은 enum과 연결 구조로 표현한다.
- `files/`는 원본 전용이며 다운로드한 바이트를 재인코딩하거나 덮어쓰지 않는다.
- 같은 URL 경로의 아카이브별 바이트가 다르면 `captures/<archive>/<timestamp>/`에 분리한다.
- 뷰어 출력은 `files/` 밖에 두며, 도구는 원본 폴더 내부 출력을 거부해야 한다.
- 코드와 문서의 예시에는 실제 조사 대상의 인물명, 도메인, site id를 사용하지 않는다.

## 변경 및 검증

- Python 변경은 테스트를 먼저 추가해 실패를 확인한 뒤 최소 구현과 리팩터링을 진행한다.
- 변경 후 `python -m pytest tests`를 실행한다.
- 커밋 전 staged diff에서 시크릿을 검사한다.
- 커밋 메시지는 한국어 `feat/fix/refactor/docs/chore: 내용` 형식을 사용한다.
- push, PR, 머지는 메인 작업자가 수행한다.
