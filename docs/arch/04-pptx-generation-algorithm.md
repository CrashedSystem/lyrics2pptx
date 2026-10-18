# 04. PPTX 생성 알고리즘 (Clone / Fill / Cycle)

상위: `아키텍처.md` §3 / 계획서 05 (전체 참조). 이 문서는 계획서 05 의 **방식 A(deepcopy)** 를
의사코드 수준으로 확정하고, 폴백과 경계 동작을 고정한다. 템플릿 실측값은 계획서 05 §1 과 동일하다.

## 1. 확정 사실 (재확인 — 재측정 불필요)

| 슬라이드 | `slide1..slide20`, 배경 20종 상이 | 실측 dump |
| slide1 | 텍스트 run **0개**(빈 슬라이드), 배경 있음 — `제목 1` 도형 유무는 T04 에서 확정 | 실측 dump + AD-11 |
| slide19 | run 2개 = 요구된 "2줄" 예시, `제목 1` 크기 `11493062 x 2247900` EMU, `sz` 6000/6600 | 실측 dump + 계획서 05 |
| slide20 | run 4개 중 `' '`(공백 run) 포함 = 조각화된 run | 실측 dump |
| slide11/17/20 | 전면 `<p:pic>` 배경 / 나머지 17장은 `<p:bg>` | 계획서 05 §1 |

> **AD-11 이 필요한 이유**: slide1 은 텍스트가 없다. 이는 (a) `제목 1` 도형이 없거나 (b) 도형은 있고 비어 있는 두 경우다.
> 아키텍처는 두 경우 모두 안전해야 한다 → `template_reader` 가 `shape_found: bool` 을 스타일과 함께 기록하고,
> 도형이 없는 슬라이드는 `default_style`(= run 을 가진 첫 슬라이드, 실측 slide2)로 폴백한다.

## 2. 배경 순환 공식과 경계 동작 (AD-10)

### 2.1 공식 (정확히 이것)

```
pool      = tuple(i for i in range(template.slide_count) if (i + 1) not in skip_slide_1based)
             # 기본 skip 없음 → pool = (0,1,...,19)
P         = len(pool)                       # 기본 20
pairs     = split_pairs(lines)              # N = len(pairs) = ceil(유효줄수 / 2)
for k in range(N):                          # k = 0-based 출력 순서
    bg_index = pool[k % P]                  # ← 배경 선택의 유일한 수식
    style    = template.styles[bg_index]    # 배경과 스타일은 같은 슬라이드에서 온다
```

- 배경과 글자 서식은 **같은 템플릿 슬라이드에서 함께** 온다(슬라이드별 `sz` 가 다르므로 분리하면 서식이 어긋난다).
- `k % P` 는 wrap-around 를 자동 처리한다. 별도 분기 금지.

### 2.2 경계 동작 표 (전부 확정)

| 입력 | N (장수) | bg_index 순서 | 동작 |
| --- | --- | --- | --- |
| 유효 줄 0 | 0 | — | `EmptyLyricsError("가사에서 유효한 줄을 찾지 못했습니다.")` — pptx 생성 안 함 |
| 유효 줄 1 | 1 | `[0]` | 1장, `line2=None` (1줄 슬라이드) |
| 유효 줄 2 | 1 | `[0]` | 1장, 2줄 |
| 유효 줄 3 | 2 | `[0,1]` | 2장, 마지막 1줄 |
| N < P | N | `[0..N-1]` | 순환 없음. 뒤쪽 배경 미사용 |
| N == P | P | `[0..P-1]` | 정확히 한 바퀴. `wrap_count=0`, 40줄→20장이 이 경우 |
| N == P+1 | P+1 | `[0..P-1, 0]` | **wrap 1회**. `pool[P % P]=pool[0]` |
| N = 42 (42줄) | 21 | `[0..19, 0]` | 계획서 05 done-when: 21번째 bg = 0 |
| `skip_slide` 로 pool 비움 | — | — | `ConfigError("배경 풀에 사용할 슬라이드가 없습니다.")` |
| `skip_slide` 범위 밖 | — | — | 파싱 단계에서 `ConfigError` (1..slide_count 만 허용) |

`wrap_count = (N - 1) // P if N > 0 else 0` — 진단/로그용.

### 2.3 검증 (T06)

```python
assert result.slide_count == 13 and lines25            # 25줄 → 13장
assert result.used_bg_indices == tuple(range(13))
assert lines42_result.slide_count == 21
assert lines42_result.used_bg_indices[20] == 0         # wrap-around
assert lines42_result.wrap_count == 1
```

