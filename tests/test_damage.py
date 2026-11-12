"""A badly worn copy of a photo must still be found (the thing that matters for old prints)."""
import os
import random
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

N = 30
POSSIBLE = core.SENSITIVITY["Normal"][1]
DUP = core.SENSITIVITY["Normal"][0]


def make_photo(seed):
    """Blobby random 'photo' with structure at several scales."""
    rng = np.random.default_rng(seed)
    base = Image.fromarray((rng.random((8, 10, 3)) * 255).astype("uint8")).resize((640, 480), Image.BICUBIC)
    detail = Image.fromarray((rng.random((40, 52, 3)) * 255).astype("uint8")).resize((640, 480), Image.BICUBIC)
    return Image.blend(base, detail, 0.35).filter(ImageFilter.GaussianBlur(2))


def fade(im):
    im = ImageEnhance.Contrast(im).enhance(0.45)
    return Image.blend(im, Image.new("RGB", im.size, (225, 220, 205)), 0.25)


def yellow(im):
    sep = ImageOps.colorize(ImageOps.grayscale(im), (40, 25, 5), (250, 235, 190))
    return Image.blend(fade(im), sep, 0.7)


def stains(im):
    rng = np.random.default_rng(1)
    w, h = im.size
    yy, xx = np.mgrid[:h, :w]
    m = np.zeros((h, w))
    for _ in range(4):
        cx, cy, s = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(0.08, 0.2) * max(w, h)
        m += np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s))
    m = np.clip(m, 0, 1)[..., None] * 0.55
    a = np.asarray(im, float)
    return Image.fromarray((a * (1 - m) + np.array([120, 80, 30.]) * m).astype("uint8"))


def scratches(im):
    rnd = random.Random(2)
    d = im.copy()
    dr = ImageDraw.Draw(d)
    w, h = im.size
    for _ in range(25):
        x = rnd.uniform(0, w)
        dr.line([(x, 0), (x + rnd.uniform(-30, 30), h)], fill=(235, 235, 235), width=1)
    for _ in range(300):
        x, y, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(0.5, 2.5)
        dr.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice([(250, 250, 250), (20, 20, 20)]))
    return d


def torn(im):
    d = im.copy()
    dr = ImageDraw.Draw(d)
    w, h = im.size
    dr.polygon([(0, 0), (w * 0.22, 0), (0, h * 0.2)], fill=(235, 230, 220))
    dr.polygon([(w, h), (w * 0.8, h), (w, h * 0.82)], fill=(235, 230, 220))
    return d


def border(im):  # white margin plus dark scanner-bed edge
    w = im.size[0]
    return ImageOps.expand(ImageOps.expand(im, int(0.07 * w), fill=(250, 250, 248)), int(0.03 * w), fill=(30, 30, 30))


def crop(im, p=0.08):
    w, h = im.size
    return im.crop((int(w * p), int(h * p), int(w * (1 - p)), int(h * (1 - p))))


def skew(im, a=3):
    return im.rotate(a, expand=True, fillcolor=(245, 245, 245), resample=Image.BICUBIC)


def worn(im):  # yellowed + scratched + slightly crooked + scanner border
    return border(skew(scratches(yellow(im)), 2))


def sideways_worn(im):
    return fade(im).rotate(90, expand=True)


DAMAGE = {
    "faded": fade, "yellowed": yellow, "stained": stains, "scratched": scratches,
    "torn_corners": torn, "scanner_border": border, "cropped": crop, "skewed": skew,
    "worn_combination": worn, "sideways_faded": sideways_worn,
}


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lib")
    folder = tmp / "scans"
    folder.mkdir()
    for i in range(N):
        make_photo(i).save(folder / f"p{i}.png")
    idx = core.Index(tmp / "i.db")
    idx.add_folder(str(folder))
    assert idx.rescan()["added"] == N
    return idx, tmp


@pytest.mark.parametrize("kind", DAMAGE)
def test_damaged_copy_is_found(kind, library):
    idx, tmp = library
    for i in (3, 17):  # two different photos
        q = tmp / f"q_{kind}_{i}.png"
        DAMAGE[kind](make_photo(i)).save(q)
        matches, _ = idx.search(str(q))
        score, path = matches[0]
        assert path.endswith(f"p{i}.png"), (kind, i, matches[:2])
        assert score >= POSSIBLE, (kind, i, score)


def test_scanner_border_and_fade_is_high_confidence(library):
    idx, tmp = library
    q = tmp / "q_bf.png"
    border(fade(make_photo(9))).save(q)
    score, path = idx.search(str(q))[0][0]
    assert path.endswith("p9.png") and score >= POSSIBLE


def test_unrelated_photo_scores_low(library):
    idx, tmp = library
    q = tmp / "q_new.png"
    make_photo(4242).save(q)
    score, _ = idx.search(str(q))[0][0]
    assert score < POSSIBLE


def test_blank_scan_matches_nothing(library):
    idx, tmp = library
    q = tmp / "blank.png"
    Image.new("RGB", (400, 300), (255, 255, 255)).save(q)
    matches, _ = idx.search(str(q))
    assert all(core.verdict(m[0]) == "looks new" for m in matches)


def test_old_index_is_upgraded(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE folders(path TEXT PRIMARY KEY); CREATE TABLE files(path TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL, ph BLOB, dh BLOB);")
    c.execute("INSERT INTO folders VALUES ('x')")
    c.execute("INSERT INTO files VALUES ('x/a.jpg','x',1,1.0,?,?)", (b"12345678", b"12345678"))
    c.commit(); c.close()
    idx = core.Index(db)
    assert idx.upgraded and idx.count() == 0 and idx.folders() == ["x"]


@pytest.mark.parametrize("kind", DAMAGE)
def test_pristine_scan_finds_worn_stored_copy(kind, tmp_path):
    """The reverse case: the library holds the worn-out print, the new scan is the nice one."""
    folder = tmp_path / "scans"
    folder.mkdir()
    for i in range(20):
        DAMAGE[kind](make_photo(i)).save(folder / f"p{i}.png")
    idx = core.Index(tmp_path / "i.db")
    idx.add_folder(str(folder))
    assert idx.rescan()["added"] == 20
    for i in (3, 14):
        q = tmp_path / f"new{i}.png"
        make_photo(i).save(q)
        matches, _ = idx.search(str(q))
        score, path = matches[0]
        assert path.endswith(f"p{i}.png"), (kind, i, matches[:2])
        assert score >= POSSIBLE, (kind, i, score)
