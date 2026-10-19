/*
 * 가사 2줄 PPTX 생성기 — 프런트엔드 로직 (vanilla JS, 프레임워크 없음).
 *
 * 상태 키: mode, query, results, selectedIndex, selectedLines, previewPairs,
 * busy, page, slideIndex, slides, lastFile.
 * 정규화(N1~N10)와 2줄 페어링은 서버 단일 소스가 계산한다: 직접 입력 미리보기는
 * POST /api/preview (pipeline.preview → text.normalize) 결과를 그대로 쓴다.
 * 배경 선택은 서버(/api/slides)가 준 썸네일 격자로 고른다 — 고른 1장을 모든
 * 슬라이드가 복제한다. 클라이언트는 마커/정규화 규칙을 재구현하지 않는다.
 *
 * 서버 수명(ADR-032)도 여기서 다룬다: 이 페이지는 "살아 있다"(/api/ping)와
 * "닫는 중"(/api/ping?closing=1)만 알려주고, 실제 종료 판단은 서버(webapp.py)가
 * 한다. 그래서 화면에는 "창을 닫으면 N분 뒤 정리"처럼 사실만 적는다.
 */
"use strict";

var PAGE_SIZE = 3; // 미리보기 페이지당 슬라이드 수
var PREVIEW_DEBOUNCE_MS = 250; // 직접 입력 미리보기 디바운스
var DEFAULT_SLIDE_INDEX = 19; // 템플릿에서 "정확히 2줄" 예시가 있는 슬라이드
var SESSION_META = "lyrics2pptx-session"; // 서버가 index.html 에 심는 종료 토큰
var STATUS_POLL_MS = 60000; // 상태 칩 갱신 주기(하트비트도 겸한다)
var KEEP_WARNING_SECONDS = 180; // 자동 정리 임박 안내를 띄우는 남은 시간

var state = {
  mode: "search",
  query: "",
  results: [],
  selectedIndex: -1,
  selectedLines: [],
  previewPairs: null, // 직접 입력 모드에서 서버가 계산한 [[a,b],...]
  busy: false,
  page: 0,
  slideIndex: DEFAULT_SLIDE_INDEX,
  slides: [],
  lastFile: "", // 마지막으로 만든 파일 이름(폴더 열기 대상)
};

// 서버가 준 종료 토큰: 종료/폴더 열기 요청에만 쓴다(다른 사이트가 못 흉내내게).
var sessionToken = (function () {
  var meta = document.querySelector('meta[name="' + SESSION_META + '"]');
  return meta ? meta.getAttribute("content") || "" : "";
})();
var serverGone = false;
var statusTimer = null;

// 직접 입력 미리보기 요청 순번: 늦게 도착한 이전 응답을 무시한다.
var previewRequestId = 0;
var previewTimer = null;

// 진행 표시: 검색/생성 중 경과 시간을 재는 시계와, "완료" 를 잠깐 보여주는 타이머.
var progressClockTimer = null;
var progressHideTimer = null;

