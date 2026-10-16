// result.js - the finished job: video player (one tab per aspect ratio), downloads, the scene
// timeline strip (with "Swap clip"), and the Insights panel (footage mix, timings, cost, models).

import { fetchJson, fetchText, friendlyMessage, newKey, pollJob, request, sameOrigin } from "./api.js";
import { $, ICONS, clock, el, icon, reducedMotion, secs, svg } from "./dom.js";
import { showSrtInPlayer } from "./create.js";

const PROVIDERS = {
  pixabay_video: "Pixabay", pixabay_image_motion: "Pixabay", wikimedia: "Wikimedia Commons",
  generated: "Motion card", mock: "Mock clip", pexels: "Pexels", user_supplied: "Your clip",
};
const SCORE_SCALE = 0.4; // a CLIP relevance of 0.4 fills the bar (real scores are about 0.15-0.35)

let current = null;        // {job, files, timeline, manifest, ratio}
let swapping = false;
let onSwappedCb = () => {};
let videoLoad = new AbortController(); // listeners of the video being loaded now (aborted by the next load)

// ---------- Show a job's result ----------

export async function showResult(job, { onSwapped, ratio, time = 0 } = {}) {
  onSwappedCb = onSwapped || onSwappedCb;
  const files = job.result?.files || {};
  const [timeline, manifest] = await Promise.all([fetchJson(files["timeline.json"]), fetchJson(files["manifest.json"])]);
  const ratios = timeline.videos.map((v) => v.aspect_ratio);
  current = { job, files, timeline, manifest, ratio: ratios.includes(ratio) ? ratio : ratios[0] };

  const swaps = timeline.swaps || [];
  $("result-sub").textContent = [
    `${timeline.scenes.length} scenes`, clock(timeline.duration), inputLabel(manifest.input?.type || timeline.input_type),
    swaps.length ? `version ${swaps.length + 1} (${swaps.length} clip swap${swaps.length > 1 ? "s" : ""})` : "",
  ].filter(Boolean).join(" · ");
  $("swap-note").hidden = true;
  renderRatioTabs(ratios);
  renderDownloads();
  renderStrip();
  renderInsights();
  $("result").hidden = false;
  $("insights").hidden = false;
  await loadVideo(time);
}

export function hideResult() {
  $("result").hidden = true;
  $("insights").hidden = true;
  $("player").pause();
  current = null;
}

export const currentJobId = () => current?.job.job_id;

function inputLabel(type) {
  return { script: "from a script", scenes: "from a scene list", upload: "from an uploaded voiceover",
    audio_url: "from an audio URL", swap: "clip swap" }[type] || type;
}

// ---------- Player ----------

function renderRatioTabs(ratios) {
  const box = $("ratio-tabs");
  box.replaceChildren(...ratios.map((r, i) => el("button", {
    role: "tab", type: "button", text: r, "aria-selected": String(r === current.ratio),
    tabindex: r === current.ratio ? "0" : "-1", "aria-controls": "stage",
    onclick: () => selectRatio(r),
    onkeydown: (e) => {
      const move = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!move) return;
      e.preventDefault();
      const next = ratios[(i + move + ratios.length) % ratios.length];
      selectRatio(next);
      box.children[ratios.indexOf(next)].focus();
    },
  })));
}

function selectRatio(ratio) {
  if (ratio === current.ratio) return;
  current.ratio = ratio;
  for (const tab of $("ratio-tabs").children) {
    const on = tab.textContent === ratio;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  }
  loadVideo($("player").currentTime);
}

