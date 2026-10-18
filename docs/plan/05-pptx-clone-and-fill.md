# 05. PPTX 슬라이드 복제와 채우기 (Clone and Fill)

담당 태스크: **T04** (복제 엔진), **T05** (`제목 1` 채우기 + autofit), **T06** (빌더/배경 순환)
이 문서가 이 프로젝트의 핵심이다. python-pptx에는 슬라이드 복제 API가 없으므로 XML 수준으로 내려간다.

## 1. 템플릿 실측 사실 (추측 아님 — zip/XML 직접 분석)

| 항목 | 값 |
| --- | --- |
| 파일 | `D:\projects\lyrics\template.pptx` (37,248,076 bytes) |
| 슬라이드 수 | 20 (`ppt/slides/slide1.xml` ~ `slide20.xml`) |
| 슬라이드 크기 | `12192000 x 6858000` EMU (13.333in x 7.5in, 16:9) |
| notesSlide | 20 (`ppt/notesSlides/notesSlide1..20.xml`) — 각 슬라이드에 rel 존재 |
| media | 20개 (`ppt/media/*` jpeg/jpg/png 혼재) |
| 슬라이드 레이아웃 | `slideLayout1` ~ `slideLayout54` (테마 전체 포함) |
| 배경 방식 | `<p:bg>` 채우기 17장 (slide1-10,12-16,18,19) / 전면 `<p:pic>` 3장 (slide11, 17, 20) |
| 텍스트 도형 | 이름 `제목 1` 하나 (일반 도형, `<p:ph>` 아님) |
| 도형 수 | 슬라이드당 1~3개 (예: slide19=1개, slide2=2개, slide20=3개) |

`제목 1` 도형 실측:

| 슬라이드 | 위치 (EMU) | 크기 (EMU) | run `sz` | run 텍스트 |
| --- | --- | --- | --- | --- |
| slide2 | (349469, 1181100) | 11493062 x 2247900 | 6000/7200 | `이 곳에 `, `생명샘`, ` 솟아나`, `눈물 골짝 지나갈 때에` |
| slide19 | (349469, 2581275) | 11493062 x 2247900 | 6000/6600 | `우리 함께 손잡고 가세`, `조금 느리고 어려울지라도` |
| slide20 | (349469, …) | 11493062 x 2247900 | 6000/7200 | `너는 담장 너머로`, ` `, `붉은 나무`, `가지에 푸른 열매처럼` |

**slide19가 요구된 "한 슬라이드 = 가사 2줄"의 실제 예시다.** → 복제 대상 슬라이드에 정확히 2개의 문단을 넣으면 된다.

## 2. 배경 풀과 순환 (D7)

- **배경 풀** = 템플릿 슬라이드 20장을 **번호 순서대로**(`slide1..slide20`) 사용.
- 출력 슬라이드 `k` (0-based) 의 배경 = `template_slides[k % 20]`.
- 따라서 20장을 넘으면 1번 배경부터 wrap-around. 배경 자체는 "템플릿에서 복제 + 내용만 교체"이므로 원본 배경(그림·`<p:bg>`)이 100% 보존된다.
- slide1은 텍스트가 비어 있으나 커버로 확정할 근거가 없으므로 **풀에서 제외하지 않는다**. (옵션 `--skip-slide 1`만 허용)
- 별도의 표지 슬라이드는 만들지 않는다(비목표). 필요하면 사용자가 첫 페어를 제목으로 쓰면 된다.

```python
# pptx/builder.py (요지)
POOL = list(range(20))              # 0-based, template slide index
for k, (line1, line2) in enumerate(pairs):
    bg_index = POOL[k % len(POOL)]
    slide = clone_slide(template_prs, bg_index, target_prs)
    fill_title1(slide, line1, line2, style=style_cache[bg_index])
```

## 3. 복제 알고리즘 (`pptx/clone.py`)

