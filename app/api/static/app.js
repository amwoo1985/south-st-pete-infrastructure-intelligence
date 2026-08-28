"use strict";

// Vanilla JS, no build step (DECISIONS #105). Every API-response field
// rendered into the DOM goes through textContent, never innerHTML — the
// corpus includes user-uploaded document content (POST /documents/upload)
// that can end up in a generated answer or a citation's section_label, so
// treat all of it as untrusted text, not markup.

const queryForm = document.getElementById("query-form");
const questionInput = document.getElementById("question");
const querySubmitBtn = document.getElementById("query-submit");
const queryStatusEl = document.getElementById("query-status");
const answerBlock = document.getElementById("answer-block");
const answerTextEl = document.getElementById("answer-text");
const notInCorpusBlock = document.getElementById("not-in-corpus-block");
const errorBlock = document.getElementById("error-block");
const errorTextEl = document.getElementById("error-text");
const citationsBlock = document.getElementById("citations-block");
const citationsList = document.getElementById("citations-list");

function hide(el) {
  el.hidden = true;
}

function show(el) {
  el.hidden = false;
}

function resetQueryResult() {
  hide(answerBlock);
  hide(notInCorpusBlock);
  hide(errorBlock);
  hide(citationsBlock);
  answerTextEl.textContent = "";
  errorTextEl.textContent = "";
  citationsList.textContent = "";
}

function formatDate(isoDateString) {
  if (!isoDateString) {
    return "date unknown";
  }
  const parsed = new Date(isoDateString);
  if (Number.isNaN(parsed.getTime())) {
    return "date unknown";
  }
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

function renderCitations(citations) {
  citationsList.textContent = "";
  if (!citations || citations.length === 0) {
    hide(citationsBlock);
    return;
  }
  for (const citation of citations) {
    const li = document.createElement("li");

    const link = document.createElement("a");
    link.href = citation.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = citation.source_url;
    li.appendChild(link);

    const meta = document.createElement("div");
    meta.className = "citation-meta";
    const parts = [citation.doc_type];
    if (citation.section_label) {
      parts.push(citation.section_label);
    }
    parts.push(formatDate(citation.published_date));
    meta.textContent = parts.join(" — ");
    li.appendChild(meta);

    citationsList.appendChild(li);
  }
  show(citationsBlock);
}

// FastAPI's own request-validation errors (a blank/oversized `question`)
// come back as {"detail": [{"loc": [...], "msg": "...", "type": "..."}]},
// distinct from this app's own HTTPException bodies, which are always
// {"detail": "<string>"} (see app/api/query.py's route — every raised
// HTTPException.detail there is a static string). Handle both shapes
// rather than assuming one.
function extractErrorMessage(body) {
  if (!body || typeof body.detail === "undefined") {
    return "An unexpected error occurred.";
  }
  if (typeof body.detail === "string") {
    return body.detail;
  }
  if (Array.isArray(body.detail)) {
    return body.detail
      .map((item) => (item && typeof item.msg === "string" ? item.msg : String(item)))
      .join(" ");
  }
  return "An unexpected error occurred.";
}

queryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetQueryResult();

  const question = questionInput.value;

  querySubmitBtn.disabled = true;
  queryStatusEl.textContent = "Asking… this can take a few seconds.";
  show(queryStatusEl);

  try {
    const response = await fetch("/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    let body;
    try {
      body = await response.json();
    } catch (parseErr) {
      body = null;
    }

    if (!response.ok) {
      errorTextEl.textContent = extractErrorMessage(body);
      show(errorBlock);
      return;
    }

    if (body.not_in_corpus) {
      show(notInCorpusBlock);
      renderCitations(body.citations);
      return;
    }

    answerTextEl.textContent = body.answer;
    show(answerBlock);
    renderCitations(body.citations);
  } catch (networkErr) {
    errorTextEl.textContent =
      "Could not reach the server. Check your connection and try again.";
    show(errorBlock);
  } finally {
    querySubmitBtn.disabled = false;
    hide(queryStatusEl);
  }
});

// --- Upload form (Phase I "if time allows" scope) ---

const uploadForm = document.getElementById("upload-form");
const uploadFileInput = document.getElementById("upload-file");
const uploadApiKeyInput = document.getElementById("upload-api-key");
const uploadSubmitBtn = document.getElementById("upload-submit");
const uploadStatusEl = document.getElementById("upload-status");

// DECISIONS #144: POST /documents/upload now requires the X-Upload-Api-Key
// header. Persisted to localStorage (this browser only, never sent
// anywhere but this page's own upload request below) purely so a repeat
// uploader doesn't have to re-paste the key on every visit — this is a
// solo-operator/small-coalition tool, not a shared multi-user page.
const UPLOAD_API_KEY_STORAGE_KEY = "uploadApiKey";
uploadApiKeyInput.value = window.localStorage.getItem(UPLOAD_API_KEY_STORAGE_KEY) || "";

