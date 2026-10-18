"""Make testpack/ratings/rate.html: one static page to rate every scene of every successful video.

The page opens straight from disk (no server, no internet). For each scene it shows the narration
text, plays ONLY that scene of the video (a media fragment "video_16x9.mp4#t=start,end" pointing to the
downloaded output file next to it), the source + licence, five rating buttons and a note field.
Ratings are kept in the browser (localStorage) while you work; "Export CSV" downloads ratings.csv
with the columns job, scene, text, provider, clip_id, clip_score, my_rating, note. Put that file at
testpack/ratings/ratings.csv and run tools/evaluate.py.

    .venv\\Scripts\\python tools\\make_rating_page.py                      (testpack/outputs/run1)
    .venv\\Scripts\\python tools\\make_rating_page.py --runs testpack/outputs/run1 testpack/outputs/resilience
    .venv\\Scripts\\python tools\\make_rating_page.py --jobs testpack/outputs/run2/hard01_abstract_and_statistics ...
"""
import argparse
import html
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = [ROOT / "testpack" / "outputs" / "run1"]
DEFAULT_OUT = ROOT / "testpack" / "ratings" / "rate.html"
VIDEO_PREFERENCE = ("video_16x9.mp4", "video_1x1.mp4", "video_9x16.mp4")


def job_name(run: Path, folder: Path) -> str:
    """The "job" column: run folder + input folder, e.g. "run1/core04_tutorial_cold_brew"."""
    return f"{run.name}/{folder.name}"


def collect_scenes(runs: list[Path], page_dir: Path,
                   folders: list[Path] | None = None) -> tuple[list[dict], list[dict]]:
    """(jobs, scenes) of every succeeded job that has a timeline.json and a video: all jobs of `runs`,
    or only the job folders in `folders` (each one's run = its parent folder)."""
    jobs, scenes = [], []
    pairs = ([(f.parent, f) for f in folders] if folders else
             [(run, f) for run in runs for f in sorted(p for p in run.iterdir() if p.is_dir())])
    for run, folder in pairs:
        job_file, timeline_file = folder / "job.json", folder / "timeline.json"
        if not (job_file.is_file() and timeline_file.is_file()):
            continue
        if json.loads(job_file.read_text(encoding="utf-8")).get("status") != "succeeded":
            continue
        timeline = json.loads(timeline_file.read_text(encoding="utf-8"))
        video = next((folder / v for v in VIDEO_PREFERENCE if (folder / v).is_file()), None)
        if video is None:
            continue
        rel = lambda p: Path(os.path.relpath(p, page_dir)).as_posix()  # noqa: E731
        name = job_name(run, folder)
        jobs.append({"job": name, "job_id": timeline.get("job_id"), "language": timeline.get("language"),
                     "duration": timeline.get("duration"), "video": rel(video), "scenes": len(timeline["scenes"])})
        for sc in timeline["scenes"]:
            thumb = folder / sc["thumbnail"] if sc.get("thumbnail") else None
            scenes.append({
                "job": name, "scene": sc["index"], "language": timeline.get("language"),
                "start": sc["start"], "end": sc["end"], "text": sc["text"],
                "search_text": sc.get("search_text"), "query": sc.get("query"), "queries": sc.get("queries") or [],
                "provider": sc.get("provider"), "clip_id": sc.get("clip_id"),
                "clip_score": sc.get("relevance_score"), "generated": bool(sc.get("generated")),
                "card_text": sc.get("card_text"), "licence": sc.get("licence"),
                "licence_url": sc.get("licence_url"), "attribution": sc.get("attribution"),
                "page_url": sc.get("page_url"), "visual_hint": sc.get("visual_hint"),
                "flags": [k for k, v in (sc.get("flags") or {}).items() if v],
                "video": rel(video), "thumb": rel(thumb) if thumb and thumb.is_file() else None,
            })
    return jobs, scenes