python-pptx의 공개 API로는 슬라이드를 복제할 수 없다. 레이아웃이 다른 슬라이드를 `add_slide`로 만들고 내용을 옮기면 **레이아웃 rel이 틀어져 배경이 사라진다.** 그래서 다음 순서로 XML을 직접 다룬다.

### 절차

1. **출력 프레젠테이션 준비**: `target_prs = Presentation(template_path)` (템플릿을 그대로 열어 테마·레이아웃·media를 물려받는다).
2. **레이아웃 확보**: 소스 슬라이드의 `slideLayout` part를 그대로 참조한다. 새 슬라이드는 빈 레이아웃에 담되, 레이아웃 rel을 소스와 동일하게 재지정한다.
   - 구현상 편의: **소스 슬라이드의 XML 전체(`<p:sld>`)를 deepcopy해 새 슬라이드 파트로 추가**하는 방식이 레이아웃 문제를 자동 해결한다(아래 "방식 A").
3. **`<p:sld>` deepcopy**: `copy.deepcopy(source_slide._element)` 로 spTree·`<p:bg>`·`<p:cSld>`를 통째로 복사.
4. **rels 재매핑**: 복사한 XML의 모든 `r:embed`, `r:link`, `r:id`가 **새 슬라이드 파트의 rels**를 가리키도록 다시 매핑한다(§4).
5. **노트 rel 제거**: notesSlide 관계는 복사하지 않는다(§6, D9).
6. **파트 등록**: 새 슬라이드 파트를 `presentation.xml`의 `sldIdLst`에 추가하고, 필요한 제약(고유 `id`, 고유 `rId`)을 만족시킨다.
7. **원본 슬라이드 정리**: 템플릿의 20장을 그대로 두면 산출물 앞에 원본 20장이 남는다. → 옵션 (a) 원본 20장을 모두 삭제하고 복제본만 남긴다(권장), (b) 원본을 "배경 원본"으로 남기고 뒤에 복제본을 붙인다.

> **결정**: (a) **원본 슬라이드를 전부 제거**하고 복제본만 남긴다. 산출물이 "가사 슬라이드만" 있는 문서여야 하고, 사용자가 원본 20장을 보길 원하지 않기 때문이다.
> 단, 배경 파트(media, layout, `<p:bg>`)는 파트 수준에서 유지되므로 삭제 대상이 아니다.

### 방식 A vs 방식 B

| 방식 | 설명 | 장단점 | 채택 |
| --- | --- | --- | --- |
| A. `<p:sld>` 통째 deepcopy + 신규 파트 등록 | 슬라이드 XML을 복제해 새 슬라이드 파트로 등록 | 레이아웃/배경/도형 모두 자동 보존. rels만 손보면 됨 | **채택** |
| B. `add_slide(blank)` 후 도형만 복사 | 빈 슬라이드에 도형 요소를 옮김 | 레이아웃 rel·`<p:bg>`를 따로 옮겨야 하고 배경 누락 위험 | 기각 |

> 단순함 제약 때문에도 A가 낫다. B는 "배경을 다시 붙이는" 추가 로직을 요구한다.

```python
# pptx/clone.py (요지 — 실제 파트 등록은 python-pptx 내부 API 사용)
import copy
from pptx.oxml.ns import qn

def clone_slide(source_slide, target_prs, *, keep_notes=False):
    new_el = copy.deepcopy(source_slide._element)          # <p:sld> 전체
    new_part = target_prs.slides._add_slide_part()         # 파트 생성(버전에 맞게 구현)
    new_part._element = new_el                             # XML 장착
    remap_relationships(source_slide, new_part)            # §4
    if not keep_notes:
        drop_notes_relationship(new_part)                  # §6
    return new_part
```

> 구현 메모: python-pptx 버전에 따라 파트 등록 API가 다르다. 확정 불가 시 **최후 수단**으로 `zipfile` + `lxml` 로 패키지를 직접 재작성하는 폴백을 둔다(§7). 어느 쪽이든 계약은 동일하다: "출력 pptx를 PowerPoint에서 열면 배경이 그대로다."

## 4. rels 재매핑 규칙 (가장 실수하기 쉬운 지점)

