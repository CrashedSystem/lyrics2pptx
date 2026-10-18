# ADR-003: robots.txt·지연을 리다이렉트 hop 마다 다시 적용한다

- 상태: **채택(구현 완료)**
- 날짜: 2026-10-18
- 관련: `리뷰리포트.md` M3(주요), `docs/arch/06 §2`, `src/lyrics2pptx/crawl/httputil.py`

## 배경

수집 예절(robots.txt 준수, 호스트별 최소 지연, UA 명시)은 전부 `crawl/httputil.py` 한 곳에서
강제하기로 했다. 어댑터는 `urllib` 을 직접 쓰지 못하고 `get_json`/`get_text` 만 호출한다(P7).

문제: 표준 `urllib` 의 `urlopen` 은 리다이렉트(`30x`)를 **내부에서 자동 추종**한다. 초기 코드는
**최초 URL 에 대해서만** robots 검사와 지연을 적용했기 때문에, 서버가 `30x` 한 번으로 다른
호스트로 보내면 **그 호스트의 robots.txt 검사와 지연이 통째로 우회**되었다. 즉
`docs/arch/06` 이 선언한 "robots/delay 우회 불가"가 실제로는 성립하지 않았다(리뷰 M3).

## 결정

**리다이렉트를 수동으로 추종하고, 매 hop 마다 게이트를 다시 통과시킨다.**

- `HTTPRedirectHandler` 를 서브클래스한 `_GuardedRedirectHandler` 를 각 요청의 opener 에 설치한다.
- `redirect_request()`(hop 이 일어날 때마다 호출되는 훅)에서:
  - 이미 방문한 URL 이면 → 리다이렉트 **루프**로 보고 `SourceUnavailableError`.
  - hop 수가 `MAX_REDIRECTS`(5) 를 넘으면 → 실패(fail closed).
  - 그 외에는 다음 hop URL 에 대해 **`_guard(newurl)` 을 다시 호출**한다 →
    robots.txt 확인 + 호스트 지연이 **매 hop 재적용**된다.
- `_guard()` 는 모든 outbound 요청이 반드시 통과하는 단일 관문이며, `allowed_by_robots()` 가
  false 면 `SourceBlockedError`.
- robots.txt 조회 자체도 `_open_and_read` 를 쓰므로, **도달 불가/파싱 불가 robots.txt 는
  차단(fail closed)** 으로 처리한다. 판정은 호스트별로 `ROBOTS_CACHE_TTL`(600초) 동안 캐시된다.
- 지연은 호스트별 마지막 요청 시각을 추적해 `HTTP_DELAY`(기본 2초) 이상 간격을 보장한다.
- 요청은 스레드/프로세스 풀 없이 **엄격히 순차** 실행된다.

## 결과

- `30x` 한 번으로 robots/지연을 우회할 수 없다 — 문서가 선언한 보장이 코드에서 성립한다.
- hop 상한과 루프 감지로 악성 리다이렉트 체인에서 무한 추종하지 않는다(fail closed).
- 403/404 는 즉시 차단, 429/5xx/타임아웃만 지수 백오프(1·2·4초, `MAX_REDIRECTS` 와 별개인
  `LYRICS2PPTX_MAX_RETRIES` 회)로 재시도한다.
- 부작용: 요청당 opener 를 새로 만든다(hop 예산을 요청 단위로 격리하기 위함 — 비용 미미).

## 근거(코드)

- `crawl/httputil.py`: `_GuardedRedirectHandler`, `_guard`, `allowed_by_robots`, `_respect_delay`,
  `MAX_REDIRECTS = 5`, `_open_and_read`
- 검증: `tests/test_httputil.py::RedirectGuardTests`
  (`test_every_hop_reapplies_robots_and_delay`, `test_redirect_target_host_robots_disallow_blocks_the_hop`,
  `test_hop_count_above_the_cap_fails_closed`, `test_two_hop_loop_is_detected` 등)
