"""Tamil and Hindi (Devanagari) captions must show real letters, not empty boxes ("tofu").

The fonts ship with the code (app/fonts, Noto, SIL OFL), so these tests prove the same thing on
Windows, in Docker and on Modal. A missing font shows up three ways, and we check all three:
1. libass logs which font it picked: it must be our Noto font, not some other system font;
2. libass logs "Glyph 0x... not found" for every letter its font cannot draw: none may be a caption letter;
3. tofu boxes all look the same: different letters must give different pictures.
"""
import re

import numpy as np
import pytest

from app.config import get_settings
from app.pipeline import media
from app.pipeline.outputs import Cue
from app.pipeline.render import caption_font, script_language, subtitles_filter, write_segment_ass
from app.pipeline.scenes import Scene

TAMIL = "சேமிப்பு மிகவும் முக்கியம்"      # "saving is very important"
HINDI = "बचत करना बहुत ज़रूरी है"          # "saving is very important"
W, H = 960, 540


def test_font_is_picked_by_caption_language():
    assert caption_font("ta") == "Noto Sans Tamil"
    assert caption_font("hi") == "Noto Sans Devanagari"
    assert caption_font("en", "Saving money matters") == get_settings().CAPTION_FONT
    # No (or an unknown) language code: the script of the text decides.
    assert caption_font("", TAMIL) == "Noto Sans Tamil"
    assert caption_font(None, HINDI) == "Noto Sans Devanagari"
    assert script_language("plain English") is None


def render_caption(tmp_path, text: str, language: str) -> tuple[np.ndarray, str]:
    """One caption frame (white text on black), as a grey-level picture + the ffmpeg/libass log."""
    scene = Scene(0, 0.0, 2.0, text)
    ass = write_segment_ass([Cue(0.0, 2.0, [text], 0)], scene, W, H, tmp_path / "cap.ass", language)
    result = media.ffmpeg("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=1",
                          "-vf", subtitles_filter(ass.name, tmp_path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "frame.gray", cwd=tmp_path, loglevel="verbose")
    pixels = np.fromfile(tmp_path / "frame.gray", dtype=np.uint8).reshape(H, W)
    return pixels, result.stderr


@pytest.mark.parametrize("text, language, font_file", [(TAMIL, "ta", "NotoSansTamil-Bold"),
                                                       (HINDI, "hi", "NotoSansDevanagari-Bold")])
def test_caption_uses_our_font_and_finds_every_letter(tmp_path, text, language, font_file):
    pixels, log = render_caption(tmp_path, text, language)
    assert f"-> {font_file}" in log, "libass did not pick the bundled Noto font"
    missing = {int(code, 16) for code in re.findall(r"Glyph 0x([0-9A-Fa-f]+) not found", log)}
    assert not missing & {ord(ch) for ch in text}, f"letters without a glyph: {sorted(map(hex, missing))}"
    assert (pixels > 128).sum() > 500, "no caption text was drawn"


def _ink_box(pixels: np.ndarray) -> np.ndarray:
    ys, xs = np.nonzero(pixels > 128)
    return pixels[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


@pytest.mark.parametrize("letters, language", [("கமபட", "ta"), ("कमपट", "hi")])
def test_different_letters_draw_different_shapes(tmp_path, letters, language):
    """Tofu = the same empty box for every letter. Real glyphs differ in size and shape."""
    shapes = []
    for i, letter in enumerate(letters):
        folder = tmp_path / str(i)
        folder.mkdir()
        shapes.append(_ink_box(render_caption(folder, letter, language)[0]))
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            a, b = shapes[i], shapes[j]
            same = a.shape == b.shape and np.abs(a.astype(int) - b.astype(int)).mean() < 2
            assert not same, f"letters {letters[i]} and {letters[j]} look identical (tofu?)"