function renderUploadStatus(body, isError) {
  uploadStatusEl.textContent = "";
  uploadStatusEl.classList.toggle("error", Boolean(isError));

  if (isError) {
    uploadStatusEl.textContent = extractErrorMessage(body);
    show(uploadStatusEl);
    return;
  }

  const lines = [`${body.original_filename}: ${body.status}`];
  if (body.status === "completed") {
    lines.push(`${body.chunk_count} chunk(s) indexed.`);
  } else {
    // No live status-push endpoint (directive: keep this simple, no
    // polling mechanism). Re-submitting the identical file is itself a
    // valid status check — POST /documents/upload is idempotent on
    // file_hash and short-circuits to the current stored status instead
    // of reprocessing.
    lines.push("Still processing — resubmit the same file to check status.");
    // api-review pre-commit finding (DECISIONS #114): a resubmit of a
    // file whose prior attempt failed used to look identical to a
    // brand-new "processing" response — the reason was captured server-
    // side but never reached this UI. Surface it now if present.
    if (body.previous_failure_reason) {
      lines.push(`Previous attempt failed: ${body.previous_failure_reason}`);
    }
  }
  uploadStatusEl.textContent = lines.join(" ");
  show(uploadStatusEl);
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const file = uploadFileInput.files[0];
  if (!file) {
    return;
  }

  uploadSubmitBtn.disabled = true;
  uploadStatusEl.classList.remove("error");
  uploadStatusEl.textContent = "Uploading…";
  show(uploadStatusEl);

  const uploadApiKey = uploadApiKeyInput.value;
  window.localStorage.setItem(UPLOAD_API_KEY_STORAGE_KEY, uploadApiKey);

  const formData = new FormData();
  formData.append("file", file);

  try {
    const response = await fetch("/documents/upload", {
      method: "POST",
      headers: { "X-Upload-Api-Key": uploadApiKey },
      body: formData,
    });

    let body;
    try {
      body = await response.json();
    } catch (parseErr) {
      body = null;
    }

    renderUploadStatus(body, !response.ok);
  } catch (networkErr) {
    renderUploadStatus(null, true);
  } finally {
    uploadSubmitBtn.disabled = false;
  }
});

// --- Crawler health panel (DECISIONS #147) ---
//
// GET /health/crawlers is the read this panel renders — replaces the old
// "checking means looking at the job-status tables directly" gap
// (README). Fetched once on page load, no polling — this is a
// solo-operator diagnostic view, not a live-updating dashboard.

const crawlerHealthStatusEl = document.getElementById("crawler-health-status");
const crawlerHealthListEl = document.getElementById("crawler-health-list");

const CRAWLER_STATUS_LABELS = {
  ok: "OK",
  failed: "FAILED",
  never_run: "NEVER RUN",
};

function formatDateTime(isoString) {
  if (!isoString) {
    return "never";
  }
  const parsed = new Date(isoString);
  if (Number.isNaN(parsed.getTime())) {
    return "unknown";
  }
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

// textContent only, same rule as renderCitations above — a source's
// error_message or a Granicus failure_reason can contain arbitrary text
// (an upstream exception message, an HTTP error body snippet), never
// treated as markup.
function renderCrawlerStatusItem(name, status, detailLines) {
  const li = document.createElement("li");
  li.className = "crawler-status-item";

  const nameEl = document.createElement("span");
  nameEl.className = "source-name";
  nameEl.textContent = name;
  li.appendChild(nameEl);

  const badge = document.createElement("span");
  badge.className = `status-badge status-${status}`;
  badge.textContent = CRAWLER_STATUS_LABELS[status] || status;
  li.appendChild(badge);

  if (detailLines && detailLines.length > 0) {
    const detail = document.createElement("div");
    detail.className = "status-detail";
    detail.textContent = detailLines.join(" — ");
    li.appendChild(detail);
  }

  return li;
}

function renderCrawlerHealth(body) {
  crawlerHealthListEl.textContent = "";

  for (const source of body.sources) {
    const detailLines = [];
    if (source.status === "never_run") {
      detailLines.push("no crawl on record yet");
    } else {
      detailLines.push(`last run: ${formatDateTime(source.last_started_at)}`);
      if (source.status === "failed" && source.error_message) {
        detailLines.push(source.error_message);
      } else {
        detailLines.push(`inserted=${source.inserted ?? 0} skipped=${source.skipped ?? 0}`);
      }
    }
    crawlerHealthListEl.appendChild(
      renderCrawlerStatusItem(source.source_name, source.status, detailLines)
    );
  }

  const g = body.granicus;
  const granicusDetail = [
    `${g.total_jobs} job(s) — pending=${g.pending} claimed=${g.claimed} ` +
      `completed=${g.completed} failed=${g.failed}`,
  ];
  if (g.status === "failed" && g.most_recent_failure_reason) {
    const label = g.most_recent_failure_meeting_title || g.most_recent_failure_mp3_url;
    granicusDetail.push(`most recent failure (${label}): ${g.most_recent_failure_reason}`);
  }
  crawlerHealthListEl.appendChild(
    renderCrawlerStatusItem("granicus (transcription pipeline)", g.status, granicusDetail)
  );
}

async function loadCrawlerHealth() {
  crawlerHealthStatusEl.textContent = "Checking crawler health…";
  show(crawlerHealthStatusEl);

  try {
    const response = await fetch("/health/crawlers");

    let body;
    try {
      body = await response.json();
    } catch (parseErr) {
      body = null;
    }

    if (!response.ok) {
      crawlerHealthStatusEl.textContent = extractErrorMessage(body);
      crawlerHealthStatusEl.classList.add("error");
      show(crawlerHealthStatusEl);
      return;
    }

    hide(crawlerHealthStatusEl);
    renderCrawlerHealth(body);
  } catch (networkErr) {
    crawlerHealthStatusEl.textContent = "Could not reach the server to check crawler health.";
    crawlerHealthStatusEl.classList.add("error");
    show(crawlerHealthStatusEl);
  }
}

loadCrawlerHealth();
