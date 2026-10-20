[![CI](https://github.com/CrashedSystem/lyrics2pptx/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/CrashedSystem/lyrics2pptx/actions/workflows/ci.yml)

# lyrics2pptx — 가사 2줄 PPTX 생성기

검색어로 가사를 모으거나 가사를 직접 붙여넣으면, `template.pptx` 의 배경 슬라이드를
복제해 **슬라이드 한 장에 가사 2줄씩** 채운 `.pptx` 를 만들어 주는 **로컬 단일 사용자** 도구다.
서버·클라우드·계정이 필요 없고, 실행은 Windows 로컬 프로세스 하나와 로컬 웹서버(127.0.0.1)뿐이다.
템플릿은 **읽기 전용 입력**으로만 열리며 절대 수정되지 않는다.

> 상태: `src/`+`web/` 구현 완료. 테스트 `596 tests OK`. 진입점 `python -m lyrics2pptx.cli` / `python -m lyrics2pptx.webapp` / 더블클릭 `lyrics2pptx.vbs` / **패키지** `dist\lyrics2pptx\lyrics2pptx.exe`(ADR-033). 기본 수집 소스는 `auto`(① blog 교차검증 → ② lrclib → ③ blog 미검증, ADR-025)이고 가사는 한글 우선이다(ADR-026). 폴백 소스 lrclib 은 곡당 총 10초 안에 답하거나 포기한다(ADR-030). 웹 UI 는 서버 수명을 스스로 관리한다 — 창을 닫으면 3분, 사용이 없으면 20분, 최대 3시간(ADR-032).

---

## 0. 설치 없이 쓰기 (베타 패키지, ADR-033)

개발자가 아니라 **쓰기만** 하는 사람에게는 이 파일 하나를 준다.

```text
dist\lyrics2pptx-0.1.0b3-win64.zip
```

압축을 풀면 폴더 하나가 나오고 그 안에 세 개가 들어 있다.

```text
lyrics2pptx\lyrics2pptx.exe   ← 이것만 더블클릭하면 끝 (Python 설치 불필요)
lyrics2pptx\template.pptx     배경 원본 (내 PPT 로 바꿔 넣을 수 있다 — §0.1)
lyrics2pptx\사용법.txt        한 장짜리 안내 — 실행·사용·종료·신고
```

- 브라우저 탭이 저절로 열린다(콘솔 창 없음). 결과는 그 폴더의 `output\` 에 쌓인다.
- 끄는 방법은 따로 없다: 탭을 닫으면 3분, 사용이 없으면 20분, 최대 3시간 뒤 스스로 종료한다(ADR-032).
- 잘 안 되면 `output\launch.log` 를 보면 된다(패키지 실행의 유일한 기록).
- 만들려면: `python -X utf8 packaging\build.py`

### 0.1 배경(template.pptx) 바꾸기 (ADR-034, ADR-035)

동봉 템플릿 대신 **내 PPT** 를 쓰려면, 압축을 푼 폴더의 `template.pptx` 를 내 파일로 덮어쓰면 된다. 다시 빌드할 필요가 없다.

- 슬라이드 수·화면비는 자유. 배경 목록은 그 템플릿의 슬라이드 전부다.
- 배경은 **슬라이드 → 레이아웃 → 마스터** 순으로 상속된다(파워포인트와 같은 규칙). 그림 배경은 원본 바이트 그대로, 단색·테마 색(`accent1` 등)·`sysClr`·`prstClr` 은 그 색으로 그려서 보여 준다.
- 가사는 **이름이 `제목 1` 인 글상자**로 들어간다. 없으면 점수로 고른다: 제목 placeholder(+100) → 이름에 `가사`/`lyrics`(+80) → `제목`/`title`(+60) → 글자 있음(+25) → 면적. 쪽번호·날짜·바닥글 placeholder 는 제외하고, 슬라이드를 거의 덮는 도형(각 변 1% 허용)은 배경으로 본다. 후보와 점수는 `output\launch.log` 의 WARNING 에 남는다.
- 그룹(`p:grpSp`) 안에 든 글상자도 찾고, 좌표·크기를 슬라이드 기준으로 환산한다.
- 상자에 글꼴/크기 서식이 없으면 상자 높이×0.42(18~66pt)로 정하고, **배경이 어두우면 흰 글자**, 아니면 검정을 쓴다. 글꼴은 템플릿 테마의 동아시아 글꼴(없으면 `맑은 고딕`). `normAutofit` 이 넘침을 흡수한다.
- 배경이 그림이 아니어도(단색) 목록에 그 색으로 뜬다.
- 엉뚱한 상자에 들어가면 그 상자 이름을 `제목 1` 로 바꾸는 것이 가장 확실하다(`LYRICS2PPTX_TITLE_SHAPE` 로 다른 이름을 지정할 수도 있다).
- 글자가 **표나 스마트아트 안에만** 있으면 "표·스마트아트는 지원하지 않습니다"라고 알려 준다 — 조용히 빈 슬라이드를 만들지 않는다.
- 확인: `--doctor` 가 `title_shape` · `title_shape_detected` · `style_source` 를 출력한다.
- 한계: 표·스마트아트 안에는 쓰지 않는다. 세로형 등 특이 화면비는 실측하지 않았다. 추정은 틀릴 수 있고, 기본 배경 번호가 범위를 넘어 고른 마지막 슬라이드에 글상자가 없으면 미리보기가 그 사실을 알린다(다른 배경을 고르면 된다).

---

## 1. 빠른 시작

```powershell
# 0) 사전 준비: Python 3.12, 그리고 리포지토리 루트에 template.pptx (20장) 가 있어야 한다.

# 1) 설치
python -m pip install -r requirements.txt
python -m pip install -e .

# 2) 실행 전 자가진단 (파일을 만들지 않음)
python -m lyrics2pptx.cli --doctor

# 3a) 검색어로 생성 (기본 소스 = auto: ① blog 교차검증 → ② lrclib → ③ blog 미검증,
#     blog·lrclib 은 동시에 조회하고 **한글 가사 후보를 먼저** 본다 — 네트워크 필요)
python -m lyrics2pptx.cli --query "아티스트 - 곡명"

# 3a-1) 합의 가사를 만들지 않고 교차검증 결과만 확인 (빌드 안 함)
python -m lyrics2pptx.cli --query "곡명" --cross-check

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

**가장 쉬운 실행 — `lyrics2pptx` 아이콘(또는 `lyrics2pptx.vbs`)을 더블클릭한다.**

| 파일 | 동작 | 종료 방법 |
| --- | --- | --- |
| `lyrics2pptx.vbs` | 콘솔 **없이** 숨김 실행(빈 포트 자동 선택, 브라우저 자동 오픈). 이미 떠 있으면 **그 탭만 연다** | **아무것도 하지 않는다** — 창을 닫으면 3분 뒤, 사용이 없으면 20분 뒤, 길어도 3시간 뒤 서버가 스스로 종료된다. 즉시 끄려면 화면 오른쪽 위 **`종료`** 버튼 |
| `webui.cmd` | **콘솔 창을 띄우고** `http://127.0.0.1:8765/` 로 실행 + 브라우저 자동 오픈(개발·확인용) | 그 창을 닫거나 **Ctrl+C** |
| `webui_stop.cmd` | (개발자용) 남아 있는 서버를 강제로 정리 | — |

- **사용자는 `webui_stop.cmd` 를 알 필요가 없다.** 페이지가 사라진 것을 서버가 스스로 알아채고(3분), 사용 기록이 없으면 20분,
  최대 3시간 안에 종료한다(ADR-032). 진행 중인 검색·생성이 있으면 작업이 끝날 때까지 종료를 미룬다.
- 같은 아이콘을 두 번 눌러도 서버가 두 개 뜨지 않는다: `output\webui.state` 에 적힌 인스턴스가 `/api/ping` 에 답하면
  새 서버 대신 그 탭만 연다.
- 포트가 매번 달라질 수 있으므로 주소는 자동으로 열린 탭, `output\webui.state`, `output\launch.log` 중 아무 데서나 확인한다.
- 서버 로그가 보이고 주소가 `8765` 로 고정되므로 **GUI 자체를 개발·확인할 때**는 `webui.cmd` 를 쓴다.
- 직접 명령으로 멈추려면: `webui_stop.cmd`(포트 인자 가능: `webui_stop.cmd 9000`) 또는 PowerShell
  `Get-NetTCPConnection -LocalPort 8765 | % { Stop-Process -Id $_.OwningProcess -Force }`.

---

## 2. CLI 사용법

진입점: `python -m lyrics2pptx.cli` (직접 실행 시 cp949 콘솔에서 `--help`/오류 메시지가 `?` 로 대체될 수 있음 — 프로세스는 죽지 않는다).

| 옵션 | 설명 |
| --- | --- |
| `--query "…"` | 검색어. `"아티스트 - 곡명"` 또는 곡명 단독 |
| `--input-file PATH` | 직접 입력 텍스트(UTF-8). 정규화(N1~N11, 추임새 제거 포함) 적용 |
| `--lines-file PATH` | 이미 정리된 줄 파일(한 줄 = 한 문장). 정규화 생략 |
| `-o`, `--output PATH` | 출력 경로 (기본 `output\<제목>.pptx`) |
| `--source NAME` | 수집 소스 이름: `auto`(기본)·`blog`·`lrclib`·`lyrics_ovh`. `auto` 순서: ① blog 교차검증 → ② lrclib → ③ blog 미검증(ADR-025) |
| `--index N` | 검색 결과 중 선택할 번호 (기본 `0`; `blog` 는 `0`=합의 가사, `1`~=개별 페이지 `호스트 · N줄`, 투표에서 빠진 페이지는 `(투표 제외)`) |
| `--cross-check` | `--query` 로 블로그를 수집·교차검증해 **합의 줄 + 지지수 + 다른 표기**만 출력 (빌드 안 함). 머리줄에 **가사 언어**(`· 한글`/`· 원어`)를 표시한다(ADR-026). 2장 미만이 투표하면 "교차검증 안 됨"으로 정직하게 보고한다(ADR-020) |
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

> 실 네트워크 `--query` 는 `lrclib.net` 으로 **검증됐다**: `어노인팅` → 후보 반환(API total 20),
> `손잡고 함께 가세` → 1건, `생명샘` → 0건(미수록). 부하 시 `503` 은 재시도 레이어가 흡수한다.
> lrclib 에 없는 곡은 기본 소스 `auto` 가 **블로그 검색 + 다수결 교차검증**으로 폴백한다(ADR-018):
> `지금은 엘리야 때처럼` → 4페이지 채택/합의 24줄/불일치 15줄(오타 `에스길`→`에스겔` 자동 교정, 변형 보고).
> 성경 구절과 제목이 같은 곡처럼 **함정이 있는 검색어**는 성경/목록 페이지를 후보에서 걸러내고(ADR-020),
> 실제로 투표한 페이지가 2장 미만이면 `교차검증 안 됨` 으로 표시한다 — 「정금같이 나오리라」 실측 참조.

> **수집 우선순위와 언어 (ADR-025·026·030)**: `auto` 는 ① 블로그 합의(교차검증 성립) → ② lrclib → ③ 교차검증
> 안 된 블로그 순으로 보고, 블로그와 lrclib 을 **동시에** 조회한다(지연 = 둘 중 긴 쪽). lrclib 은 폴백이라
> 곡당 총 10초(`LYRICS2PPTX_LRCLIB_TIMEOUT`)에서 끊고 blog 는 재시도를 유지한다. 후보군은 **한글 가사
> 먼저**, 한글이 하나도 없을 때만 원어로 간다. 고른 언어는 결과에 보인다 — `--cross-check` 머리줄이
> `· 한글`/`· 원어`, `--json` 이 후보마다 `"korean": true|false`. 근거 실측은 `조사보고서.md §16`(21곡 스윕)과
> `§17`(수정 후 재실행).

---

## 3. 웹 UI 사용법

**가장 쉬운 방법 — `lyrics2pptx.vbs` 더블클릭.** 콘솔 없이 서버가 뜨고 브라우저 탭이 자동으로 열린다.
끄는 방법은 **창을 닫는 것**이다(3분 뒤 서버가 정리된다). 지금 바로 끄려면 화면 오른쪽 위 **`종료`** 버튼을 누른다.

```powershell
.\scripts\run.ps1 -Web     # 또는: python -m lyrics2pptx.webapp  → 브라우저에서 http://127.0.0.1:8765 (Ctrl+C 로 종료)
```

화면은 **단계 배지 1~5** 로 이어진다: (1) 가사 가져오기(검색 / 직접 입력) → (2) 곡 선택(검색했을 때만) → (3) 배경 슬라이드 선택
→ (4) 미리보기 → (5) 저장(파일 이름 + `PPTX 생성`). 오른쪽 **미리보기는 항상 보인다** — PPTX 를 열지 않아도 슬라이드에 들어갈
내용이 그대로 보이고 쪽 단위로 넘겨 볼 수 있다. 만든 파일은 저장 패널의 결과 박스에서 **다시 내려받기** 하거나 **폴더 열기**로
탐색기에서 바로 찾을 수 있다(내려받은 파일이 아니라 `output\` 의 파일을 선택해 준다).

직접 입력 모드의 미리보기는 서버(`POST /api/preview`)가 계산한 값을 그대로 보여 주므로 **미리보기와 실제 파일이 어긋나지 않는다**.
검색 모드의 기본 소스는 `auto`(① blog 교차검증 → ② lrclib → ③ blog 미검증, ADR-025)이며 드롭다운에서 `blog`·`lrclib`·`lyrics_ovh` 로 바꿀 수 있다.
`blog` 의 결과 `0`번은 **합의 가사**, 그 뒤는 개별 페이지 전사본이다(ADR-018).

검색 중에는 화면 하단에 **진행 카드**가 고정된다(ADR-019): 지금 무엇을 하는지(단계 목록),
얼마나 걸렸는지(경과 초), 어디까지 왔는지(진행 막대)를 보여 준다. 블로그 수집처럼 오래
걸리는 단계는 `3/5 lyrics.co.kr 페이지 확인 중…` 처럼 **실제 페이지 수**를 함께 보고하며,
총량을 알 수 없는 단계는 숫자를 지어내지 않고 흐르는 막대로 대신한다. 생성 중에도 같은
카드가 쓰이므로 "멈춘 건지 도는 건지" 알 수 없던 구간이 없다.

HTTP 라우트(로컬 전용):

| 메서드 | 경로 | 역할 |
| --- | --- | --- |
| `GET` | `/` | UI 페이지 |
| `GET` | `/static/*` | 정적 자산(web/ 밖으로 나가는 경로는 404) |
| `GET` | `/api/search?q=…&source=…` | 검색 결과(제목/아티스트/부분 미리보기 3줄) — 수집이 끝날 때까지 블로킹 |
| `GET` | `/api/search/start?q=…&source=…` | 검색 작업 시작 → `202 {job_id, state, elapsed_ms, steps, progress}` |
| `GET` | `/api/search/job?id=…` | 진행 스냅샷(400ms 폴링). `done` 이면 `results`, 실패면 `error` |
| `POST` | `/api/preview` | `{raw_text, dedupe_chorus}` → 정규화된 줄 + 2줄 페어 |
| `POST` | `/api/generate` | `{query\|raw_text\|lines, …}` → 생성된 `.pptx` 바이트 다운로드 |
| `GET`/`POST` | `/api/ping` | 세션 스냅샷(`instance`·`auto_shutdown`·`busy`·남은 초·`closing`·`uptime_s`·`shutdown_reason`). `?closing=1`=탭 닫힘 비콘, `?keep=1`=사용 표시 |
| `POST` | `/api/shutdown` | 페이지에서 서버 종료 요청(진행 중 작업이 끝나면 내려간다) |
| `POST` | `/api/reveal` | 마지막 산출물을 탐색기에서 선택(`explorer /select`) |

서버는 `127.0.0.1` 에만 바인딩되고, 요청 본문은 최대 1 MiB, 정적 서빙은 `web/` 안으로 제한된다.
상태를 바꾸는 요청(`/api/shutdown`·`/api/reveal`·closing 비콘)은 **페이지에만 심긴 1회 토큰**(`X-Lyrics2Pptx-Token`)과
`Origin` 이 둘 다 맞아야 하며 아니면 403 이다 — 다른 웹사이트가 cross-site POST 로 서버를 끄거나 탐색기를 열 수 없다(ADR-032).
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
| `LYRICS2PPTX_SOURCE` | str | `auto` | config, pipeline |
| `LYRICS2PPTX_SEARCH_BACKEND` | str | `ddg` | config, crawl/websearch |
| `LYRICS2PPTX_BLOG_MAX_PAGES` | int | `5` | config, crawl/blog |
| `LYRICS2PPTX_HTTP_DELAY` | float(초) | `2.0` | httputil |
| `LYRICS2PPTX_HTTP_TIMEOUT` | float(초) | `10` | httputil |
| `LYRICS2PPTX_LRCLIB_TIMEOUT` | float(초) | `10` | config, crawl/lrclib (ADR-030) |
| `LYRICS2PPTX_MAX_RETRIES` | int | `3` | httputil |
| `LYRICS2PPTX_USER_AGENT` | str | `lyrics2pptx/0.1 (+local single-user tool)` | httputil |
| `LYRICS2PPTX_BIND_HOST` | str | `127.0.0.1` | webapp |
| `LYRICS2PPTX_BIND_PORT` | int | `8765` | webapp |
| `LYRICS2PPTX_WEBUI_TAB_GRACE` | float(초) | `180` | config, webapp (ADR-032) |
| `LYRICS2PPTX_WEBUI_IDLE` | float(초) | `1200` | config, webapp (ADR-032) |
| `LYRICS2PPTX_WEBUI_MAX_LIFETIME` | float(초) | `10800` | config, webapp (ADR-032) |
| `LYRICS2PPTX_WEBUI_AUTOSHUTDOWN` | `0`/`1` | `1` | config, webapp (ADR-032) |
| `LYRICS2PPTX_WEBUI_STATE_FILE` | path | `<repo>\output\webui.state` | config, webapp (ADR-032) |
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
  packaging/               릴리스 빌드 (entry.py = PyInstaller 진입, build.py = 얼리기→조립→zip) + 사용법.txt (ADR-033)
  dist/                    [빌드 산출, gitignore] lyrics2pptx-<버전>-win64.zip — 사용자에게 주는 파일 하나
  getport.py               무콘솔 실행용 빈 포트 선택기 (ADR-017)
  lyrics2pptx.vbs          더블클릭 실행 진입점 (콘솔 없음, ASCII/no-BOM)
  webui.cmd                더블클릭 실행 진입점 (콘솔 보임, 8765 고정, 창 닫기=종료)
  webui_stop.cmd           웹 UI 종료 (포트 8765 + lyrics2pptx.webapp 프로세스)
  web/                     index.html · app.js · style.css (vanilla JS)
  output/                  생성된 .pptx 저장 위치 + webui.state 런처 rendezvous + launch.log (gitignore; .gitkeep 만 추적)
  src/lyrics2pptx/
    cli.py, webapp.py      L4 진입점
    pipeline.py            L3 조립 + describe_error() 단일 오류 매핑
    config.py, errors.py   L0 설정/예외
    crawl/                 수집 축 (base, httputil, lrclib, lyrics_ovh, html_generic,
                           blog, crosscheck, chain, websearch/{base,ddg})
    text/                  정규화/2줄 분할 (normalize, split)
    pptx/                  템플릿 읽기·복제·채우기·쓰기 (writer*, builder, clone, fill…)
  tests/                   596 tests (표준 unittest)
  docs/plan/ docs/arch/     설계 문서 · docs/guide/ 사용가이드 · docs/adr/ 결정 기록
```

---

## 7. 테스트 실행

```powershell
python -m unittest discover -s tests -t .
```

검증된 결과(실제 실행): `Ran 596 tests … OK`, exit 0. 네트워크 실호출 0회이며 collect 단계는
fixture/mock 으로 격리된다. `.\scripts\run.ps1 -Test` 도 동일하게 596 tests / OK / exit 0.

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
- 자동 수집은 **robots.txt 와 이용약관을 준수하는 소스만 기본 제공**한다(`lrclib.net` — 키 없는 공개 API,
  그리고 `auto` 체인의 웹 검색은 robots 가 허용하는 DuckDuckGo HTML/lite 만 사용 — ADR-018).
  `lrclib.net` 의 ToS 문서는 확인하지 못했으므로 **개인 용도**로만 쓴다(ADR-017).
  블로그 필사 가사는 **저작권 상태가 확인되지 않으므로** 개인 용도로만 쓴다.
  `crawl/html_generic.py` 는 **기본 비활성**이며, 활성화하려면 대상 사이트의 robots.txt 와
  이용약관을 직접 확인해야 한다. 법률 자문이 아니며 책임은 사용자에게 있다.
- 저작권이 걱정되면 자동 수집 대신 **직접 입력**(`--input-file`, 웹 UI "직접 입력")으로
  본인이 사용 권한을 가진 가사만 넣어라.

---

## 9. 알려진 한계

- **`auto` 의 첫 후보는 이제 lrclib 이 아니라 블로그 교차검증 합의다**(ADR-025). 순서는 ① blog 교차검증 →
  ② lrclib → ③ blog 미검증이고, blog 와 lrclib 은 **동시에** 조회한다(지연 = 둘 중 긴 쪽). 그래서 lrclib 의
  커버리지 한계(「생명샘」처럼 DB에 없는 곡)가 곧바로 실패로 이어지지 않는다. lrclib 은 ② 계층(폴백)이라
  **곡당 총 10초**(`LYRICS2PPTX_LRCLIB_TIMEOUT`, ADR-030) 안에 답하지 못하면 포기하고 blog 로 내려간다 —
  죽은 폴백이 체인을 붙잡지 않는다(재시도 없이 한 번만 시도). 그래도 아무 소스에도 없으면 **직접 입력**을 쓴다
  (`--input-file` / 웹 UI 직접 입력).
  다른 소스는 `crawl/<site>.py` 추가 + `SOURCES` 한 줄로 확장한다(robots.txt/약관 확인 필수).
- **가사 언어는 한글이 먼저다**(ADR-026). 후보를 한글/원어 후보군으로 나눠 한글이 하나라도 있으면 그쪽만 보고,
  하나도 없을 때만 원어로 간다(`--lang` 스위치는 없다). 고른 언어는 `--cross-check` 머리줄(`· 한글`/`· 원어`)과
  `--json` 후보의 `"korean"` 으로 보인다.
- **글·댓글·표지가 가사에 섞이는 결함은 21곡 스윕으로 분류해 고쳤다**(`조사보고서.md §16`·`§17`): 사이드바
  댓글은 앵커(`⋯`)를 줄 단위로 추적해 배제하고(ADR-027), 줄 머리의 절 번호(`1.`)·`후렴 :`·`[후렴]`·이모지
  헤딩·`(새창열림)` 같은 크롬은 투표 **전에** 걷어낸다(ADR-028). 기준 페이지가 두 줄을 붙여 인쇄한 경우
  (맨 위 요약 블록) 같은 말이 두 번 나오지 않도록 합의에서 결합 줄과 조각 줄 중 약한 쪽을 버리고 다른 표기로
  남긴다(ADR-029).
- **블로그 폴백(`blog`)은 만능이 아니다.** 검색 결과 품질에 의존하고, 가사를 **이미지로만** 올린
  페이지·JS 렌더링 페이지는 수집하지 못한다. 다수결도 항상 정답은 아니므로(2-2 동률·후렴 합치기)
  `--cross-check` 로 지지수와 **다른 표기**를 확인하고 필요하면 개별 페이지(`--index 1`~)를 고른다.
  합의의 **줄 순서**는 *노래를 한 번 인쇄한 페이지*(다수와 같은 개행)를 따른다: 같은 곡을 3회 인쇄한
  페이지·두 소절을 한 문단으로 뭉친 페이지는 기준이 되지 않는다(ADR-023).
  한 자리를 **다른 말로** 적은 페이지가 섞여도(실로암 `주님 맘 속에 사랑 있음을` ↔ `당신 눈 속에 여명
  있음을`) 그 표기는 합의 줄 **뒤에 붙지 않고** 그 자리의 `다른 표기` 로 보고된다(ADR-024) — 12줄 노래가
  한 줄 때문에 7장이 되지 않는다.
  웹 수집은 페이지당 `HTTP_DELAY`(기본 2초)를 지켜 느리다(`BLOG_MAX_PAGES` 기본 5).
  후보 목록의 첫 줄이 `교차검증 안 됨` 이면 **투표가 성립하지 않았다는 뜻**이므로(성경 구절 페이지,
  목록 페이지, 동명이곡 등) 반드시 `호스트 · N줄` 라벨과 미리보기를 보고 고른다(ADR-020).
- 자동 수집은 robots.txt 를 **우회하지 않는다**. `robots.txt` 404/410 은 "규칙 없음 → 허용"
  (RFC 9309), 그 외 실패는 차단이다. 그래서 `search.naver.com`·`openapi.naver.com`(둘 다 `Disallow: /`)
  경로는 **의도적으로 미탑재**다.
- `lyrics_ovh` 는 **모듈만 남아 있고 기본값이 아니다.** 이 소스의 `robots.txt` 는 404 이며,
  fail-closed 정책 때문에 어떤 검색어로도 요청이 차단된다(도달률 0%).
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

## 11. 설치 없이 받기 · 버그 신고

- **베타 릴리스**: <https://github.com/CrashedSystem/lyrics2pptx/releases/tag/v0.1.0-beta.1> — `lyrics2pptx-0.1.0-win64.zip` 을 받아 압축을 풀고
  `lyrics2pptx\lyrics2pptx.exe` 를 더블클릭하면 끝입니다(Python 설치 불필요, ADR-033).
- **버그 신고**: <https://github.com/CrashedSystem/lyrics2pptx/issues> — 증상과 함께 `output\launch.log` 를 붙여 주세요.
  로그에 검색어·파일 경로가 들어 있을 수 있으니 남에게 보이기 싫은 줄은 지우고 올리세요.
