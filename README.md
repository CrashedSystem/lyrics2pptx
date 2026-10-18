# lyrics2pptx — 가사 2줄 PPTX 생성기

검색어로 가사를 모으거나 가사를 직접 붙여넣으면, `template.pptx` 의 배경 슬라이드를
복제해 **슬라이드 한 장에 가사 2줄씩** 채운 `.pptx` 를 만들어 주는 **로컬 단일 사용자** 도구다.
서버·클라우드·계정이 필요 없고, 실행은 Windows 로컬 프로세스 하나와 로컬 웹서버(127.0.0.1)뿐이다.
템플릿은 **읽기 전용 입력**으로만 열리며 절대 수정되지 않는다.

> 상태: `src/`+`web/` 구현 완료. 테스트 `293 tests OK`. 진입점 `python -m lyrics2pptx.cli` / `python -m lyrics2pptx.webapp`.

---

## 1. 빠른 시작

```powershell
# 0) 사전 준비: Python 3.12, 그리고 리포지토리 루트에 template.pptx (20장) 가 있어야 한다.

# 1) 설치
python -m pip install -r requirements.txt
python -m pip install -e .

# 2) 실행 전 자가진단 (파일을 만들지 않음)
python -m lyrics2pptx.cli --doctor

# 3a) 검색어로 생성 (기본 소스 = lyrics.ovh, 네트워크 필요)
python -m lyrics2pptx.cli --query "아티스트 - 곡명"

# 3b) 가사를 직접 붙여넣어 생성 (오프라인, 한국어에 권장)
python -m lyrics2pptx.cli --input-file "가사.txt"

# 4) 산출물 위치
#    기본: output\<제목>.pptx   (제목을 못 얻으면 output\가사.pptx)
#    지정: -o "output\내파일.pptx"
```

Windows 한국어 콘솔(cp949)에서는 위 명령의 한글이 깨질 수 있다. `.\scripts\run.ps1` 은
콘솔과 자식 인터프리터를 UTF-8 로 고정하므로 **한글이 깨지지 않는다**(§4 참고).

```powershell
.\scripts\run.ps1 -Query "아티스트 - 곡명"     # 검색 → 빌드
.\scripts\run.ps1 -LinesFile "lines.txt"      # 이미 정리된 줄 파일 → 빌드
.\scripts\run.ps1 -Web                        # 웹 UI (Ctrl+C 로 종료)
.\scripts\run.ps1                             # 스위치 없음 = -Doctor (읽기 전용)
```

---

## 2. CLI 사용법

진입점: `python -m lyrics2pptx.cli` (직접 실행 시 cp949 콘솔에서 `--help`/오류 메시지가 `?` 로 대체될 수 있음 — 프로세스는 죽지 않는다).

| 옵션 | 설명 |
| --- | --- |
| `--query "…"` | 검색어. `"아티스트 - 곡명"` 또는 곡명 단독 |
| `--input-file PATH` | 직접 입력 텍스트(UTF-8). 정규화(N1~N10) 적용 |
| `--lines-file PATH` | 이미 정리된 줄 파일(한 줄 = 한 문장). 정규화 생략 |
| `-o`, `--output PATH` | 출력 경로 (기본 `output\<제목>.pptx`) |
| `--source NAME` | 수집 소스 이름 (기본 `lyrics_ovh`) |
| `--index N` | 검색 결과 중 선택할 번호 (기본 `0`) |
| `--dedupe-chorus` | 후렴 등 3회 이상 반복 줄을 2회까지만 유지 |
| `--skip-slide N` | 배경 풀에서 제외할 템플릿 슬라이드 번호(1-based, 반복 가능) |
| `--keep-notes` | 템플릿 노트(noteSlide)도 복제 (기본: 복제 안 함) |
| `--strategy {auto,parts,zip}` | 슬라이드 복제 전략 (기본 `auto`) |
| `--json` | 빌드하지 않고 `--query` 수집 결과만 JSON 으로 stdout 출력 |
| `--log-level LEVEL` | 로그 수준 (기본 `INFO`) |
| `--doctor` | 템플릿/전략 진단만 출력, 파일 미생성 |
| `-h`, `--help` | 도움말 |