## 3. 복제 알고리즘 — 방식 A (`pptx/clone.py`)

### 3.1 왜 방식 A 인가 (요약)

python-pptx 에는 슬라이드 복제 API 가 없다. `add_slide(blank)` 후 도형만 옮기면 **slideLayout rel 과 `<p:bg>` 가
따라오지 않아 배경이 사라진다.** `<p:sld>` 전체를 deepcopy 하면 layout·bg·spTree·도형이 한꺼번에 보존되고,
우리가 손볼 것은 **rels 재매핑 하나**로 줄어든다.

### 3.2 파트 등록 의사코드 (핵심 — T04)

```python
# pptx/clone.py — 방식 A 본체 (버전 차이 대응 위해 심볼은 feature-detect 로 얻는다)
RT_SLIDE         = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
RT_SLIDE_LAYOUT  = ".../slideLayout"
RT_NOTES_SLIDE   = ".../notesSlide"

def clone_slide(src_slide, target_prs, *, keep_notes=False) -> SlidePart:
    """
    src_slide   : python-pptx Slide (템플릿의 원본. 읽기만 함)
    target_prs  : Presentation (템플릿을 연 것 — 같은 패키지에 파트를 추가한다)
    반환        : 새로 등록된 SlidePart (아직 sldIdLst 정리는 안 함)
    """
    # 1) 원본 XML 전체를 복사 (<p:sld> : cSld/spTree/bg/clrMapOvr 전부)
    new_el = copy.deepcopy(src_slide._element)

    # 2) 새 슬라이드 파트 생성 — 파트명은 패키지가 유일하게 발급
    partname = target_prs.part.package.next_partname("/ppt/slides/slide%d.xml")
    new_part = _make_slide_part(partname, src_slide.part.content_type, target_prs.part.package)
    # _make_slide_part 는 pptx.parts.slide.SlidePart(...) 을 생성하는 얇은 래퍼.
    # 내부 API 시그니처는 버전별로 다르므로 _make_slide_part 안에서만 흡수한다(§5 probe).

    # 3) 복사한 XML 을 새 파트에 장착 (python-pptx 의 part._element 는 lxml 요소)
    new_part._element = new_el
    new_el.part = new_part              # lxml custom element 의 back-pointer 재설정

    # 4) rels 재매핑 — layout/image/hyperlink 를 새 파트 기준으로 다시 연결
    remap_relationships(src_slide.part, new_part, keep_notes=keep_notes)

    return new_part
```

**주의(구현 메모)**: 2~4 단계의 정확한 호출 형태는 python-pptx 1.0.2 내부 구조에 의존한다.
그래서 ① 내부 API 접근을 `_make_slide_part()` 한 함수에 격리하고, ② 실패 시 `CapabilityProbe` 가 폴백을 켠다(§6).

### 3.3 rels 재매핑 (가장 실수하기 쉬운 지점)

```python
def remap_relationships(src_part, new_part, *, keep_notes: bool) -> dict[str, str]:
    id_map: dict[str, str] = {}
    for rel in src_part.rels.values():
        # (1) notesSlide 는 복제하지 않는다 (AD-08 / D9)
        if rel.reltype == RT_NOTES_SLIDE and not keep_notes:
            continue
        # (2) 외부 rel(하이퍼링크 등)은 대상 URL 유지
        if rel.is_external:
            id_map[rel.rId] = new_part.relate_to(rel.target_ref, rel.reltype, is_external=True)
            continue
        # (3) 내부 rel(layout, image)은 ★같은 target_part 를 재사용★ 한다
        id_map[rel.rId] = new_part.relate_to(rel.target_part, rel.reltype)

    # (4) XML 안의 모든 r:embed / r:link / r:id 를 새 rId 로 치환
    for attr in ("r:embed", "r:link", "r:id"):
        for el in new_part._element.iter():
            v = el.get(qn(attr))
            if v is not None:
                if v not in id_map:
                    raise TemplateStructureError(
                        f"복제 슬라이드에 매핑되지 않은 관계 참조가 있습니다: {attr}={v}"
                    )
                el.set(qn(attr), id_map[v])
    return id_map
```

