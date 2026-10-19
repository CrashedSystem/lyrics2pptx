# 07. 관측성과 설정 (Observability & Configuration)

상위: `아키텍처.md` §4 / 환경변수 목록은 `05-interface-contracts.md` §5 와 동일 (여기서는 정책·이벤트를 확정).

## 1. 로깅 정책 (확정)

| 항목 | 값 |
| --- | --- |
| 라이브러리 | 표준 `logging` (`print` 금지 — 단 `cli --json` 의 stdout 페이로드 제외) |
| 루트 로거 | `lyrics2pptx` (패키지 로거). 모듈 로거는 `logging.getLogger(__name__)` |
| 기본 수준 | `INFO` (`LYRICS2PPTX_LOG_LEVEL` 로 변경) |
| 포맷 | `%(asctime)s %(levelname)s %(name)s %(message)s` |
| 날짜 포맷 | `%Y-%m-%dT%H:%M:%S` |
| 스트림 | `stderr` (stdout 은 데이터 전용 → 파이프 안전) |
| 초기화 지점 | 진입점에서 **1회만** `logging.basicConfig(level=..., format=..., stream=sys.stderr)` |
| 라이브러리 로거 | `crawl`/`text`/`pptx`/`pipeline` 은 `basicConfig` 를 호출하지 않는다 (진입점이 소유) |
| 서드파티 로거 | `pptx`, `lxml`, `PIL` 로거는 `WARNING` 으로 올린다(노이즈 억제) |

```python
# 진입점 공통 초기화 (cli.py / webapp.py)
logging.basicConfig(
    level=config.LOG_LEVEL, stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
for noisy in ("pptx", "PIL", "lxml"): logging.getLogger(noisy).setLevel(logging.WARNING)
```

## 2. 로그 이벤트 표 (무엇을 어느 수준에 남기는가)

| 이벤트 | 로거 | 수준 | 메시지(형식) | 금지 내용 |
| --- | --- | --- | --- | --- |
| 프로세스 시작 | `lyrics2pptx.cli`/`.webapp` | INFO | `started version=%s python=%s` | — |
| 템플릿 프로필 로드 | `pptx.template_reader` | INFO | `template profile loaded slides=%d pool=%d cached=%s` | — |
| 캐시 히트/미스 | `pptx.template_reader` | DEBUG | `profile cache %s key=(size=%d,mtime=%d)` | — |
| 수집 요청 | `crawl.httputil` | INFO | `GET host=%s path=%s attempt=%d` | 쿼리스트링 원문(검색어 60자 truncate) |
| robots 판정 | `crawl.httputil` | INFO | `robots host=%s allowed=%s` | — |
| 지연 대기 | `crawl.httputil` | DEBUG | `delay host=%s wait=%.2fs` | — |
| 재시도 | `crawl.httputil` | WARNING | `retry attempt=%d status=%s backoff=%ds` | — |
| 수집 성공 | `crawl.<site>` | INFO | `source=%s hits=%d chosen_index=%d` | 가사 본문 |
| 교차검증 투표 | `crawl.blog` | INFO | `blog voted pages=%d of=%d lines=%d disputed=%d` (`of` = 제안된 페이지 수, ADR-020) | 가사 본문 |
| 정규화 결과 | `text.normalize` | DEBUG | `normalized lines=%d dropped=%d` | 줄 텍스트 |
| 분할 결과 | `text.split` | INFO | `split pairs=%d odd=%s` | 줄 텍스트 |
| 복제 전략 선택 | `pptx.builder` | INFO | `clone strategy=%s (probe=%s)` | — |
| 방식 A 실패→폴백 | `pptx.builder` | WARNING | `fallback to zip writer reason=%s` | — |
| 빌드 완료 | `pptx.builder` | INFO | `built output=%s slides=%d pool=%d wraps=%d bytes=%d` | — |
| 재열기 검증 실패 | `pptx.builder` | ERROR | `reopen verification failed output=%s` | — |
| 스타일 폴백 | `pptx.fill` | WARNING | `title1 style fallback slide=%d → default` | — |
| HTTP 접근 로그 | `lyrics2pptx.webapp` | INFO | `%s %s -> %d %.0fms` (method path status dur) | 요청 본문 |
| 처리되지 않은 예외 | `lyrics2pptx.webapp` | ERROR | `unhandled error path=%s` + `logging.exception` | 응답 본문에는 미포함 |
| 사용자 오류 응답 | `lyrics2pptx.webapp` | WARNING | `error code=%s status=%d` | 스택트레이스(별도 ERROR 로만) |
| 검색 작업 종료 | `lyrics2pptx.webapp` | INFO | `search job id=%s state=%s steps=%d` | 가사 본문 |
| 검색 진행 상황 | `crawl.progress` | (로그 아님) | 싱크로 전달되는 표시용 문자열 — `job.steps` 에만 쌓이고 로그로는 남기지 않는다 | 가사 본문(진행 문구는 호스트명/건수만) |
| 검색어/가사 본문 | 어디든 | DEBUG | 60자 truncate, prefix `q=` | INFO 이상 금지 |

