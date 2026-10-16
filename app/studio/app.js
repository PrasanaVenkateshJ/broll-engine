// app.js - starts the Studio and connects the parts: top bar (ready pill, token, theme),
// the Create panel, the job progress view, the result view and the "Recent jobs" list.

import { friendlyMessage, getToken, pollJob, request, setToken } from "./api.js";
import { applyConfig, initCreate } from "./create.js";
import { $, ICONS, ago, clock, el, hideError, icon, reducedMotion, secs, showError } from "./dom.js";
import { currentJobId, hideResult, showResult } from "./result.js";

const LAST_JOB_KEY = "broll.lastJob";
const THEME_KEY = "broll.theme";

// Stepper: the 9 steps the user sees, and which pipeline stage names (job.stage) belong to each.
const STEPS = [
  { label: "Intake", stages: ["queued", "starting", "intake", "load"] },
  { label: "Voice", stages: ["tts"] },
  { label: "Transcribe", stages: ["transcribe"] },
  { label: "Scenes", stages: ["scenes"] },
  { label: "Understand", stages: ["understand"] },
  { label: "Footage", stages: ["footage"] },
  { label: "Render", stages: ["render"] },
  { label: "QA", stages: [] },          // QA runs at the end of "render" (progress 90+)
  { label: "Upload", stages: ["upload", "cache"] },
];
const QA_STEP = 7;

let watch = null;       // AbortController of the job being watched
let elapsedTimer = null;

// ---------- Theme ----------

function storedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch { return null; }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
}

function initTheme() {
  const system = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  applyTheme(storedTheme() || system);
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* not saved: fine */ }
  });
}

// ---------- Banner (problems that affect the whole page: token, network) ----------

// kind "info" = a calm hint (no token yet), otherwise an error colour.
function banner(text, kind = "error") {
  const box = $("banner");
  box.textContent = text || "";
  box.classList.toggle("is-info", kind === "info");
  box.setAttribute("role", kind === "info" ? "status" : "alert");
  box.hidden = !text;
}

function handleGlobalError(err) {
  if (err.code === "UNAUTHORIZED" || err.code === "NETWORK" || err.code === "RATE_LIMITED") banner(friendlyMessage(err));
}

// ---------- Ready pill (GET /ready, no token needed) ----------

async function checkReady() {
  const pill = $("ready-pill");
  try {
    const resp = await fetch("/ready", { cache: "no-store" });
    const ok = resp.ok;
    pill.className = `pill ${ok ? "pill-ok" : "pill-bad"}`;
    pill.textContent = ok ? "Ready" : "Not ready";
    pill.title = ok ? "Database and storage answer" : "The database or storage is not reachable";
  } catch {
    pill.className = "pill pill-bad";
    pill.textContent = "Offline";
    pill.title = "The API cannot be reached";
  }
}

// ---------- Token + settings ----------

function initToken() {
  const input = $("token");
  input.value = getToken();
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { setToken(input.value.trim()); reloadForToken(); }, 500);
  });
}

// No token yet: ask for one instead of calling the API (a 401 would only add noise).
// If the server runs with auth off (API_TOKENS empty), any value works.
async function reloadForToken() {
  if (!getToken()) {
    banner("Paste your API token in the top bar to start. It stays in this browser.", "info");
    renderRecent([]);
    return [];
  }
  banner("");
  await loadConfig();
  return loadRecent();
}

async function loadConfig() {
  try {
    applyConfig(await request("/api/v1/config/public"));
  } catch (err) {
    handleGlobalError(err);
  }
}

// ---------- Recent jobs ----------

const INPUT_NAMES = { script: "Script", scenes: "Scene list", upload: "Uploaded voiceover", audio_url: "Audio URL", swap: "Clip swap" };

async function loadRecent() {
  try {
    const { jobs } = await request("/api/v1/jobs?limit=20");
    renderRecent(jobs);
    return jobs;
  } catch (err) {
    renderRecent([]);
    handleGlobalError(err);
    return [];
  }
}

function statusPill(status) {
  const cls = { queued: "pill-wait", running: "pill-run", succeeded: "pill-ok", failed: "pill-bad" }[status] || "";
  return el("span", { class: `pill ${cls}`, text: status });
}

function renderRecent(jobs) {
  const list = $("recent-list");
  $("recent-empty").hidden = jobs.length > 0;
  const open = currentJobId() || watch?.jobId;
  list.replaceChildren(...jobs.map((j) => el("li", {},
    el("button", {
      type: "button", "data-job": j.job_id, class: `recent-item${j.parent_job_id ? " is-swap" : ""}`, "aria-current": j.job_id === open ? "true" : null,
      "aria-label": `${INPUT_NAMES[j.input_type] || j.input_type}, ${j.status}, ${ago(j.created_at)}`,
      onclick: () => openJob(j.job_id),
    },
    el("span", { class: "recent-title", text: INPUT_NAMES[j.input_type] || j.input_type }),
    statusPill(j.status),
    el("span", { class: "recent-sub", text: [ago(j.created_at), j.duration_seconds != null ? secs(j.duration_seconds) : null,
      j.error_code, j.job_id.slice(0, 8)].filter(Boolean).join(" · ") }),
    ))));
}

// ---------- Job progress ----------

function renderStepper() {
  $("stepper").replaceChildren(...STEPS.map((s, i) => el("li", { class: "step", "data-step": i },
    el("span", { class: "step-dot", text: String(i + 1) }), el("span", { text: s.label }))));
}

