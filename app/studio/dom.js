// dom.js - tiny helpers so the other files can build the page without a framework.
// All text goes in with textContent (never innerHTML), so text from the API can never run as code.

export const $ = (id) => document.getElementById(id);

// el("button", {class: "btn", text: "Go", onclick: fn}, child, child...)
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

// A small line icon from one SVG path ("d" attribute), hidden from screen readers.
export function icon(d) {
  return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d }));
}

export const ICONS = {
  check: "M5 12.5 10 17 19 7",
  x: "M6 6l12 12M18 6 6 18",
  download: "M12 4v11m0 0-4-4m4 4 4-4M5 19h14",
  swap: "M7 7h11l-3-3m3 13H7l3 3",
};

// 75.4 -> "1:15"
export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// 3.25 -> "3.3 s"
export const secs = (x) => `${Number(x || 0).toFixed(x >= 10 ? 0 : 1)} s`;

export function ago(iso) {
  const diff = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(diff)) return "";
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

// Show an API error in a box: the error code, a friendly message, and any invalid fields.
export function showError(box, err, message) {
  box.replaceChildren(el("code", { text: err.code || "ERROR" }), message || err.message || "Something went wrong.");
  const fields = err.details && Array.isArray(err.details.fields) ? err.details.fields : [];
  if (fields.length) {
    box.append(el("ul", {}, fields.map((f) => el("li", { text: `${(f.loc || []).filter((p) => p !== "body").join(".")}: ${f.msg}` }))));
  }
  box.hidden = false;
}

export function hideError(box) {
  box.hidden = true;
  box.replaceChildren();
}

export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
