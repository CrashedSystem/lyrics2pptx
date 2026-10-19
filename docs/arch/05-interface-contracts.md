# 05. 인터페이스 계약 (Interface Contracts)

상위: `아키텍처.md` §4 / 계획서 06 §2–5 (참조). 이 문서는 계약의 **경계 소유자**와 **검증 방법**을 확정한다.
프로토콜/플래그 목록은 계획서 06 과 동일하며, 여기서는 "어느 모듈이 이 계약을 소유하는가"를 못박는다.

## 1. 소유권 표 (누가 계약을 지키는가)

| 계약 | 소유 모듈 | 소비자 | 검증 |
| --- | --- | --- | --- |
| HTTP 라우트/상태코드 | `webapp.py` | 브라우저(`app.js`) | `test_webapp.py` (핸들러 단위) |
| JSON 요청/응답 스키마 | `webapp.py` (직렬화) | 브라우저 | `test_webapp.py` |
| CLI 인자/종료코드 | `cli.py` | 셸/자동화 | `test_cli.py` |
| 오류 코드 ↔ 예외 | `pipeline.describe_error()` | `webapp`, `cli` | `test_pipeline_errors.py` |
| 파이프라인 함수 시그니처 | `pipeline.run()` | `webapp`, `cli` | `test_preview.py` |
| 정적 자산 경로 | `webapp.py` | 브라우저 | `test_webapp.py` |

## 2. HTTP 계약 (`webapp.py`)

### 2.1 라우트 (계획서 06 §2 그대로)

| 메서드 | 경로 | 입력 | 출력 | 소유 핸들러 |
| --- | --- | --- | --- | --- |
| GET | `/` | — | `web/index.html` (text/html; charset=utf-8) | `do_GET` |
| GET | `/static/*` | — | `web/` 내 파일 | `do_GET` |
| GET | `/api/search?q=&source=` | 쿼리스트링 | `{"results":[SearchHit,...]}` | `_handle_search` |
| GET | `/api/search/start?q=&source=` | 쿼리스트링 | `202 {"job_id", "state", "elapsed_ms", "steps", "progress"}` | `_handle_search_start` |
| GET | `/api/search/job?id=` | 쿼리스트링 | `200` 작업 스냅샷 / `404 NOT_FOUND` | `_handle_search_job` |
| GET | `/api/slides` · `/api/slides/{index}/thumbnail` | — | 슬라이드 메타 / PNG | `_handle_slides`, `_handle_thumbnail` |
| POST | `/api/preview` | JSON | `{"lines":[...], "pairs":[...]}` | `_handle_preview` |
| POST | `/api/generate` | JSON | pptx 바이너리 | `_handle_generate` |
| GET·POST | `/api/ping` (`?closing=1`·`?keep=1`) | 쿼리스트링(+토큰) | `200` 세션 스냅샷 / closing 은 `403`(토큰 불일치) | `_handle_ping` |
| POST | `/api/shutdown` | (본문 없음) | `200 {"ok":true,"reason",…snapshot}` / `403` | `_handle_shutdown` |
| POST | `/api/reveal` | JSON `{"filename"}` | `200 {"ok":true,"filename":…}` / `403` / `404` | `_handle_reveal` |

`/api/search/start` + `/api/search/job` 는 같은 계약의 두 조각이다(ADR-019): 수집은 블로킹이므로
브라우저가 작업을 시작해 두고 400ms 간격으로 스냅샷을 폴링한다. 스냅샷은
`{job_id, state: running|done|error, elapsed_ms, steps:[{message, done?, total?, percent?}], progress}` 이고
`done` 이면 `results`, `error` 면 `error`(=`describe_error` 와 동일한 3필드)가 함께 온다.
단발 `/api/search` 는 그대로 유지된다 — 스크립트와 테스트가 쓰는 계약이다.

`/api/ping`·`/api/shutdown`·`/api/reveal` 은 ADR-032 의 세션 계약이다(아래 §2.5).

### 2.2 요청/응답 스키마 (확정)

