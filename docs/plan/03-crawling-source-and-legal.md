# 03. 수집 소스와 법적 리스크 (Crawling Source and Legal)

담당 태스크: **T07** (수집 인터페이스 + HTTP 예절 레이어), **T08** (참조 어댑터 1종)

## 요구사항 재확인

- 사용자 3차 원문: "검색어 기반 수집" — 곡 제목/가수 등 검색어로 가사를 찾는다.
- 제약: 단순함. 사이트를 확정할 수 없으므로 **교체 가능한 어댑터**로 분리한다 (D1).

## 후보 소스 비교

| 후보 | 접근 방식 | 합법성 | 지속가능성 | 한국어 커버리지 | 판단 |
| --- | --- | --- | --- | --- | --- |
| 공개 가사 API (키 불필요, JSON) | 공식 엔드포인트 GET | 높음 (공개 API) | 중 (서비스 존속 의존) | 낮음~중 | **기본 참조 어댑터로 채택** |
| 공식 상용 API (토큰 발급, 예: Genius 스타일) | 공식 API + OAuth 토큰 | 높음 | 높음 | 중 | 토큰 필요 → 옵션 어댑터로만 |
| 국내 음원 사이트 HTML | 검색 + 파싱 | **낮음** (이용약관 위반 소지, 저작권) | 낮음 (차단/구조 변경) | 높음 | 기본값 **제외**, 사용자 책임 하 옵션 |
| 사용자 보유 텍스트 파일 직접 입력 | 파일 업로드/붙여넣기 | 높음 | 높음 | 무관 | **오프라인 경로로 반드시 제공** |

### 결정 (D1·D2)

1. **소스를 하드코딩하지 않는다.** `Source` 프로토콜을 두고, 어댑터는 레지스트리(`crawl/__init__.py`)에서 이름으로 조회한다.
2. **기본 참조 어댑터는 키가 필요 없는 공개 JSON API 1종**(`lyrics_ovh`)으로 한다. "동작하는 최소 예"를 확보하기 위함이며, 품질/커버리지 보증이 아니다. 파일명·모듈명만 맞추면 다른 API 어댑터로 즉시 교체 가능하다.
3. **HTML 스크래핑은 기본 활성화하지 않는다.** `html_generic.py` 스켈레톤을 제공하되, 사용자가 대상 사이트의 robots.txt 허용 여부와 이용약관을 직접 확인했을 때만 활성화한다. 스켈레톤 상단에 경고 주석을 둔다.
4. **오프라인 경로를 1급으로 지원한다.** 검색이 막히면 사용자가 가사를 붙여넣어 pptx만 생성할 수 있어야 한다(웹 UI의 "직접 입력" 탭).

> 권고 요약: **합법·지속 가능 우선**. 기본은 공개 API + 사용자 직접입력. 사이트 특정 스크래핑은 사용자 판단·책임 영역으로 격리한다.

## 법적/윤리 체크리스트 (코드가 강제하는 것)

| 항목 | 강제 방식 |
| --- | --- |
| robots.txt 확인 | `httputil.allowed_by_robots(url)` — `Disallow`면 `SourceBlockedError` |
| 요청 간 지연 | 기본 `delay_seconds = 2.0`, 요청 사이 `time.sleep` |
| User-Agent 명시 | `lyrics2pptx/0.1 (+local single-user tool)` — 브라우저 위장 금지 |
| 재시도 | 429/5xx/타임아웃 시 지수 백오프, 최대 3회 (`1s, 2s, 4s`) |
| 동시성 0 | 순차 요청만 (병렬 금지) |
| 캐시 | 동일 검색어 결과를 프로세스 메모리에서 1회 캐시(선택) |
| 라이선스 고지 | 저장소 README/웹 UI 하단에 "가사 저작권은 권리자에게 있음, 개인 용도 한정" 명시 |

## 인터페이스 명세 (`crawl/base.py`)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Sequence

@dataclass(frozen=True)
class RawSong:
    title: str
    artist: str | None
    lyrics: str            # 원문, 정규화 전
    source_name: str
    source_url: str | None = None

class SourceError(Exception):
    """모든 수집 예외의 상위."""

class SourceNotFoundError(SourceError):
    """검색어에 대한 결과 없음."""

class SourceBlockedError(SourceError):
    """robots.txt 또는 4xx 차단."""

class SourceUnavailableError(SourceError):
    """재시도 후에도 실패."""

class Source(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 5) -> Sequence[RawSong]:
        """검색어로 후보 목록 반환. 결과 없으면 SourceNotFoundError."""
```

`crawl/__init__.py` 레지스트리:

```python
SOURCES: dict[str, type] = {
    "lyrics_ovh": LyricsOvhSource,
    # "html_generic": HtmlGenericSource,   # 사용자가 약관 확인 후 주석 해제
}

