// api.js - every call from the Studio to the engine's API goes through this file.
// It adds the bearer token, turns any failure into one ApiError shape, and polls jobs politely.

const TOKEN_KEY = "broll.token";

// ---------- Token (kept in this browser only; never logged) ----------

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

export function setToken(value) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* private mode: the token then lasts until the page closes */ }
}

// ---------- Errors ----------

// One shape for every failure: the API's JSON error {code, message, retryable, details},
// plus NETWORK (API not reachable) and the HTTP status / Retry-After seconds.
export class ApiError extends Error {
  constructor({ status = 0, code = "NETWORK", message = "", retryable = false, details = {}, retryAfter = 0 }) {
    super(message);
    Object.assign(this, { status, code, retryable, details, retryAfter });
  }
}

// Friendlier words for the codes a person can do something about. Other codes show the API's message.
const FRIENDLY = {
  NETWORK: "Cannot reach the API. Check that it is running, then try again.",
  UNAUTHORIZED: "The API token is missing or wrong. Enter a valid token in the top bar.",
  RATE_LIMITED: "Too many requests right now.",
  INTERNAL_ERROR: "The server hit an unexpected error. Trying again later may work.",
};

export function friendlyMessage(err) {
  if (err.code === "RATE_LIMITED" && err.retryAfter) return `Too many requests. Please wait ${err.retryAfter} s.`;
  return FRIENDLY[err.code] || err.message || "Something went wrong.";
}

async function toApiError(resp) {
  let body = null;
  try { body = await resp.json(); } catch { /* not JSON */ }
  const e = (body && body.error) || {};
  return new ApiError({
    status: resp.status,
    code: e.code || `HTTP_${resp.status}`,
    message: e.message || resp.statusText,
    retryable: !!e.retryable,
    details: e.details || {},
    retryAfter: parseInt(resp.headers.get("Retry-After") || "0", 10) || 0,
  });
}

// ---------- Requests ----------

export function newKey() {
  // Idempotency-Key: one per user action, so a double submit never makes two jobs.
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function request(path, { method = "GET", json, form, idempotencyKey, signal, auth = true } = {}) {
  const headers = {};
  const token = getToken();
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form; // the browser sets the multipart boundary itself
  let resp;
  try {
    resp = await fetch(path, { method, headers, body, signal, cache: "no-store" });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError({ code: "NETWORK", message: FRIENDLY.NETWORK, retryable: true });
  }
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

// Signed download links carry the API's own address (PUBLIC_BASE_URL). The signature only covers
// job id + file name + expiry, so we load them from the address this page came from (same origin).
export function sameOrigin(url) {
  try {
    const u = new URL(url, location.href);
    return location.origin + u.pathname + u.search;
  } catch { return url; }
}

export async function fetchJson(link) {
  const resp = await fetch(sameOrigin(link), { cache: "no-store" }).catch(() => null);
  if (!resp) throw new ApiError({ code: "NETWORK", message: FRIENDLY.NETWORK, retryable: true });
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

export async function fetchText(link) {
  const resp = await fetch(sameOrigin(link), { cache: "no-store" }).catch(() => null);
  if (!resp || !resp.ok) return "";
  return resp.text();
}

// ---------- Polling ----------

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("aborted", "AbortError")); }, { once: true });
});

// Polls GET /api/v1/jobs/{id} until it ends. onUpdate(job) after every answer, onNote(text) for
// "waiting" messages. The pause grows 1 s -> x1.4 -> 5 s max (10 s while the tab is hidden),
// a 429 waits Retry-After seconds, and a network error backs off up to 15 s and keeps trying.
// 401 / 404 stop at once (the token or the job is wrong; waiting will not fix it).
export async function pollJob(jobId, { onUpdate, onNote, signal } = {}) {
  let pause = 1000;
  let netPause = 2000;
  for (;;) {
    try {
      const job = await request(`/api/v1/jobs/${encodeURIComponent(jobId)}`, { signal });
      onNote?.("");
      onUpdate?.(job);
      netPause = 2000;
      if (job.status === "succeeded" || job.status === "failed") return job;
      await sleep(document.hidden ? 10000 : pause, signal);
      pause = Math.min(pause * 1.4, 5000);
    } catch (err) {
      if (err.name === "AbortError") throw err;
      if (err.code === "RATE_LIMITED") {
        const wait = Math.max(1, err.retryAfter || 5);
        onNote?.(`Rate limited by the API: checking again in ${wait} s.`);
        await sleep(wait * 1000, signal);
      } else if (err.code === "NETWORK" || err.status >= 500) {
        onNote?.(`Connection problem: retrying in ${Math.round(netPause / 1000)} s.`);
        await sleep(netPause, signal);
        netPause = Math.min(netPause * 2, 15000);
      } else {
        throw err;
      }
    }
  }
}