| reltype | 처리 | 근거 |
| --- | --- | --- |
| `slideLayout` | `relate_to(src_layout_part, RT_SLIDE_LAYOUT)` | 배경/테마 상속의 유일한 근거 |
| `image` (`<p:bg>` 내 `a:blip` / 전면 `<p:pic>`) | `relate_to` 로 **동일 target_part 재사용** | media 파트 중복 등록 금지 (AD-06) |
| `notesSlide` | **skip** | 템플릿 노트는 원곡 전용 (AD-08) |
| 외부 hyperlink | `is_external=True` 복사 | 대상 URL 유지 |
| 그 외/미매핑 | `TemplateStructureError` | 조용한 배경 누락을 금지 |

### 3.4 media 파트 중복 금지 규칙 (AD-06)

| 규칙 | 내용 |
| --- | --- |
| R-M1 | 이미지 파트를 **절대 새로 만들지 않는다.** `add_image()`/`Image.from_file()` 금지 |
| R-M2 | 모든 image rel 은 `relate_to(rel.target_part, ...)` 로 **기존 파트를 재사용**한다 |
| R-M3 | `relate_to` 는 (reltype, 대상)이 같으면 기존 rId 를 반환하므로, 같은 media 를 20장이 참조해도 **media 파트는 1개**다 |
| R-M4 | 검증: 복제 N장 후 패키지의 `ppt/media/*` 개수가 템플릿과 **동일**해야 한다 |
| R-M5 | 서로 다른 media 파일은 슬라이드별 rId 가 달라도 되지만, 파트는 각각 1개만 존재해야 한다 |

> 왜 중요한가: media 20개 × 출력 슬라이드 수만큼 새로 넣으면 25장 출력에서 파일이 수백 MB 로 팽창한다.
> rel 재사용이면 산출 크기가 ≈ 템플릿 크기로 유지된다.

### 3.5 `sldIdLst` 처리와 원본 20장 제거 (AD-12)

```python
def assemble(target_prs, clone_plan: list[tuple[int, str, str | None]], profile, pairs):
    """clone_plan[k] = (bg_index, line1, line2)"""
    created: list[tuple[SlidePart, str]] = []   # (part, slide_rId)

    for bg_index, line1, line2 in clone_plan:
        src_slide = target_prs.slides[bg_index]
        new_part  = clone_slide(src_slide, target_prs, keep_notes=KEEP_NOTES)
        rId       = target_prs.part.relate_to(new_part, RT_SLIDE)   # presentation → slide
        created.append((new_part, rId))

    sldIdLst = target_prs.slides._sldIdLst

    # (a) 원본 20장 참조를 모두 제거 (파트 삭제가 아니라 참조 제거 — AD-12/R7)
    original_ids = [el.get("id") for el in list(sldIdLst)]
    original_rids = [el.get(qn("r:id")) for el in list(sldIdLst)]
    for el in list(sldIdLst):
        sldIdLst.remove(el)

    # (b) 복제본만 순서대로 재등록 — sldId/@id 는 패키지 내 유일해야 한다
    next_id = max((int(i) for i in original_ids if i and i.isdigit()), default=255) + 1
    for new_part, rId in created:
        el = sldIdLst.add_sldId(rId)          # python-pptx 헬퍼
        el.set("id", str(next_id)); next_id += 1

    # (c) 원본 슬라이드 orphan rel 정리 (선택적, 재열기 검증 통과 시에만)
    for rId in original_rids:
        if rId is not None and rId not in {r for _, r in created}:
            _drop_rel_if_present(target_prs.part, rId)
```

| 결정 | 값 | 근거 |
| --- | --- | --- |
| 원본 20장 | **전부 제거** (참조만 제거) | 산출물은 가사 슬라이드만 (계획서 05 §3 결정) |
| 파트 삭제 | **하지 않음** | media/layout/theme 파트는 유지되어야 배경이 산다 (R7) |
| 순서 | 복제본 등록 → 원본 제거 → 복제본 재등록 | 뒤에 붙은 복제본을 남기고 앞의 원본만 걷어내면 순서가 자연 보존된다 |
| `sldId/@id` | 기존 최대값 + 1 부터 증가 | 유일성 보장. 재사용 금지 |
| `presentation → slide` rel | 남겨도 되지만 orphan 은 rel 제거 | PowerPoint 경고 최소화 |

검증: `len(sldIdLst) == N`, 그리고 `Presentation(out)` 재열기 성공 (T04/T06).

### 3.6 notesSlide 처리 (AD-08)