**원칙**: INFO 는 "운영자가 봐야 하는 사건", WARNING 은 "복구했지만 주목할 사건", ERROR 는 "사용자에게 실패를 반환한 사건".
DEBUG 는 진단용이며 개인정보를 포함할 수 있어 기본 비활성.

## 3. 상관관계(Correlation)

| 항목 | 결정 |
| --- | --- |
| 요청 ID | 웹앱은 요청마다 `uuid4().hex[:8]` 을 생성, 해당 요청 로그 라인 접두에 `rid=%s` 로 붙인다 (LoggerAdapter 사용) |
| 빌드 ID | CLI/웹 모두 빌드 1건에 `build_id` 를 부여, 산출 파일명과 무관하게 로그로만 추적 |
| 전파 | `LoggerAdapter({"rid": rid})` 를 통해 하위 로거로 전파하지 않고, 진입점 로그에만 붙인다 (단순성) |
| 분산 트레이싱 | 채택 안 함 — in-process 단일 프로세스, 스팬 개념이 무의미 |

```python
# webapp.py 요지
log = logging.LoggerAdapter(logging.getLogger("lyrics2pptx.webapp"), {"rid": uuid4().hex[:8]})
log.info("POST /api/generate -> begin")
```

## 4. 오류 관측 경계 (스택트레이스는 어디로)

| 계층 | 하는 일 | 하지 않는 일 |
| --- | --- | --- |
| `pipeline.describe_error()` | 예외 → `UserFacingError`(코드/문구/HTTP) | 로깅하지 않는다(순수 변환) |
| `webapp.py` | `logging.exception(rid=...)` 로 ERROR 기록 → 응답은 `UserFacingError` 만 | 응답 본문에 스택/경로 원문 미포함 |
| `cli.py` | `logging.debug(..., exc_info=True)` (DEBUG 시) → stderr 에 `message`, 종료코드 | stdout 오염 금지 |
| 미처리 예외 | `webapp` 최상위 `except Exception` → 500 `INTERNAL` + ERROR 로그 | 프로세스 종료(서버는 계속) — CLI 는 종료코드 4 |

## 5. 관측하지 않는 것 (의도적 미채택)

| 미채택 | 이유 |
| --- | --- |
| 메트릭 수집기(Prometheus/StatsD) | 로컬 단일 프로세스·단일 사용자. 로그의 `slides=`, `bytes=`, `dur` 로 충분 |
| 분산 트레이싱(OpenTelemetry) | 프로세스 1개, 호출 스택이 곧 트레이스 |
| APM/에러 트래커(Sentry) | 외부 전송 없는 로컬 도구(§06 §4 텔레메트리 없음) |
| 헬스체크 엔드포인트 | 서버 생존 확인은 `GET /` 로 충분 |
| 로그 파일 로테이션 | stdout/stderr 를 사용자 셸이 관리. 파일 로깅 미지원 |
| 구조화 로깅(JSON) | 사람이 읽는 로그 + `--json` 은 데이터 전용으로 분리 |