`--query` / `--input-file` / `--lines-file` 는 **상호 배타**다(하나만). 우선순위: `lines` > `raw_text` > `query`.

**종료코드**: `0` 성공 · `1` 잘못된 요청/검색 결과 없음 · `2` 소스 차단/일시 장애 · `3` 유효한 가사 줄 없음 · `4` 템플릿/저장 오류.

검증된 예 (실제 실행):

```
> python -m lyrics2pptx.cli --doctor            # exit 0, 슬라이드 20장/풀 20 보고
> python -m lyrics2pptx.cli --input-file tests\fixtures\lyrics_sample.txt -o out.pptx
                                                 # exit 0, 4줄 → 2장 생성
> $env:LYRICS2PPTX_FIXTURE_DIR="tests\fixtures"; python -m lyrics2pptx.cli --query "생명샘"
                                                 # exit 0 (오프라인 fixture 모드)
```

> 실 네트워크 `--query` 는 이 문서 작성 환경(오프라인)에서 **미검증**이다. 위 fixture 모드와
> 동일한 코드 경로를 타며, 저장 응답으로만 대체된다.

---

## 3. 웹 UI 사용법

```powershell
python -m lyrics2pptx.webapp        # 또는: .\scripts\run.ps1 -Web
# 브라우저에서 http://127.0.0.1:8765 열기 (Ctrl+C 로 종료)
```

화면 흐름: **(1) 가사 가져오기**(검색 / 직접 입력) → **(2) 곡 선택** → **(3) 미리보기**
→ **(4) 다운로드**. 직접 입력 모드의 미리보기는 서버(`POST /api/preview`)가 계산한 값을
그대로 보여 주므로 **미리보기와 실제 파일이 어긋나지 않는다**.

HTTP 라우트(로컬 전용):

| 메서드 | 경로 | 역할 |
| --- | --- | --- |
| `GET` | `/` | UI 페이지 |
| `GET` | `/static/*` | 정적 자산(web/ 밖으로 나가는 경로는 404) |
| `GET` | `/api/search?q=…&source=…` | 검색 결과(제목/아티스트/부분 미리보기 3줄) |
| `POST` | `/api/preview` | `{raw_text, dedupe_chorus}` → 정규화된 줄 + 2줄 페어 |
| `POST` | `/api/generate` | `{query\|raw_text\|lines, …}` → 생성된 `.pptx` 바이트 다운로드 |

서버는 `127.0.0.1` 에만 바인딩되고, 요청 본문은 최대 1 MiB, 정적 서빙은 `web/` 안으로 제한된다.
빌드는 전역 락으로 1건씩 직렬 처리된다(템플릿이 37 MB in-process 자원이기 때문).

검증된 예 (실제 실행): `GET /`→200, `GET /api/search?q=생명샘`→200,
`POST /api/preview {raw_text:"[Verse 1]\nhello\nworld\nfoo"}`→200
`{"lines":["hello","world","foo"],"pairs":[["hello","world"],["foo",null]],"removed_marker_count":1}`.

---

## 4. `scripts/run.ps1` 스위치

Windows 전용 러너(ASCII 전용 스크립트 — PS 5.1 이 BOM 없는 .ps1 을 ANSI 로 해석하는 문제를 피한다).
콘솔과 `PYTHONIOENCODING=utf-8` 을 UTF-8 로 고정하고, 리포지토리 루트에서 실행하며,
venv → `py -3.12` → `python` 순으로 인터프리터를 찾는다. 미설치 상태면 `PYTHONPATH=src` 로 폴백한다.

| 스위치 | 하는 일 | 검증 |
| --- | --- | --- |
| (없음) / `-Doctor` | 읽기 전용 진단 (`--doctor`) | exit 0 |
| `-Query "…"` | 검색 → 빌드 | (라이브 네트워크 미검증) |
| `-LinesFile PATH` | 정리된 줄 파일 → 빌드 | exit 0 |
| `-Web` | 로컬 웹 UI 실행 (Ctrl+C 종료) | 서버 기동/라우트 200 |
| `-Test` | 전체 회귀 테스트 | exit 0 |
| `-Output PATH` | `-Query`/`-LinesFile` 의 출력 경로 | 적용 |
| `-Extra '…','…'` | CLI 에 그대로 덧붙일 추가 인자 | 적용 |

