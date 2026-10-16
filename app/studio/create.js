// create.js - the "Create a video" panel: input tabs, upload, script, scene rows, options, presets,
// and sending POST /api/v1/broll/jobs. Errors from the API are shown under the form with their code.

import { ApiError, friendlyMessage, newKey, request } from "./api.js";
import { $, ICONS, clock, el, hideError, icon, showError } from "./dom.js";
import { PRESETS } from "./presets.js";

const WORDS_PER_SECOND = 2.6; // rough speaking speed, only for the "about M:SS" estimate

const state = {
  input: "script",               // voiceover | script | scenes
  file: null,                     // the chosen audio File
  ratios: new Set(["16:9"]),
  avoid: [],
  config: {
    voices: ["en-US-female"], default_voice: "en-US-female", aspect_ratios: ["16:9", "9:16", "1:1"],
    max_upload_mb: 100, max_audio_seconds: 600, allowed_audio_extensions: ["m4a", "mp3", "wav"],
    max_script_chars: 20000, max_scenes: 200,
  },
};

let onCreated = () => {};

export function initCreate({ onJobCreated }) {
  onCreated = onJobCreated;
  initTabs();
  initDropzone();
  initScript();
  initScenes();
  initAvoid();
  renderRatios();
  renderPresets();
  $("create-form").addEventListener("submit", submit);
  $("create-form").addEventListener("input", (e) => e.target.removeAttribute("aria-invalid"));
}

// Settings from GET /api/v1/config/public (voices, ratios, limits).
export function applyConfig(config) {
  Object.assign(state.config, config);
  const select = $("voice");
  const current = select.value;
  select.replaceChildren(...config.voices.map((v) => el("option", { value: v, text: v })));
  select.value = config.voices.includes(current) ? current : config.default_voice;
  $("dropzone-sub").textContent =
    `${config.allowed_audio_extensions.join(", ")} · up to ${config.max_upload_mb} MB and ${Math.round(config.max_audio_seconds / 60)} min`;
  $("script").maxLength = config.max_script_chars;
  renderRatios();
  updateScriptMeta();
}

export const showSrtInPlayer = () => $("srt-player").checked;

// ---------- Tabs (arrow keys move between them, like native tabs) ----------

function initTabs() {
  const tabs = [...document.querySelectorAll('#create [role="tab"]')];
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.input));
    tab.addEventListener("keydown", (e) => {
      const move = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      let next = null;
      if (move) next = tabs[(i + move + tabs.length) % tabs.length];
      if (e.key === "Home") next = tabs[0];
      if (e.key === "End") next = tabs[tabs.length - 1];
      if (next) { e.preventDefault(); selectTab(next.dataset.input); next.focus(); }
    });
  });
}

function selectTab(input) {
  state.input = input;
  for (const tab of document.querySelectorAll('#create [role="tab"]')) {
    const on = tab.dataset.input === input;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
  }
  $("voice-field").hidden = input === "voiceover"; // an uploaded voice needs no TTS voice
  hideError($("form-error"));
}

// ---------- Voiceover: drag and drop, or choose a file ----------

function initDropzone() {
  const zone = $("dropzone");
  const inputEl = $("audio-file");
  inputEl.addEventListener("change", () => setFile(inputEl.files[0] || null));
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("is-over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, () => zone.classList.remove("is-over"));
  }
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) setFile(file);
  });
}

function setFile(file) {
  const { allowed_audio_extensions: allowed, max_upload_mb: maxMb } = state.config;
  hideError($("form-error"));
  if (file) {
    const ext = file.name.split(".").pop().toLowerCase();
    if (!allowed.includes(ext)) {
      showError($("form-error"), new ApiError({ code: "UNSUPPORTED_FILE_TYPE" }), `".${ext}" files are not supported. Use ${allowed.join(", ")}.`);
      file = null;
    } else if (file.size > maxMb * 1024 * 1024) {
      showError($("form-error"), new ApiError({ code: "FILE_TOO_LARGE" }), `The file is larger than ${maxMb} MB.`);
      file = null;
    }
  }
  state.file = file;
  if (!file) $("audio-file").value = "";
  $("dropzone").classList.toggle("has-file", !!file);
  $("dropzone-title").textContent = file
    ? `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`
    : "Drop an audio file here, or choose one";
}

