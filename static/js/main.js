import * as pdfjsLib from "/static/vendor/pdfjs/pdf.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.mjs";

const state = {
  mode: null, // server | picker
  currentPdfPath: null,
  currentJsonPath: null,
  currentCuratedPath: null,
  pdfDoc: null,
  pdfBytes: null,
  currentPage: 1,
  totalPages: 0,
  zoom: 1.0,
  rawObj: null,
  curatedObj: null,
  curatedValid: false,
  issues: [],
  baseFolder: "",
};

const el = {
  docSelect: document.getElementById("doc-select"),
  refreshDocsBtn: document.getElementById("refresh-docs"),
  loadServerBtn: document.getElementById("load-server-doc"),
  loadPickedBtn: document.getElementById("load-picked-files"),
  pdfFileInput: document.getElementById("pdf-file"),
  jsonFileInput: document.getElementById("json-file"),
  pickerOutputDirInput: document.getElementById("picker-output-dir"),
  pickerOutputFileInput: document.getElementById("picker-output-file"),
  status: document.getElementById("status-message"),
  rawJsonView: document.getElementById("raw-json-view"),
  curatedEditor: document.getElementById("curated-editor"),
  jsonError: document.getElementById("json-error"),
  saveBtn: document.getElementById("save-curated"),
  threshold: document.getElementById("threshold-select"),
  emptyPages: document.getElementById("empty-pages-checkbox"),
  pageAvgInput: document.getElementById("page-avg-input"),
  issueSearch: document.getElementById("issue-search"),
  issuesBody: document.getElementById("issues-body"),
  canvas: document.getElementById("pdf-canvas"),
  pageInput: document.getElementById("page-input"),
  pageCount: document.getElementById("page-count"),
  prevPageBtn: document.getElementById("prev-page"),
  nextPageBtn: document.getElementById("next-page"),
  zoomInBtn: document.getElementById("zoom-in"),
  zoomOutBtn: document.getElementById("zoom-out"),
  fitWidthBtn: document.getElementById("fit-width"),
  pdfScroll: document.getElementById("pdf-scroll"),
  curPageSelect: document.getElementById("cur-page-select"),
  curPageKind: document.getElementById("cur-page-kind"),
  curPageStatus: document.getElementById("cur-page-status"),
  curPageText: document.getElementById("cur-page-text"),
};

function setStatus(message, type = "neutral") {
  el.status.classList.remove("ok", "error", "neutral");
  el.status.classList.add(type);
  el.status.textContent = message;
}

function parseJsonError(err) {
  const msg = String(err?.message || err);
  const m = msg.match(/line\s+(\d+)\s+column\s+(\d+)/i);
  if (m) {
    return `JSON parse error at line ${m[1]}, column ${m[2]}: ${msg}`;
  }
  return `JSON parse error: ${msg}`;
}

function getAnalysisPages(rawObj) {
  if (!rawObj || typeof rawObj !== "object") return [];
  if (Array.isArray(rawObj.pages)) return rawObj.pages;
  if (rawObj.document && typeof rawObj.document === "object" && Array.isArray(rawObj.document.pages)) {
    return rawObj.document.pages;
  }
  if (Array.isArray(rawObj.contents)) {
    const seen = new Set();
    const merged = [];
    rawObj.contents.forEach((entry) => {
      if (!entry || typeof entry !== "object" || !Array.isArray(entry.pages)) return;
      entry.pages.forEach((page) => {
        if (!page || typeof page !== "object") return;
        const num = page.pageNumber;
        const key = Number.isInteger(num) ? num : -1;
        if (key >= 0 && seen.has(key)) return;
        if (key >= 0) seen.add(key);
        merged.push(page);
      });
    });
    merged.sort((a, b) => Number(a.pageNumber) - Number(b.pageNumber));
    return merged;
  }
  return [];
}

function validateContentUnderstanding(rawObj) {
  if (!rawObj || typeof rawObj !== "object") {
    throw new Error("Top-level JSON must be an object.");
  }
  const pages = getAnalysisPages(rawObj);
  if (pages.length === 0) {
    throw new Error(
      "Expected a Content Understanding document result with a pages array (top-level 'pages', " +
        "inside the 'document' object, or inside 'contents[].pages'). Top-level keys found: " +
        (Object.keys(rawObj).join(", ") || "(none)") +
        ".",
    );
  }

  for (let i = 0; i < pages.length; i += 1) {
    const p = pages[i];
    if (!p || typeof p !== "object") throw new Error(`pages[${i}] must be an object.`);
    if (!Object.prototype.hasOwnProperty.call(p, "pageNumber")) throw new Error(`pages[${i}] missing pageNumber.`);
    if (!Array.isArray(p.words)) throw new Error(`pages[${i}].words must be an array.`);
    for (let j = 0; j < p.words.length; j += 1) {
      const w = p.words[j];
      if (!w || typeof w !== "object") throw new Error(`pages[${i}].words[${j}] must be an object.`);
      if (!Object.prototype.hasOwnProperty.call(w, "content")) throw new Error(`pages[${i}].words[${j}] missing content.`);
      if (!Object.prototype.hasOwnProperty.call(w, "confidence")) throw new Error(`pages[${i}].words[${j}] missing confidence.`);
    }
  }
}

