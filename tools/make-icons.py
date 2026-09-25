"""Render resources/icons/*.png from Material Symbols.

Run: python3 tools/make-icons.py   (needs inkscape; dev-only, not shipped)

Kodi's own Default*.png names cover almost every folder in this add-on and are
preferred, because they resolve out of whichever skin the user runs and so
match their theme. These are only for the handful Kodi has no icon for.

Material Symbols are Apache-2.0. Icons downloaded from fonts.google.com keep
their original file name in tools/ as provenance; the rest are inlined below
because a single path is not worth a file.

Every icon is normalised to the same proportion Kodi's own Default*.png use,
so a bundled icon does not sit noticeably larger than a skin one beside it in
the same list. Measured across nine of Contuary's Default icons, the glyph's
larger dimension averages 52% of the canvas; a Material Symbol rendered
straight to PNG lands at 75-92%, which reads as oversized.
"""

import pathlib
import re
import subprocess
import sys

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
DEST = ROOT / "resources" / "icons"

# Material Symbols share this viewBox.
VIEWBOX = "0 -960 960 960"
FILL = "#E3E3E3"

# name -> single path d, in the Material Symbols coordinate space.
INLINE = {
    "sort": "M120-240v-80h240v80H120Zm0-200v-80h480v80H120Zm0-200v-80h720v80H120Z",
    "navigate_next": "M504-480 320-664l56-56 240 240-240 240-56-56 184-184Z",
    "login": (
        "M480-120v-80h280v-560H480v-80h280q33 0 56.5 23.5T840-760v560q0 33-23.5 "
        "56.5T760-120H480Zm-80-160-55-58 102-102H120v-80h327L345-622l55-58 200 "
        "200-200 200Z"
    ),
}

# name -> downloaded SVG in tools/, used as-is apart from the fill.
FROM_FILE = {
    "books": "auto_stories_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg",
    "continue": "books_movies_and_music_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg",
}

SIZE = 256
# Kodi's own Default*.png put the glyph at ~52% of the canvas on its larger
# dimension, averaged over nine of them. Matching that is what stops a bundled
# icon sitting noticeably larger than a skin one beside it: measured in the
# rendered list, 0.52 gives ~21px against the skin's 20-22px, where a Material
# Symbol rendered straight to PNG (75-92%) gave 29-31px.
#
# If you change this, ReloadSkin() before believing a screenshot. Kodi holds
# loaded textures in memory for the session, so an add-on disable/enable
# bounce redraws the list with the *old* images and nothing says so.
GLYPH_RATIO = 0.52
# Render larger than SIZE first: the glyph is then downscaled into place, so
# the edges stay clean instead of being resampled up.
RENDER_SIZE = SIZE * 3


def render(name, svg_text):
    DEST.mkdir(parents=True, exist_ok=True)
    tmp = DEST / (name + ".svg")
    raw = DEST / (name + ".raw.png")
    tmp.write_text(svg_text)
    try:
        subprocess.run(
            [
                "inkscape",
                str(tmp),
                "--export-type=png",
                "-w",
                str(RENDER_SIZE),
                "-h",
                str(RENDER_SIZE),
                "--export-filename={}".format(raw),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _normalise(raw, DEST / (name + ".png"))
    finally:
        tmp.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
    out = DEST / (name + ".png")
    print("  {:<16} {:>6} bytes  {}".format(name, out.stat().st_size, _describe(out)))


def _normalise(source, target):
    """Crop to the glyph, scale it to GLYPH_RATIO, centre it on a SIZE canvas.

    Cropping first means the source SVG's own padding — which differs between
    a downloaded icon and an inlined path — stops mattering. What is measured
    is the ink, which is what the eye compares.
    """
    image = Image.open(source).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        image.resize((SIZE, SIZE), Image.LANCZOS).save(target)
        return
    glyph = image.crop(bbox)
    longest = max(glyph.size)
    scale = (SIZE * GLYPH_RATIO) / longest
    glyph = glyph.resize(
        (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(glyph, ((SIZE - glyph.width) // 2, (SIZE - glyph.height) // 2), glyph)
    canvas.save(target)


def _describe(path):
    image = Image.open(path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return "empty"
    return "glyph {}% x {}%".format(
        round((bbox[2] - bbox[0]) / SIZE * 100), round((bbox[3] - bbox[1]) / SIZE * 100)
    )


def main():
    for name, d in sorted(INLINE.items()):
        render(
            name,
            '<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" '
            'viewBox="{v}"><path fill="{f}" d="{d}"/></svg>'.format(
                s=SIZE, v=VIEWBOX, f=FILL, d=d
            ),
        )
    for name, filename in sorted(FROM_FILE.items()):
        source = TOOLS / filename
        if not source.exists():
            sys.exit("missing source SVG: {}".format(source))
        svg = source.read_text()
        # Normalise size and fill; the downloads come at 24px and whatever
        # colour was picked in the web UI.
        svg = re.sub(r'height="[^"]*"', 'height="{}px"'.format(SIZE), svg, count=1)
        svg = re.sub(r'width="[^"]*"', 'width="{}px"'.format(SIZE), svg, count=1)
        svg = re.sub(r'fill="[^"]*"', 'fill="{}"'.format(FILL), svg, count=1)
        render(name, svg)


if __name__ == "__main__":
    main()