var el = {
  modeInputs: document.querySelectorAll('input[name="mode"]'),
  searchMode: document.getElementById("search-mode"),
  manualMode: document.getElementById("manual-mode"),
  query: document.getElementById("query"),
  source: document.getElementById("source"),
  searchButton: document.getElementById("search-button"),
  rawText: document.getElementById("raw-text"),
  slidesPanel: document.getElementById("slides-panel"),
  slidesSummary: document.getElementById("slides-summary"),
  slides: document.getElementById("slides"),
  resultsPanel: document.getElementById("results-panel"),
  results: document.getElementById("results"),
  previewPanel: document.getElementById("preview-panel"),
  previewSummary: document.getElementById("preview-summary"),
  preview: document.getElementById("preview"),
  previewEmpty: document.getElementById("preview-empty"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageLabel: document.getElementById("page-label"),
  dedupe: document.getElementById("dedupe-chorus"),
  filename: document.getElementById("filename"),
  generateButton: document.getElementById("generate-button"),
  downloadPanel: document.getElementById("download-panel"),
  downloadLink: document.getElementById("download-link"),
  downloadSummary: document.getElementById("download-summary"),
  revealButton: document.getElementById("reveal-button"),
  serverState: document.getElementById("server-state"),
  serverStateText: document.getElementById("server-state-text"),
  serverGone: document.getElementById("server-gone"),
  shutdownButton: document.getElementById("shutdown-button"),
  keepButton: document.getElementById("keep-button"),
  error: document.getElementById("error"),
  progress: document.getElementById("progress"),
  progressLabel: document.getElementById("progress-label"),
  progressTime: document.getElementById("progress-time"),
  progressTrack: document.getElementById("progress-track"),
  progressFill: document.getElementById("progress-fill"),
  progressSteps: document.getElementById("progress-steps"),
};

/* ---------------------------------------------------------------- helpers */

function showError(message) {
  el.error.textContent = message;
  el.error.hidden = false;
}

function clearError() {
  el.error.textContent = "";
  el.error.hidden = true;
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) return "";
  var minutes = Math.round(seconds / 60);
  if (minutes <= 1) return "1분";
  if (minutes < 60) return minutes + "분";
  return Math.round((minutes / 60) * 10) / 10 + "시간";
}

/* ------------------------------------------------------------ server session */

// 서버(ADR-032)와의 계약: 창을 닫으면 몇 분 뒤, 오래 쓰지 않아도 스스로 정리된다.
// 화면은 그 사실을 숨기지 않고 상태 칩에 적는다. 여기서 타이머 하나로 "살아
// 있음"을 판단하지 않는 이유: 브라우저는 배경 탭의 타이머를 늦추거나 얼리므로
// heartbeat 가 없다고 닫힌 탭이라고 단정할 수 없다(pagehide 신호를 따로 보낸다).

function pageHeaders(extra) {
  var headers = extra || {};
  if (sessionToken) headers["X-Lyrics2Pptx-Token"] = sessionToken;
  return headers;
}

// 네트워크 실패(서버가 이미 정리됨)를 한 곳에서 처리한다: 화면에 그 사실을
// 띄우고 버튼을 잠근다. 원인을 감추고 "생성에 실패했습니다" 만 보여주면
// 사용자는 무엇을 해야 하는지 알 수 없다.
async function apiFetch(url, options) {
  try {
    return await fetch(url, options);
  } catch (err) {
    markServerGone();
    throw new Error("서버가 정리되었습니다. 실행 파일을 다시 눌러 주세요.");
  }
}

function setServerState(name, text) {
  el.serverState.dataset.state = name;
  el.serverStateText.textContent = text;
}

function markServerGone() {
  if (serverGone) return;
  serverGone = true;
  if (statusTimer !== null) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  el.serverGone.hidden = false;
  el.keepButton.hidden = true;
  setServerState("gone", "서버 종료됨");
  updateBusyUi();
}

function describeLifetime(payload) {
  if (!payload.auto_shutdown) return "자동 정리 꺼짐";
  return "창을 닫으면 " + formatDuration(payload.tab_grace_s) + " 뒤 정리";
}

async function refreshServerState() {
  if (serverGone) return;
  var response;
  try {
    response = await apiFetch("/api/ping");
    if (!response.ok) throw new Error("상태를 읽지 못했습니다.");
    var payload = await response.json();
  } catch (err) {
    return; // apiFetch 가 이미 안내를 띄웠다
  }
  if (payload.shutdown_reason) {
    markServerGone();
    return;
  }
  var idleLeft = payload.idle_seconds_left;
  var warning =
    payload.auto_shutdown &&
    typeof idleLeft === "number" &&
    idleLeft <= KEEP_WARNING_SECONDS;
  if (warning) {
    setServerState("warning", "곧 자동 정리 (" + formatDuration(idleLeft) + ")");
    el.keepButton.hidden = false;
  } else {
    setServerState("", describeLifetime(payload));
    el.keepButton.hidden = true;
  }
}