function buildCuratedFromRaw(rawObj, pdfName, sourceJsonName) {
  const pages = getAnalysisPages(rawObj);
  return {
    schema: "curated/1",
    document: {
      fileName: pdfName || "unknown.pdf",
      sourceJson: sourceJsonName || "unknown.json",
      updatedAt: new Date().toISOString(),
    },
    pages: pages.map((page, idx) => {
      const words = Array.isArray(page.words) ? page.words : [];
      const normalizedWords = words.map((w) => {
        const text = String(w.content ?? "");
        const conf = Number(w.confidence);
        return {
          text,
          original: text,
          confidence: Number.isFinite(conf) ? conf : null,
          status: "pending",
          note: "",
        };
      });
      return {
        pageNumber: Number(page.pageNumber ?? idx + 1),
        kind: normalizedWords.length === 0 ? "blank" : "unknown",
        text: normalizedWords.map((w) => w.text).join(" ").trim(),
        status: "pending",
        words: normalizedWords,
        issues: [],
      };
    }),
  };
}

function syncEditorFromCurated() {
  if (!state.curatedObj) {
    el.curatedEditor.value = "";
    return;
  }
  el.curatedEditor.value = JSON.stringify(state.curatedObj, null, 2);
  validateCuratedEditor();
  rebuildPageEditorControls();
}

function syncCuratedFromEditor() {
  const txt = el.curatedEditor.value;
  state.curatedObj = JSON.parse(txt);
  rebuildPageEditorControls();
}

function validateCuratedEditor() {
  try {
    const parsed = JSON.parse(el.curatedEditor.value);
    if (!parsed || typeof parsed !== "object") {
      throw new Error("Curated JSON must be an object.");
    }
    if (!Array.isArray(parsed.pages)) {
      throw new Error("Curated JSON must contain 'pages' array.");
    }
    state.curatedValid = true;
    state.curatedObj = parsed;
    el.jsonError.textContent = "JSON valid.";
    el.jsonError.style.color = "#0a7a34";
    el.saveBtn.disabled = false;
  } catch (err) {
    state.curatedValid = false;
    el.jsonError.textContent = parseJsonError(err);
    el.jsonError.style.color = "#ad6800";
    el.saveBtn.disabled = true;
  }
}

function getNormalizedPages(rawObj) {
  const pages = getAnalysisPages(rawObj);
  return pages.map((page, idx) => {
    const pageNumber = Number(page.pageNumber ?? idx + 1);
    const words = Array.isArray(page.words)
      ? page.words.map((w) => {
          const text = String(w.content ?? "");
          const confidence = Number(w.confidence);
          return {
            text,
            confidence: Number.isFinite(confidence) ? confidence : null,
          };
        })
      : [];
    return { pageNumber, words };
  });
}

function clampPage(page) {
  return Math.max(1, Math.min(state.totalPages || 1, page));
}

async function renderCurrentPage() {
  if (!state.pdfDoc) return;
  const pageNum = clampPage(state.currentPage);
  state.currentPage = pageNum;
  el.pageInput.value = String(pageNum);

  const page = await state.pdfDoc.getPage(pageNum);
  const viewport = page.getViewport({ scale: state.zoom });
  const ctx = el.canvas.getContext("2d");

  el.canvas.width = Math.floor(viewport.width);
  el.canvas.height = Math.floor(viewport.height);

  await page.render({ canvasContext: ctx, viewport }).promise;
}

async function jumpToPage(pageNum) {
  state.currentPage = clampPage(pageNum);
  await renderCurrentPage();
}

async function loadPdfFromUrl(url) {
  const task = pdfjsLib.getDocument({ url });
  state.pdfDoc = await task.promise;
  state.totalPages = state.pdfDoc.numPages;
  el.pageCount.textContent = `/ ${state.totalPages}`;
  state.currentPage = 1;
  await renderCurrentPage();
}

