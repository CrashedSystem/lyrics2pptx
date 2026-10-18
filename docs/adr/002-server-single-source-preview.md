# ADR-002: 프리뷰의 정규화·페어링을 서버 단일 소스(`POST /api/preview`)로 통합한다

- 상태: **채택(구현 완료)**
- 날짜: 2026-10-18
- 관련: `리뷰리포트.md` M1(주요), `docs/arch/02 §6`, `docs/arch/05 §2`,
  `src/lyrics2pptx/webapp.py`, `web/app.js`, `src/lyrics2pptx/pipeline.py::preview`

## 배경

초기 구현에서 섹션 마커 제거(N6)와 같은 정규화 판정이 **서버와 브라우저에 이중 구현**되어 있었다.
브라우저(`web/app.js`)는 `lower.indexOf(marker)` 같은 **부분 문자열** 매칭을 썼고, 서버는
**전체 일치 + 숫자 접미** 규칙을 썼다. 그래서 "미리보기에서는 지워질 것처럼 보이는 줄"과
"실제 생성 파일에서 지워지는 줄"이 서로 달랐다(리뷰 M1). 프리뷰가 거짓 안심을 준다.

정규화 규칙을 클라이언트에 다시 구현하는 한, 규칙이 바뀔 때마다 두 곳이 어긋날 위험이 영구히 남는다.

## 결정

**미리보기는 서버가 계산한다.** 브라우저는 규칙을 재구현하지 않는다.

- 새 라우트 `POST /api/preview` 가 `{raw_text, dedupe_chorus}` 를 받아
  `pipeline.preview()` 를 호출한다.
- `pipeline.preview()` 는 실제 빌드가 쓰는 것과 **정확히 같은** 정규화 함수
  (`text.normalize.normalize_with_stats` → `text.split.split_pairs`)를 호출한다.
  그래야 프리뷰가 생성 슬라이드와 어긋날 수 없다.
- 응답: `{lines, pairs, removed_marker_count}`.
- 클라이언트(직접 입력 모드):
  - 입력에 **250ms 디바운스**를 걸어 서버를 과도하게 부르지 않는다.
  - 요청마다 **단조 증가 request id** 를 붙이고, 늦게 도착한 이전 응답은 **무시**한다
    (빠른 타이핑 중 응답 순서 뒤바뀜 방지).
  - 생성 직전 디바운스를 취소하고 **프리뷰를 한 번 더 최신화**하여, 클릭 시점 값이 아니라
    서버 기준 최신 값으로 판정한다(빈 입력 판정도 서버 기준).
- 검색 모드에서는 서버가 이미 정규화한 부분 미리보기 3줄(`preview_lines`)을 주므로,
  클라이언트는 그 3줄을 2줄로 **묶기만** 한다(정규화 재구현 없음).

## 결과

- 프리뷰와 실제 생성물이 **구조적으로 일치**한다(단일 정규화 함수).
- 클라이언트의 마커 판정 코드가 제거되어 재구현 드리프트 위험이 사라진다.
- 비용: 입력 시 로컬 HTTP 왕복 1회(루프백, 250ms 디바운스) — 로컬 단일 사용자라 무시 가능.

## 근거(코드)

- `pipeline.py::preview` — "the *same* `text.normalize` pass `run` uses"
- `webapp.py::_handle_preview` — 라우트 → `pipeline.preview`
- `web/app.js`: `refreshManualPreview`(서버 호출), `scheduleManualPreview`(디바운스),
  `previewRequestId`(늦은 응답 무시), `currentPairs`(직접 입력=서버 페어, 검색=클라이언트 페어)
- 검증: `tests/test_preview.py`(`PreviewMatchesBuildTests` — 프리뷰 = 빌드 결과 단언)
