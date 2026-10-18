# 06. 전달 형태와 웹 UI (Delivery and Web UI)

담당 태스크: **T09** (CLI), **T10** (로컬 웹앱)

## 1. 웹앱 vs CLI — 비교와 결론 (D10)

사용자는 "웹 앱"을 선택했다. 다만 "단순함"이 최상위 제약이므로, 웹 UI가 실제로 필요한지 검증한다.

| 기준 | CLI + 로컬 HTML 결과물 | 로컬 웹앱 (표준 `http.server`) | 순수 CLI |
| --- | --- | --- | --- |
| 검색어 입력 | 명령행 인자 | 브라우저 입력창 | 명령행 인자 |
| 결과 미리보기 | 생성된 HTML을 열어 확인 | 즉시 화면 표시 | 없음(파일만) |
| 파일 다운로드 | 파일 탐색기에서 열기 | 다운로드 버튼 | 파일 탐색기 |
| 구현량 | HTML 생성기 + CLI | 정적 1페이지 + 엔드포인트 3개 | 가장 적음 |
| 사용자 선택 부합 | 부분 | **부합** | 부분 |
| 추가 의존성 | 없음 | 없음(표준 라이브러리) | 없음 |

**결론**: **로컬 웹앱을 채택**하되, 프레임워크 없이 표준 `http.server`로 만든다. CLI도 유지한다(테스트·자동화·배치). 둘 다 `pipeline.py` 하나를 호출하므로 중복 로직이 없다.

- 웹앱을 채택한 이유: 사용자가 웹 앱을 선택했고, "검색 → 확인 → 다운로드" 흐름에서 브라우저가 파일 탐색기보다 자연스럽다.
- 로컬 HTML 미리보기를 별도로 만들지 않은 이유: 웹앱이 이미 미리보기를 제공하므로 중복이다.
- 프레임워크를 안 쓴 이유: 라우트 3개·동시 사용자 1명 → 도입 이득 < 비용.

## 2. 서버 스펙 (`src/lyrics2pptx/webapp.py`)

| 항목 | 값 |
| --- | --- |
| 실행 | `python -m lyrics2pptx.webapp` |
| 바인딩 | `127.0.0.1:8765` (외부 노출 없음) |
| 서버 | `http.server.ThreadingHTTPServer` + 커스텀 `BaseHTTPRequestHandler` |
| 정적 파일 | `web/` 디렉터리에서 `index.html`, `app.js`, `style.css` 제공 |
| 상태 | 없음(무상태). 요청마다 독립 처리 |
| 종료 | Ctrl+C |

### 엔드포인트