한 번에 **정확히 한 모드**만 허용한다. 두 개를 주면 사용법을 출력하고 `exit 2` (검증됨).

> 주의: `.\scripts\run.ps1 -Doctor 2>&1 | …` 처럼 호출 **바깥에서 stderr 를 리다이렉트**하면
> `$ErrorActionPreference='Stop'` 과 겹쳐 스크립트가 조기 종료(exit 1)될 수 있다. 스위치는
> 리다이렉트 없이 그대로 호출하라.

---

## 5. 환경변수

전부 `LYRICS2PPTX_*` 이며 **필수 없음**(모두 기본값 보유). 접근은 `config.py` 한 곳(규칙 P9).
오타 방지를 위해 알 수 없는 `LYRICS2PPTX_*` 는 실행 시 WARNING 으로 경고된다.

| 변수 | 타입 | 기본값 | 소비 |
| --- | --- | --- | --- |
| `LYRICS2PPTX_TEMPLATE` | path | `<repo>\template.pptx` | config, template_reader |
| `LYRICS2PPTX_OUTPUT_DIR` | path | `<repo>\output` | config, builder |
| `LYRICS2PPTX_WEB_DIR` | path | `<repo>\web` | config, webapp |
| `LYRICS2PPTX_FIXTURE_DIR` | path | (없음) | config, httputil (오프라인 모드) |
| `LYRICS2PPTX_DEFAULT_FILENAME` | str | `가사.pptx` | config, pipeline |
| `LYRICS2PPTX_SOURCE` | str | `lyrics_ovh` | config, pipeline |
| `LYRICS2PPTX_HTTP_DELAY` | float(초) | `2.0` | httputil |
| `LYRICS2PPTX_HTTP_TIMEOUT` | float(초) | `10` | httputil |
| `LYRICS2PPTX_MAX_RETRIES` | int | `3` | httputil |
| `LYRICS2PPTX_USER_AGENT` | str | `lyrics2pptx/0.1 (+local single-user tool)` | httputil |
| `LYRICS2PPTX_BIND_HOST` | str | `127.0.0.1` | webapp |
| `LYRICS2PPTX_BIND_PORT` | int | `8765` | webapp |
| `LYRICS2PPTX_LOG_LEVEL` | str | `INFO` | config, 진입점 |
| `LYRICS2PPTX_KEEP_NOTES` | `0`/`1` | `0` | builder |
| `LYRICS2PPTX_DEDUPE_CHORUS` | `0`/`1` | `0` | pipeline |
| `LYRICS2PPTX_CACHE_ENABLED` | `0`/`1` | `1` | pipeline |
| `LYRICS2PPTX_CLONE_STRATEGY` | `auto`/`parts`/`zip` | `auto` | builder |
| `LYRICS2PPTX_TITLE_SHAPE` | str | `제목 1` | fill, template_reader |
| `LYRICS2PPTX_<SOURCE>_TOKEN` | str | (없음) | 토큰형 어댑터 — **비밀값, 로그 금지** |

---

## 6. 디렉터리 구조

```
lyrics/
  template.pptx            읽기 전용 입력 (20장, 약 37 MB)
  pyproject.toml           패키지 메타 (src 레이아웃, pip install -e .)
  requirements.txt         런타임 핀: python-pptx==1.0.2
  scripts/run.ps1          Windows 러너
  web/                     index.html · app.js · style.css (vanilla JS)
  output/                  생성된 .pptx 저장 위치 (gitignore; .gitkeep 만 추적)
  src/lyrics2pptx/
    cli.py, webapp.py      L4 진입점
    pipeline.py            L3 조립 + describe_error() 단일 오류 매핑
    config.py, errors.py   L0 설정/예외
    crawl/                 수집 축 (base, httputil, lyrics_ovh, html_generic)
    text/                  정규화/2줄 분할 (normalize, split)
    pptx/                  템플릿 읽기·복제·채우기·쓰기 (writer*, builder, clone, fill…)
  tests/                   293 tests (표준 unittest)
  docs/plan/ docs/arch/     설계 문서 · docs/guide/ 사용가이드 · docs/adr/ 결정 기록
```