def render_page(jobs: list[dict], scenes: list[dict], title: str) -> str:
    data = json.dumps({"jobs": jobs, "scenes": scenes}, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make the static scene rating page.")
    parser.add_argument("--runs", type=Path, nargs="+", default=DEFAULT_RUNS, help="batch output folders")
    parser.add_argument("--jobs", type=Path, nargs="+", help="only these job folders (instead of --runs)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write rate.html")
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    jobs, scenes = collect_scenes([r.resolve() for r in args.runs], args.out.parent.resolve(),
                                  [j.resolve() for j in args.jobs] if args.jobs else None)
    if not scenes:
        print("no succeeded jobs with a timeline.json + video found", file=sys.stderr)
        return 2
    args.out.write_text(render_page(jobs, scenes, "Rate the B-roll"), encoding="utf-8")
    print(f"{args.out}: {len(jobs)} videos, {len(scenes)} scenes")
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f5f2; --card:#ffffff; --ink:#1d1d1f; --muted:#5f6368; --line:#dedbd4; --accent:#2f5d8a;
  --accent-ink:#ffffff; --good:#2e7d4f; --warn:#a15c00; --chip:#eceae4; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#16171a; --card:#202226; --ink:#ececec;
  --muted:#a3a7ad; --line:#34373d; --accent:#7fb0e0; --accent-ink:#0d1620; --good:#6fcf97; --warn:#f2b25c;
  --chip:#2b2e33; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, "Segoe UI", "Noto Sans",
  "Nirmala UI", sans-serif; }
header { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line);
  padding:12px 16px; }
.bar { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:center; max-width:1100px; margin:0 auto; }
h1 { font-size:18px; margin:0; }
.progress { flex:1 1 200px; min-width:160px; }
.track { height:8px; background:var(--chip); border-radius:4px; overflow:hidden; }
.fill { height:100%; background:var(--good); width:0; }
.small { font-size:13px; color:var(--muted); }
button, select { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--ink);
  border-radius:6px; padding:5px 10px; cursor:pointer; }