// ---------- Script: live word count + estimated length ----------

function initScript() {
  $("script").addEventListener("input", updateScriptMeta);
  updateScriptMeta();
}

function updateScriptMeta() {
  const text = $("script").value.trim();
  const words = text ? text.split(/\s+/).length : 0;
  const est = words / WORDS_PER_SECOND;
  let note = `${words} word${words === 1 ? "" : "s"}`;
  if (words) note += ` · about ${clock(est)} of narration`;
  if (est > state.config.max_audio_seconds) note += ` · over the ${Math.round(state.config.max_audio_seconds / 60)} min limit`;
  $("script-meta").textContent = note;
}

// ---------- Scene list: editable rows ----------

function initScenes() {
  $("add-scene").addEventListener("click", () => {
    if ($("scene-rows").children.length >= state.config.max_scenes) return;
    addSceneRow().querySelector("textarea").focus();
  });
  addSceneRow();
}

function addSceneRow(text = "", hint = "") {
  const list = $("scene-rows");
  const n = list.children.length + 1;
  const row = el("li", { class: "scene-row" },
    el("textarea", { "aria-label": `Scene ${n} narration`, maxlength: 2000, rows: 1, placeholder: "What the narrator says" }),
    el("input", { type: "text", "aria-label": `Scene ${n} visual hint`, maxlength: 500, placeholder: "What to show (optional)" }),
    el("button", { type: "button", class: "icon-btn", "aria-label": `Remove scene ${n}`, onclick: () => removeSceneRow(row) }, icon(ICONS.x)),
  );
  row.querySelector("textarea").value = text;
  row.querySelector("input").value = hint;
  list.append(row);
  return row;
}

function removeSceneRow(row) {
  const list = $("scene-rows");
  const next = row.nextElementSibling || row.previousElementSibling;
  row.remove();
  if (!list.children.length) addSceneRow();
  renumberRows();
  (next || list.firstElementChild).querySelector("textarea").focus();
}

function renumberRows() {
  [...$("scene-rows").children].forEach((row, i) => {
    row.querySelector("textarea").setAttribute("aria-label", `Scene ${i + 1} narration`);
    row.querySelector("input").setAttribute("aria-label", `Scene ${i + 1} visual hint`);
    row.querySelector("button").setAttribute("aria-label", `Remove scene ${i + 1}`);
  });
}

// ---------- Options ----------

function renderRatios() {
  const box = $("ratios");
  box.replaceChildren(...state.config.aspect_ratios.map((r) => el("button", {
    type: "button", class: "chip", text: r, "aria-pressed": String(state.ratios.has(r)),
    onclick: (e) => {
      if (state.ratios.has(r)) state.ratios.delete(r); else state.ratios.add(r);
      e.currentTarget.setAttribute("aria-pressed", String(state.ratios.has(r)));
    },
  })));
}

function initAvoid() {
  const input = $("avoid-input");
  input.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === ",") && input.value.trim()) {
      e.preventDefault();
      addAvoid(input.value.trim());
      input.value = "";
    } else if (e.key === "Enter") {
      e.preventDefault(); // never submit the whole form from this box
    } else if (e.key === "Backspace" && !input.value && state.avoid.length) {
      state.avoid.pop();
      renderAvoid();
    }
  });
  $("avoid-box").addEventListener("click", (e) => { if (e.target.id === "avoid-box") input.focus(); });
}

function addAvoid(text) {
  if (!state.avoid.includes(text) && state.avoid.length < 50) state.avoid.push(text);
  renderAvoid();
}

function renderAvoid() {
  $("avoid-tags").replaceChildren(...state.avoid.map((tag, i) => el("li", { class: "tag" }, tag,
    el("button", { type: "button", "aria-label": `Remove ${tag}`, onclick: () => {
      state.avoid.splice(i, 1);
      renderAvoid();
      $("avoid-input").focus();
    } }, icon(ICONS.x)))));
}

// ---------- Presets ----------

