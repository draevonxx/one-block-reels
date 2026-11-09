"""Render designed 1080x1920 Instagram covers for the five Warzone reels.

Usage: python tools/make_wz_covers.py [wz1 wz2 ...]

The staged clips are letterboxed (picture band ~y480-1290) and carry a burned
hook card in the top bar for their whole runtime, so a raw frame grab is not a
cover. This grabs the hand-picked frame from the committed clip, erases the
top-bar card, and designs into the two black bars: a kicker up top, a big
two-line Impact headline in the bottom bar, both inside Instagram's 4:5
profile-grid crop (y285-1635). Accent is the yellow measured off the wz5 key
art's "PLAY FREE" / Warzone logo (255, 248, 21), not a picked colour.

Run with the interpreter that has Pillow: backend/.venv/Scripts/python.exe.
"""

import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS = os.path.join(ROOT, "clips-highlight")
OUT = os.path.join(ROOT, "covers-highlight")
FONT = "C:/Windows/Fonts/impact.ttf"
KFONT = "C:/Windows/Fonts/seguibl.ttf"

W, H = 1080, 1920
WHITE = (255, 255, 255)
ACCENT = (255, 248, 21)  # measured off the wz5 key-art PLAY FREE / logo yellow
KICKER = "CALL OF DUTY  ·  WARZONE"
MAX_W = 950
TEXT_BOTTOM = 1610  # inside the 4:5 grid crop (keeps y285-1635)
LINE_GAP = 20
# fixed by make_wz.py: 1080x810 picture at y=480 on a 1080x1920 canvas
BAND_TOP, BAND_BOT = 480, 1290

# t = the hand-picked frame each cover already showed, matched back to the clip
COVERS = {
    "wz1": dict(t=5.125, lines=[("STRAIGHT INTO", WHITE), ("HAJIN", ACCENT)]),
    "wz2": dict(t=6.875, lines=[("RAIN FIREFIGHT", WHITE), ("IN SEASON 1", ACCENT)]),
    "wz3": dict(t=10.125, lines=[("JUMPED FROM A", WHITE), ("BURNING TOWER", ACCENT)]),
    "wz4": dict(t=9.875, lines=[("LONG-RANGE", WHITE), ("SHOTS LAND DIFFERENT", ACCENT)]),
    "wz5": dict(t=1.875, lines=[("FREE TO PLAY", WHITE), ("RIGHT NOW", ACCENT)]),
}


def grab(name, t):
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t),
         "-i", os.path.join(CLIPS, f"{name}.mp4"), "-frames:v", "1", path],
        check=True,
    )
    img = Image.open(path).convert("RGB")
    os.unlink(path)
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)
    return img


def fit_size(draw, texts, max_size=172, min_size=88):
    """One common size so both headline lines match."""
    size = max_size
    while size > min_size:
        f = ImageFont.truetype(FONT, size)
        if all(draw.textlength(t, font=f) <= MAX_W for t in texts):
            return size
        size -= 2
    return min_size


def line_sprite(text, font, color):
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
    return sprite


def render(name, cfg):
    img = grab(name, cfg["t"])
    top, bot = BAND_TOP, BAND_BOT

    img = ImageEnhance.Color(img).enhance(1.2)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    luma = sum(img.crop((0, top, W, bot)).convert("L").resize((1, 1)).getdata())
    if luma < 95:
        img = ImageEnhance.Brightness(img).enhance(1.13)

    # erase the burned hook card: the whole top bar is black anyway
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, top], fill=(0, 0, 0))

    # gentle scrim over the bottom of the picture so the headline's top line reads
    grad = Image.new("L", (1, H), 0)
    gp = grad.load()
    for y in range(H):
        if y < bot - 320:
            a = 0
        elif y < bot:
            a = int(190 * ((y - (bot - 320)) / 320) ** 1.2)
        else:
            a = 0  # bottom bar is already black
        gp[0, y] = a
    img.paste(Image.new("RGB", (W, H), (8, 8, 10)), (0, 0), grad.resize((W, H)))
    draw = ImageDraw.Draw(img)

    # kicker in the top bar, grid-crop safe
    kf = ImageFont.truetype(KFONT, 36)
    kw = draw.textlength(KICKER, font=kf)
    ky = 352
    draw.text(((W - kw) / 2 + 3, ky + 4), KICKER, font=kf, fill=(0, 0, 0))
    draw.text(((W - kw) / 2, ky), KICKER, font=kf, fill=(240, 240, 244))
    draw.rectangle([W / 2 - 84, ky + 62, W / 2 + 84, ky + 69], fill=ACCENT)

    texts = [t for t, _ in cfg["lines"]]
    size = fit_size(draw, texts)
    font = ImageFont.truetype(FONT, size)
    heights = [(lambda b: b[3] - b[1])(draw.textbbox((0, 0), t, font=font, stroke_width=8))
               for t in texts]
    block = sum(heights) + LINE_GAP * (len(texts) - 1)
    y = TEXT_BOTTOM - block
    for (text, color), h in zip(cfg["lines"], heights):
        sprite = line_sprite(text, font, color)
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