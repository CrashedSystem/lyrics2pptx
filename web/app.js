/*
 * 가사 2줄 PPTX 생성기 — 프런트엔드 로직 (vanilla JS, 프레임워크 없음).
 *
 * 상태 키: mode, query, results, selectedIndex, selectedLines, previewPairs,
 * busy, page.
 * 정규화(N1~N10)와 2줄 페어링은 서버 단일 소스가 계산한다: 직접 입력 미리보기는
 * POST /api/preview (pipeline.preview → text.normalize) 결과를 그대로 쓴다.
 * 클라이언트는 마커/정규화 규칙을 재구현하지 않는다 — 중복 구현이 프리뷰와 실제
 * 결과의 불일치를 낳았기 때문이다(리뷰 M1). 검색 모드에서는 서버가 보낸 부분
 * 미리보기 줄(preview_lines, 이미 정규화됨)만 이 스크립트에서 2줄로 묶는다.
 */
"use strict";

var PAGE_SIZE = 3; // 미리보기 페이지당 슬라이드 수
var PREVIEW_DEBOUNCE_MS = 250; // 직접 입력 미리보기 디바운스

var state = {
  mode: "search",
  query: "",
  results: [],
  selectedIndex: -1,
  selectedLines: [],
  previewPairs: null, // 직접 입력 모드에서 서버가 계산한 [[a,b],...]
  busy: false,
  page: 0,
};

// 직접 입력 미리보기 요청 순번: 늦게 도착한 이전 응답을 무시한다.
var previewRequestId = 0;
var previewTimer = null;

var el = {
  modeInputs: document.querySelectorAll('input[name="mode"]'),
  searchMode: document.getElementById("search-mode"),
  manualMode: document.getElementById("manual-mode"),
  query: document.getElementById("query"),
  source: document.getElementById("source"),
  searchButton: document.getElementById("search-button"),
  rawText: document.getElementById("raw-text"),
  resultsPanel: document.getElementById("results-panel"),
  results: document.getElementById("results"),
  previewPanel: document.getElementById("preview-panel"),
  previewSummary: document.getElementById("preview-summary"),
  preview: document.getElementById("preview"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageLabel: document.getElementById("page-label"),
  dedupe: document.getElementById("dedupe-chorus"),
  filename: document.getElementById("filename"),
  generateButton: document.getElementById("generate-button"),
  downloadPanel: document.getElementById("download-panel"),
  downloadLink: document.getElementById("download-link"),
  error: document.getElementById("error"),
  status: document.getElementById("status"),
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

function setStatus(message) {
  if (message) {
    el.status.textContent = message;
    el.status.hidden = false;
  } else {
    el.status.textContent = "";
    el.status.hidden = true;
  }
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
}

function selectResult(index) {
  var hit = state.results.find(function (item) { return item.index === index; });
  if (!hit) return;
  state.selectedIndex = index;
  // 검색 모드에서는 서버가 보낸 미리보기 줄만 알 수 있다(부분 미리보기).
  state.selectedLines = (hit.preview_lines || []).slice();
  state.previewPairs = null;
  state.page = 0;
  renderPreview();
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
  el.searchButton.disabled = true;
  setStatus("검색 중…");
  try {
    var url = "/api/search?q=" + encodeURIComponent(query) + "&source=" + encodeURIComponent(el.source.value);
    var response = await fetch(url);
    var payload = await response.json();
    if (!response.ok) {
      throw new Error((payload.error && payload.error.message) || "검색에 실패했습니다.");
    }
    state.results = payload.results || [];
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
    showError(err.message || "검색에 실패했습니다.");
  } finally {
    state.busy = false;
    el.searchButton.disabled = false;
    setStatus("");
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
    var response = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_text: el.rawText.value,
        dedupe_chorus: el.dedupe.checked,
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
  if (!state.selectedLines.length || !pairs.length) {
    el.previewPanel.hidden = true;
    return;
  }
  el.previewPanel.hidden = false;

  var slideCount = pairs.length;
  el.previewSummary.textContent = "줄 " + state.selectedLines.length + "개 → 슬라이드 " + slideCount + "장";
  el.pageLabel.textContent = "배경 순환: template 1 → 20 순서로 반복";

  var pageCount = Math.max(1, Math.ceil(slideCount / PAGE_SIZE));
  if (state.page >= pageCount) state.page = pageCount - 1;
  var start = state.page * PAGE_SIZE;
  var slice = pairs.slice(start, start + PAGE_SIZE);

  el.preview.textContent = "";
  slice.forEach(function (pair, offset) {
    var li = document.createElement("li");
    var number = document.createElement("span");
    number.className = "slide-number";
    number.textContent = String(start + offset + 1);
    var body = document.createElement("span");
    body.textContent = pair[1] === null ? pair[0] : pair[0] + " / " + pair[1];
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

  var body = { dedupe_chorus: el.dedupe.checked, filename: el.filename.value.trim() || "가사.pptx" };
  if (state.mode === "manual") {
    body.raw_text = el.rawText.value;
  } else {
    body.query = state.query;
    body.source = el.source.value;
    body.index = state.selectedIndex;
  }

  state.busy = true;
  el.generateButton.disabled = true;
  setStatus("PPTX 생성 중…");
  try {
    var response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      throw new Error((payload.error && payload.error.message) || "생성에 실패했습니다.");
    }
    var blob = await response.blob();
    var url = URL.createObjectURL(blob);
    el.downloadLink.href = url;
    el.downloadLink.download = el.filename.value.trim() || "가사.pptx";
    el.downloadPanel.hidden = false;
    el.downloadLink.click();
  } catch (err) {
    showError(err.message || "생성에 실패했습니다.");
  } finally {
    state.busy = false;
    el.generateButton.disabled = false;
    setStatus("");
  }
}

/* ------------------------------------------------------------------- wiring */

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

setMode("search");
renderPreview();