async function loadPdfFromBytes(arrayBuffer) {
  const data = new Uint8Array(arrayBuffer);
  const task = pdfjsLib.getDocument({ data });
  state.pdfDoc = await task.promise;
  state.totalPages = state.pdfDoc.numPages;
  el.pageCount.textContent = `/ ${state.totalPages}`;
  state.currentPage = 1;
  await renderCurrentPage();
}

function recomputeIssues() {
  if (!state.rawObj) {
    state.issues = [];
    renderIssues();
    return;
  }

  const threshold = Number(el.threshold.value || 0.8);
  const flagEmpty = el.emptyPages.checked;
  const pageAvgThreshold = Number(el.pageAvgInput.value || 0.8);
  const searchText = (el.issueSearch.value || "").toLowerCase().trim();

  const pages = getNormalizedPages(state.rawObj);
  const allIssues = [];

  pages.forEach((page) => {
    const words = page.words;

    if (flagEmpty && words.length === 0) {
      allIssues.push({
        page: page.pageNumber,
        snippet: "(no extracted words)",
        confidence: null,
        reason: "empty-page",
        type: "empty-page",
      });
    }

    words.forEach((word) => {
      if (word.confidence !== null && word.confidence < threshold) {
        allIssues.push({
          page: page.pageNumber,
          snippet: word.text || "(empty text)",
          confidence: word.confidence,
          reason: "low-confidence",
          type: "low-confidence",
        });
      }
    });

    if (words.length > 0) {
      const numericConf = words.map((w) => w.confidence).filter((v) => Number.isFinite(v));
      if (numericConf.length > 0) {
        const avg = numericConf.reduce((a, b) => a + b, 0) / numericConf.length;
        if (avg < pageAvgThreshold) {
          allIssues.push({
            page: page.pageNumber,
            snippet: `Page average confidence ${avg.toFixed(3)}`,
            confidence: avg,
            reason: "low-page-average",
            type: "low-page-average",
          });
        }
      }
    }
  });

  let issues = allIssues;
  if (searchText) {
    issues = issues.filter((i) => i.snippet.toLowerCase().includes(searchText));
  }

  state.issues = issues;
  renderIssues();
  syncIssuesIntoCurated(allIssues);
}

function syncIssuesIntoCurated(allIssues) {
  if (!state.curatedObj || !Array.isArray(state.curatedObj.pages)) return;
  const grouped = new Map();
  allIssues.forEach((issue) => {
    const arr = grouped.get(issue.page) || [];
    arr.push({
      type: issue.type,
      text: issue.snippet,
      confidence: issue.confidence,
    });
    grouped.set(issue.page, arr);
  });

  state.curatedObj.pages.forEach((page) => {
    page.issues = grouped.get(Number(page.pageNumber)) || [];
  });

  const currentSelection = el.curPageSelect.value;
  el.curatedEditor.value = JSON.stringify(state.curatedObj, null, 2);
  validateCuratedEditor();
  if (currentSelection) {
    el.curPageSelect.value = currentSelection;
    syncPageEditorFromSelection();
  }
}

function renderIssues() {
  el.issuesBody.innerHTML = "";
  for (const issue of state.issues) {
    const tr = document.createElement("tr");
    tr.className = "issue-row";
    tr.innerHTML = `
      <td>${issue.page}</td>
      <td>${escapeHtml(issue.snippet)}</td>
      <td>${issue.confidence == null ? "-" : Number(issue.confidence).toFixed(3)}</td>
      <td>${issue.reason}</td>
    `;
    tr.addEventListener("click", async () => {
      await jumpToPage(Number(issue.page));
      setStatus(`Jumped to page ${issue.page} from issue list.`, "ok");
      const pageStr = String(issue.page);
      if (el.curPageSelect.querySelector(`option[value="${pageStr}"]`)) {
        el.curPageSelect.value = pageStr;
        syncPageEditorFromSelection();
      }
    });
    el.issuesBody.appendChild(tr);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function rebuildPageEditorControls() {
  const pages = Array.isArray(state.curatedObj?.pages) ? state.curatedObj.pages : [];
  const previous = el.curPageSelect.value;
  el.curPageSelect.innerHTML = "";

  pages.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = String(p.pageNumber);
    opt.textContent = `Page ${p.pageNumber}`;
    el.curPageSelect.appendChild(opt);
  });

  if (pages.length === 0) {
    el.curPageText.value = "";
    return;
  }

  const hasPrevious = previous && pages.some((p) => String(p.pageNumber) === previous);
  el.curPageSelect.value = hasPrevious ? previous : String(pages[0].pageNumber);
  syncPageEditorFromSelection();
}