| 항목 | 확정 |
| --- | --- |
| 복제 여부 | **하지 않음** (`keep_notes=False` 기본) |
| 구현 | `remap_relationships` 에서 `RT_NOTES_SLIDE` skip → 새 파트에 notesSlide rel 없음 |
| 결과 | 산출 슬라이드의 노트는 비어 있음 |
| 오버라이드 | `LYRICS2PPTX_KEEP_NOTES=1` 또는 `--keep-notes` 로 `keep_notes=True` 가능 (기본 off) |
| 진단 | `BuildResult.dropped_notes_count == N` | 

## 4. 텍스트 채우기 (`pptx/fill.py`) — T05

### 4.1 대상 도형 탐색

```python
def find_title1(slide) -> Shape:
    for shp in slide.shapes:                 # placeholders 아님 — shapes 순회 (실측: <p:ph> 아님)
        if shp.name == "제목 1":
            return shp
    raise TemplateStructureError(
        '도형 "제목 1" 을 찾지 못했습니다. 존재하는 도형: '
        + ", ".join(repr(s.name) for s in slide.shapes)
    )
```

- **이름 비교는 정확히 `"제목 1"`** (공백 포함). 트레일링 공백 제거 후 비교(`name.strip()`).
- `slide.placeholders` 를 쓰지 않는다. 템플릿 도형은 placeholder 가 아니다.
- 없으면 즉시 실패 + 진단(도형명 목록) — 조용한 통과 금지.

### 4.2 txBody 재구성 절차 (조각 run 문제 해결)

slide20 은 `['너는 담장 너머로', ' ', '뻗은 나무', '가지에 푸른 열매처럼']` 처럼 run 이 조각나 있다.
run 을 그대로 두고 텍스트만 교체하면 조각이 남는다 → **문단을 전부 새로 만든다.**

```
fill_title1(slide, line1, line2, style: StyleSpec):
  shp    = find_title1(slide)
  txBody = shp.text_frame._txBody

  1) 스타일 소스 확보
     - style.rpr_xml / style.bodypr_xml 은 template_reader 가 슬라이드별로 직렬화해 둔 스냅샷
     - style.has_rpr == False (예: slide1) → profile.default_style 사용 + WARNING 1회 로그

  2) 기존 문단 전부 제거
     for p in txBody.findall(qn("a:p")): txBody.remove(p)

  3) bodyPr 확보·보존
     bodyPr = txBody.find(qn("a:bodyPr"))
     if bodyPr is None: bodyPr = SubElement(txBody, qn("a:bodyPr")); txBody.insert(0, bodyPr)
     ensure_norm_autofit(bodyPr)     # <a:normAutofit/> 추가(fontScale 미지정 = 100%). anchor/wrap 유지
     # 나머지 자식(예: <a:lstStyle>) 순서 유지 — p:cSld 스키마 순서 주의

  4) 문단 생성 (line2 가 None 이면 1개만)
     lines = [line1] if line2 is None else [line1, line2]
     for text in lines:
        p = SubElement(txBody, qn("a:p"))
        # 4a) 정렬/줄간격 보존을 위해 첫 원본 문단의 <a:pPr> 이 있으면 복제
        if original_pPr is not None: p.append(deepcopy(original_pPr))
        r = SubElement(p, qn("a:r"))
        r.append(parse_xml(style.rpr_xml))     # ★ run 서식(rPr) 상속 — sz/글꼴/색 그대로
        t = SubElement(r, qn("a:t"))
        t.text = text                          # lxml 이 &,<,> 이스케이프 처리
  5) 저장 전 {{ slide._element }} 은 이미 in-place 수정됨 (별도 커밋 불필요)
```

| 규칙 | 값 |
| --- | --- |
| F1 스타일 소스 | **그 슬라이드 첫 run 의 `rPr`** (deepcopy 스냅샷) → 슬라이드별 `sz` 유지(6000~7200) |
| F2 문단 수 | `line2 is None` → 1개, 아니면 2개. **3개 이상 만들지 않음** (요구 "2줄" 위반) |
| F3 줄바꿈 | 삽입하지 않음. 한 줄 = 한 문단 |
| F4 `normAutofit` | 항상 보장(없으면 추가, `fontScale` 미지정). `spAutoFit`/`noAutofit` 금지 |
| F5 `bodyPr` | `anchor`(세로 정렬)와 `wrap` 유지. anchor 를 바꾸지 않는다 |
| F6 위치/크기 | `제목 1` 의 `a:off`/`a:ext` 를 **변경하지 않는다** (템플릿 위치 보존) |
| F7 이스케이프 | `t.text =` 로 설정(lxml 이 처리). 문자열 조합으로 XML 만들지 않음 |
| F8 빈 문자열 | F3 규칙상 발생하지 않음(정규화가 빈 줄 제거). `line1` 이 빈 문자열이면 `TemplateStructureError` |

