"""Calibrate the logo check on REAL Pixabay video thumbnails (needs PIXABAY_API_KEY + open_clip).

Step 1 - fetch thumbnails (the same small picture the pipeline scores) into a local folder
         (data/ is git-ignored: no images go into git), plus 2x2 contact sheets for labelling:
    python tools/logo_calibration.py fetch --out data/calibration
Step 2 - look at every picture and write testpack/calibration/logo_labels.csv by hand:
    pixabay_video_id,query,label        (label = logo | no_logo)
Step 3 - score the labelled pictures with the app's own logo check and print precision / recall
         for a range of thresholds (and what the OLD margin rule would have done):
    python tools/logo_calibration.py score --images data/calibration/thumbs \
        --labels testpack/calibration/logo_labels.csv --scores testpack/calibration/logo_scores.csv

"Positive" = the picture shows a logo. precision = rejected pictures that really show a logo /
all rejected; recall = logo pictures rejected / all logo pictures; clean kept = clean pictures
NOT rejected / all clean pictures.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run as a script from the project folder

QUERIES = ["laptop", "phone", "office", "city street", "food", "nature", "money", "handshake", "server room",
           "shopping"]
OLD_PROMPTS = ["a product with a printed brand logo", "a famous company logo",
               "a brand name printed on a can, cup or bottle", "a glowing apple logo on a laptop lid",
               "a sports brand logo on shoes or clothes", "readable brand name text", "a text watermark"]
OLD_REJECT_MARGIN = -0.04  # the rule before this calibration: logo score - scene score >= -0.04 -> rejected


def fetch(out: Path, per_query: int, queries: list[str], tag: str = "") -> None:
    from PIL import Image, ImageDraw

    from app.pipeline.providers.pixabay import PixabayVideoProvider

    thumbs = out / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    old = out / "candidates.csv"
    previous = list(csv.DictReader(old.open(encoding="utf-8"))) if old.is_file() else []
    provider, rows, seen = PixabayVideoProvider(), [], {r["pixabay_video_id"] for r in previous}
    for query in queries:
        found = [c for c in provider.search(query, "landscape", 0.0) if c.id not in seen][:per_query]
        for c in found:
            seen.add(c.id)
            path = provider.fetch_preview(c, thumbs)
            rows.append({"pixabay_video_id": c.id, "query": query, "file": path.name, "page_url": c.page_url})
            print(f"{query:12} {c.id:>10} {path.name}")
    with old.open("w", newline="", encoding="utf-8") as f:  # earlier batches are kept
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(previous + rows)
    sheets = out / "sheets"
    sheets.mkdir(exist_ok=True)
    for n in range(0, len(rows), 4):  # 2x2 pictures per sheet, each 640x360, id written on top
        sheet = Image.new("RGB", (1280, 720), "white")
        for k, row in enumerate(rows[n:n + 4]):
            img = Image.open(thumbs / row["file"]).convert("RGB").resize((640, 360))
            ImageDraw.Draw(img).rectangle((0, 0, 230, 22), fill="black")
            ImageDraw.Draw(img).text((4, 4), f"{row['pixabay_video_id']} ({row['query']})", fill="yellow")
            sheet.paste(img, ((k % 2) * 640, (k // 2) * 360))
        sheet.save(sheets / f"sheet_{tag}{n // 4:02d}.jpg", quality=92)
    print(f"{len(rows)} thumbnails in {thumbs}, contact sheets in {sheets}")


def old_rule_rejects(scorer, paths: list[Path], queries: list[str]) -> list[bool]:
    """The previous rule, for comparison: max similarity to OLD_PROMPTS minus the scene score
    (mean similarity to 'a photo of <query>') >= OLD_REJECT_MARGIN -> rejected."""
    from PIL import Image

    model, _, _, torch = scorer._load()
    out = []
    with torch.no_grad():
        logo_text = scorer._encode_texts(OLD_PROMPTS)
        for path, query in zip(paths, queries):
            image = scorer._encode_pictures([Image.open(path).convert("RGB")])
            scene = float((image @ scorer._encode_texts([f"a photo of {query}"]).T).mean())
            logo = float((image @ logo_text.T).max())
            out.append(logo - scene >= OLD_REJECT_MARGIN)
    return out


def rates(labels: list[bool], rejected: list[bool]) -> dict:
    tp = sum(1 for y, r in zip(labels, rejected) if y and r)
    fp = sum(1 for y, r in zip(labels, rejected) if not y and r)
    logos, clean = sum(labels), len(labels) - sum(labels)
    return {"rejected": tp + fp, "tp": tp, "fp": fp,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / logos if logos else None,
            "clean_kept": (clean - fp) / clean if clean else None}


def pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def score(images: Path, labels_csv: Path, scores_csv: Path | None) -> None:
    from app.config import get_settings
    from app.pipeline.rank import ClipScorer

    with labels_csv.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["label"] in ("logo", "no_logo")]
    paths = [images / f"pixabay_video_{r['pixabay_video_id']}.jpg" for r in rows]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        sys.exit(f"{len(missing)} labelled pictures are missing, e.g. {missing[0]} (run 'fetch' first)")
    scorer = ClipScorer()
    probs = scorer.logo_probs([None] * len(paths), paths)
    labels = [r["label"] == "logo" for r in rows]
    s = get_settings()
    print(f"{len(rows)} labelled pictures: {sum(labels)} logo, {len(rows) - sum(labels)} no_logo")
    print(f"logo prompts:    {s.logo_prompts}\nneutral prompts: {s.logo_neutral_prompts}\n")

    print("| reject if logo prob > | rejected | logo rejected (TP) | clean rejected (FP) | precision | recall | "
          "clean kept |")
    print("|---|---|---|---|---|---|---|")
    for t in [x / 100 for x in range(20, 100, 5)]:
        r = rates(labels, [p > t for p in probs])
        mark = " **(LOGO_REJECT_PROB)**" if abs(t - s.LOGO_REJECT_PROB) < 1e-9 else ""
        print(f"| {t:.2f}{mark} | {r['rejected']} | {r['tp']} | {r['fp']} | {pct(r['precision'])} | "
              f"{pct(r['recall'])} | {pct(r['clean_kept'])} |")
    old = rates(labels, old_rule_rejects(scorer, paths, [r["query"] for r in rows]))
    print(f"\nOLD rule (margin {OLD_REJECT_MARGIN}, scene = 'a photo of <query>'): rejected {old['rejected']}, "
          f"TP {old['tp']}, FP {old['fp']}, precision {pct(old['precision'])}, recall {pct(old['recall'])}, "
          f"clean kept {pct(old['clean_kept'])}")
    band = sum(1 for p in probs if s.LOGO_PENALTY_PROB < p <= s.LOGO_REJECT_PROB)
    print(f"penalty band ({s.LOGO_PENALTY_PROB} < prob <= {s.LOGO_REJECT_PROB}): {band} pictures")

    print("\nper picture, highest logo probability first:")
    order = sorted(range(len(rows)), key=lambda i: probs[i], reverse=True)
    for i in order:
        print(f"  {rows[i]['pixabay_video_id']:>10} {rows[i]['query']:12} {rows[i]['label']:8} {probs[i]:.3f}")
    if scores_csv:
        with scores_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["pixabay_video_id", "query", "label", "logo_prob"])
            for i in order:
                writer.writerow([rows[i]["pixabay_video_id"], rows[i]["query"], rows[i]["label"], probs[i]])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download thumbnails + contact sheets")
    f.add_argument("--out", type=Path, default=Path("data/calibration"))
    f.add_argument("--per-query", type=int, default=6)
    f.add_argument("--queries", default=",".join(QUERIES), help="comma-separated search words")
    f.add_argument("--tag", default="", help="prefix for this batch's contact sheet names")
    sc = sub.add_parser("score", help="score labelled thumbnails, print precision / recall per threshold")
    sc.add_argument("--images", type=Path, default=Path("data/calibration/thumbs"))
    sc.add_argument("--labels", type=Path, default=Path("testpack/calibration/logo_labels.csv"))
    sc.add_argument("--scores", type=Path, default=None, help="also write every picture's logo probability here")
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        fetch(args.out, args.per_query, [q.strip() for q in args.queries.split(",") if q.strip()], args.tag)
    else:
        score(args.images, args.labels, args.scores)
    return 0


if __name__ == "__main__":
    sys.exit(main())