async function keepSessionAlive() {
  var response;
  try {
    response = await apiFetch("/api/ping?keep=1");
    if (!response.ok) throw new Error("연장하지 못했습니다.");
    var payload = await response.json();
  } catch (err) {
    return;
  }
  setServerState("", describeLifetime(payload));
  el.keepButton.hidden = true;
}

async function shutdownServer() {
  clearError();
  el.shutdownButton.disabled = true;
  try {
    var response = await apiFetch("/api/shutdown", {
      method: "POST",
      headers: pageHeaders({ "Content-Type": "application/json" }),
      body: "{}",
    });
    var payload = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error(
        (payload.error && payload.error.message) || "서버를 종료하지 못했습니다."
      );
    }
    markServerGone();
  } catch (err) {
    el.shutdownButton.disabled = serverGone;
    if (!serverGone) showError(err.message || "서버를 종료하지 못했습니다.");
  }
}

// 브라우저가 탭을 닫는다고 알려주는 유일한 신호. 헤더를 실어야 하므로
// sendBeacon 대신 keepalive fetch 를 쓴다(토큰이 없으면 서버가 무시한다).
function reportTabClosing() {
  if (serverGone || !sessionToken) return;
  try {
    fetch("/api/ping?closing=1", {
      method: "POST",
      keepalive: true,
      headers: pageHeaders({ "Content-Type": "application/json" }),
      body: "{}",
    });
  } catch (err) {
    /* 신호를 못 보내도 서버는 무사용 시간(idle) 규칙으로 정리된다. */
  }
}

/* ---------------------------------------------------------------- progress */

// 서버는 "몇 개 중 몇 번째" 를 실제로 알 때만 퍼센트를 보낸다(모르면 null).
// 총량을 모를 때는 퍼센트를 지어내지 않고 흐르는 막대로 대신한다 — 숫자를
// 보여주는 순간 사용자는 그 숫자를 믿기 때문이다.

function buildStepItem(message) {
  var li = document.createElement("li");
  var mark = document.createElement("span");
  mark.className = "mark";
  mark.setAttribute("aria-hidden", "true");
  var text = document.createElement("span");
  text.className = "step-text";
  text.textContent = message;
  li.appendChild(mark);
  li.appendChild(text);
  return li;
}

function renderProgressSteps(steps) {
  el.progressSteps.textContent = "";
  var recent = steps.slice(-5);
  recent.forEach(function (step, index) {
    var isCurrent = index === recent.length - 1;
    var li = buildStepItem(step.message);
    li.className = isCurrent ? "current" : "done";
    if (!isCurrent) li.querySelector(".mark").textContent = "✓";
    el.progressSteps.appendChild(li);
  });
}

function setProgressPercent(percent) {
  var known = typeof percent === "number" && isFinite(percent);
  el.progress.classList.toggle("indeterminate", !known);
  if (known) {
    var clamped = Math.max(0, Math.min(100, percent));
    el.progressFill.style.width = clamped + "%";
    el.progressTrack.setAttribute("aria-valuenow", String(Math.round(clamped)));
    el.progressTrack.setAttribute("aria-valuetext", Math.round(clamped) + "%");
  } else {
    el.progressFill.style.width = "";
    el.progressTrack.removeAttribute("aria-valuenow");
    el.progressTrack.setAttribute("aria-valuetext", "진행 중");
  }
}

function tickProgressClock() {
  var seconds = (Date.now() - state.progressStartedAt) / 1000;
  el.progressTime.textContent = seconds.toFixed(1) + "초";
}