button.primary { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
main { max-width:1100px; margin:0 auto; padding:16px; }
.guide { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin-bottom:18px; }
.guide ul { margin:6px 0 0; padding-left:0; list-style:none; }
.job { margin:26px 0 10px; display:flex; flex-wrap:wrap; gap:6px 14px; align-items:baseline; }
.job h2 { font-size:16px; margin:0; word-break:break-all; }
.scene { display:grid; grid-template-columns:minmax(0,340px) minmax(0,1fr); gap:14px; background:var(--card);
  border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:10px; }
.scene.rated { border-left:4px solid var(--good); }
video { width:100%; aspect-ratio:16/9; background:#000; border-radius:6px; display:block; }
.text { font-size:16px; margin:0 0 6px; }
.meta { font-size:13px; color:var(--muted); margin:2px 0; word-break:break-word; }
.chip { display:inline-block; background:var(--chip); border-radius:10px; padding:0 8px; margin-right:4px;
  font-size:12px; color:var(--ink); }
.chip.warn { color:var(--warn); }
.rate { display:flex; gap:6px; margin:8px 0 6px; flex-wrap:wrap; }
.rate button { width:40px; height:36px; font-weight:600; }
.rate button.on { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
textarea { width:100%; min-height:36px; font:inherit; font-size:13px; border:1px solid var(--line);
  border-radius:6px; background:var(--bg); color:var(--ink); padding:6px; resize:vertical; }
a { color:var(--accent); }
.hidden { display:none !important; }
@media (max-width:720px) { .scene { grid-template-columns:minmax(0,1fr); } }
</style>
</head>
<body>
<header><div class="bar">
  <h1>Rate the B-roll</h1>
  <div class="progress"><div class="track"><div class="fill" id="fill"></div></div>
    <div class="small" id="count"></div></div>
  <label class="small">Show <select id="filter"><option value="all">all scenes</option>
    <option value="unrated">unrated only</option></select></label>
  <label class="small">Language <select id="lang"><option value="">all</option></select></label>
  <button class="primary" id="export">Export CSV</button>
  <label class="small"><button id="importBtn" type="button">Import CSV</button>
    <input type="file" id="import" accept=".csv,text/csv" class="hidden"></label>
</div></header>
<main>
<section class="guide">
  <strong>How to rate.</strong> Play each scene (only that part of the video plays) and ask: does the picture
  fit what the narration <em>means</em> at that moment?
  <ul>
    <li><strong>5</strong> = clearly matches the meaning &nbsp;·&nbsp; <strong>4</strong> = good match</li>
    <li><strong>3</strong> = acceptable (related, a viewer would not mind)</li>
    <li><strong>2</strong> = weak (only loosely related) &nbsp;·&nbsp; <strong>1</strong> = wrong or nonsense</li>
  </ul>
  <p class="small">Rate the footage, not the captions or the voice. A generated number card counts as a match
  if it shows the number the line says. Progress is saved in this browser; use <em>Export CSV</em> and save the
  file as <code>testpack/ratings/ratings.csv</code>, then run <code>tools/evaluate.py</code>.</p>
</section>
<div id="list"></div>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("data").textContent);
  var KEY = "broll-ratings-v1";
  var store = {};
  try { store = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { store = {}; }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(store)); } catch (e) { /* private window */ } }
  function id(s) { return s.job + "#" + s.scene; }
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text; return n; }

  var list = document.getElementById("list");
  var cards = [];
  var byJob = {};
  DATA.jobs.forEach(function (j) { byJob[j.job] = j; });
  var langs = {};
  var lastJob = null;
  DATA.scenes.forEach(function (s) {
    langs[s.language || "?"] = true;
    if (s.job !== lastJob) {
      lastJob = s.job;
      var j = byJob[s.job], head = el("div", "job");
      head.dataset.job = s.job;
      head.appendChild(el("h2", null, s.job));
      head.appendChild(el("span", "small", "language " + (j.language || "?") + " · " + j.duration + " s · " +
        j.scenes + " scenes"));
      var full = el("a", "small", "open full video"); full.href = j.video; full.target = "_blank";
      head.appendChild(full);
      list.appendChild(head);
    }
    var card = el("article", "scene");
    card.dataset.id = id(s); card.dataset.lang = s.language || "?"; card.dataset.job = s.job;
    var left = el("div");
    var v = el("video"); v.controls = true; v.preload = "none"; v.playsInline = true;
    if (s.thumb) v.poster = s.thumb;
    v.src = s.video + "#t=" + s.start.toFixed(2) + "," + s.end.toFixed(2);
    left.appendChild(v);
    left.appendChild(el("div", "meta", "scene " + s.scene + " · " + s.start.toFixed(2) + "–" + s.end.toFixed(2) + " s"));
    var right = el("div");
    right.appendChild(el("p", "text", s.text));
    if (s.search_text) right.appendChild(el("p", "meta", "English used for search: " + s.search_text));
    if (s.visual_hint) right.appendChild(el("p", "meta", "visual hint: " + s.visual_hint));
    right.appendChild(el("p", "meta", "query: " + (s.query || "") + (s.queries.length ? "  (all: " + s.queries.join(" | ") + ")" : "")));
    var src = el("p", "meta");
    src.appendChild(el("span", "chip", s.provider + (s.clip_id ? ":" + s.clip_id : "")));
    if (s.clip_score !== null && s.clip_score !== undefined) src.appendChild(el("span", "chip", "CLIP " + s.clip_score));
    if (s.generated) src.appendChild(el("span", "chip", "generated card" + (s.card_text ? " “" + s.card_text + "”" : "")));
    s.flags.forEach(function (f) { src.appendChild(el("span", "chip warn", f)); });
    right.appendChild(src);
    var lic = el("p", "meta");
    if (s.licence_url) { var a = el("a", null, s.licence || "licence"); a.href = s.licence_url; a.target = "_blank"; lic.appendChild(a); }
    else lic.appendChild(document.createTextNode(s.licence || ""));
    if (s.attribution) lic.appendChild(document.createTextNode(" · " + s.attribution));
    if (s.page_url) { lic.appendChild(document.createTextNode(" · ")); var p = el("a", null, "source page");
      p.href = s.page_url; p.target = "_blank"; lic.appendChild(p); }
    right.appendChild(lic);
    var rate = el("div", "rate");
    for (var r = 1; r <= 5; r++) {
      var b = el("button", null, String(r)); b.type = "button"; b.dataset.r = r;
      b.title = ["", "wrong or nonsense", "weak", "acceptable", "good match", "clearly matches"][r];
      rate.appendChild(b);
    }
    right.appendChild(rate);
    var note = el("textarea"); note.placeholder = "optional note"; note.rows = 1;
    right.appendChild(note);
    card.appendChild(left); card.appendChild(right);
    list.appendChild(card);
    cards.push({ s: s, card: card, rate: rate, note: note });

    rate.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button"); if (!btn) return;
      var key = id(s); var entry = store[key] || {};
      entry.rating = Number(btn.dataset.r); store[key] = entry; save(); paint();
    });
    var timer = null;
    note.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { var key = id(s); var entry = store[key] || {};
        entry.note = note.value; store[key] = entry; save(); }, 300);
    });
  });
  var langSel = document.getElementById("lang");
  Object.keys(langs).sort().forEach(function (l) { var o = el("option", null, l); o.value = l; langSel.appendChild(o); });

  function paint() {
    var done = 0, filter = document.getElementById("filter").value, lang = langSel.value, visibleJobs = {};
    cards.forEach(function (c) {
      var entry = store[id(c.s)] || {};
      var rated = !!entry.rating; if (rated) done++;
      c.card.classList.toggle("rated", rated);
      Array.prototype.forEach.call(c.rate.children, function (b) { b.classList.toggle("on", Number(b.dataset.r) === entry.rating); });
      if (document.activeElement !== c.note) c.note.value = entry.note || "";
      var show = (filter === "all" || !rated) && (!lang || c.card.dataset.lang === lang);
      c.card.classList.toggle("hidden", !show);
      if (show) visibleJobs[c.s.job] = true;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".job"), function (h) {
      h.classList.toggle("hidden", !visibleJobs[h.dataset.job]); });
    document.getElementById("count").textContent = done + " of " + cards.length + " scenes rated";
    document.getElementById("fill").style.width = (cards.length ? 100 * done / cards.length : 0) + "%";
  }
  document.getElementById("filter").addEventListener("change", paint);
  langSel.addEventListener("change", paint);

  function csvCell(v) { v = v === null || v === undefined ? "" : String(v);
    return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
  document.getElementById("export").addEventListener("click", function () {
    var rows = [["job", "scene", "text", "provider", "clip_id", "clip_score", "my_rating", "note"]];
    cards.forEach(function (c) { var e = store[id(c.s)] || {};
      rows.push([c.s.job, c.s.scene, c.s.text, c.s.provider, c.s.clip_id, c.s.clip_score,
                 e.rating || "", e.note || ""]); });
    var text = "﻿" + rows.map(function (r) { return r.map(csvCell).join(","); }).join("\r\n") + "\r\n";
    var blob = new Blob([text], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "ratings.csv";
    document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });

  function parseCsv(text) {
    var rows = [], row = [], cell = "", q = false;
    text = text.replace(/^﻿/, "");
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) { if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += ch; }
      else if (ch === '"') q = true;
      else if (ch === ",") { row.push(cell); cell = ""; }
      else if (ch === "\n" || ch === "\r") { if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); rows.push(row); row = []; cell = ""; }
      else cell += ch;
    }
    if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
    return rows;
  }
  document.getElementById("importBtn").addEventListener("click", function () { document.getElementById("import").click(); });
  document.getElementById("import").addEventListener("change", function (ev) {
    var f = ev.target.files[0]; if (!f) return;
    f.text().then(function (text) {
      var rows = parseCsv(text), head = rows.shift() || [], n = 0;
      var ji = head.indexOf("job"), si = head.indexOf("scene"), ri = head.indexOf("my_rating"), ni = head.indexOf("note");
      rows.forEach(function (r) { if (!r[ji]) return; var key = r[ji] + "#" + r[si];
        var rating = Number(r[ri]); var entry = store[key] || {};
        if (rating >= 1 && rating <= 5) { entry.rating = rating; n++; }
        if (ni >= 0 && r[ni]) entry.note = r[ni];
        store[key] = entry; });
      save(); paint(); alert("Imported " + n + " ratings.");
    });
  });
  paint();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