---

## 7. 테스트 실행

```powershell
python -m unittest discover -s tests -t .
```

검증된 결과(실제 실행): `Ran 293 tests … OK`, exit 0. 네트워크 실호출 0회이며 collect 단계는
fixture/mock 으로 격리된다. `.\scripts\run.ps1 -Test` 도 동일하게 293 tests / OK / exit 0.

정적 게이트:

- `python -m compileall -q src tests` (문법 검사, exit 0) — CI 의 유일한 정적 게이트.
- `node --check web/app.js` (exit 0).
- **린트는 게이트가 아니다.** 저장소에 `ruff` 설정이 없고 기본값 baseline 은
  `ruff check src tests web` → 77건(exit 1). 정책은 환경선언서 §3 참고.

---

## 8. 가사 저작권 고지

가사는 **저작권법의 보호를 받는 창작물**이다. 이 도구는 다음과 같은 용도로만 사용해야 한다.

- **개인적·비상업적 용도 한정**(개인 감상, 발표 자료 초안 등).
- 가사 권리는 **권리자(작사가·작곡가·음원 권리자)에게 있다.** 이 도구는 권리를 양도하지 않는다.
- 생성물을 **배포·공연·상업적 이용·재배포**하면 저작권 침해가 될 수 있다.
- 자동 수집은 **robots.txt 와 이용약관을 준수하는 소스만 기본 제공**한다(`lyrics_ovh` — 키 없는 공개 API).
  `crawl/html_generic.py` 는 **기본 비활성**이며, 활성화하려면 대상 사이트의 robots.txt 와
  이용약관을 직접 확인해야 한다. 법률 자문이 아니며 책임은 사용자에게 있다.
- 저작권이 걱정되면 자동 수집 대신 **직접 입력**(`--input-file`, 웹 UI "직접 입력")으로
  본인이 사용 권한을 가진 가사만 넣어라.

---

## 9. 알려진 한계

- **기본 검색 소스의 한국어 커버리지가 낮다.** `lyrics_ovh`(키 없는 공개 API)는 K-pop/한국 가요
  데이터가 부실하다. 한국어 가사는 **직접 입력 경로를 권장**한다(`--input-file` / 웹 UI 직접 입력).
  다른 소스는 `crawl/<site>.py` 추가 + `SOURCES` 한 줄로 확장한다(단, robots.txt/약관 확인 필수).
- **배경은 템플릿에 실재하는 슬라이드만** 쓴다. `template.pptx` 가 20장이면 배경 풀도 20장이며
  `pool[k % 20]` 으로 순환한다. 템플릿이 없으면 빌드는 실패한다(스킵 아님).
- **산출물 크기 ≈ 템플릿 크기**(약 37 MB). 원본 슬라이드 파트/미디어를 재사용하고 남겨 두기
  때문이다(배경·이미지 중복 생성 방지). orphan 파트 GC 는 제공하지 않는다.
- **원본 20장 슬라이드 파트는 패키지에 남는다.** 참조(`sldIdLst`)만 제거되어 화면엔 안 보이지만,
  템플릿의 원래 가사 텍스트가 산출물 파일 안에 물리적으로 남는다(용량/프라이버시 유의).
- **로컬 단일 사용자 전용.** 인증 없음(`127.0.0.1` 바인딩), 빌드 직렬화(동시 빌드 0).
  외부 노출·다중 사용자를 상정하지 않는다.
- `coverage`/`pytest`/`ruff` 는 **의존성에 없다**. 커버리지 수치가 필요하면 별도 설치해야 한다
  (미설치 상태의 자체 측정치는 과소집계).
- Windows 한국어 콘솔(cp949)에서 `python -m lyrics2pptx.cli` 의 한글이 `?` 로 보일 수 있다.
  `.\scripts\run.ps1` 을 쓰면 해결된다.

---

## 10. 더 읽기

- 사용 시나리오·동작 규칙: `docs/guide/사용가이드.md`
- 아키텍처: `아키텍처.md`, `docs/arch/01..10`
- 결정 기록: `docs/adr/`
- 환경/명령: `환경선언서.md` · 테스트: `테스트명세서.md`