function progressStart(label) {
  if (progressHideTimer !== null) {
    clearTimeout(progressHideTimer);
    progressHideTimer = null;
  }
  state.progressStartedAt = Date.now();
  el.progressLabel.textContent = label;
  el.progressSteps.textContent = "";
  el.progress.classList.remove("done");
  el.progress.hidden = false;
  setProgressPercent(null);
  tickProgressClock();
  if (progressClockTimer !== null) clearInterval(progressClockTimer);
  progressClockTimer = setInterval(tickProgressClock, 100);
}

function progressStop() {
  if (progressClockTimer !== null) {
    clearInterval(progressClockTimer);
    progressClockTimer = null;
  }
}

// 완료: 막대를 가득 채우고 잠깐 보여준 뒤 사라진다(다음 작업은 progressStart 부터).
function progressFinish(label) {
  progressStop();
  tickProgressClock();
  el.progressLabel.textContent = label;
  el.progress.classList.remove("indeterminate");
  el.progress.classList.add("done");
  el.progressFill.style.width = "100%";
  el.progressTrack.setAttribute("aria-valuenow", "100");
  el.progressTrack.setAttribute("aria-valuetext", "완료");
  // 마지막 단계도 끝났으니 맥동하는 점을 체크로 바꾼다.
  Array.prototype.forEach.call(el.progressSteps.children, function (li) {
    li.className = "done";
    li.querySelector(".mark").textContent = "✓";
  });
  progressHideTimer = setTimeout(function () {
    el.progress.hidden = true;
    progressHideTimer = null;
  }, 1200);
}

// 실패: 진행 표시는 즉시 치우고 오류 문구만 남긴다.
function progressFail() {
  progressStop();
  el.progress.hidden = true;
  el.progress.classList.remove("done", "indeterminate");
  el.progressSteps.textContent = "";
}

/* ------------------------------------------------------------ client rules */

// D4/D5: 2줄씩 묶기 (+ 선택적 반복 줄 축소).
// 검색 모드의 부분 미리보기(서버가 보낸 preview_lines)에만 쓰인다. 직접 입력
// 모드의 페어링은 서버(/api/preview)가 계산한 값을 그대로 사용한다(M1).
function splitPairs(lines, dedupeRepeat) {
  var src = lines.slice();
  if (dedupeRepeat) {
    var counts = {};
    src.forEach(function (line) { counts[line] = (counts[line] || 0) + 1; });
    var seen = {};
    src = src.filter(function (line) {
      if (counts[line] >= 3) {
        var used = seen[line] || 0;
        if (used >= 2) return false;
        seen[line] = used + 1;
      }
      return true;
    });
  }
  var pairs = [];
  for (var i = 0; i < src.length; i += 2) {
    pairs.push([src[i], i + 1 < src.length ? src[i + 1] : null]);
  }
  return pairs;
}

/* ------------------------------------------------------- slide selection */

// 서버(/api/slides)가 준 20개 슬라이드 메타로 썸네일 격자를 그린다. 기본 19가
// 미리 선택되며, 고른 1장을 모든 산출 슬라이드가 복제한다.
function backgroundLabel() {
  return "template " + state.slideIndex;
}

function updateSlidesSummary() {
  el.slidesSummary.textContent =
    "선택한 배경: template " + state.slideIndex + " (모든 슬라이드에 동일 적용)";
}

