# 07. 테스트 전략 (Testing Strategy)

담당 태스크: **T11** (테스트 스위트 정리), **T12** (수동 인수 검증)

## 1. 원칙

| 원칙 | 이유 |
| --- | --- |
| 네트워크 실호출 금지 | 크롤링은 비결정적이고, 테스트가 외부 사이트 상태에 의존하면 안 된다 |
| fixture 기반 | `tests/fixtures/`의 저장된 응답으로 수집 로직을 검증 |
| 표준 `unittest`만 | 추가 설치 없이 `python -m unittest discover` 로 실행 |
| 템플릿은 실제 파일 사용 | 복제 로직은 실물 `template.pptx` 로만 검증 가능 |
| 손상 검증 포함 | 생성물을 다시 `Presentation()` 으로 열어 무결성 확인 |

## 2. 테스트 레이어

| 레이어 | 파일 | 대상 | 네트워크 |
| --- | --- | --- | --- |
| 단위 | `test_normalize.py` | 정규화 N1~N11 | 없음 |
| 단위 | `test_split.py` | 2줄 분할, 홀수/0줄/후렴 | 없음 |
| 단위 | `test_httputil.py` | 지연/재시도/robots/UA | 없음(모킹) |
| 통합 | `test_clone.py` | 슬라이드 복제 + rels | 없음(로컬 템플릿) |
| 통합 | `test_fill.py` | `제목 1` 채우기 + 서식 | 없음(로컬 템플릿) |
| 통합 | `test_builder.py` | 배경 순환 + 조립 + 재열기 | 없음(로컬 템플릿) |
| 인수(수동) | 체크리스트 §5 | PowerPoint 육안 확인 | 없음 |

## 3. fixture

| 파일 | 내용 | 용도 |
| --- | --- | --- |
| `tests/fixtures/lyrics_ovh_sample.json` | 참조 어댑터 응답 1건(가사 포함) | `lyrics_ovh.py` 파싱 검증 |
| `tests/fixtures/lyrics_sample.txt` | CRLF·다중공백·섹션마커가 섞인 원문 | 정규화/분할 골든 케이스 |
| `tests/fixtures/not_found.json` | 빈 결과 응답 | `SourceNotFoundError` 검증 |

fixture 로딩 훅: `LYRICS2PPTX_FIXTURE_DIR` 환경변수가 설정되면 `httputil` 이 실제 요청 대신 파일을 읽는다(03 §오프라인 모드).

## 4. 핵심 단언 (요약)

### test_normalize.py
- CRLF/CR 혼재 → `\n` 통일
- 3+ 연속 개행 → 빈 줄 제거
- `"붉은  나무가지에   푸른"` → `"붉은 나무가지에 푸른"`
- NBSP/제로폭 제거
- `[Verse 1]`, `(×2)`, `※` 줄 제거
- 골든: `lyrics_sample.txt` → 정확히 4줄(04 §대표 예시와 일치)

### test_split.py
- `["a","b","c","d"]` → `[("a","b"),("c","d")]`
- `["a","b","c"]` → `[("a","b"),("c",None)]`
- `["a"]` → `[("a",None)]`
- `[]` → `[]`
- `dedupe_repeat=True` vs `False` 결과 차이

### test_httputil.py
- 429 × 3 → `SourceUnavailableError`
- robots `Disallow` → `SourceBlockedError`
- UA 문자열이 실제 헤더에 포함(모킹된 요청 캡처)
- `sleep_between` 호출 횟수 ≥ 요청 수 - 1

### test_clone.py
- 복제 후 새 슬라이드 rels에 `image` rel 존재, 대상이 원본 media 파트
- slide20(전면 `<p:pic>`) 복제 시 `<p:pic r:embed>` 가 새 rels의 유효 rId
- `sldIdLst` 길이 = 복제 슬라이드 수(원본 제거 확인)
- notesSlide rel 부재

### test_fill.py
- `find_title1` 텍스트 = `["l1","l2"]` (홀수면 `["l1"]`)
- run `sz` = 소스 첫 run `sz`
- `bodyPr` 의 `normAutofit` 유지

### test_builder.py
- 25줄 → 13 페어 → 13장
- 40줄 → 20장, 배경 인덱스 `0..19`
- 42줄 → 21장, 21번째 배경 인덱스 = 0 (wrap-around)
- 산출물을 `Presentation()` 으로 재열기 성공
- `EmptyLyricsError` (0줄 입력)

## 5. 수동 인수 체크리스트 (T12)

PowerPoint(또는 Keynote/Google Slides)로 생성물을 열어 확인한다.

- [ ] 슬라이드 총 장수가 `ceil(줄수/2)` 와 일치한다.
- [ ] 각 슬라이드에 가사가 2줄씩, `제목 1` 위치(템플릿과 동일 위치)에 표시된다.
- [ ] 홀수 입력의 마지막 슬라이드는 1줄이며 빈 슬라이드가 아니다.
- [ ] 배경이 슬라이드마다 다르고, 20장 초과 시 1번 배경이 다시 나온다.
- [ ] 전면 그림 배경 슬라이드(템플릿 11/17/20에 해당하는 순번)의 배경이 정상 표시된다.
- [ ] 폰트 크기가 템플릿과 크게 다르지 않고, 긴 줄도 2줄을 넘지 않는다(자동 축소 동작).
- [ ] 원본 템플릿 20장이 산출물에 남아있지 않다.
- [ ] 노트(발표자 노트)가 비어 있다.
- [ ] 파일이 두 번 열어도 손상 경고가 없다.

## 6. 실행

```bat
python -m unittest discover -s tests -v
set LYRICS2PPTX_FIXTURE_DIR=tests\fixtures
python -m unittest discover -s tests -v
```

## 7. done when (T11, T12)

- [ ] **T11** 위 §4의 모든 파일이 존재하고, `python -m unittest discover -s tests`(네트워크 차단 상태) 전체 통과.
- [ ] **T11** fixture 모드에서도 전체 통과.
- [ ] **T11** 테스트가 실 네트워크를 호출하지 않음(소켓 모킹 또는 fixture 훅으로 보장).
- [ ] **T12** §5 체크리스트 전 항목 육안 통과, 결과를 `output/`에 보존.
- [ ] **T12** 실패 시 재현 명령이 이슈에 기록된다.

## 상위 문서 연결

- 인덱스: `계획서.md` / 선행: `06-web-ui-and-delivery.md`
- 관련 결정: D11
- 다음: `08-roadmap.md`
