# 02. 기술 스택과 실행 환경 (Tech Stack and Runtime)

## 런타임 (실측 확정)

| 항목 | 값 | 확인 방법 |
| --- | --- | --- |
| OS | Windows 10/11 (win32) | 실행 환경 정보 |
| Python | 3.12.10 | `python --version` |
| python-pptx | **미설치** | `pip show python-pptx` → "Package(s) not found" |
| 템플릿 | `D:\projects\lyrics\template.pptx` (37,248,076 bytes) | 파일 실측 |

설치 명령:

```bat
python -m pip install -r requirements.txt
```

`requirements.txt` (핀 고정, 딱 한 줄):

```
python-pptx==1.0.2
```

> python-pptx는 `lxml`과 `Pillow`를 전이 의존성으로 끌어온다. 그 외 추가 설치는 하지 않는다.

## 왜 이 스택인가

| 선택 | 대안 | 채택 이유 (한 줄) |
| --- | --- | --- |
| `python-pptx` | `python-docx`식 수동 OPC 조작만 | 읽기/쓰기 API가 있어 복제 로직을 얹기 좋고, 부족한 복제만 XML로 보완하면 된다 |
| `urllib.request` | `requests`, `httpx`, `playwright` | 요청 수가 적고 동적 렌더링이 필요 없으며, 의존성 0을 유지한다 |
| `http.server` | Flask, FastAPI, uvicorn | 라우트 3개·동시 사용자 1명이라 프레임워크 도입 비용이 이득을 넘는다 |
| `unittest` | pytest | 표준 라이브러리로 충분하고 CI 설정이 필요 없다 |
| vanilla HTML/JS | React/Vue | 상태가 "검색어, 결과, 로딩"뿐인 단일 페이지다 |

## 디렉터리 구조 (실제 생성 경로)

```
D:\projects\lyrics\
  계획서.md
  template.pptx                     # 사용자 제공, 절대 수정하지 않음
  requirements.txt
  docs\plan\
    01-goals-and-scope.md
    02-tech-stack-and-runtime.md
    03-crawling-source-and-legal.md
    04-text-normalization-and-splitting.md
    05-pptx-clone-and-fill.md
    06-web-ui-and-delivery.md
    07-testing-strategy.md
    08-roadmap.md
  src\lyrics2pptx\
    __init__.py                     # 버전, 공개 API 재노출
    cli.py                          # 진입점: CLI
    webapp.py                       # 진입점: 로컬 웹 서버
    config.py                       # 경로/상수(템플릿 경로, 배경 풀 크기, 지연 시간)
    pipeline.py                     # fetch → normalize → split → build 오케스트레이션
    crawl\
      __init__.py                   # 소스 레지스트리
      base.py                       # RawSong, LyricsLine, SourceError, Source 프로토콜
      httputil.py                   # UA, 지연, 재시도, robots.txt 확인
      lyrics_ovh.py                 # 키 없는 공개 API 어댑터(참조 구현)
      html_generic.py               # 사용자 지정 사이트용 HTML 어댑터 스켈레톤
    text\
      __init__.py
      normalize.py                  # 정규화 파이프라인
      split.py                      # 2줄 분할
    pptx\
      __init__.py
      template_reader.py            # template.pptx 분석: 배경 풀, "제목 1" 서식 추출
      clone.py                      # 슬라이드 XML 복제 + rels 재매핑
      fill.py                       # "제목 1" 텍스트 교체 + autofit
      builder.py                    # 배경 순환 + 슬라이드 조립 + 저장
  web\
    index.html                      # 단일 페이지
    app.js                          # fetch 호출 2개 + 상태 3개
    style.css
  tests\
    __init__.py
    fixtures\
      lyrics_ovh_sample.json        # 저장된 응답(오프라인)
      lyrics_sample.txt             # 원문 가사 샘플
    test_normalize.py
    test_split.py
    test_clone.py
    test_fill.py
    test_builder.py
    test_httputil.py
  output\                           # 생성된 .pptx 저장 위치 (.gitignore 대상)
```

## 모듈 책임과 경계

| 모듈 | 하는 일 | 하지 않는 일 |
| --- | --- | --- |
| `crawl/base.py` | 데이터 계약(`RawSong`, `LyricsLine`, `Source`)과 예외 정의 | 네트워크 접근 |
| `crawl/httputil.py` | UA 지정, 요청 간 지연, 재시도, robots.txt 확인 | 사이트별 파싱 |
| `crawl/<site>.py` | 특정 소스에서 텍스트 추출 → `RawSong` | 파일 저장, 텍스트 정규화 |
| `text/normalize.py` | 개행/공백/마커 정리 → `list[str]` (줄 배열) | 슬라이드 분할 |
| `text/split.py` | 줄 배열 → `list[tuple[str, str | None]]` (2줄 페어) | pptx 접근 |
| `pptx/template_reader.py` | 템플릿 1회 분석, 배경 풀/서식 캐시 | 슬라이드 생성 |
| `pptx/clone.py` | 슬라이드 1장 복제 | 텍스트 내용 결정 |
| `pptx/fill.py` | `제목 1` 도형 텍스트 교체 | 슬라이드 복제 |
| `pptx/builder.py` | 배경 순환 + 조립 + 저장 | 크롤링, 정규화 |
| `pipeline.py` | 위 모듈을 순서대로 호출 | 각 단계 내부 로직 |

경계 규칙: **`text/`는 pptx를 모르고, `pptx/`는 크롤링을 모른다.** 두 축을 연결하는 것은 `pipeline.py`뿐이다.

## 데이터 계약 (요약, 상세는 03/04)

```python
# crawl/base.py
@dataclass(frozen=True)
class RawSong:
    title: str
    artist: str | None
    lyrics: str          # 원문 그대로, 정규화 전
    source_name: str     # 예: "lyrics_ovh"
    source_url: str | None

# text/split.py
LinePair = tuple[str, str | None]   # (첫째 줄, 둘째 줄) — 홀수 마지막은 둘째가 None
```

## 코딩 규칙 (강제)

| 규칙 | 값 |
| --- | --- |
| 인코딩 | 모든 텍스트 파일 **UTF-8, BOM 없음** |
| 줄바꿈 | **LF** |
| 타입 | 표준 `typing`, `from __future__ import annotations` |
| 예외 | 도메인 예외는 `crawl/base.py`의 `SourceError` 하위로만 |
| 로깅 | 표준 `logging` (print 금지, CLI 진입점 예외) |
| 네트워크 | 테스트에서 절대 실호출하지 않음(07 참조) |
| 템플릿 | `template.pptx`는 읽기 전용. 산출물은 항상 새 파일 |

## 실행 진입점

| 목적 | 명령 |
| --- | --- |
| 웹앱 | `python -m lyrics2pptx.webapp` → http://127.0.0.1:8765 |
| CLI | `python -m lyrics2pptx.cli --query "생명샘" -o output/song.pptx` |
| 테스트 | `python -m unittest discover -s tests -v` |

## 상위 문서 연결

- 인덱스: `계획서.md` / 선행: `01-goals-and-scope.md`
- 관련 태스크: T01
- 다음: `03-crawling-source-and-legal.md`