function renderPresets() {
  $("preset-list").replaceChildren(...PRESETS.map((p) => el("button", {
    type: "button", class: "chip", text: p.label, title: p.title || p.label, "data-preset": p.id,
    onclick: () => applyPreset(p),
  })));
}

function applyPreset(p) {
  selectTab(p.input);
  if (p.input === "script") {
    $("script").value = p.script;
    updateScriptMeta();
  } else {
    $("scene-rows").replaceChildren();
    p.scenes.forEach((s) => addSceneRow(s.text, s.visual_hint));
  }
  state.ratios = new Set(p.ratios);
  renderRatios();
  $("style").value = p.style || "";
  state.avoid = [...(p.avoid || [])];
  renderAvoid();
  for (const node of document.querySelectorAll("#create [aria-invalid]")) node.removeAttribute("aria-invalid");
  $("form-status").textContent = `Filled the "${p.label}" example.`;
}

// ---------- Submit ----------

function options() {
  const out = {
    aspect_ratios: state.config.aspect_ratios.filter((r) => state.ratios.has(r)),
    avoid: state.avoid,
    captions: $("burn-captions").checked,
  };
  const style = $("style").value.trim();
  if (style) out.style = style;
  if (state.input !== "voiceover") out.voice = $("voice").value;
  return out;
}

// Returns {json} or {form}, or throws an ApiError for problems we can see before sending.
function buildRequest() {
  const opts = options();
  if (!opts.aspect_ratios.length) {
    throw new ApiError({ code: "INVALID_INPUT", message: "Choose at least one aspect ratio." });
  }
  if (state.input === "voiceover") {
    const url = $("audio-url").value.trim();
    if (state.file && url) throw new ApiError({ code: "INVALID_INPUT", message: "Use a file OR a URL, not both." });
    if (state.file) {
      const form = new FormData();
      form.append("audio", state.file, state.file.name);
      form.append("options", JSON.stringify(opts));
      return { form };
    }
    if (!url) throw markInvalid("audio-url", "Choose an audio file or paste an audio URL.");
    return { json: { ...opts, audio_url: url } };
  }
  if (state.input === "script") {
    return { json: { ...opts, script: $("script").value } }; // an empty script gets the API's EMPTY_SCRIPT
  }
  const scenes = [...$("scene-rows").children]
    .map((row) => ({ text: row.querySelector("textarea").value.trim(), visual_hint: row.querySelector("input").value.trim() || null }))
    .filter((s) => s.text);
  if (!scenes.length) throw markInvalid(null, "Write the narration for at least one scene.");
  return { json: { ...opts, scenes } };
}

function markInvalid(id, message) {
  if (id) $(id).setAttribute("aria-invalid", "true");
  return new ApiError({ code: "INVALID_INPUT", message });
}

// Mark the inputs the API named in details.fields (e.g. ["body", "script"]).
function markApiFields(err) {
  const ids = { script: "script", audio_url: "audio-url", style: "style", voice: "voice" };
  for (const f of err.details?.fields || []) {
    const id = (f.loc || []).map((p) => ids[p]).find(Boolean);
    if (id) $(id).setAttribute("aria-invalid", "true");
  }
  if (err.code === "EMPTY_SCRIPT") $("script").setAttribute("aria-invalid", "true");
}

let submitting = false;

async function submit(e) {
  e.preventDefault();
  if (submitting) return;
  const box = $("form-error");
  hideError(box);
  let req;
  try {
    req = buildRequest();
  } catch (err) {
    showError(box, err);
    return;
  }
  submitting = true;
  $("generate").disabled = true;
  $("form-status").textContent = req.form ? "Uploading and checking the audio" : "Sending";
  try {
    const ratios = req.json ? req.json.aspect_ratios : options().aspect_ratios;
    const resp = await request("/api/v1/broll/jobs", { method: "POST", json: req.json, form: req.form, idempotencyKey: newKey() });
    $("form-status").textContent = "";
    onCreated(resp.job_id, { ratios });
  } catch (err) {
    $("form-status").textContent = "";
    markApiFields(err);
    showError(box, err, friendlyMessage(err));
  } finally {
    submitting = false;
    $("generate").disabled = false;
  }
}
