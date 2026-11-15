"""Builds icon.icns for the Mac app from icon.ico (run on a Mac: python make_icns.py <out.icns>)."""
import os
import subprocess
import sys
import tempfile

from PIL import Image

im = Image.open("icon.ico")
im.size = max(im.ico.sizes())
im = im.convert("RGBA").resize((1024, 1024), Image.LANCZOS)
d = os.path.join(tempfile.mkdtemp(), "icon.iconset")
os.makedirs(d)
for s in (16, 32, 64, 128, 256, 512):
    im.resize((s, s), Image.LANCZOS).save(f"{d}/icon_{s}x{s}.png")
    if s <= 256:
        im.resize((s * 2, s * 2), Image.LANCZOS).save(f"{d}/icon_{s}x{s}@2x.png")
subprocess.run(["iconutil", "-c", "icns", d, "-o", sys.argv[1]], check=True)
