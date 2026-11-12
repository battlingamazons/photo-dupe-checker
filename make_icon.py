"""Regenerates icon.ico (run: python make_icon.py). Two overlapping photos = 'duplicates'."""
from PIL import Image, ImageDraw

N = 1024  # draw big, shrink for smooth edges


def card(size, fill, outline=None):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 10, fill=fill, outline=outline, width=size // 40)
    return im


def photo(size):
    im = card(size, (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    m = size // 9
    box = (m, m, size - m, size - m)
    d.rounded_rectangle(box, radius=size // 18, fill=(168, 213, 245, 255))             # sky
    d.ellipse((size * 0.58, size * 0.2, size * 0.78, size * 0.4), fill=(255, 200, 60, 255))  # sun
    b = size - m
    d.polygon([(m, b), (size * 0.38, size * 0.38), (size * 0.66, b)], fill=(70, 120, 90, 255))   # hill
    d.polygon([(size * 0.42, b), (size * 0.7, size * 0.5), (b, b)], fill=(46, 94, 70, 255))      # hill 2
    # re-round the bottom corners clipped by polygons
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=size // 18, fill=255)
    inner = im.copy()
    base = card(size, (255, 255, 255, 255))
    base.paste(inner, (0, 0), mask)
    return base


def build():
    icon = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(icon).rounded_rectangle((0, 0, N - 1, N - 1), radius=N // 5, fill=(31, 78, 140, 255))
    back = card(int(N * 0.56), (190, 214, 240, 255))
    front = photo(int(N * 0.56))
    icon.alpha_composite(back, (int(N * 0.12), int(N * 0.12)))
    icon.alpha_composite(front, (int(N * 0.32), int(N * 0.32)))
    return icon


if __name__ == "__main__":
    big = build()
    big.resize((256, 256), Image.LANCZOS).save("icon_preview.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    big.resize((256, 256), Image.LANCZOS).save("icon.ico", sizes=sizes)
    print("wrote icon.ico")