def get_source(name: str) -> Source: ...
```

## HTTP 예절 레이어 (`crawl/httputil.py`)

```python
DEFAULT_UA = "lyrics2pptx/0.1 (+local single-user tool)"
DEFAULT_DELAY = 2.0

def allowed_by_robots(url: str, *, ua: str = DEFAULT_UA) -> bool: ...
def get_json(url: str, *, ua=DEFAULT_UA, timeout=10) -> dict: ...
def get_text(url: str, *, ua=DEFAULT_UA, timeout=10) -> str: ...
def sleep_between(seconds: float = DEFAULT_DELAY) -> None: ...
```

실패 분류 규칙:

| 상황 | 처리 |
| --- | --- |
| HTTP 404/403 | 즉시 `SourceBlockedError` (재시도 무의미) |
| HTTP 429 | 지수 백오프 재시도(최대 3), 소진 시 `SourceUnavailableError` |
| HTTP 5xx | 지수 백오프 재시도(최대 3), 소진 시 `SourceUnavailableError` |
| 타임아웃/socket 오류 | 지수 백오프 재시도(최대 3) |
| 응답 파싱 실패 | `SourceUnavailableError` (사이트 구조 변경으로 간주) |
| 필드 없음 | `SourceNotFoundError` |

## 참조 어댑터 (`crawl/lyrics_ovh.py`)

- 동작: `GET {BASE}/v1/{artist}/{title}` 또는 단일 검색 경로 → JSON의 `lyrics` 필드 사용.
- 검색어 해석: `"{artist} - {title}"` 또는 단일 문자열이면 그대로 검색어로 사용. `artist` 미상이면 `artist`는 빈 값으로 시도.
- 출력: `RawSong(title=..., artist=..., lyrics=<원문>, source_name="lyrics_ovh", source_url=...)`.
- 실패: 위 표에 따라 변환.
- **한계 명시**: 한국어 커버리지가 낮을 수 있다. 이는 설계 결함이 아니라 소스 미확정 상태의 정직한 기본값이며, 어댑터 교체로 해결한다.

## HTML 어댑터 스켈레톤 규칙 (`crawl/html_generic.py`)

1. 파일 상단에 경고 블록: "이 어댑터는 기본 비활성. 대상 사이트의 robots.txt와 이용약관을 사용자가 확인한 뒤에만 활성화하라."
2. 선택자(selector)는 **모듈 상수**로 노출해 사용자가 쉽게 바꾸게 한다:

```python
SEARCH_URL = ""          # 사용자가 채움
SELECTORS = {
    "result_link": "",
    "title": "",
    "artist": "",
    "lyrics_container": "",
}
```

3. 파싱은 정규식 기반으로 최소한만 구현(BeautifulSoup 미도입 — 의존성 증가 회피). 태그 제거 후 텍스트만 남긴다.
4. 반드시 `allowed_by_robots()`를 통과해야 실행된다.

## 오프라인/테스트 모드

`httputil.get_json`은 `LYRICS2PPTX_FIXTURE_DIR` 환경변수가 있으면 실제 요청 대신
`tests/fixtures/`에서 파일을 읽어 반환한다. 이 훅 하나로 크롤러 전체가 네트워크 없이 테스트된다.

## done when (T07, T08)

- [ ] **T07**: `LYRICS2PPTX_FIXTURE_DIR=tests/fixtures` 상태에서 `get_source("lyrics_ovh").search("x")` 가 fixture를 읽어 `RawSong` 반환.
- [ ] **T07**: `test_httputil.py` 통과 — 429 시 재시도 3회 후 `SourceUnavailableError`, robots `Disallow` 시 `SourceBlockedError`.
- [ ] **T07**: 실제 요청 없이(소켓 차단 또는 fixture 모드) 전체 테스트 통과.
- [ ] **T08**: 실사용 검색어 1건(예: 템플릿과 무관한 아무 곡)으로 `RawSong` 획득, `lyrics`가 비어있지 않음.
- [ ] **T08**: 결과 없음/차단/불가 3가지 예외가 각각 올바른 타입으로 발생.
- [ ] **T08**: `pipeline.py`가 `SourceError`를 사용자 메시지로 변환(스택트레이스 노출 없음).

## 실패 케이스 (명시)

| 케이스 | 사용자에게 보이는 결과 |
| --- | --- |
| 검색 결과 0건 | "검색 결과가 없습니다. 다른 검색어를 시도하거나 가사를 직접 붙여넣으세요." |
| 소스 차단(403/robots) | "이 소스는 자동 수집이 차단되었습니다. 직접 입력을 사용하세요." |
| 소스 불가(타임아웃) | "일시적으로 수집에 실패했습니다. 잠시 후 재시도하세요." |
| 가사 필드 비어 있음 | "가사를 찾지 못했습니다." + 직접 입력 유도 |

## 상위 문서 연결

- 인덱스: `계획서.md` / 선행: `02-tech-stack-and-runtime.md`
- 관련 결정: D1, D2, D11
- 다음: `04-text-normalization-and-splitting.md`