| 메서드 | 경로 | 입력 | 출력 | 오류 |
| --- | --- | --- | --- | --- |
| GET | `/` | - | `web/index.html` | 500 |
| GET | `/static/*` | - | `web/` 파일 | 404 |
| GET | `/api/search?q=<검색어>&source=<name>` | 쿼리스트링 | `{ "results": [ {index,title,artist,source_name,preview_lines} ] }` | 400/502/504 |
| POST | `/api/generate` | JSON `{ "source": "...", "index": 0, "lines": [...], "dedupe_chorus": false, "filename": "..." }` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` 바이너리 | 400/500 |
| POST | `/api/generate` (직접입력 모드) | JSON `{ "raw_text": "...", ... }` | 위와 동일 | 400/500 |

- `lines`가 있으면 정규화를 건너뛰고 그대로 2줄 분할에 사용한다(사용자가 이미 정리한 텍스트).
- `raw_text`가 있으면 `normalize → split` 을 적용한다.
- `index`는 `/api/search` 결과 중 사용자가 고른 항목.

### 오류 응답 규약

```json
{ "error": { "code": "SOURCE_NOT_FOUND", "message": "검색 결과가 없습니다. 직접 입력을 사용해 보세요." } }
```

| HTTP | code | 발생 조건 |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | q/필드 누락 |
| 404 | `SOURCE_NOT_FOUND` | `SourceNotFoundError` |
| 423 | `SOURCE_BLOCKED` | `SourceBlockedError` (robots/403) |
| 502 | `SOURCE_UNAVAILABLE` | `SourceUnavailableError` |
| 422 | `EMPTY_LYRICS` | `EmptyLyricsError` |
| 500 | `TEMPLATE_ERROR` | `TemplateStructureError` |

스택트레이스는 응답에 넣지 않는다(서버 로그에만 `logging.exception`).

## 3. 화면 스펙 (`web/index.html`)

단일 페이지. 상태는 4개뿐이다: `query`, `results`, `selectedLines`, `busy`.

```
┌───────────────────────────────────────────────┐
│  가사 2줄 PPTX 생성기                          │
├───────────────────────────────────────────────┤
│ [검색어 입력........................] [검색]    │
│ 소스: ( ) lyrics_ovh   ( ) 직접 입력           │
├───────────────────────────────────────────────┤
│ 결과 목록 (라디오)                             │
│  (•) 곡제목 — 아티스트   [미리보기 3줄]         │
├───────────────────────────────────────────────┤
│ 미리보기: 줄 12개 → 슬라이드 6장, 배경 순환 표시 │
│ ① 너는 담장 너머로 / 붉은 나무가지에 푸른 열매처럼│
│ ② 우리 함께 손잡고 가세 / 조금 느리고 어려울지라도│
├───────────────────────────────────────────────┤
│ [← 이전 3장] [다음 3장 →]                      │
│ [ ] 후렴 중복 제거                             │
│ 파일명 [가사.pptx........]  [PPTX 생성]        │
├───────────────────────────────────────────────┤
│ 생성되면 다운로드 링크 표시                     │
│ 가사 저작권은 권리자에게 있습니다(개인 용도).   │
└───────────────────────────────────────────────┘
```

| 요소 | 동작 |
| --- | --- |
| 검색 버튼 | `GET /api/search` → 결과 목록 채움 |
| 직접 입력 탭 | textarea 표시, 검색 대신 `raw_text`로 진행 |
| 결과 선택 | `selectedLines` 확정 |
| 미리보기 | 클라이언트에서 2줄 페어를 계산해 표시(서버 왕복 불필요) |
| 페이지 넘김 | 3장 단위(단순) |
| 생성 버튼 | `fetch('/api/generate')` → `blob` → `<a download>` 자동 클릭 |
| 오류 | 상단에 `error.message` 표시 |

`app.js`는 대략 120줄 규모(상태 4개, fetch 2개, 렌더 함수 3개). 프레임워크 없음.

## 4. CLI 스펙 (`src/lyrics2pptx/cli.py`)

```
python -m lyrics2pptx.cli --query "곡 제목" -o output/song.pptx [--source lyrics_ovh]
python -m lyrics2pptx.cli --input-file lyrics.txt -o output/song.pptx
python -m lyrics2pptx.cli --query "곡 제목" --json          # 수집 결과만 출력
```

| 옵션 | 설명 | 기본 |
| --- | --- | --- |
| `--query` | 검색어 | - |
| `--input-file` | 직접 입력 텍스트 파일 | - |
| `-o/--output` | 출력 경로 | `output/<제목>.pptx` |
| `--source` | 소스 이름 | `lyrics_ovh` |
| `--index` | 검색 결과 중 선택 | 0 |
| `--dedupe-chorus` | 후렴 중복 제거 | off |
| `--skip-slide` | 배경 풀에서 제외할 슬라이드 번호 | 없음 |
| `--json` | 수집 결과 JSON 출력 | off |

`--query`와 `--input-file` 중 하나는 필수. `argparse` 사용(외부 의존성 없음).

## 5. 공통 파이프라인 (`src/lyrics2pptx/pipeline.py`)

```python
def run(query=None, *, source="lyrics_ovh", index=0, raw_text=None,
        dedupe_chorus=False, output_path=None) -> Path:
    song = get_source(source).search(query)[index] if raw_text is None else RawSong(...)
    lines = normalize_lines(song.lyrics if raw_text is None else raw_text)
    pairs = split_pairs(lines, dedupe_repeat=dedupe_chorus)
    if not pairs:
        raise EmptyLyricsError("가사에서 유효한 줄을 찾지 못했습니다.")
    return build_presentation(pairs, TEMPLATE_PATH, output_path)
```

웹엔드포인트와 CLI는 모두 이 함수만 호출한다.

## 6. done when (T09, T10)

- [ ] **T09** `python -m lyrics2pptx.cli --query "..." -o output/t.pptx` 실행 후 파일이 생성되고 PowerPoint로 열린다.
- [ ] **T09** `--input-file` 경로가 네트워크 없이 동작한다.
- [ ] **T09** `--json`이 유효한 JSON을 stdout에 출력한다.
- [ ] **T10** `python -m lyrics2pptx.webapp` 실행 후 `http://127.0.0.1:8765` 접속 가능.
- [ ] **T10** 브라우저에서 검색 → 결과 선택 → 생성 → 다운로드까지 완료(수동).
- [ ] **T10** 각 오류 케이스(404/423/502/422)가 규약대로 표시된다.
- [ ] **T10** 외부 인터페이스에서 접근 불가(`127.0.0.1` 바인딩 확인).

## 상위 문서 연결

- 인덱스: `계획서.md` / 선행: `05-pptx-clone-and-fill.md`
- 관련 결정: D10
- 다음: `07-testing-strategy.md`
