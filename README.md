# Archives Toolkit

Wayback Machine과 Common Crawl의 공개 색인을 조사하고, 보존된 원본 바이트를 내려받아 검증하며, 로컬 뷰어를 만드는 Python 도구입니다. 도구 코드와 조사 데이터를 분리하므로 공개 저장소에는 실제 조사 대상이나 내려받은 자료를 둘 필요가 없습니다.

## 요구 사항

- Python 3.10 이상
- 테스트 실행 시 pytest

기본 기능은 Python 표준 라이브러리만 사용합니다. `view --max-dim`으로 큰 이미지를
뷰어 표시용으로 축소하려면 Pillow가 필요하며, 설치되어 있지 않으면 리사이즈 없이 원본을 포함합니다.

## 데이터 구조

모든 명령은 선택된 데이터 루트 아래의 같은 구조를 사용합니다.

```text
<data-root>/
└── sites/
    └── sample/
        ├── inventory.json
        ├── files/
        ├── captures/
        └── viewer.html
```

데이터 루트는 다음 우선순위로 결정됩니다.

1. CLI 전역 옵션 `--data-root`
2. 환경변수 `ARCHIVES_DATA_ROOT`
3. 저장소 내부 `data` 폴더

실제 조사 자료는 저장소 밖의 별도 비공개 디렉터리를 데이터 루트로 지정하는 방식을 권장합니다.

## 사용법

`--data-root`는 하위 명령 앞에 둡니다.

```powershell
python archive_toolkit.py --data-root D:\private\archive survey --site sample --domains example.test
python archive_toolkit.py --data-root D:\private\archive download --site sample --domains example.test
python archive_toolkit.py --data-root D:\private\archive extract --site sample
python archive_toolkit.py --data-root D:\private\archive sheet --site sample
python archive_toolkit.py --data-root D:\private\archive view --site sample
python archive_toolkit.py --data-root D:\private\archive verify --site sample --domains example.test
```

환경변수를 사용하면 모든 명령에서 옵션을 생략할 수 있습니다.

```powershell
$env:ARCHIVES_DATA_ROOT = "D:\private\archive"
python archive_toolkit.py survey --site sample --domains example.test
```

Common Crawl을 명시하려면 `download` 또는 `verify`에 `--archive common_crawl`을 추가합니다.

`survey`는 색인만 조회하고 원본 바이트를 내려받지 않습니다. `download`는 기존 파일을 덮어쓰지 않으며, `verify`는 기록된 SHA-1 지문과 로컬 바이트를 비교합니다. `view`는 `files/`의 자료로 자체완결 HTML 뷰어를 생성합니다.

`extract`는 `files/`의 SWF에서 내장 JPEG을 꺼내 `extracted/`에 저장합니다. 2000년대 사이트는 갤러리와 메뉴를 통째로 Flash에 넣은 경우가 많아 HTML만으로는 내용을 알 수 없습니다. `FFD8`~`FFD9`로 단순히 잘라내면 `DefineBitsJPEG2/3`가 데이터 앞에 넣는 `FFD9FFD8` 구분자에서 끊겨 깨진 파일이 나오므로, 태그 구조를 파싱해 그 구분자를 걷어냅니다.

`sheet`는 `files/`의 이미지를 파일명 라벨이 붙은 격자 시트로 묶어 `sheets/`에 저장합니다. 복구한 이미지가 수백 장일 때 한 장씩 여는 대신 시트로 훑고 볼 것만 원본으로 다시 엽니다. Pillow가 필요합니다.

`extract`와 `sheet`는 결과를 `files/` 안에 쓰지 않습니다. `files/`는 아카이브 원본 전용이며, 출력 경로가 그 안이면 거부합니다.

## 테스트

```powershell
python -m pytest tests
```

공개 저장소에는 `data/`, `sites/`, `inventory.json`, `viewer.html`, `files/`, `captures/`, 조사 상태 문서를 커밋하지 않습니다.
