"""Render the 1080x1920 Instagram covers for the highlight reels.

Usage: python tools/make_covers.py [mm01 mm02 ...]
"""

import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS = os.path.join(ROOT, "clips-highlight")
OUT = os.path.join(ROOT, "covers-highlight")
FONT = "C:/Windows/Fonts/seguibl.ttf"

W, H = 1080, 1920
YELLOW = (255, 208, 0)
WHITE = (255, 255, 255)
KICKER = "SOLVED  ·  MARK MANSON"
MAX_W = 940
TEXT_BOTTOM = 1560  # keeps every headline inside Instagram's 4:5 grid crop
LINE_GAP = 26

COVERS = {
    "mm01": dict(t=30, lines=[("ONE VIDEO", WHITE), ("RUINED", YELLOW), ("HER LIFE", WHITE)]),
    "mm02": dict(t=5, lines=[("HER MOM SAID", WHITE), ("SHE DESERVED", YELLOW), ("IT", WHITE)]),
    "mm03": dict(t=11, lines=[("QUINCY JONES'", WHITE), ("DIVORCE ADVICE", YELLOW)]),
    "mm04": dict(t=22, lines=[("HE BOMBED", YELLOW), ("ONSTAGE", WHITE)]),
    "mm05": dict(t=34, lines=[("INSTAGRAM IS A", WHITE), ("SHAME MACHINE", YELLOW)]),
}


def grab(name, t):
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t),
         "-i", os.path.join(CLIPS, f"{name}.mp4"), "-frames:v", "1", path],
        check=True,
    )
    img = Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS)
    os.unlink(path)
    return img


def scrim(img):
    grad = Image.new("L", (1, H), 0)
    px = grad.load()
    for y in range(H):
        if y < 560:
            a = 0
        elif y < 1240:
            a = int(242 * ((y - 560) / 680) ** 1.2)
        else:
            a = 252
        px[0, y] = a
    grad = grad.resize((W, H))

    bed = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bed).rectangle([0, 1080, W, 1780], fill=252)
    grad = ImageChops.lighter(grad, bed.filter(ImageFilter.GaussianBlur(70)))

    img.paste(Image.new("RGB", (W, H), (8, 8, 10)), (0, 0), grad)
    return img


def fit(draw, text, max_w, max_size=172, min_size=70):
    size = max_size
    while size > min_size:
        f = ImageFont.truetype(FONT, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return ImageFont.truetype(FONT, min_size)


def render(name, cfg):
    img = scrim(grab(name, cfg["t"]))
    draw = ImageDraw.Draw(img)

    fonts = [fit(draw, text, MAX_W) for text, _ in cfg["lines"]]
    heights = [
        (lambda b: b[3] - b[1])(draw.textbbox((0, 0), text, font=f, stroke_width=6))
        for (text, _), f in zip(cfg["lines"], fonts)
    ]

    y = TEXT_BOTTOM - (sum(heights) + LINE_GAP * (len(cfg["lines"]) - 1))
    kfont = ImageFont.truetype(FONT, 40)
    ky = y - 74

    draw.text(((W - draw.textlength(KICKER, font=kfont)) / 2, ky), KICKER, font=kfont, fill=(236, 236, 240))
    draw.rectangle([W / 2 - 60, ky + 56, W / 2 + 60, ky + 60], fill=YELLOW)

    for (text, color), f, h in zip(cfg["lines"], fonts, heights):
        x = (W - draw.textlength(text, font=f)) / 2
        draw.text((x + 3, y + 8), text, font=f, fill=(0, 0, 0), stroke_width=7, stroke_fill=(0, 0, 0))
        draw.text((x, y), text, font=f, fill=color, stroke_width=7, stroke_fill=(8, 8, 10))
        y += h + LINE_GAP

    out = os.path.join(OUT, f"{name}.jpg")
    img.save(out, "JPEG", quality=92, optimize=True)
    return out


def main():
    names = sys.argv[1:] or list(COVERS)
    unknown = [n for n in names if n not in COVERS]
    if unknown:
        sys.exit(f"unknown cover(s): {', '.join(unknown)}")

    for name in names:
        out = render(name, COVERS[name])
        print(f"{name}: {os.path.getsize(out) // 1024} KB -> {out}")


if __name__ == "__main__":
    main()