### 4.3 1줄 마지막 슬라이드

| 항목 | 결정 |
| --- | --- |
| 문단 수 | 1개 (빈 2번째 문단을 **넣지 않는다**) |
| 세로 위치 | `bodyPr/@anchor` 를 그대로 따름 (변경 금지) → 박스가 비어 보이지 않음 |
| 자동 축소 | 1줄이므로 `normAutofit` 은 무해(fontScale 100% 유지) |

### 4.4 서식 상속 검증 (T05)

```python
assert [p.text for p in find_title1(out_slide).text_frame.paragraphs] == [l1, l2]   # 또는 [l1]
assert run_sz(out_slide) == run_sz(template_slide_bg)                                # 예: 6600
assert find_norm_autofit(out_slide.text_frame._txBody) is not None
assert shape_off_ext(out_slide) == shape_off_ext(template_slide_bg)                  # 위치 불변
```

## 5. 능력 감지(Capability Probe) — T04 진입 시 1회

```python
def pptx_can_register_slide_part() -> bool:
    """python-pptx 내부 API 로 슬라이드 파트 등록이 가능한지 1회 시험한다."""
    try:
        prs = Presentation(TEMPLATE_PATH); pkg = prs.part.package
        from pptx.parts.slide import SlidePart
        src = prs.slides[1].part
        return SlidePart(pkg.next_partname("/ppt/slides/slide%d.xml"), src.content_type, pkg) is not None
    except Exception as exc:            # AttributeError/TypeError/ImportError 등
        log.warning("방식 A 불가: %s → 폴백 F1 사용", exc); return False
```

- 결과를 프로세스에 캐시(1회만 시험). `config.CLONE_STRATEGY = "auto" | "parts" | "zip"` 로 강제 가능.
- `auto`: probe 성공 → 방식 A, 실패 → F1. probe 예외는 **사용자 오류가 아니다**(내부 폴백 신호).

## 6. 폴백 F1 — zipfile + lxml 패키지 재작성 (`pptx/writer_zip.py`)

방식 A 가 불가하거나 저장 후 재열기 검증에 실패하면 사용한다. **외부 계약은 완전히 동일하다**
("출력 pptx 를 열면 배경이 그대로다"). clone/fill 의 상위 인터페이스(`PptxWriter`)만 바뀐다.

```python
class PptxWriter(Protocol):
    def write(self, plan: list[tuple[int, str, str | None]], profile, out: Path) -> BuildResult: ...

# filesystem-level 폴백 (요지)
def write_via_zip(plan, profile, out: Path) -> BuildResult:
    with zipfile.ZipFile(TEMPLATE_PATH) as zin:
        # 1) 템플릿의 모든 파트를 출력 zip 에 복사 (원본 slide1..20 은 미참조로 남겨도 무방)
        # 2) 출력 슬라이드 k 마다:
        #    - out = f"ppt/slides/slide{1000+k}.xml", src = f"ppt/slides/slide{bg_index+1}.xml"
        #    - src slide XML 을 그대로 복사, rels 는 Target 재작성(rId 재부여)
        #    - [Content_Types].xml 에 Override 추가, presentation.xml.rels 에 slide rel 추가
        #    - presentation.xml 의 <p:sldIdLst> 를 복제 슬라이드 rId 목록으로 교체
        # 3) slides/_rels 에서 notesSlide rel 제거(AD-08)
        # 4) zip 쓰기 후 os.replace 로 원자적 교체
    return BuildResult(...)
```

| 항목 | 확정 |
| --- | --- |
| 트리거 | probe 실패 또는 방식 A 산출물의 재열기 검증 실패 |
| 대상 파트 | `ppt/slides/*`, `ppt/slides/_rels/*`, `[Content_Types].xml`, `ppt/presentation.xml(.rels)` |
| media/layout/theme | 템플릿에서 **그대로 복사** (재사용, 재생성 금지 — AD-06 유지) |
| rels 재작성 | rId 를 새로 부여하고 슬라이드 XML 의 `r:embed`/`r:id` 를 치환 (동일 로직 재사용) |
| 계약 | 방식 A 와 동일: `Presentation(out)` 재열기 성공 + 배경 유지 + `sldIdLst` 길이 N |
| 테스트 | `test_writer_contract.py` 가 동일 plan 으로 두 writer 의 결과를 **동등성 단언** (배경 인덱스, 장수, 텍스트) |