// The video streams straight from its signed link. The API answers HTTP Range requests
// (206 Partial Content), so playback starts at once and a click on a scene card seeks to any
// moment without downloading the whole file first.
async function loadVideo(time = 0) {
  const player = $("player");
  const frame = $("frame");
  const video = current.timeline.videos.find((v) => v.aspect_ratio === current.ratio);
  frame.className = "frame" + (current.ratio === "9:16" ? " is-phone" : current.ratio === "1:1" ? " is-square" : "");
  player.setAttribute("aria-label", `${current.ratio} video`);
  const loading = $("player-loading");
  const wasPlaying = !player.paused;
  loading.hidden = false;
  loading.firstElementChild.hidden = false;
  loading.lastElementChild.textContent = "Loading video";
  videoLoad.abort(); // a ratio switch / swap during a load: the old load's listeners must not fire
  const done = (videoLoad = new AbortController()); // removes both listeners once one fired
  player.addEventListener("loadedmetadata", () => {
    done.abort();
    loading.hidden = true;
    if (time) player.currentTime = Math.min(time, player.duration || time);
    if (wasPlaying) player.play().catch(() => {});
  }, { signal: done.signal });
  player.addEventListener("error", () => {
    done.abort();
    loading.lastElementChild.textContent = "Could not load the video. Use the download link below.";
    loading.firstElementChild.hidden = true;
  }, { signal: done.signal });
  player.src = sameOrigin(current.files[video.file]);
  player.replaceChildren();
  await addCaptionTrack(player);
}

// "Show SRT in player": captions.srt -> WebVTT (the format <track> understands) as a blob.
async function addCaptionTrack(player) {
  if (!showSrtInPlayer() || !current.files["captions.srt"]) return;
  const srt = await fetchText(current.files["captions.srt"]);
  if (!srt) return;
  const vtt = "WEBVTT\n\n" + srt.replace(/\r/g, "").replace(/(\d\d:\d\d:\d\d),(\d\d\d)/g, "$1.$2");
  const track = el("track", { kind: "captions", label: "Captions", srclang: "en", default: true,
    src: URL.createObjectURL(new Blob([vtt], { type: "text/vtt" })) });
  player.append(track);
}