```jsonc
// GET /api/search 응답 200
{ "results": [
    { "index": 0, "title": "…", "artist": "…", "source_name": "lyrics_ovh",
      "preview_lines": ["…", "…", "…"] }   // 정규화 후 앞 3줄, 최대 3개
] }

// POST /api/generate 요청 (검색 기반)
{ "source": "lyrics_ovh", "index": 0, "query": "생명샘",
  "dedupe_chorus": false, "filename": "가사.pptx" }

// POST /api/generate 요청 (직접 입력)
{ "raw_text": "…", "dedupe_chorus": false, "filename": "가사.pptx" }

// POST /api/generate 요청 (이미 정리된 줄 배열)
{ "lines": ["…", "…"], "dedupe_chorus": false, "filename": "가사.pptx" }

// 성공 응답
HTTP/1.1 200 OK
Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation
Content-Disposition: attachment; filename*=UTF-8''<urlencoded>
Content-Length: <n>
```

| 입력 규칙 | 확정 |
| --- | --- |
| `raw_text` 우선 | `raw_text` 가 있으면 수집을 **호출하지 않는다**(네트워크 0회) |
| `lines` 우선 | `lines` 가 있으면 정규화를 건너뛰고 바로 `split_pairs` |
| `query` 필수 | `raw_text`/`lines` 가 없는데 `query` 도 없으면 400 `BAD_REQUEST` |
| `source` 기본 | 미지정 시 `config.DEFAULT_SOURCE` |
| `filename` | 미지정 시 `config.DEFAULT_FILENAME` (`가사.pptx`). 경로 문자(`/`,`\`,`:`) 제거 |
| `dedupe_chorus` | 기본 `false` (D5) |
| 부분 실패 | `lines`/`raw_text` 동시 제공 시 `raw_text` 우선, 로그에 WARNING |

### 2.3 오류 응답 계약

```json
{ "error": { "code": "SOURCE_UNAVAILABLE", "message": "일시적으로 수집에 실패했습니다. 잠시 후 재시도하세요.",
             "recoverable": true } }
```

| HTTP | code | 예외 | 계획서 대응 |
| --- | --- | --- | --- |
| 400 | `BAD_REQUEST` | `ConfigError`, 요청 검증 실패 | 06 §2 |
| 404 | `SOURCE_NOT_FOUND` | `SourceNotFoundError` | 06 §2 |
| 423 | `SOURCE_BLOCKED` | `SourceBlockedError` | 06 §2 |
| 502 | `SOURCE_UNAVAILABLE` | `SourceUnavailableError` | 06 §2 |
| 422 | `EMPTY_LYRICS` | `EmptyLyricsError` | 06 §2 |
| 500 | `TEMPLATE_ERROR` | `TemplateStructureError` | 06 §2 |
| 500 | `OUTPUT_WRITE_ERROR` | `OutputWriteError` | 08 §5 |
| 500 | `INTERNAL` | 그 외 예외 | 07 §4 |

**불변**: 응답 본문에 스택트레이스/파일 경로 원문을 넣지 않는다. 상세는 서버 로그에만(07 §2).
`webapp.py` 는 `pipeline.describe_error()` 가 만든 `UserFacingError` 를 그대로 직렬화한다.

### 2.4 전송 규칙

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 바인딩 | `config.BIND_HOST` 기본 `127.0.0.1` | 외부 노출 차단 (AD-15) |
| 포트 | `config.BIND_PORT` 기본 `8765` | 계획서 06 §2 |
| 서버 | `ThreadingHTTPServer` + 커스텀 `BaseHTTPRequestHandler` | 계획서 06 §2 |
| 빌드 동시성 | 프로세스 전역 `threading.Lock` | 메모리/파일 잠금 보호 (AD-14) |
| 요청 크기 상한 | 1 MiB (`Content-Length` 초과 시 413) | 붙여넣기 텍스트 상한. 악의적 대용량 차단 |
| 캐시 헤더 | 정적 자산 `Cache-Control: no-store` | 개발 편의(단일 사용자) |
| charset | 모든 텍스트 응답 `charset=utf-8` | 인코딩 규칙(08 §4) |

### 2.5 세션 수명과 상태 변경 (ADR-032)

`GET|POST /api/ping` 은 세션의 현재 상태를 그대로 돌려준다(하트비트·탭 닫힘 비콘·상태 조회를 겸한다).

```jsonc
// GET /api/ping → 200 (필드 전부 webapp.Session.snapshot() 에서 나온다)
{ "ok": true, "version": "0.1.0", "instance": "f6c42b0ad7d5c0e7",
  "auto_shutdown": true, "busy": false,
  "tab_grace_s": 180.0, "idle_timeout_s": 1200.0,
  "idle_seconds_left": 1091, "lifetime_seconds_left": 10518,
  "closing": false, "uptime_s": 282, "shutdown_reason": null }
```

| 규칙 | 값(기본) | 발동 조건 |
| --- | --- | --- |
| 탭 신호 | `WEBUI_TAB_GRACE` = 180초 | `?closing=1`(토큰 필요) 을 받은 뒤 180초. 어떤 ping 이든 취소한다 |
| 무활동 | `WEBUI_IDLE` = 1200초 | **사용자 요청**이 없는 채 1200초. ping/하트비트는 "사용"이 아니다 |
| 최대 수명 | `WEBUI_MAX_LIFETIME` = 10800초 | 탭이 열려 있어도 강제 종료 |
| 보류 | — | `in_flight > 0` 또는 실행 중 검색 작업이면 위 세 규칙 모두 대기 |
| 전부 끄기 | `WEBUI_AUTOSHUTDOWN=0` | 세 규칙 전부 비활성(스크립트·배치) |

- **권한 판정은 한 곳**(`_page_may_ask`)이다: `X-Lyrics2Pptx-Token` 이 `hmac.compare_digest` 로 일치해야 하고,
  `Origin` 헤더가 있으면 `http://<Host>`/`https://<Host>` 여야 한다. 불일치는 `403 FORBIDDEN`(상태·이유 로그만 남긴다).
  토큰은 `GET /` 응답의 `<meta name="lyrics2pptx-session">` 으로만 나가고 정적 JS/CSS 에는 넣지 않는다.
- `?closing=1` 은 페이지가 `pagehide` 에서 `keepalive` 로 보내는 **탭 닫힘 비콘**이다. 일반 ping 은 이 판단을 취소한다.
  `?keep=1` 은 화면의 "계속 쓰기"이고 **사용**으로 세어 idle 을 되돌린다.
- `POST /api/shutdown` 은 즉시 종료가 아니라 **요청 기록**(`reason="사용자 요청"`)이다. 진행 중 작업이 끝나면 워치독이 내린다
  (응답을 먼저 보낸 뒤 타이머로 `shutdown()`).
- `POST /api/reveal` 의 `filename` 은 sanitize 후 `OUTPUT_DIR` 안으로 resolve 한다. 벗어나거나 파일이 없으면 404 —
  정적 서빙 containment 와 같은 규칙이다.
- **재사용 판정은 클라이언트 쪽에도 한 벌 있다**(`webapp.reuse_url()`, ADR-033): `output/webui.state` 의 `port`·`instance` 를 읽어
  `127.0.0.1` 로 다시 물어보고(`/api/ping`) instance 가 같을 때만 그 URL 을 돌려준다. 주소는 파일에서 믿지 않고 **직접 만든다** —
  손으로 고친 상태 파일이 런처를 다른 호스트로 보낼 수 없다.

## 3. CLI 계약 (`cli.py`)

| 옵션 | 값 | 기본 | 검증 |
| --- | --- | --- | --- |
| `--query` | 검색어 | — | `--input-file`/`--lines-file` 과 배타 |
| `--input-file` | 텍스트 파일(UTF-8) | — | 존재 검사 → `ConfigError` |
| `-o/--output` | 출력 경로 | `output/<filename>` | 디렉터리 자동 생성 |
| `--source` | 소스 이름(`auto`·`blog`·`lrclib`·`lyrics_ovh`) | `config.DEFAULT_SOURCE`=`auto` | `get_source()` 가 `ConfigError` |
| `--index` | 검색 결과 선택 (`blog` 는 0=합의, 1~=개별 페이지 = `호스트 · N줄`; 투표에서 빠진 페이지는 `(투표 제외)` 이며 뒤로 정렬) | `0` | 범위 밖 → `SourceNotFoundError` |
| `--cross-check` | 블로그 수집·교차검증 결과만 출력(빌드 안 함). 합의의 줄 순서는 *노래를 한 번 인쇄한 페이지*를 따르고(ADR-023), 한 자리를 다른 말로 적은 페이지의 표기는 그 자리의 `다른 표기` 로 보고된다(ADR-024). 2장 미만이 투표하면 헤더부터 `교차검증 안 됨` + 경고 줄, `--json` 은 `cross_checked`·`pages_seen` (ADR-020) | off | `--query` 필요 |
| `--dedupe-chorus` | 후렴 중복 제거 | off | D5 |
| `--skip-slide` | 배경 풀 제외(1-based, 반복) | 없음 | 범위 검증 → `ConfigError` |
| `--keep-notes` | 노트 복제 | off | AD-08 |
| `--json` | 수집 결과 JSON stdout | off | RawSong 직렬화 |
| `--strategy` | `auto`/`parts`/`zip` | `auto` | §6 폴백 강제 |
| `--log-level` | 로깅 수준 | env/INFO | AD-16 |

**종료 코드 (확정)**

| 코드 | 의미 |
| --- | --- |
| 0 | 성공 |
| 1 | 사용자 오류 (`ConfigError`, `SourceNotFoundError`) |
| 2 | 재시도 후 실패 (`SourceBlockedError`, `SourceUnavailableError`) |
| 3 | 콘텐츠 문제 (`EmptyLyricsError`) |
| 4 | 템플릿/저장 실패 (`TemplateStructureError`, `OutputWriteError`) |

`cli.py` 는 `pipeline.describe_error()` 의 `message` 를 stderr 로, 성공 시 `output_path` 를 stdout 으로 출력한다.
`--json` 은 stdout 에 JSON 만 출력하고 로그는 stderr 로 보낸다(파이프 안전성).

## 4. 파이프라인 계약 (`pipeline.py`)

```python
def run(query: str | None = None, *, source: str = DEFAULT_SOURCE, index: int = 0,
        raw_text: str | None = None, lines: list[str] | None = None,
        dedupe_chorus: bool = False, skip_slide: tuple[int, ...] = (),
        keep_notes: bool = False, strategy: str = "auto",
        output_path: Path | None = None) -> BuildResult: ...

def search_hits(query: str, *, source: str = DEFAULT_SOURCE, limit: int = 5) -> list[SearchHit]: ...
def describe_error(exc: BaseException) -> UserFacingError: ...
def resolve_output_path(output_path: Path | None, *, title: str | None) -> Path: ...
```

| 계약 | 확정 |
| --- | --- |
| `raw_text` 제공 | `search()` 를 **호출하지 않는다** (네트워크 0회 보장) |
| `lines` 제공 | `normalize_lines()` 도 건너뛴다 |
| 세 입력 동시 | `lines` > `raw_text` > `query` 우선순위, 낮은 우선순위 무시 + WARNING |
| 반환 | 항상 `BuildResult`(경로 포함). 실패 시 `Lyrics2PptxError` 하위만 |
| 파일 I/O | `output_path` 결정과 원자적 저장만 담당, 저장 자체는 `builder` |

## 5. 설정 계약 (환경변수 — 전체 목록)

**접근 지점은 오직 `config.py`.** 아래 이름 외에 새 env 를 도입하지 않는다(추가 시 이 표 갱신 필수).

| 환경변수 | 타입 | 기본값 | 소비 모듈 |
| --- | --- | --- | --- |
| `LYRICS2PPTX_TEMPLATE` | path | `<repo>/template.pptx` | `config`, `template_reader` |
| `LYRICS2PPTX_OUTPUT_DIR` | path | `<repo>/output` | `config`, `builder` |
| `LYRICS2PPTX_WEB_DIR` | path | `<repo>/web` | `config`, `webapp` |
| `LYRICS2PPTX_FIXTURE_DIR` | path | (없음) | `config`, `httputil` |
| `LYRICS2PPTX_DEFAULT_FILENAME` | str | `가사.pptx` | `config`, `pipeline` |
| `LYRICS2PPTX_SOURCE` | str | `auto` | `config`, `pipeline` |
| `LYRICS2PPTX_SEARCH_BACKEND` | str | `ddg` | `config`, `websearch` |
| `LYRICS2PPTX_BLOG_MAX_PAGES` | int(1–10) | `5` | `config`, `blog` |
| `LYRICS2PPTX_HTTP_DELAY` | float(초) | `2.0` | `httputil` |
| `LYRICS2PPTX_HTTP_TIMEOUT` | float(초) | `10` | `httputil` |
| `LYRICS2PPTX_LRCLIB_TIMEOUT` | float(초) | `10` | `config`, `crawl/lrclib` |
| `LYRICS2PPTX_MAX_RETRIES` | int | `3` | `httputil` |
| `LYRICS2PPTX_USER_AGENT` | str | `lyrics2pptx/0.1 (+local single-user tool)` | `httputil` |
| `LYRICS2PPTX_BIND_HOST` | str | `127.0.0.1` | `webapp` |
| `LYRICS2PPTX_BIND_PORT` | int | `8765` | `webapp` |
| `LYRICS2PPTX_LOG_LEVEL` | str | `INFO` | `config`, 진입점 |
| `LYRICS2PPTX_KEEP_NOTES` | `0`/`1` | `0` | `builder` |
| `LYRICS2PPTX_DEDUPE_CHORUS` | `0`/`1` | `0` | `pipeline` |
| `LYRICS2PPTX_CACHE_ENABLED` | `0`/`1` | `1` | `pipeline` (검색 메모리 캐시) |
| `LYRICS2PPTX_CLONE_STRATEGY` | `auto`/`parts`/`zip` | `auto` | `builder` |
| `LYRICS2PPTX_TITLE_SHAPE` | str | `제목 1` | `fill`, `template_reader` |
| `LYRICS2PPTX_WEBUI_TAB_GRACE` | float(초) | `180` | `config`, `webapp` (ADR-032) |
| `LYRICS2PPTX_WEBUI_IDLE` | float(초) | `1200` | `config`, `webapp` (ADR-032) |
| `LYRICS2PPTX_WEBUI_MAX_LIFETIME` | float(초) | `10800` | `config`, `webapp` (ADR-032) |
| `LYRICS2PPTX_WEBUI_AUTOSHUTDOWN` | `0`/`1` | `1` | `config`, `webapp` (ADR-032) |
| `LYRICS2PPTX_WEBUI_STATE_FILE` | path | `<repo>/output/webui.state` | `config`, `webapp` (ADR-032) |

| 규칙 | 값 |
| --- | --- |
| 잘못된 값 | `ConfigError` 로 즉시 실패(조용한 기본값 대체 금지). 단 `LOG_LEVEL` 은 WARNING 후 INFO |
| 우선순위 | CLI 인자 > 환경변수 > 기본값 |
| 비밀값 | 토큰형 어댑터는 `LYRICS2PPTX_<SOURCE>_TOKEN` 규약을 따르되 **로그에 절대 출력 금지** |

## 6. 파일 계약

| 항목 | 규칙 | 소유 |
| --- | --- | --- |
| 템플릿 | 읽기 전용. 쓰기 모드 open 금지 | `template_reader` |
| 산출물 경로 | `resolve_output_path()`: `output/` 안. 이름 sanitize | `pipeline` |
| 임시 파일 | `output/.tmp-<uuid>.pptx` → `os.replace` | `builder` (AD-13) |
| 인코딩 | 모든 텍스트 파일 UTF-8 no BOM, LF | 전 모듈 |
| 정적 자산 | `web/` 밖 파일 제공 금지(경로 해결 후 containment 검사) | `webapp` (AD-15) |
| 런처 rendezvous | `output/webui.state` — ASCII `key=value` + `#` 주석, 원자적 쓰기, **instance 일치 시에만** 삭제 | `webapp` (ADR-032) |
| 숨김 실행 로그 | `output/launch.log` — 런처의 stdout/stderr. `.vbs` 는 콜드 스타트마다 새로 만들고(truncate), 패키지 실행은 **append + 1MiB 회전**이라 두 번 눌러도 기록이 살아남는다 | 런처 (ADR-017) · `webapp.launch` (ADR-033) |
| 패키지 배치 | 얼린 실행에서 `ROOT` = **exe 가 있는 폴더**(템플릿·`output/`), `web/` = 번들 내부(`sys._MEIPASS`). 환경변수가 항상 이긴다 | `config` (ADR-033) |
| 탐색기 열기 대상 | `/api/reveal` 은 `OUTPUT_DIR` 안의 **파일**만, sanitize 후 resolve | `webapp` (ADR-032) |
| fixture | `tests/fixtures/` 에서만, `LYRICS2PPTX_FIXTURE_DIR` 설정 시 | `httputil` |