function renderSlides() {
  el.slides.textContent = "";
  if (!state.slides.length) return;

  state.slides.forEach(function (meta) {
    var selected = meta.index === state.slideIndex;
    var li = document.createElement("li");
    li.className = "slide-item" + (selected ? " selected" : "");
    li.setAttribute("role", "radio");
    li.setAttribute("aria-checked", selected ? "true" : "false");
    li.setAttribute("tabindex", "0");

    var frame = document.createElement("span");
    frame.className = "slide-thumb";
    var img = document.createElement("img");
    img.src = meta.thumbnail_url;
    img.alt = "슬라이드 " + meta.index + " 배경";
    img.loading = "lazy";
    img.addEventListener("error", function () {
      img.hidden = true;
      if (!frame.querySelector(".slide-thumb-fallback")) {
        var fallback = document.createElement("span");
        fallback.className = "slide-thumb-fallback";
        fallback.textContent = "이미지 없음";
        frame.appendChild(fallback);
      }
    });
    frame.appendChild(img);

    var caption = document.createElement("span");
    caption.className = "slide-caption";
    caption.textContent = meta.index + (meta.has_title_shape ? "" : " (제목 없음)");

    li.appendChild(frame);
    li.appendChild(caption);
    li.addEventListener("click", function () { selectSlide(meta.index); });
    li.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectSlide(meta.index);
      }
    });
    el.slides.appendChild(li);
  });
}

function selectSlide(index) {
  if (index === state.slideIndex) return;
  state.slideIndex = index;
  renderSlides();
  updateSlidesSummary();
  // 선택이 바뀌면 미리보기를 즉시 갱신한다.
  if (state.mode === "manual") {
    scheduleManualPreview();
  } else {
    renderPreview();
  }
}

async function loadSlides() {
  try {
    var response = await apiFetch("/api/slides");
    var payload = await response.json();
    if (!response.ok) {
      throw new Error(
        (payload.error && payload.error.message) || "배경 슬라이드를 불러오지 못했습니다."
      );
    }
    state.slides = Array.isArray(payload) ? payload : (payload.slides || []);
    var exists = state.slides.some(function (meta) { return meta.index === state.slideIndex; });
    if (!exists && state.slides.length) {
      state.slideIndex = state.slides[state.slides.length - 1].index;
    }
  } catch (err) {
    state.slides = [];
    el.slidesSummary.textContent =
      err.message || "배경 슬라이드를 불러오지 못했습니다.";
    return;
  }
  renderSlides();
  updateSlidesSummary();
}

/* ------------------------------------------------------------------ search */

function renderResults() {
  el.results.textContent = "";
  if (!state.results.length) {
    el.resultsPanel.hidden = true;
    return;
  }
  el.resultsPanel.hidden = false;

  state.results.forEach(function (hit) {
    var li = document.createElement("li");
    li.className = "result-item";

    var label = document.createElement("label");
    label.className = "radio";

    var input = document.createElement("input");
    input.type = "radio";
    input.name = "result";
    input.value = String(hit.index);
    input.checked = hit.index === state.selectedIndex;
    input.addEventListener("change", function () { selectResult(hit.index); });

    var text = document.createElement("span");
    var title = document.createElement("strong");
    title.textContent = hit.title || "(제목 없음)";
    text.appendChild(title);
    if (hit.artist) {
      var artist = document.createElement("span");
      artist.className = "artist";
      artist.textContent = " — " + hit.artist;
      text.appendChild(artist);
    }

    label.appendChild(input);
    label.appendChild(text);
    li.appendChild(label);

    if (hit.preview_lines && hit.preview_lines.length) {
      var preview = document.createElement("p");
      preview.className = "result-preview";
      preview.textContent = hit.preview_lines.join(" / ");
      li.appendChild(preview);
    }

    el.results.appendChild(li);
  });
  highlightSelectedResult();
}

// 선택 표시는 클래스를 직접 갱신한다(다시 그리면 방금 누른 라디오가 사라진다).
function highlightSelectedResult() {
  Array.prototype.forEach.call(el.results.children, function (li) {
    var input = li.querySelector("input");
    var on = !!input && Number(input.value) === state.selectedIndex;
    li.classList.toggle("selected", on);
  });
}

function selectResult(index) {
  var hit = state.results.find(function (item) { return item.index === index; });
  if (!hit) return;
  state.selectedIndex = index;
  // 검색 모드에서는 서버가 보낸 미리보기 줄만 알 수 있다(부분 미리보기).
  state.selectedLines = (hit.preview_lines || []).slice();
  state.previewPairs = null;
  state.page = 0;
  highlightSelectedResult();
  renderPreview();
}