## 6. 설정 아키텍처 (config.py 단일 출처)

```python
# config.py — os.environ 에 접근하는 유일한 모듈 (P9)
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # D:\projects\lyrics

def _env_path(name: str, default: Path) -> Path: ...
def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float: ...
def _env_int(name: str, default: int, *, minimum: int = 0) -> int: ...
def _env_bool(name: str, default: bool) -> bool: ...
def _env_str(name: str, default: str) -> str: ...

TEMPLATE_PATH   = _env_path("LYRICS2PPTX_TEMPLATE", ROOT / "template.pptx")
OUTPUT_DIR      = _env_path("LYRICS2PPTX_OUTPUT_DIR", ROOT / "output")
WEB_DIR         = _env_path("LYRICS2PPTX_WEB_DIR", ROOT / "web")
FIXTURE_DIR     = (p if (p := os.environ.get("LYRICS2PPTX_FIXTURE_DIR")) else None)
...
LOG_LEVEL       = _env_str("LYRICS2PPTX_LOG_LEVEL", "INFO").upper()
```

| 규칙 | 값 |
| --- | --- |
| 타입 강제 | 각 `_env_*` 함수가 변환. 실패 시 `ConfigError("환경변수 X 값이 올바르지 않습니다")` |
| 범위 검증 | delay ≥ 0, port 1..65535, retries 0..10. 위반 시 `ConfigError` |
| 기본값 | 전부 저장소 기준 상대 경로(`ROOT` 기준) → cwd 무관 |
| 동결 | import 시 1회 평가(모듈 상수). 테스트는 `importlib.reload(config)` 또는 `unittest.mock.patch` |
| 파생 계산 | `LOG_LEVEL_OBJ = getattr(logging, LOG_LEVEL, logging.INFO)` |
| 미정의 env | 무시(S3: 오타 방지를 위해 시작 시 `LYRICS2PPTX_*` prefix 의 미지의 키를 WARNING 로그) |

문자열 상수도 `config.py` 소유:

| 상수 | 값 | 사용처 |
| --- | --- | --- |
| `DEFAULT_SOURCE` | `"auto"` (lrclib → blog 체인, ADR-018) | `pipeline`, `cli`, `webapp` |
| `SEARCH_BACKEND` | `"ddg"` (robots 허용 색인만) | `websearch` |
| `BLOG_MAX_PAGES` | `5` | `blog` |
| `DEFAULT_FILENAME` | `"가사.pptx"` | `pipeline` |
| `DEFAULT_UA` | `"lyrics2pptx/0.1 (+local single-user tool)"` | `httputil` |
| `TITLE_SHAPE_NAME` | `"제목 1"` | `fill`, `template_reader` |
| `NORM_AUTOFIT` | `True` | `fill` |
| `REQUEST_MAX_BYTES` | `1_048_576` | `webapp` |
| `ROBOTS_CACHE_TTL` | `600.0` | `httputil` |
| `TEMPLATE_SHAPE_MAX_...` | (없음) | — |

> `TITLE_SHAPE_NAME` 을 `config` 에 두는 이유: 도형명이 바뀌면 env 로 재배포 없이 대응 가능하게 하되,
> 기본값은 실측값 `"제목 1"` 로 고정한다. (`LYRICS2PPTX_TITLE_SHAPE` env 추가는 §05 §5 표 갱신 후)

## 7. 진단(Diagnostics) 명령 (읽기 전용, 선택)

| 명령 | 출력 | 목적 |
| --- | --- | --- |
| `python -m lyrics2pptx.cli --doctor` | 템플릿 존재/크기/슬라이드 수/풀/스타일 유무, python-pptx 버전, clone strategy probe 결과, fixture 모드 여부 | T04 실패 시 즉시 원인 파악 |
| `--doctor --json` | 위 정보를 JSON | 스크립트/이슈 리포트 |

`--doctor` 는 파일을 생성하지 않는다(읽기 전용). 스타일 폴백 대상 슬라이드(slide1 등)도 여기서 확인 가능.