function getSelectedCuratedPage() {
  const selected = Number(el.curPageSelect.value);
  if (!Number.isFinite(selected)) return null;
  return (state.curatedObj?.pages || []).find((p) => Number(p.pageNumber) === selected) || null;
}

function syncPageEditorFromSelection() {
  const p = getSelectedCuratedPage();
  if (!p) return;
  el.curPageKind.value = p.kind || "unknown";
  el.curPageStatus.value = p.status || "pending";
  el.curPageText.value = p.text || "";
}

function applyPageEditorToCurated() {
  const p = getSelectedCuratedPage();
  if (!p) return;
  p.kind = el.curPageKind.value;
  p.status = el.curPageStatus.value;
  p.text = el.curPageText.value;
  if (!state.curatedObj.document) state.curatedObj.document = {};
  state.curatedObj.document.updatedAt = new Date().toISOString();

  el.curatedEditor.value = JSON.stringify(state.curatedObj, null, 2);
  validateCuratedEditor();
}

async function loadServerDocumentsList() {
  const resp = await fetch("/api/list-documents");
  const data = await resp.json();
  el.docSelect.innerHTML = "";

  if (!Array.isArray(data.documents) || data.documents.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No PDF + sibling JSON pairs found";
    el.docSelect.appendChild(opt);
    setStatus("No PDF + sibling JSON pairs were found in the server folder.", "neutral");
    return;
  }

  data.documents.forEach((doc) => {
    const opt = document.createElement("option");
    opt.value = JSON.stringify(doc);
    opt.textContent = `${doc.label}  |  JSON: ${doc.jsonPath}${doc.curatedExists ? "  | curated exists" : ""}`;
    el.docSelect.appendChild(opt);
  });

  setStatus(`Found ${data.documents.length} loadable PDF + JSON pair(s).`, "ok");
}

async function loadFromServerSelection() {
  if (!el.docSelect.value) {
    setStatus("No server document selected.", "error");
    return;
  }
  try {
    const selected = JSON.parse(el.docSelect.value);
    const resp = await fetch("/api/load-server-document", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdfPath: selected.pdfPath, jsonPath: selected.jsonPath }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || `HTTP ${resp.status}`);
    }

    validateContentUnderstanding(data.rawJson);

    state.mode = "server";
    state.currentPdfPath = selected.pdfPath;
    state.currentJsonPath = selected.jsonPath;
    state.currentCuratedPath = data.curatedPath;
    state.rawObj = data.rawJson;
    state.curatedObj = data.curatedJson;

    el.rawJsonView.textContent = JSON.stringify(state.rawObj, null, 2);
    syncEditorFromCurated();
    await loadPdfFromUrl(data.pdfUrl);
    recomputeIssues();

    setStatus(`Loaded from server folder: ${selected.pdfPath} and ${selected.jsonPath}`, "ok");
  } catch (err) {
    setStatus(`Load failed (server mode): ${err.message || err}`, "error");
  }
}

async function loadFromPickedFiles() {
  const pdfFile = el.pdfFileInput.files?.[0];
  const jsonFile = el.jsonFileInput.files?.[0];
  if (!pdfFile || !jsonFile) {
    setStatus("Picker load failed: choose both a PDF and a JSON file.", "error");
    return;
  }

  try {
    const [pdfArrayBuffer, jsonText] = await Promise.all([pdfFile.arrayBuffer(), jsonFile.text()]);
    const rawObj = JSON.parse(jsonText);
    validateContentUnderstanding(rawObj);

    state.mode = "picker";
    state.currentPdfPath = pdfFile.name;
    state.currentJsonPath = jsonFile.name;
    state.currentCuratedPath = null;
    state.rawObj = rawObj;
    state.curatedObj = buildCuratedFromRaw(rawObj, pdfFile.name, jsonFile.name);

    el.rawJsonView.textContent = JSON.stringify(state.rawObj, null, 2);
    syncEditorFromCurated();
    await loadPdfFromBytes(pdfArrayBuffer);
    recomputeIssues();

    setStatus(
      "Loaded from browser file picker. Save will use backend endpoint and write to chosen output folder (or server default folder).",
      "ok"
    );
  } catch (err) {
    setStatus(`Load failed (file-picker mode): ${err.message || err}`, "error");
  }
}

async function saveCurated() {
  if (!state.curatedValid) {
    setStatus("Save blocked: curated JSON is invalid.", "error");
    return;
  }

  try {
    const payload = {
      mode: state.mode,
      curatedJson: state.curatedObj,
      pdfPath: state.currentPdfPath,
      outputDir: el.pickerOutputDirInput.value.trim() || undefined,
      outputFile: el.pickerOutputFileInput.value.trim() || undefined,
    };

    const resp = await fetch("/api/save-curated", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || `HTTP ${resp.status}`);
    }

    setStatus(data.message, "ok");
  } catch (err) {
    setStatus(`Save failed: ${err.message || err}`, "error");
  }
}