/* ------------------------------------------------------------ search job */

// 검색은 블로그 페이지를 여러 장 받아오느라 10초 이상 걸릴 수 있다. 그래서 한
// 번에 응답을 기다리지 않고, 작업을 시작해 두고(202) 상태를 폴링하면서 진행
// 상황을 그린다 (서버: /api/search/start + /api/search/job, crawl.progress).
var SEARCH_POLL_MS = 400;

function sleep(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

async function startSearchJob(query, source) {
  var url =
    "/api/search/start?q=" +
    encodeURIComponent(query) +
    "&source=" +
    encodeURIComponent(source);
  var response = await apiFetch(url);
  var payload = await response.json().catch(function () {
    return {};
  });
  if (!response.ok) {
    throw new Error((payload.error && payload.error.message) || "검색을 시작하지 못했습니다.");
  }
  return payload.job_id;
}

async function waitForSearchJob(jobId) {
  for (;;) {
    var response = await apiFetch("/api/search/job?id=" + encodeURIComponent(jobId));
    var payload = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error((payload.error && payload.error.message) || "검색 상태를 읽지 못했습니다.");
    }
    renderProgressSteps(payload.steps || []);
    setProgressPercent(payload.progress ? payload.progress.percent : null);
    if (payload.state === "done") return payload.results || [];
    if (payload.state === "error") {
      throw new Error((payload.error && payload.error.message) || "검색에 실패했습니다.");
    }
    await sleep(SEARCH_POLL_MS);
  }
}

async function doSearch() {
  clearError();
  var query = el.query.value.trim();
  if (!query) {
    showError("검색어를 입력하세요.");
    return;
  }
  state.query = query;
  state.busy = true;
  updateBusyUi();
  progressStart("가사를 찾는 중…");
  try {
    var jobId = await startSearchJob(query, el.source.value);
    var results = await waitForSearchJob(jobId);
    progressFinish(results.length ? "검색 완료 — " + results.length + "건" : "검색 완료 — 결과 없음");
    state.results = results;
    state.selectedIndex = -1;
    state.selectedLines = [];
    state.previewPairs = null;
    state.page = 0;
    renderResults();
    if (state.results.length) {
      selectResult(state.results[0].index);
    } else {
      el.previewPanel.hidden = true;
      showError("검색 결과가 없습니다. 직접 입력을 사용해 보세요.");
    }
  } catch (err) {
    progressFail();
    showError(err.message || "검색에 실패했습니다.");
  } finally {
    state.busy = false;
    updateBusyUi();
  }
}

/* ----------------------------------------------------------------- preview */

// 직접 입력 모드: 서버 단일 소스(/api/preview)로 정규화 + 페어링을 받는다.
// 클라이언트에서 규칙을 재구현하지 않으므로 프리뷰와 실제 결과가 항상 일치한다.
async function refreshManualPreview() {
  if (state.mode !== "manual") return true;
  var requestId = previewRequestId + 1;
  previewRequestId = requestId;
  try {
    var response = await apiFetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_text: el.rawText.value,
        dedupe_chorus: el.dedupe.checked,
        slide_index: state.slideIndex,
      }),
    });
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      throw new Error((payload.error && payload.error.message) || "미리보기를 만들지 못했습니다.");
    }
    if (requestId !== previewRequestId) return true; // 늦게 도착한 이전 응답 무시
    state.selectedLines = payload.lines || [];
    state.previewPairs = payload.pairs || [];
    state.page = 0;
    renderPreview();
    return true;
  } catch (err) {
    if (requestId === previewRequestId) showError(err.message || "미리보기를 만들지 못했습니다.");
    return false;
  }
}