function stepIndex(job) {
  if (job.status === "succeeded") return STEPS.length;
  if (job.stage === "render" && job.progress >= 90) return QA_STEP;
  const i = STEPS.findIndex((s) => s.stages.includes(job.stage));
  return i < 0 ? 0 : i;
}

function updateStepper(job, lastActive) {
  const active = job.status === "failed" ? lastActive : stepIndex(job);
  const skipped = new Set();
  if (["upload", "audio_url"].includes(job.input_type)) skipped.add(1);  // no TTS for a real voiceover
  if (job.input_type === "swap") [1, 2, 3, 4].forEach((i) => skipped.add(i)); // a swap reuses all of these
  for (const node of $("stepper").children) {
    const i = Number(node.dataset.step);
    const dot = node.firstElementChild;
    node.className = "step";
    node.removeAttribute("aria-current");
    if (skipped.has(i) && i !== active) {
      node.classList.add("is-skipped");
      dot.replaceChildren(String(i + 1));
    } else if (i < active || job.status === "succeeded") {
      node.classList.add("is-done");
      dot.replaceChildren(icon(ICONS.check));
    } else if (i === active && job.status === "failed") {
      node.classList.add("is-failed");
      dot.replaceChildren(icon(ICONS.x));
    } else if (i === active && job.status !== "queued") {
      node.classList.add("is-active");
      node.setAttribute("aria-current", "step");
      dot.replaceChildren(String(i + 1));
    } else {
      dot.replaceChildren(String(i + 1));
    }
  }
  return active;
}

function updateJobView(job, lastActive) {
  const title = { queued: "Waiting in the queue", running: "Rendering", succeeded: "Done", failed: "The job failed" }[job.status];
  $("job-title").textContent = title;
  $("job-id").textContent = `Job ${job.job_id}`;
  const pill = $("job-status");
  pill.className = statusPill(job.status).className;
  pill.textContent = job.status === "running" ? `${job.stage}` : job.status;
  const bar = $("progress");
  bar.setAttribute("aria-valuenow", String(job.progress));
  bar.setAttribute("aria-valuetext", `${job.progress}% - ${job.stage}`);
  bar.classList.toggle("is-failed", job.status === "failed");
  $("progress-fill").style.width = `${job.progress}%`;
  const warnings = job.warnings || [];
  $("job-warnings").hidden = !warnings.length;
  $("warning-count").textContent = String(warnings.length);
  $("warning-list").replaceChildren(...warnings.map((w) => el("li", { text: w })));
  if (job.status === "failed" && job.error) showError($("job-error"), job.error, job.error.message);
  else hideError($("job-error"));
  startElapsed(job);
  return updateStepper(job, lastActive);
}

function startElapsed(job) {
  clearInterval(elapsedTimer);
  const start = Date.parse(job.created_at);
  const end = job.finished_at ? Date.parse(job.finished_at) : null;
  const tick = () => { $("job-elapsed").textContent = clock(((end || Date.now()) - start) / 1000); };
  tick();
  if (!end) elapsedTimer = setInterval(tick, 1000);
}

// Open any job: watch it while it runs, then show its result (or its error).
// quiet: opened by itself on page load; a job that no longer exists just closes again.
export async function openJob(jobId, { ratio, quiet = false } = {}) {
  watch?.abort();
  const controller = new AbortController();
  controller.jobId = jobId;
  watch = controller;
  try { localStorage.setItem(LAST_JOB_KEY, jobId); } catch { /* fine */ }
  hideResult();
  hideError($("job-error"));
  $("job-note").textContent = "";
  renderStepper();
  $("job").hidden = false;
  markRecent(jobId);
  let lastActive = 0;
  try {
    const job = await pollJob(jobId, {
      signal: controller.signal,
      onUpdate: (j) => { lastActive = updateJobView(j, lastActive); },
      onNote: (text) => { $("job-note").textContent = text; },
    });
    if (watch !== controller) return;
    clearInterval(elapsedTimer);
    loadRecent();
    if (job.status === "succeeded") {
      try {
        await showResult(job, { ratio, onSwapped: () => loadRecent() });
        $("job").hidden = true;
        $("result").scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
      } catch (err) {
        showError($("job-error"), err, `The job finished, but its files could not be loaded. ${friendlyMessage(err)}`);
      }
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    clearInterval(elapsedTimer);
    if (quiet) {
      $("job").hidden = true;
      if (err.code === "JOB_NOT_FOUND") { try { localStorage.removeItem(LAST_JOB_KEY); } catch { /* fine */ } }
      return;
    }
    $("job-title").textContent = "Could not load the job";
    showError($("job-error"), err, friendlyMessage(err));
    handleGlobalError(err);
  }
}

function markRecent(jobId) {
  for (const item of document.querySelectorAll(".recent-item")) {
    if (item.dataset.job === jobId) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  }
}

// ---------- Start ----------

function start() {
  initTheme();
  initToken();
  initCreate({
    onJobCreated: (jobId, { ratios }) => {
      loadRecent();
      openJob(jobId, { ratio: ratios[0] });
      $("job").scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    },
  });
  $("recent-refresh").addEventListener("click", loadRecent);
  checkReady();
  setInterval(checkReady, 30000);
  // Re-open the job that was open last time, if it is still one of this token's jobs.
  reloadForToken().then((jobs) => {
    let last = null;
    try { last = localStorage.getItem(LAST_JOB_KEY); } catch { /* fine */ }
    if (last && jobs.some((j) => j.job_id === last)) openJob(last, { quiet: true });
  });
}

start();
