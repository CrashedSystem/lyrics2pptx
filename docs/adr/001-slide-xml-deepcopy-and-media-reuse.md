# ADR-001: 슬라이드 `<p:sld>` XML deepcopy 로 복제하고 media 파트는 재사용한다

- 상태: **채택(구현 완료)**
- 날짜: 2026-10-18
- 관련: `docs/arch/04 §3`, AD-04/AD-06/AD-11/AD-12, `src/lyrics2pptx/pptx/clone.py`

## 배경

`template.pptx` 의 배경 슬라이드를 복제해 가사를 채운 뒤, **원본과 동일한 배경(레이아웃·테마·
이미지)을 유지한 채** 슬라이드만 늘려야 한다. python-pptx 에는 슬라이드 복사 API 가 없다.

검토한 두 방식:

- **방식 B(기각)**: 빈 슬라이드를 추가하고 도형을 옮긴다. 이 방식은 슬라이드와
  `slideLayout` 관계 및 `<p:bg>`(배경) 를 잃는다. 이 프로젝트의 핵심 요구(배경이 슬라이드마다
  다름)를 정면으로 깨뜨린다.
- **방식 A(채택)**: `<p:sld>` **전체 요소**를 `copy.deepcopy` 로 새 슬라이드 파트에 복사한 뒤,
  그 안의 관계 참조(`r:embed`/`r:link`/`r:id`)를 새 파트 기준으로 재매핑한다.

## 결정

`pptx/clone.py` 의 `clone_slide()` 가 방식 A 를 구현한다.

1. `copy.deepcopy(src_slide._element)` 로 `<p:sld>` 전체를 복사한다.
2. `package.next_partname("/ppt/slides/slide%d.xml")` 로 새 파트명을 받는다.
3. `remap_relationships()` 로 모든 관계를 재매핑한다.
   - **내부 관계(`slideLayout`, `image`)는 원본의 target part 를 그대로 재사용**한다 →
     **media 파트를 새로 만들지 않는다**(중복 이미지 방지).
   - 외부 관계는 target URL 을 유지하고 external 로 다시 건다.
   - `notesSlide` 관계는 기본적으로 건너뛴다(AD-08).
   - 매핑되지 않는 `r:*` 참조는 **조용히 두지 않고** `TemplateStructureError` 로 실패시킨다
     — 배경(이미지)이 말없이 사라지는 것을 차단하는 안전장치.
4. 새 파트를 `presentation.part.relate_to(new_part, RT_SLIDE)` 로 즉시 등록한다(관계 등록은
   get-or-add 라 이후 같은 호출은 같은 rId 를 돌려준다).

원본 슬라이드는 **참조(`sldIdLst`)만 제거**하고 파트 자체는 남긴다. 그 결과 `ppt/media/*` 개수는
템플릿과 동일하게 유지된다.

## 결과

- 복제 슬라이드가 배경·이미지·레이아웃을 **원본과 동일하게** 갖는다.
- `ppt/media/*` 개수가 변하지 않는다 → 산출물 크기는 대략 템플릿 크기(≈37 MB)에 수렴한다.
- 파트명 충돌 회피를 위해 `prs.slides` 접근은 **정확히 1회**만 한다(두 번째 접근은 저장 시
  파트명 충돌을 유발하므로 `writer_parts` 가 그 프록시를 끝까지 붙든다).
- python-pptx 내부 생성자(`SlidePart(...)`)는 `_make_slide_part()` 한 함수에 격리해, 라이브러리
  버전이 올라가도 흡수 지점이 한 곳이다.
- 방식 A 가 불가/불안정할 때를 대비해 동일 계약의 폴백 F1(zip 재작성, `writer_zip.py`)을 둔다.
  `auto` 전략만 폴백하고, 강제 지정한 전략은 실패를 그대로 노출한다.

## 근거(코드)

- `pptx/clone.py`: `clone_slide`, `remap_relationships`, `_make_slide_part`, `pptx_can_register_slide_part`
- `pptx/writer_parts.py`: `sldIdLst` 재구성 + `fill_txBody`
- `pptx/writer_zip.py`: 폴백 F1(모든 멤버를 그대로 복사, media 불변)
- 검증: `tests/test_clone.py`, `tests/test_writer_contract.py`(parts vs zip 동등성), `tests/test_builder.py`
