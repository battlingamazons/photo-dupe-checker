"""Every variant below encodes the SAME picture; it must hash like the 8-bit reference."""
import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

tifffile = pytest.importorskip("tifffile")

LIKELY_DUP = core.SENSITIVITY["Normal"][0]


def picture():
    rng = np.random.default_rng(11)
    small = rng.random((30, 40, 3))
    return (np.kron(small, np.ones((10, 10, 1))) * 255).astype("uint8")  # 300x400 RGB


def dist(a, b):
    ha, hb = core.hash_file(str(a)), core.hash_file(str(b))
    return sum(bin(x ^ y).count("1") for p, q in zip(ha, hb) for x, y in zip(p, q))


@pytest.fixture(scope="module")
def ref(tmp_path_factory):
    p = tmp_path_factory.mktemp("fmt") / "ref.png"
    Image.fromarray(picture()).save(p)
    return p


def gray_of(a):
    return np.asarray(Image.fromarray(a).convert("L"))


TIFF_CASES = {
    "rgb8_none": lambda a: dict(data=a, photometric="rgb"),
    "rgb8_lzw": lambda a: dict(data=a, photometric="rgb", compression="lzw"),
    "rgb8_deflate": lambda a: dict(data=a, photometric="rgb", compression="zlib"),
    "rgb8_packbits": lambda a: dict(data=a, photometric="rgb", compression="packbits"),
    "rgb8_jpeg": lambda a: dict(data=a, photometric="rgb", compression="jpeg"),
    "rgb8_tiled": lambda a: dict(data=a, photometric="rgb", tile=(16, 16)),
    "rgb8_planar": lambda a: dict(data=a, photometric="rgb", planarconfig="separate"),
    "rgb8_planar_lzw": lambda a: dict(data=a, photometric="rgb", planarconfig="separate", compression="lzw"),
    "rgb8_bigtiff": lambda a: dict(data=a, photometric="rgb", bigtiff=True),
    "rgb8_bigendian": lambda a: dict(data=a, photometric="rgb", byteorder=">"),
    "rgb16": lambda a: dict(data=a.astype("uint16") * 257, photometric="rgb"),
    "rgb16_lzw_predictor": lambda a: dict(data=a.astype("uint16") * 257, photometric="rgb", compression="lzw", predictor=True),
    "rgb16_planar": lambda a: dict(data=a.astype("uint16") * 257, photometric="rgb", planarconfig="separate"),
    "rgba8": lambda a: dict(data=np.dstack([a, np.full(a.shape[:2], 255, "uint8")]), photometric="rgb", extrasamples=["unassalpha"]),
    "gray8": lambda a: dict(data=gray_of(a), photometric="minisblack"),
    "gray16": lambda a: dict(data=gray_of(a).astype("uint16") * 257, photometric="minisblack"),
    "gray_miniswhite": lambda a: dict(data=255 - gray_of(a), photometric="miniswhite"),
    "gray_float32": lambda a: dict(data=gray_of(a).astype("float32") / 255, photometric="minisblack"),
}


@pytest.mark.parametrize("name", TIFF_CASES)
def test_tiff_variant_matches_reference(name, ref, tmp_path):
    p = tmp_path / f"{name}.tif"
    kw = TIFF_CASES[name](picture())
    tifffile.imwrite(p, kw.pop("data"), **kw)
    assert dist(ref, p) <= LIKELY_DUP, name


def test_cmyk_tiff(ref, tmp_path):
    p = tmp_path / "cmyk.tif"
    Image.fromarray(picture()).convert("CMYK").save(p)
    assert dist(ref, p) <= LIKELY_DUP


def test_multipage_tiff_uses_first_page(ref, tmp_path):
    p = tmp_path / "multi.tif"
    tifffile.imwrite(p, picture(), photometric="rgb")
    tifffile.imwrite(p, picture()[::-1, ::-1], photometric="rgb", append=True)
    assert dist(ref, p) <= LIKELY_DUP


def test_pillow_multiframe_tiff(ref, tmp_path):
    p = tmp_path / "pil_multi.tif"
    a, b = Image.fromarray(picture()), Image.fromarray(picture()[::-1])
    a.save(p, save_all=True, append_images=[b], compression="tiff_lzw")
    assert dist(ref, p) <= LIKELY_DUP


def test_high_resolution_tiff(ref, tmp_path):
    p = tmp_path / "big.tif"
    big = np.kron(picture(), np.ones((8, 8, 1), dtype="uint8"))  # 2400x3200
    tifffile.imwrite(p, big, photometric="rgb", compression="zlib")
    assert dist(ref, p) <= LIKELY_DUP


@pytest.mark.parametrize("ext", [".png", ".bmp", ".gif", ".webp", ".jp2", ".tga", ".ppm", ".pcx", ".jpeg", ".avif"])
def test_other_pillow_formats(ext, ref, tmp_path):
    p = tmp_path / f"x{ext}"
    img = Image.fromarray(picture())
    if ext == ".gif":
        img = img.convert("P")
    img.save(p)
    assert core.is_image(p.name)
    assert dist(ref, p) <= LIKELY_DUP + 4


def test_pdf_first_page(ref, tmp_path):
    pytest.importorskip("pypdfium2")
    p = tmp_path / "x.pdf"
    Image.fromarray(picture()).save(p)
    assert dist(ref, p) <= LIKELY_DUP + 4


def test_heic(ref, tmp_path):
    ph = pytest.importorskip("pillow_heif")
    p = tmp_path / "x.heic"
    ph.from_pillow(Image.fromarray(picture())).save(p)
    assert dist(ref, p) <= LIKELY_DUP + 4