// Highlight the card of the scene that is playing.
let playingIndex = -1;
$("player").addEventListener("timeupdate", () => {
  if (!current) return;
  const t = $("player").currentTime;
  const scene = current.timeline.scenes.find((s) => t >= s.start && t < s.end) || null;
  const index = scene ? scene.index : -1;
  if (index === playingIndex) return;
  playingIndex = index;
  for (const card of $("strip").children) {
    const on = Number(card.dataset.index) === index;
    card.classList.toggle("is-playing", on);
    if (on) {
      card.querySelector(".scene-seek").setAttribute("aria-current", "true");
      if (!$("player").paused) card.scrollIntoView({ block: "nearest", inline: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
    } else {
      card.querySelector(".scene-seek").removeAttribute("aria-current");
    }
  }
});

function seek(scene) {
  const player = $("player");
  player.currentTime = scene.start + 0.01;
  player.play().catch(() => {});
}

// ---------- Downloads ----------

function renderDownloads() {
  const names = Object.keys(current.files).filter((n) => !n.startsWith("thumb_"));
  const label = (n) => {
    const v = current.timeline.videos.find((x) => x.file === n);
    return v ? `${v.aspect_ratio} video` : n;
  };
  $("downloads").replaceChildren(...names.map((n) => el("a", {
    class: "btn btn-small", href: sameOrigin(current.files[n]), download: n, "aria-label": `Download ${label(n)}`,
  }, icon(ICONS.download), label(n))));
}

// ---------- Scene timeline strip ----------

function renderStrip() {
  playingIndex = -1;
  $("strip").replaceChildren(...current.timeline.scenes.map(sceneCard));
}

function clipType(scene) {
  if (scene.generated) return "Generated card";
  return scene.clip_details?.kind === "image" ? "Photo motion" : "Video";
}

function flagList(flags) {
  if (Array.isArray(flags)) return flags;
  return Object.entries(flags || {}).filter(([, on]) => on === true).map(([name]) => name);
}

const LOGO_TEXT = { ok: "Logo check passed", penalised: "Logo check: possible logo, ranked lower", rejected: "Logo check: rejected" };

function logoStatus(check) {
  if (!check) return "Logo check: not needed";
  const probs = [check.thumbnail?.logo_prob, ...(check.frames || []).map((f) => f.logo_prob)].filter((p) => typeof p === "number");
  const max = probs.length ? ` (max ${Math.max(...probs).toFixed(2)})` : "";
  return `${LOGO_TEXT[check.result] || `Logo check: ${check.result}`}${max}`;
}

function sceneCard(scene) {
  const thumbLink = scene.thumbnail && current.files[scene.thumbnail];
  const minScore = current.manifest.settings?.STOCK_MIN_SCORE;
  const score = typeof scene.relevance_score === "number" ? scene.relevance_score : null;

  const fill = el("div", { class: "score-fill" });
  const bar = el("div", { class: "score-bar", role: "img",
    "aria-label": score === null ? "No relevance score" : `Relevance ${score.toFixed(3)}${minScore ? `, threshold ${minScore}` : ""}` }, fill);
  if (score !== null) fill.style.width = `${Math.min(100, (score / SCORE_SCALE) * 100)}%`;
  if (minScore) {
    const mark = el("span", { class: "score-mark", title: `STOCK_MIN_SCORE ${minScore}` });
    mark.style.left = `${Math.min(100, (minScore / SCORE_SCALE) * 100)}%`;
    bar.append(mark);
  }

  const licence = scene.licence_url
    ? el("a", { class: "badge", href: scene.licence_url, target: "_blank", rel: "noopener noreferrer", title: "Open the licence" }, scene.licence || "Licence")
    : el("span", { class: "badge" }, scene.licence || "No licence needed");

  const status = el("span", { class: "scene-status", "aria-live": "polite" });
  const swapBtn = el("button", { type: "button", class: "btn btn-small swap-btn", "aria-label": `Swap the clip of scene ${scene.index + 1}`,
    onclick: () => swapScene(scene.index) }, icon(ICONS.swap), "Swap clip");
  swapBtn.disabled = swapping;

  const flags = flagList(scene.flags).filter((f) => f !== "abstract");
  const img = thumbLink ? el("img", { src: sameOrigin(thumbLink), alt: "", loading: "lazy", decoding: "async" }) : el("span", { class: "thumb-none", text: "No preview" });

  return el("li", { class: "scene", "data-index": scene.index },
    el("button", { type: "button", class: "scene-seek", "aria-label": `Play scene ${scene.index + 1} from ${clock(scene.start)}: ${scene.text}`, onclick: () => seek(scene) },
      el("div", { class: "thumb" }, img,
        el("span", { class: "thumb-index", text: `Scene ${scene.index + 1}` }),
        el("span", { class: "thumb-time", text: `${clock(scene.start)} – ${clock(scene.end)}` })),
    ),
    el("div", { class: "scene-body" },
      el("p", { class: "scene-text", text: scene.text }),
      el("div", { class: "badges" },
        el("span", { class: "badge badge-teal", text: `${PROVIDERS[scene.provider] || scene.provider} · ${clipType(scene)}` }),
        licence,
        scene.ai_generated ? el("span", { class: "badge badge-accent", text: "AI-generated" }) : null,
        scene.generated ? el("span", { class: "badge badge-accent", text: "Generated" }) : null,
        flags.map((f) => el("span", { class: f === "sensitive" || f === "brand" || f === "named_person" ? "badge badge-warn" : "badge", text: f.replace("_", " ") })),
      ),
      scene.attribution ? el("p", { class: "meta", text: `Credit: ${scene.attribution}` }) : null,
      el("div", { class: "score" }, el("span", { text: "Relevance" }), bar, el("span", { class: "mono", text: score === null ? "n/a" : score.toFixed(3) })),
      el("p", { class: "meta", text: logoStatus(scene.logo_check) }),
      el("details", { class: "reason" }, el("summary", { text: "Why this clip" }), el("p", { text: scene.reason || "No reason recorded." })),
    ),
    el("div", { class: "scene-foot" }, status, swapBtn),
  );
}

// ---------- Swap one scene's clip ----------

async function swapScene(index) {
  if (swapping || !current) return;
  swapping = true;
  setSwapButtons(true);
  const jobId = current.job.job_id;
  const card = () => $("strip").querySelector(`[data-index="${index}"]`);
  const status = (text, kind) => {
    const box = card()?.querySelector(".scene-status");
    if (!box) return;
    box.className = "scene-status" + (kind === "error" ? " is-error" : "");
    box.replaceChildren(kind === "busy" ? el("span", { class: "spinner", "aria-hidden": "true" }) : "", text);
  };
  card()?.classList.add("is-swapping");
  status("Finding a new clip", "busy");
  try {
    const accepted = await request(`/api/v1/broll/jobs/${encodeURIComponent(jobId)}/scenes/${index}/swap`,
      { method: "POST", json: { mode: "next_best" }, idempotencyKey: newKey() });
    const done = await pollJob(accepted.job_id, {
      onUpdate: (j) => status(j.status === "queued" ? "Waiting in queue" : `${j.stage} ${j.progress}%`, "busy"),
      onNote: (text) => text && status(text, "busy"),
    });
    if (done.status === "failed") {
      status(`${done.error?.code}: ${done.error?.message}`, "error");
      return;
    }
    await applySwap(done, index);
  } catch (err) {
    status(`${err.code}: ${friendlyMessage(err)}`, "error");
  } finally {
    swapping = false;
    card()?.classList.remove("is-swapping");
    setSwapButtons(false);
  }
}

function setSwapButtons(disabled) {
  for (const btn of document.querySelectorAll(".swap-btn")) btn.disabled = disabled;
}

// The swap job holds the new version (all videos, timeline, manifest). Reload the player (new links,
// so nothing comes from a cache) and only the swapped card; keep the playback position.
async function applySwap(swapJob, index) {
  const time = $("player").currentTime;
  const files = swapJob.result.files;
  const [timeline, manifest] = await Promise.all([fetchJson(files["timeline.json"]), fetchJson(files["manifest.json"])]);
  current = { ...current, job: swapJob, files, timeline, manifest };
  forgetOtherVideos(swapJob.job_id);
  const old = $("strip").querySelector(`[data-index="${index}"]`);
  const fresh = sceneCard(timeline.scenes[index]);
  old.replaceWith(fresh);
  playingIndex = -1;
  renderDownloads();
  renderInsights();
  const swaps = timeline.swaps || [];
  const last = swaps[swaps.length - 1];
  $("result-sub").textContent = `${timeline.scenes.length} scenes · ${clock(timeline.duration)} · version ${swaps.length + 1} (${swaps.length} clip swap${swaps.length > 1 ? "s" : ""})`;
  if (last) {
    const note = $("swap-note");
    note.textContent = `Scene ${index + 1} swapped in ${secs(last.swap_seconds)} (full render took ${secs(last.full_render_seconds)})`;
    note.hidden = false;
  }
  fresh.querySelector(".scene-status").textContent = "Swapped";
  await loadVideo(time);
  onSwappedCb(swapJob);
}

// ---------- Insights ----------

function renderInsights() {
  const { manifest, timeline } = current;
  // Footage mix from the timeline (works for every version, also before manifests had footage_mix).
  const counts = { video: 0, photo: 0, card: 0 };
  for (const s of timeline.scenes) {
    if (s.generated) counts.card += 1;
    else if (s.clip_details?.kind === "image") counts.photo += 1;
    else counts.video += 1;
  }
  const total = timeline.scenes.length || 1;
  const tile = (value, label) => el("div", { class: "tile" },
    el("div", { class: "tile-value", text: String(value) }), el("div", { class: "tile-label", text: label }),
    el("div", { class: "tile-share", text: `${Math.round((value / total) * 100)}% of scenes` }));
  $("mix").replaceChildren(tile(counts.video, "Stock video"), tile(counts.photo, "Photo motion"), tile(counts.card, "Generated card"));

  const cost = manifest.cost_estimate || {};
  $("cost").textContent = `$${Number(cost.total_usd || 0).toFixed(2)}`;
  $("cost-note").textContent = `${cost.llm_tokens ?? 0} LLM tokens. ${cost.method || ""}`;

  renderTimings(manifest.timings_seconds || {});

  const models = Object.entries(manifest.models || {});
  $("models").replaceChildren(...models.map(([role, info]) => {
    let text;
    if (typeof info === "string") text = info;
    else {
      const name = info.provider || info.scorer || info.library || "";
      text = [name, info.model, info.version && `v${info.version}`, info.voice].filter(Boolean).join(" · ");
    }
    return el("div", {}, el("dt", { text: role }), el("dd", { text: text || "n/a" }));
  }));
}

// Horizontal bar chart in plain SVG: one bar per stage (one series, so one colour and no legend),
// values written at the bar ends, a tooltip on hover / keyboard focus, and a hidden table for screen readers.
function renderTimings(timings) {
  const rows = Object.entries(timings).filter(([, v]) => typeof v === "number");
  const box = $("timings");
  if (!rows.length) { box.replaceChildren(el("p", { class: "hint", text: "No timings recorded." })); return; }
  const W = 640, labelW = 96, valueW = 64, rowH = 30, barH = 14;
  const max = Math.max(...rows.map(([, v]) => v), 0.001);
  const total = rows.reduce((a, [, v]) => a + v, 0);
  const plotW = W - labelW - valueW;
  const H = rows.length * rowH + 4;
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Stage timings, ${secs(total)} in total` });
  chart.append(svg("line", { class: "grid", x1: labelW, x2: labelW, y1: 0, y2: H }));
  const tip = document.querySelector(".chart-tip") || document.body.appendChild(el("div", { class: "chart-tip", hidden: true }));
  rows.forEach(([name, value], i) => {
    const y = i * rowH + 4;
    const w = Math.max(2, (value / max) * plotW);
    const label = `${name}: ${value.toFixed(2)} s (${Math.round((value / total) * 100)}% of the time)`;
    const g = svg("g", { tabindex: "0", "aria-label": label });
    const lbl = svg("text", { class: "lbl", x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end" });
    lbl.textContent = name;
    const val = svg("text", { class: "val", x: labelW + w + 8, y: y + barH / 2 + 4 });
    val.textContent = secs(value);
    g.append(
      svg("rect", { class: "bar-hit", x: 0, y: y - 6, width: W, height: rowH }),
      svg("path", { class: "bar", d: barPath(labelW, y, w, barH) }),
      lbl, val,
    );
    const show = (x, yy) => { tip.textContent = label; tip.hidden = false; tip.style.left = `${x + 12}px`; tip.style.top = `${yy + 12}px`; };
    g.addEventListener("mousemove", (e) => show(e.clientX, e.clientY));
    g.addEventListener("mouseleave", () => { tip.hidden = true; });
    g.addEventListener("focus", () => { const r = g.getBoundingClientRect(); show(r.left + 40, r.top); });
    g.addEventListener("blur", () => { tip.hidden = true; });
    chart.append(g);
  });
  const table = el("table", { class: "sr-only" },
    el("caption", { text: "Stage timings in seconds" }),
    el("tr", {}, el("th", { text: "Stage" }), el("th", { text: "Seconds" })),
    rows.map(([n, v]) => el("tr", {}, el("td", { text: n }), el("td", { text: v.toFixed(2) }))));
  box.replaceChildren(chart, table);
}

// A bar anchored at the baseline (square left end) with a 4 px rounded data end on the right.
function barPath(x, y, w, h) {
  const r = Math.min(4, w / 2, h / 2);
  return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`;
}