function setupThresholdOptions() {
  el.threshold.innerHTML = "";
  for (let v = 0.5; v <= 0.950001; v += 0.05) {
    const value = v.toFixed(2);
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    if (Math.abs(v - 0.8) < 0.0001) {
      opt.selected = true;
    }
    el.threshold.appendChild(opt);
  }
}

function setupSplitters() {
  const panes = document.getElementById("panes");
  const leftPane = document.getElementById("left-pane");
  const middlePane = document.getElementById("middle-pane");
  const rightPane = document.getElementById("right-pane");

  function attach(splitter, left, right) {
    splitter.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const rect = panes.getBoundingClientRect();
      const leftRect = left.getBoundingClientRect();
      const rightRect = right.getBoundingClientRect();
      const startX = e.clientX;
      const startLeft = leftRect.width;
      const startRight = rightRect.width;

      const onMove = (me) => {
        const dx = me.clientX - startX;
        let newLeft = startLeft + dx;
        let newRight = startRight - dx;
        const min = 220;
        if (newLeft < min) {
          newRight -= min - newLeft;
          newLeft = min;
        }
        if (newRight < min) {
          newLeft -= min - newRight;
          newRight = min;
        }

        const leftPct = (newLeft / rect.width) * 100;
        const rightPct = (newRight / rect.width) * 100;
        left.style.flex = `0 0 ${leftPct}%`;
        right.style.flex = `0 0 ${rightPct}%`;
      };

      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });
  }

  attach(document.getElementById("splitter-1"), leftPane, middlePane);
  attach(document.getElementById("splitter-2"), middlePane, rightPane);
}

function hookEvents() {
  el.refreshDocsBtn.addEventListener("click", loadServerDocumentsList);
  el.loadServerBtn.addEventListener("click", loadFromServerSelection);
  el.loadPickedBtn.addEventListener("click", loadFromPickedFiles);

  el.threshold.addEventListener("change", recomputeIssues);
  el.emptyPages.addEventListener("change", recomputeIssues);
  el.pageAvgInput.addEventListener("input", recomputeIssues);
  el.issueSearch.addEventListener("input", recomputeIssues);

  el.curatedEditor.addEventListener("input", () => {
    validateCuratedEditor();
    if (state.curatedValid) {
      syncCuratedFromEditor();
      recomputeIssues();
    }
  });

  el.saveBtn.addEventListener("click", saveCurated);

  el.prevPageBtn.addEventListener("click", () => jumpToPage(state.currentPage - 1));
  el.nextPageBtn.addEventListener("click", () => jumpToPage(state.currentPage + 1));
  el.pageInput.addEventListener("change", () => jumpToPage(Number(el.pageInput.value || 1)));
  el.zoomInBtn.addEventListener("click", async () => {
    state.zoom = Math.min(4.0, state.zoom + 0.1);
    await renderCurrentPage();
  });
  el.zoomOutBtn.addEventListener("click", async () => {
    state.zoom = Math.max(0.2, state.zoom - 0.1);
    await renderCurrentPage();
  });

  el.fitWidthBtn.addEventListener("click", async () => {
    if (!state.pdfDoc) return;
    const page = await state.pdfDoc.getPage(state.currentPage);
    const baseViewport = page.getViewport({ scale: 1.0 });
    const targetWidth = el.pdfScroll.clientWidth - 24;
    state.zoom = Math.max(0.2, targetWidth / baseViewport.width);
    await renderCurrentPage();
  });

  el.curPageSelect.addEventListener("change", syncPageEditorFromSelection);
  el.curPageKind.addEventListener("change", applyPageEditorToCurated);
  el.curPageStatus.addEventListener("change", applyPageEditorToCurated);
  el.curPageText.addEventListener("input", applyPageEditorToCurated);
}

async function bootstrap() {
  setupThresholdOptions();
  setupSplitters();
  hookEvents();

  try {
    const cfgResp = await fetch("/api/config");
    const cfg = await cfgResp.json();
    state.baseFolder = cfg.baseFolder || "";
    el.pickerOutputDirInput.placeholder = `Default: ${state.baseFolder || "server folder"}`;
  } catch {
    // non-fatal
  }

  await loadServerDocumentsList();
  setStatus("Ready. Load a server pair or use Open files fallback.", "neutral");
}

bootstrap();