> 폴백도 실패하면 `TemplateStructureError("슬라이드 파트 등록에 실패했습니다(방식 A/F1 모두).")` 로
> 명시적 실패하고, 사용자에게 `template.pptx` 교체 또는 재저장을 안내한다. 조용한 빈 문서 생성은 금지.

## 7. 빌드 오케스트레이션 (`pptx/builder.py`)

```python
def build_presentation(pairs, template_path, output_path=None,
                       *, skip_slide=(), keep_notes=False, strategy="auto") -> BuildResult:
    if not pairs:
        raise EmptyLyricsError("가사에서 유효한 줄을 찾지 못했습니다.")     # 방어적 2차 검사

    profile = get_template_profile(template_path, skip_slide=skip_slide)  # 캐시(1회 분석)
    P, N = len(profile.pool), len(pairs)

    plan = [
        (profile.pool[k % P], line1, line2)                              # §2.1 공식
        for k, (line1, line2) in enumerate(pairs)
    ]
    if not profile.pool:
        raise ConfigError("배경 풀에 사용할 슬라이드가 없습니다.")

    out = resolve_output_path(output_path, title=None)                   # config 규칙
    tmp = out.with_name(f".tmp-{uuid4().hex}{out.suffix}")

    try:
        writer = select_writer(strategy)                                 # parts | zip
        result = writer.write(plan, profile, tmp)
        _verify_reopenable(tmp)                                          # Presentation(tmp) 성공 검증
        os.replace(tmp, out)                                             # 원자적 교체 (AD-13)
    except Lyrics2PptxError:
        _cleanup(tmp); raise
    except OSError as exc:
        _cleanup(tmp); raise OutputWriteError(f"파일을 저장하지 못했습니다: {out}") from exc

    return BuildResult(out, N, P, wrap_count=(N - 1) // P if N else 0,
                       used_bg_indices=tuple(b for b, _, _ in plan),
                       dropped_notes_count=0 if keep_notes else N)
```

`_verify_reopenable(tmp)`: `Presentation(tmp)` 로 열고 `len(prs.slides._sldIdLst) == N` 을 확인한다.
실패하면 방식 A → F1 로 1회 재시도(§6), 그래도 실패하면 `TemplateStructureError`.

## 8. 실패 케이스 → 예외 → 사용자 메시지

| 케이스 | 예외 (모듈) | 사용자 메시지 |
| --- | --- | --- |
| `제목 1` 도형 없음 | `TemplateStructureError` (`fill.py`) | "템플릿 구조를 읽을 수 없습니다: 도형 '제목 1' 없음 (도형: ...)" |
| 복제 후 배경 누락(미매핑 rel) | `TemplateStructureError` (`clone.py`) | "템플릿 구조를 읽을 수 없습니다: 관계 참조 r:embed=... 미매핑" |
| 파트 등록 불가(A/F1 모두) | `TemplateStructureError` (`builder.py`) | "슬라이드 복제에 실패했습니다. template.pptx 를 다시 저장해 주세요." |
| 저장/교체 실패 | `OutputWriteError` (`builder.py`) | "파일을 저장하지 못했습니다: <경로>" |
| 페어 0개 | `EmptyLyricsError` (`pipeline.py`) | "가사에서 유효한 줄을 찾지 못했습니다." |
| 템플릿 없음/잠김 | `ConfigError` (`template_reader.py`) | "템플릿을 열 수 없습니다: <경로>" |

## 9. T04/T05/T06 완료 기준 (계획서 05 §8 그대로 + 강화)

- [ ] T04 `zlib` 재열기: 복제 슬라이드 rels 에 `image` rel 존재·대상=원본 media, slide20(전면 `<p:pic>`)의 `r:embed` 가 새 rels 의 유효 rId
- [ ] T04 **복제 N장 후 `ppt/media/*` 개수 = 템플릿과 동일**(AD-06 R-M4)
- [ ] T04 `sldIdLst` 길이 = N, 원본 20장 부재, `sldId/@id` 전부 유일, notesSlide rel 부재(keep_notes=False)
- [ ] T05 `find_title1` 텍스트 == `[l1, l2]` (홀수면 `[l1]`), `sz` 동일, `normAutofit` 존재, 위치 불변
- [ ] T06 25줄→13장, 40줄→20장, 42줄→21장(21번째 bg=0), `Presentation()` 재열기 성공