function scheduleManualPreview() {
  if (state.mode !== "manual") return;
  if (previewTimer !== null) clearTimeout(previewTimer);
  previewTimer = setTimeout(function () {
    previewTimer = null;
    refreshManualPreview();
  }, PREVIEW_DEBOUNCE_MS);
}

function currentPairs() {
  // 직접 입력 모드: 서버가 계산한 페어링을 그대로 사용(M1).
  if (state.mode === "manual") return state.previewPairs || [];
  // 검색 모드: 서버가 보낸 부분 미리보기 줄을 클라이언트에서 2줄로 묶는다.
  return splitPairs(state.selectedLines, el.dedupe.checked);
}

function renderPreview() {
  var pairs = currentPairs();
  // 미리보기 패널은 항상 보이게 두고, 내용이 없으면 안내 문구로 대신한다.
  // (빈 오른쪽 칸이 "고장난 화면"처럼 보이는 것을 막는다.)
  el.previewPanel.hidden = false;
  if (!state.selectedLines.length || !pairs.length) {
    el.preview.textContent = "";
    el.previewEmpty.hidden = false;
    el.previewSummary.textContent = "";
    el.pageLabel.textContent = "";
    el.prevPage.disabled = true;
    el.nextPage.disabled = true;
    return;
  }
  el.previewEmpty.hidden = true;

  var slideCount = pairs.length;
  el.previewSummary.textContent =
    "가사 " + state.selectedLines.length + "줄 → 슬라이드 " + slideCount +
    "장 (두 줄이 한 장, 배경 " + backgroundLabel() + ")";

  var pageCount = Math.max(1, Math.ceil(slideCount / PAGE_SIZE));
  if (state.page >= pageCount) state.page = pageCount - 1;
  var start = state.page * PAGE_SIZE;
  var slice = pairs.slice(start, start + PAGE_SIZE);
  el.pageLabel.textContent = (state.page + 1) + " / " + pageCount + "쪽";

  el.preview.textContent = "";
  slice.forEach(function (pair, offset) {
    // 실제 슬라이드처럼 보이게 두 줄을 각각 한 줄로 그린다(합쳐서 "/" 로
    // 붙이면 두 줄이 어디서 잘리는지 눈으로 확인할 수 없다).
    var li = document.createElement("li");
    li.className = "preview-item";
    var number = document.createElement("span");
    number.className = "slide-number";
    number.textContent = String(start + offset + 1);
    var body = document.createElement("span");
    body.className = "slide-body";
    pair.forEach(function (line) {
      if (line === null || line === undefined) return;
      var span = document.createElement("span");
      span.className = "slide-line";
      span.textContent = line;
      body.appendChild(span);
    });
    li.appendChild(number);
    li.appendChild(body);
    el.preview.appendChild(li);
  });

  el.prevPage.disabled = state.page <= 0;
  el.nextPage.disabled = state.page >= pageCount - 1;
}

/* ---------------------------------------------------------------- generate */

async function doGenerate() {
  clearError();
  if (state.mode === "manual") {
    // 디바운스 전 클릭/입력에도 프리뷰(그리고 빈 입력 판정)를 서버 기준으로 최신화한다.
    if (previewTimer !== null) {
      clearTimeout(previewTimer);
      previewTimer = null;
    }
    var refreshed = await refreshManualPreview();
    if (!refreshed) return;
  }
  if (!state.selectedLines.length) {
    showError("생성할 가사 줄이 없습니다.");
    return;
  }

  var body = { dedupe_chorus: el.dedupe.checked, filename: el.filename.value.trim() || "가사.pptx", slide_index: state.slideIndex };
  if (state.mode === "manual") {
    body.raw_text = el.rawText.value;
  } else {
    body.query = state.query;
    body.source = el.source.value;
    body.index = state.selectedIndex;
  }

  state.busy = true;
  updateBusyUi();
  progressStart("PPTX 생성 중…");
  try {
    var response = await apiFetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      throw new Error((payload.error && payload.error.message) || "생성에 실패했습니다.");
    }
    // 서버가 실제로 저장한 이름을 쓴다(정리·확장자 규칙이 클라이언트와 다를 수 있다).
    var saved = decodeDispositionName(response.headers.get("Content-Disposition"));
    state.lastFile = saved || el.filename.value.trim() || "가사.pptx";
    var blob = await response.blob();
    var url = URL.createObjectURL(blob);
    el.downloadLink.href = url;
    el.downloadLink.download = state.lastFile;
    el.downloadSummary.textContent = state.lastFile + " · output 폴더에도 저장됨";
    el.downloadPanel.hidden = false;
    el.downloadLink.click();
    progressFinish("PPTX 생성 완료");
  } catch (err) {
    progressFail();
    showError(err.message || "생성에 실패했습니다.");
  } finally {
    state.busy = false;
    updateBusyUi();
  }
}