| reltype | 처리 |
| --- | --- |
| `slideLayout` | 소스가 참조한 레이아웃 파트로 새 슬라이드를 연결 |
| `image` (`r:embed`, `<p:bg>` 내 `a:blip`) | 소스 rel의 대상 파트를 그대로 새 파트에 rel 추가 → 새 `rId`로 치환 |
| `image` (`r:embed`, 전면 `<p:pic>`) | 위와 동일 |
| `notesSlide` | **추가하지 않음** (D9) |
| hyperlink (`r:id`, 외부) | 외부 rel은 그대로 복사(대상 URL 유지) |

치환 절차:

```python
def remap_relationships(src_part, new_part):
    id_map = {}
    for rel in src_part.rels.values():
        if rel.reltype == RT.NOTES_SLIDE:
            continue
        if rel.is_external:
            new_rId = new_part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            new_rId = new_part.relate_to(rel.target_part, rel.reltype)
        id_map[rel.rId] = new_rId
    # XML 안의 모든 r:embed / r:link / r:id 를 id_map 으로 치환
    for attr in ("r:embed", "r:link", "r:id"):
        for el in new_part._element.iter():
            v = el.get(qn(attr))
            if v in id_map:
                el.set(qn(attr), id_map[v])
```

> 검증 포인트: 전면 `<p:pic>` 배경을 쓰는 slide11/17/20을 복제했을 때 이미지가 보여야 한다. `<p:bg>` 슬라이드는 blip rel이 살아있어야 한다.

## 5. `제목 1` 채우기 (`pptx/fill.py`)

### 대상 도형 찾기

```python
def find_title1(slide):
    for shp in slide.shapes:
        if shp.name == "제목 1":
            return shp
    raise TemplateStructureError('도형 "제목 1" 을 찾지 못했습니다.')
```

이름으로 찾는다. `placeholders`가 아니라 `shapes`를 순회해야 한다(`제목 1`은 실제 `<p:ph>`가 아님 — 실측).

### 기존 run 구조 처리 (D8)

템플릿의 run은 `["이 곳에 ", "생명샘", " 솟아나", "눈물 골짝 지나갈 때에"]`처럼 쪼개져 있다. 이걸 그대로 두고 텍스트만 갈아끼우면 조각이 남는다. 따라서 **txBody를 재구성**한다.

1. `제목 1`의 `txBody`에서 **첫 번째 run의 `rPr`(글꼴·크기·색)** 을 deepcopy해 **스타일 템플릿**으로 보관한다.
2. `bodyPr`(자동맞춤 속성)도 보관.
3. 기존 문단을 전부 제거.
4. 새 문단 2개를 만든다. 각 문단 = `a:p` → `a:r` → `a:rPr`(템플릿 복제) + `a:t`(해당 줄 텍스트).
5. `line2 is None`이면 문단 1개만 만든다(홀수 마지막 슬라이드).
6. 저장한 `bodyPr`를 되돌려 자동맞춤 설정을 보존한다.

스타일 소스 선택 규칙: **첫 run의 `rPr`**. 슬라이드마다 `sz`가 다르므로(6000~7200) 슬라이드별로 추출한다 → 각 슬라이드가 원래 자기 크기를 유지한다.

```python
def fill_title1(slide, line1: str, line2: str | None, *, style):
    shp = find_title1(slide)
    txBody = shp.text_frame._txBody
    # 1) 기존 문단 제거, 2) style.rPr 로 문단 2개 재구성, 3) bodyPr 복원
    ...
```

### 자동 축소 vs 고정 (D8)

| 옵션 | 동작 | 채택 |
| --- | --- | --- |
| 폰트 크기 고정 + `normAutofit` | 템플릿 크기 유지, 넘치면 PowerPoint가 열 때 축소 | **채택** |
| 직접 계산해 축소 | EMU로 텍스트 폭을 계산 | 기각 (폰트 메트릭 측정 불가, 과설계) |
| 줄바꿈 강제 삽입 | 긴 줄을 여러 줄로 쪼갬 | 기각 (줄 수가 2를 초과해 요구 위반) |

