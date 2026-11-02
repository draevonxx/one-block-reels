"""Render the 1080x1920 Instagram covers for the SEGA Virtua Fighter reels.

Usage: python tools/make_sega_covers.py [sv01 sv02 ...]

Frames come from the staged clips themselves, so a cover can never drift from
what the reel shows. Every t sits past the burned hook title (it clears at 4 s),
otherwise the cover would carry the same headline twice.
"""

import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS = os.path.join(ROOT, "clips-highlight")
OUT = os.path.join(ROOT, "covers-highlight")
FONT = "C:/Windows/Fonts/seguibl.ttf"

W, H = 1080, 1920
WHITE = (255, 255, 255)
ACCENT = (255, 99, 48)  # the logo's VF mark, pushed bright enough to hold on teal
KICKER = "VIRTUA FIGHTER  ·  CROSSROADS"
MAX_W = 900
TEXT_BOTTOM = 1470  # keeps every headline inside Instagram's 4:5 grid crop
LINE_GAP = 26
SHEAR = 0.07  # italic lean, so a still frame reads as motion

COVERS = {
    "sv01": dict(t=4.5, lines=[("MEET", WHITE), ("LIONEL", WHITE), ("RAFALE", ACCENT)]),
    "sv02": dict(t=11, lines=[("RAFALE", WHITE), ("VS CIELO", ACCENT)]),
    "sv03": dict(t=9, lines=[("STREET FIGHT", ACCENT), ("IN VILASAPARA", WHITE)]),
    "sv04": dict(t=5, lines=[("THE CITY", WHITE), ("OF VILASAPARA", ACCENT)]),
    "sv05": dict(t=4.5, lines=[("WHY HE", WHITE), ("CAME HERE", ACCENT)]),
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


def punch(img):
    img = ImageEnhance.Color(img).enhance(1.34)
    img = ImageEnhance.Contrast(img).enhance(1.16)
    img = ImageEnhance.Brightness(img).enhance(1.04)
    return img.filter(ImageFilter.UnsharpMask(radius=3, percent=140, threshold=3))


def scrim(img):
    """Shade only the lower half; the burned logo sits top-left and must stay readable."""
    grad = Image.new("L", (1, H), 0)
    px = grad.load()
    for y in range(H):
        if y < 620:
            a = 0
        elif y < 1180:
            a = int(238 * ((y - 620) / 560) ** 1.25)
        else:
            a = 250
        px[0, y] = a
    grad = grad.resize((W, H))

    bed = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bed).rectangle([0, 1120, W, 1760], fill=250)
    grad = ImageChops.lighter(grad, bed.filter(ImageFilter.GaussianBlur(70)))

    img.paste(Image.new("RGB", (W, H), (10, 8, 12)), (0, 0), grad)
    return img


def fit(draw, text, max_w, max_size=178, min_size=78):
    size = max_size
    while size > min_size:
        f = ImageFont.truetype(FONT, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return ImageFont.truetype(FONT, min_size)


def line_sprite(text, font, color):
    """One headline line: black drop, hard stroke, sheared for lean."""
    pad, stroke = 46, 8
    x0, y0, _, y1 = font.getbbox(text)
    w = int(font.getlength(text)) + pad * 2
    h = (y1 - y0) + pad * 2

    sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(sprite)
    ox, oy = pad - x0, pad - y0
    d.text((ox + 5, oy + 10), text, font=font, stroke_width=stroke,
           fill=(0, 0, 0, 220), stroke_fill=(0, 0, 0, 220))
    d.text((ox, oy), text, font=font, stroke_width=stroke,
           fill=color + (255,), stroke_fill=(6, 6, 10, 255))

    lean = int(h * SHEAR)
    out = sprite.transform((w + lean, h), Image.AFFINE,
                           (1, SHEAR, -SHEAR * h / 2, 0, 1, 0), resample=Image.BICUBIC)
    return out


def render(name, cfg):
    img = scrim(punch(grab(name, cfg["t"])))
    draw = ImageDraw.Draw(img)

    fonts = [fit(draw, text, MAX_W) for text, _ in cfg["lines"]]
    heights = [
        (lambda b: b[3] - b[1])(draw.textbbox((0, 0), text, font=f, stroke_width=8))
        for (text, _), f in zip(cfg["lines"], fonts)
    ]
    block = sum(heights) + LINE_GAP * (len(cfg["lines"]) - 1)
    top = TEXT_BOTTOM - block

    kfont = ImageFont.truetype(FONT, 34)
    kw = draw.textlength(KICKER, font=kfont)
    ky = top - 104
    draw.text(((W - kw) / 2 + 4, ky + 5), KICKER, font=kfont, fill=(0, 0, 0))
    draw.text(((W - kw) / 2, ky), KICKER, font=kfont, fill=(240, 240, 244))
    draw.rectangle([W / 2 - 84, ky + 60, W / 2 + 84, ky + 67], fill=ACCENT)

    y = top
    for (text, color), f, h in zip(cfg["lines"], fonts, heights):
        sprite = line_sprite(text, f, color)
        img.paste(sprite, ((W - sprite.width) // 2, y - 20), sprite)
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