function decodeDispositionName(value) {
  var match = /filename\*=UTF-8''([^;]+)/i.exec(value || "");
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch (err) {
    return "";
  }
}

// "폴더 열기": 서버가 output 안의 그 파일만 열어 준다(경로 탈출 불가).
async function revealLastFile() {
  if (!state.lastFile) return;
  clearError();
  try {
    var response = await apiFetch("/api/reveal", {
      method: "POST",
      headers: pageHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ filename: state.lastFile }),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      throw new Error((payload.error && payload.error.message) || "폴더를 열지 못했습니다.");
    }
  } catch (err) {
    if (!serverGone) showError(err.message || "폴더를 열지 못했습니다.");
  }
}

/* ------------------------------------------------------------------- wiring */

function updateBusyUi() {
  var locked = state.busy || serverGone;
  el.searchButton.disabled = locked;
  el.generateButton.disabled = locked;
  el.shutdownButton.disabled = locked;
}

function setMode(mode) {
  state.mode = mode;
  el.searchMode.hidden = mode !== "search";
  el.manualMode.hidden = mode !== "manual";
  clearError();
  if (mode === "manual") {
    state.selectedIndex = -1;
    state.previewPairs = null;
    renderPreview();
    refreshManualPreview();
  }
}

el.modeInputs.forEach(function (input) {
  input.addEventListener("change", function () {
    if (input.checked) setMode(input.value);
  });
});

el.searchButton.addEventListener("click", doSearch);
el.query.addEventListener("keydown", function (event) {
  if (event.key === "Enter") doSearch();
});

el.prevPage.addEventListener("click", function () {
  if (state.page > 0) { state.page -= 1; renderPreview(); }
});
el.nextPage.addEventListener("click", function () {
  state.page += 1;
  renderPreview();
});
el.dedupe.addEventListener("change", function () {
  if (state.mode === "manual") {
    refreshManualPreview();
    return;
  }
  renderPreview();
});

el.generateButton.addEventListener("click", doGenerate);
el.rawText.addEventListener("input", function () {
  scheduleManualPreview();
});
el.filename.addEventListener("keydown", function (event) {
  if (event.key === "Enter") doGenerate();
});

/* ----------------------------------------------------------- session wiring */

el.shutdownButton.addEventListener("click", shutdownServer);
el.keepButton.addEventListener("click", keepSessionAlive);
el.revealButton.addEventListener("click", revealLastFile);

// 탭이 닫힐 때만 "닫는 중"을 알린다. 다시 보이거나 되살아나면(pageshow) 곧바로
// heartbeat 를 보내 서버가 정리 결정을 취소하게 한다(새로고침·뒤로가기 포함).
window.addEventListener("pagehide", reportTabClosing);
window.addEventListener("pageshow", function () { refreshServerState(); });
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") refreshServerState();
});

setMode("search");
renderPreview();
loadSlides();
refreshServerState();
statusTimer = setInterval(function () {
  if (document.visibilityState === "visible") refreshServerState();
}, STATUS_POLL_MS);