근거: 박스는 `11493062 x 2247900` EMU(≈12.6in x 2.46in)이고 60~72pt 2줄(≈2.0in)은 높이상 들어간다. 폭은 한글 가사 길이에 따라 달라질 수 있어 **렌더러에게 위임**하는 것이 안전하고 단순하다. `bodyPr`에 `normAutofit`(`fontScale` 미지정 = 100%) 를 두면 PowerPoint가 열 때 초과분을 줄인다.

> 사용자 확인 필요 사항: 어떤 가사 줄은 12.6in 폭을 넘길 수 있다. 이때 `normAutofit`이 폰트를 줄이므로 "2줄 유지"는 지켜진다(요구 충족). 만약 사용자가 "폰트 크기 절대 고정"을 원하면 이 결정을 재검토한다.

### 홀수 마지막 슬라이드

`line2 is None` → 문단 1개. 위치는 그대로 두어 슬라이드가 비어 보이지 않게 한다(세로 중앙 정렬은 템플릿 `bodyPr`의 `anchor`를 따름).

## 6. notesSlide 처리 (D9)

| 선택 | 결정 |
| --- | --- |
| 노트 복제 | **하지 않음** |
| 이유 | 템플릿 노트는 원곡 전용 발표자 노트라 다른 곡 슬라이드에 붙으면 오히려 오정보가 된다 |
| 구현 | `remap_relationships`에서 `RT.NOTES_SLIDE`를 skip. 새 슬라이드에 notesSlide 파트 없음 |
| 결과 | 산출물에는 노트가 비어 있다 |

## 7. 실패 케이스와 폴백

| 케이스 | 처리 |
| --- | --- |
| `제목 1` 도형 없음 | `TemplateStructureError` — 원인과 도형명 목록을 메시지에 포함 |
| 복제 후 배경 그림 누락 | rels 매핑 검증 실패 → 명시적 예외 + 폴백 경로 안내 |
| python-pptx 내부 API 버전 차이 | **폴백**: `zipfile`로 `ppt/slides/slideN.xml`·`_rels/slideN.xml.rels`·`[Content_Types].xml`·`presentation.xml`을 직접 복제·수정 |
| 템플릿 파일 없음/잠김 | 시작 시 검사 후 종료. 사용자에게 경로 표시 |
| 페어 0개 | `EmptyLyricsError` (04) |

## 8. done when (T04, T05, T06)

- [ ] **T04** `test_clone.py`: 템플릿에서 1장 복제 후 저장한 pptx를 `zipfile`로 열어, 복제 슬라이드의 rels에 image rel이 존재하고 원본 media 파트를 참조한다.
- [ ] **T04** 전면 `<p:pic>` 배경 슬라이드(slide20) 복제 시 `<p:pic>`의 `r:embed`가 유효한 rel을 가리킨다.
- [ ] **T04** 원본 20장이 산출물에 남지 않는다(`sldIdLst` 길이 = 복제 수).
- [ ] **T05** `test_fill.py`: 채운 뒤 `find_title1`의 텍스트가 정확히 `["line1", "line2"]` (홀수면 `["line1"]`).
- [ ] **T05** 채운 run의 `sz`가 소스 슬라이드의 첫 run `sz`와 동일.
- [ ] **T05** `bodyPr`의 `normAutofit`이 유지된다.
- [ ] **T06** `test_builder.py`: 25줄 → 13 페어 → 13장. 배경 순서 = `[0,1,2,...,12]` (20 미만이라 그대로), 40줄 → 20장, 42줄 → 21장이고 21번째 배경 인덱스 = 0(wrap-around).
- [ ] **T06** 산출물이 `Presentation()`으로 다시 열린다(손상 없음).

## 상위 문서 연결

- 인덱스: `계획서.md` / 선행: `04-text-normalization-and-splitting.md`
- 관련 결정: D6, D7, D8, D9
- 다음: `06-web-ui-and-delivery.md`
