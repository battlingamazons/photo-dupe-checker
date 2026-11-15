"""Matching engine: perceptual hashing plus a small on-disk index.

No GUI code lives here so it can be tested without a display.
"""
import os
import hashlib
import zlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # big archival TIFFs are expected

# Formats Pillow reads on its own.
EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".tif", ".tiff", ".bmp", ".dib",
    ".gif", ".webp", ".jp2", ".j2k", ".jpx", ".tga", ".pcx", ".ppm", ".pgm",
    ".pbm", ".psd", ".ico", ".avif",
}
RAW_EXTS = {
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".mrw",
    ".x3f", ".iiq",
}
HEIF_EXTS = {".heic", ".heif", ".hif"}
PDF_EXTS = {".pdf"}

# Optional readers: if one is missing the app still works, just without it.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    EXTS |= HEIF_EXTS
except Exception:  # noqa: BLE001
    pillow_heif = None
try:
    import pypdfium2

    EXTS |= PDF_EXTS
except Exception:  # noqa: BLE001
    pypdfium2 = None
try:
    import tifffile
except Exception:  # noqa: BLE001
    tifffile = None
try:
    import rawpy

    EXTS |= RAW_EXTS
except Exception:  # noqa: BLE001
    rawpy = None

# Match score runs 0..1 (1 = identical). (likely duplicate, possible match) cut-offs.
SENSITIVITY = {
    "Strict": (0.85, 0.72),
    "Normal": (0.80, 0.66),
    "Generous": (0.75, 0.60),
}
SCHEMA_VERSION = 6


def file_hash(path):
    """SHA-256 of the file's bytes: identical files (copies) give identical hashes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_db_path():
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    folder = root / ("PhotoDupeChecker" if base else ".photodupechecker")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "index.db"


def is_image(name):
    return os.path.splitext(name)[1].lower() in EXTS


def _is_planar_tiff(path):
    """True for TIFFs stored one colour plane at a time (PlanarConfiguration=2)."""
    try:
        with Image.open(path) as im:
            return im.tag_v2.get(284) == 2
    except Exception:  # noqa: BLE001
        return False


def _load_pdf(path, max_side):
    pdf = pypdfium2.PdfDocument(path)
    try:
        page = pdf[0]  # first page only
        w, h = page.get_size()
        scale = max(0.5, min(4.0, max_side / max(w, h, 1)))
        return page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()


def _load_raw(path, max_side):
    with rawpy.imread(path) as raw:
        try:  # the embedded preview is far faster than developing the raw data
            t = raw.extract_thumb()
            import io

            if t.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(t.data))
                img.load()
                return img.convert("RGB")
            return Image.fromarray(t.data).convert("RGB")
        except Exception:  # noqa: BLE001  (no usable preview)
            return Image.fromarray(raw.postprocess(half_size=True, use_camera_wb=True))


def _load_tiff_fallback(path, max_side):
    """Read TIFFs Pillow can't (planar layout, odd bit depths, exotic compression)."""
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]  # first page only
        a = page.asarray()
        photo = page.photometric
    while a.ndim > 3:
        a = a[0]
    if a.ndim == 3 and a.shape[0] <= 5 and a.shape[0] < a.shape[-1]:
        a = np.moveaxis(a, 0, -1)  # planar (channels, h, w) -> (h, w, channels)
    step = max(1, max(a.shape[0], a.shape[1]) // (max_side * 2))
    a = a[::step, ::step]
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    if a.dtype == np.bool_:
        a = a.astype(np.uint8) * 255
    if a.dtype != np.uint8:
        f = a.astype(np.float64)
        lo, hi = float(f.min()), float(f.max())
        a = ((f - lo) / max(hi - lo, 1e-12) * 255).astype(np.uint8)
    if "MINISWHITE" in str(photo):
        a = 255 - a
    if a.ndim == 2:
        return Image.fromarray(a, "L")
    c = a.shape[2]
    if c >= 4 and "SEPARATED" in str(photo):
        return Image.fromarray(a[:, :, :4], "CMYK").convert("RGB")
    if c == 2:
        return Image.fromarray(a[:, :, 0], "L")
    return Image.fromarray(a[:, :, :3], "RGB")


def _load_pillow(path, max_side):
    with Image.open(path) as im:
        try:
            im.draft("RGB", (max_side * 2, max_side * 2))  # fast path for JPEG
        except Exception:  # noqa: BLE001
            pass
        im = ImageOps.exif_transpose(im)
        if im.mode in ("P", "PA", "LA", "RGBA"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)  # transparency -> white
        elif im.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
            a = np.asarray(im, dtype=np.float64)
            im = Image.fromarray(((a - a.min()) / max(a.max() - a.min(), 1e-12) * 255).astype("uint8"))
        elif im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        return im.copy()


def load_image(path, max_side=512):
    """Open any supported file as a (possibly shrunken) RGB/L PIL image.

    Multi-page files (PDF, TIFF) use their first page.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS and pypdfium2:
        img = _load_pdf(path, max_side)
    elif ext in RAW_EXTS and rawpy:
        img = _load_raw(path, max_side)
    elif ext in (".tif", ".tiff") and tifffile:
        if _is_planar_tiff(path):  # Pillow can silently mis-decode these
            img = _load_tiff_fallback(path, max_side)
        else:
            try:
                img = _load_pillow(path, max_side)
            except Exception:  # noqa: BLE001  (odd bit depth, exotic compression, ...)
                img = _load_tiff_fallback(path, max_side)
    else:
        img = _load_pillow(path, max_side)
    img = img.convert("RGB") if img.mode not in ("L", "RGB") else img
    # Shrink early so huge TIFFs don't make later steps slow.
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


# ---------------------------------------------------------------------------
# Matching: each photo is reduced to a "band-pass" thumbnail that ignores overall
# brightness, fading, yellowing and stains and keeps the picture's structure.
# A new scan is compared at many slight zoom/shift/rotation positions, so scanner
# borders, crops, skew and torn corners don't stop it from lining up with the
# stored copy. Each stored photo keeps four views (whole picture + three centre
# crops of increasing tightness) so a border/frame of any common width on EITHER
# copy can be cut away before comparing.
# ---------------------------------------------------------------------------
_SZ = 96            # working resolution
_PAD = 48           # edge padding so zooming out has something to sample
_T = 32             # descriptor is _T x _T
DB_WINDOWS = (1.0, 0.9, 0.8, 0.7)
QUERY_SCALES = (0.74, 0.8, 0.86, 0.92, 0.96, 1.0)  # zoom IN only: zooming out would drag frames into view
QUERY_OFFSETS = (-0.04, 0.0, 0.04)
_LAM, _MU = 0.25, 0.5   # small penalty so exact alignment wins over a lucky offset


def _gauss_matrix(n, sigma):
    i = np.arange(n)
    m = np.exp(-((i[:, None] - i[None, :]) ** 2) / (2 * sigma**2))
    return (m / m.sum(1, keepdims=True)).astype(np.float32)


_MH, _ML = _gauss_matrix(_T, 0.8), _gauss_matrix(_T, 2.2)
# Weight each pixel by distance from the centre: full weight in the middle, fading to
# ZERO towards the edge. Borders, scanner-bed edges and frames live there, and they look
# alike across different photos from the same scanner - they must never drive a match.
_FLAT, _END = 0.20, 0.36


def _taper():
    r = np.abs((np.arange(_T) + 0.5) / _T - 0.5)
    w1 = np.where(r <= _FLAT, 1.0, np.where(r >= _END, 0.0, np.cos(np.pi / 2 * (r - _FLAT) / (_END - _FLAT)) ** 2))
    return np.outer(w1, w1).astype(np.float32)


_W = _taper()


_FRAME_MAX = 0.22   # a border may take up to this fraction of each side
_FLAT_TOL = 3.0     # grey-level spread below which an edge line counts as "flat"


def _flat_run(a):
    """How many lines from the top edge are flat (one colour): a white/black border or scanner bed."""
    n, m = a.shape
    lo, hi = int(0.2 * m), int(0.8 * m)
    k = 0
    while k < int(_FRAME_MAX * n):
        seg = a[k, lo:hi]
        if np.percentile(np.abs(seg - np.median(seg)), 80) < _FLAT_TOL:
            k += 1
        else:
            break
    return k


def strip_frame(a):
    """Erase flat borders/scanner-bed edges so only the photograph itself is described.

    The border is overwritten by repeating the first real picture line outward, so the
    picture keeps its position and no artificial frame edge remains to be matched.
    """
    out = a.copy()
    n = a.shape[0]
    kt, kb, kl, kr = _flat_run(a), _flat_run(a[::-1]), _flat_run(a.T), _flat_run(a.T[::-1])
    if kt >= 2:
        out[:kt] = out[kt]
    if kb >= 2:
        out[n - kb :] = out[n - kb - 1]
    if kl >= 2:
        out[:, :kl] = out[:, kl : kl + 1]
    if kr >= 2:
        out[:, n - kr :] = out[:, n - kr - 1 : n - kr]
    return out


def crop_image(img, crop):
    """crop = (left, top, right, bottom) as fractions 0..1 of the picture; None = whole picture."""
    if not crop:
        return img
    w, h = img.size
    l, t, r, b = (min(max(float(v), 0.0), 1.0) for v in crop)
    box = (int(round(l * w)), int(round(t * h)), int(round(r * w)), int(round(b * h)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return img
    return img.crop(box)


def content_box(img, blank_hi=235, blank_lo=20, min_frac=0.02):
    """Fractions (l, t, r, b) of the area that is not plain white / scanner-black margin.
    None when there is nothing worth trimming."""
    g = np.asarray(img.convert("L").copy().resize((min(img.size[0], 400), min(img.size[1], 400))), np.uint8)
    ink = (g < blank_hi) & (g > blank_lo)
    rows = np.nonzero(ink.mean(axis=1) > min_frac)[0]
    cols = np.nonzero(ink.mean(axis=0) > min_frac)[0]
    if len(rows) < 8 or len(cols) < 8:
        return None
    h, w = g.shape
    box = (cols[0] / w, rows[0] / h, (cols[-1] + 1) / w, (rows[-1] + 1) / h)
    if box[0] < 0.02 and box[1] < 0.02 and box[2] > 0.98 and box[3] > 0.98:
        return None
    return box


def auto_crop(path):
    """Where the photo itself is within a scan (blank paper / scanner background left out), or None."""
    try:
        return content_box(load_image(path, max_side=512))
    except Exception:  # noqa: BLE001
        return None


def _prep(path, crop=None):
    g = crop_image(load_image(path, max_side=1600 if crop else 512), crop).convert("L").resize((_SZ, _SZ), Image.LANCZOS)
    return np.pad(strip_frame(np.asarray(g, dtype=np.float32)), _PAD, mode="edge")


def _window(P, s, dx, dy):
    """Descriptor of the picture zoomed by s and shifted by (dx, dy) fractions."""
    im = Image.fromarray(np.ascontiguousarray(P), "F")
    sc = _SZ * s / _T
    c = _PAD + _SZ / 2 + dx * _SZ - sc * _T / 2
    f = _PAD + _SZ / 2 + dy * _SZ - sc * _T / 2
    w = np.asarray(im.transform((_T, _T), Image.AFFINE, (sc, 0, c, 0, sc, f), resample=Image.BILINEAR), np.float32)
    if w.std() < 1.5:  # blank / featureless: matches nothing
        return np.zeros(_T * _T, np.float32)
    b = _MH @ w @ _MH.T - _ML @ w @ _ML.T
    sd = 1.4826 * np.median(np.abs(b)) + 1e-6
    b = np.clip(b / sd, -3, 3) * _W
    b = b - (b.sum() / _W.sum()) * _W
    n = np.linalg.norm(b)
    return (b / n).ravel() if n > 1e-4 else np.zeros(_T * _T, np.float32)


def describe(path):
    """What gets stored for an indexed photo: (len(DB_WINDOWS), T*T) float16."""
    P = _prep(path)
    return np.stack([_window(P, s, 0, 0) for s in DB_WINDOWS]).astype(np.float16)


def describe_full(path):
    """Everything stored for an indexed photo: (4 windows, coarse crop windows, small picture)."""
    g = _load_gray(path)
    P = np.pad(strip_frame(np.asarray(g.resize((_SZ, _SZ), Image.LANCZOS), dtype=np.float32)), _PAD, mode="edge")
    d4 = np.stack([_window(P, s, 0, 0) for s in DB_WINDOWS]).astype(np.float16)
    big = _big_from_gray(g)
    return d4, _crop_desc(big), _thumb_blob(big)


def _query(path, crop=None):
    """All the positions a new scan is tried at (4 rotations x zooms x shifts)."""
    P = _prep(path, crop)
    vs, scales, shifts = [], [], []
    for rot in range(4):
        Pr = np.rot90(P, rot)
        for s in QUERY_SCALES:
            for dx in QUERY_OFFSETS:
                for dy in QUERY_OFFSETS:
                    vs.append(_window(Pr, s, dx, dy))
                    scales.append(s)
                    shifts.append(abs(dx) + abs(dy))
    return np.array(vs, np.float32), np.array(scales, np.float32), np.array(shifts, np.float32)


def _best_scores(V, scales, shifts, db_views):
    """db_views: one (N, T*T) float32 array per DB window. Returns best score per photo."""
    best = None
    for D, sd in zip(db_views, DB_WINDOWS):
        pen = (_LAM * np.abs(np.log(scales / sd)) + _MU * shifts).astype(np.float32)
        for i in range(0, len(V), 128):
            c = D @ V[i : i + 128].T
            c -= pen[i : i + 128]
            m = c.max(axis=1)
            best = m if best is None else np.maximum(best, m)
    return best


def similarity(path_a, path_b):
    """Match score (0..1) between two photo files; mainly for tests."""
    V, s, sh = _query(path_a)
    d = describe(path_b).astype(np.float32)
    return float(_best_scores(V, s, sh, [d[0:1], d[1:2]])[0])


# ---------------------------------------------------------------------------
# Crop-aware matching. Two scans of one photo can differ in how tightly they are cropped (one
# edge trimmed more than another, a corner cut off...). The descriptors above only cope with a
# modest centred difference, so this second pass looks for "one picture is a window cut out of the
# other". Stage 1: every stored photo keeps 64 coarse windows (trimmed by 0 / 12 / 24 % on each
# side, independently), and the new scan is tried whole and as many windows of its own. Stage 2:
# the CROP_K best candidates are lined up precisely at higher resolution; only a precise fit
# counts, which keeps unrelated photos from being reported.
# ---------------------------------------------------------------------------
_CSZ, _CPAD = 192, 96      # stored picture size (and edge padding) for crop matching
_CT, _FT = 16, 48          # coarse (all photos) and fine (shortlist) descriptor sizes
CROP_K = 150               # photos looked at more closely after the coarse pass
CROP_REFINE = 40           # ... and of those, how many get the full precise alignment
_CA1, _CA2 = 0.2, 0.15     # penalty per unit of cropping (coarse / precise)
_CMIN = 0.45               # a window must keep at least this much of each side
_cfgs = {}


def _cfg(T):
    if T not in _cfgs:
        r = np.abs((np.arange(T) + 0.5) / T - 0.5)
        w1 = np.where(r <= _FLAT, 1.0, np.where(r >= _END, 0.0, np.cos(np.pi / 2 * (r - _FLAT) / (_END - _FLAT)) ** 2))
        _cfgs[T] = (_gauss_matrix(T, 0.8 * T / 32), _gauss_matrix(T, 2.2 * T / 32), np.outer(w1, w1).astype(np.float32))
    return _cfgs[T]


class _Big:
    """A stored-size picture (padded) from which windows of any shape can be cut."""

    def __init__(self, P):
        self.P = P
        self.im = Image.fromarray(np.ascontiguousarray(P), "F")

    def window(self, box, T):
        x0, x1, y0, y1 = box
        scx, scy = _CSZ * (x1 - x0) / T, _CSZ * (y1 - y0) / T
        cx = _CPAD + _CSZ * (x0 + x1) / 2 - scx * T / 2
        cy = _CPAD + _CSZ * (y0 + y1) / 2 - scy * T / 2
        w = np.asarray(self.im.transform((T, T), Image.AFFINE, (scx, 0, cx, 0, scy, cy), resample=Image.BILINEAR), np.float32)
        if w.std() < 1.5:
            return np.zeros(T * T, np.float32)
        MH, ML, W = _cfg(T)
        b = MH @ w @ MH.T - ML @ w @ ML.T
        b = np.clip(b / (1.4826 * np.median(np.abs(b)) + 1e-6), -3, 3) * W
        b = b - (b.sum() / W.sum()) * W
        n = np.linalg.norm(b)
        return (b / n).ravel() if n > 1e-4 else np.zeros(T * T, np.float32)


def _axis(starts, ends):
    return [(a, b) for a in starts for b in ends if b - a >= 0.5]


_AX_DB = [p for p in _axis((0, 0.12, 0.24), (1, 0.88, 0.76)) if p != (0.24, 0.76)]
_BOXES_DB = [(a, b, c, d) for (a, b) in _AX_DB for (c, d) in _AX_DB]                 # windows kept per stored photo
_FULL_DB = _BOXES_DB.index((0, 1, 0, 1))
_AX_Q = _axis((0, 0.06, 0.12, 0.18, 0.24), (1, 0.94, 0.88, 0.82, 0.76))
_BOXES_Q = [(a, b, c, d) for (a, b) in _AX_Q for (c, d) in _AX_Q]                    # windows tried on the new scan
_AX_J = [(0.5 + sh - sc / 2, 0.5 + sh + sc / 2) for sh, sc in ((-0.04, 1.0), (0, 1.0), (0.04, 1.0), (0, 0.94))]
_BOXES_J = [(a, b, c, d) for (a, b) in _AX_J for (c, d) in _AX_J]                    # the whole scan, nudged a little


def _trim(b):
    return b[0] + (1 - b[1]) + b[2] + (1 - b[3])


_TR_DB = np.array([_trim(b) for b in _BOXES_DB], np.float32)
_TR_Q = np.array([_trim(b) for b in _BOXES_Q], np.float32)


def _big_from_gray(g):
    g = g.resize((_CSZ, _CSZ), Image.LANCZOS)
    return _Big(np.pad(strip_frame(np.asarray(g, dtype=np.float32)), _CPAD, mode="edge"))


def _crop_desc(big):
    return np.stack([big.window(b, _CT) for b in _BOXES_DB]).astype(np.float16)


def _thumb_blob(big):
    return zlib.compress(np.ascontiguousarray(big.P[_CPAD:-_CPAD, _CPAD:-_CPAD]).astype(np.uint8).tobytes(), 3)


def _big_from_blob(blob):
    a = np.frombuffer(zlib.decompress(blob), np.uint8).reshape(_CSZ, _CSZ).astype(np.float32)
    return _Big(np.pad(a, _CPAD, mode="edge"))


def _load_gray(path, crop=None):
    return crop_image(load_image(path, max_side=1600 if crop else 512), crop).convert("L")


def _crop_query(path, crop=None):
    """What the new scan is tried as: (whole, nudged) vectors and (windows) vectors for each of 4 turns."""
    big = _big_from_gray(_load_gray(path, crop))
    bigs, qj, qb = [], [], []
    for rot in range(4):
        b = big if rot == 0 else _Big(np.rot90(big.P, rot))
        bigs.append(b)
        qj.append(np.stack([b.window(x, _CT) for x in _BOXES_J]))
        qb.append(np.stack([b.window(x, _CT) for x in _BOXES_Q]))
    return bigs, np.concatenate(qj).astype(np.float32), np.concatenate(qb).astype(np.float32)


def _crop_stage1(cviews, qj, qb):
    """Coarse crop-tolerant score of the new scan against every stored photo (N,)."""
    N = cviews[0].shape[0]
    best = np.full(N, -9.0, np.float32)
    tq = np.tile(_TR_Q, 4)
    for i in range(0, N, 8192):
        sl = slice(i, i + 8192)
        m = best[sl]
        for w, D in enumerate(cviews):
            d = D[sl].astype(np.float32)
            m = np.maximum(m, (d @ qj.T).max(axis=1) - _CA1 * _TR_DB[w])      # whole scan fits a window of the stored photo
        d = cviews[_FULL_DB][sl].astype(np.float32)
        m = np.maximum(m, ((d @ qb.T) - _CA1 * tq[None, :]).max(axis=1))       # a window of the scan fits the whole stored photo
        best[sl] = m
    return best


def _refine(Q, S, qb, sb):
    """Line two pictures up precisely: slide the window edges of both until the fit stops improving."""
    def ok(b):
        return b[1] - b[0] >= _CMIN and b[3] - b[2] >= _CMIN and min(b[0], b[2]) >= -0.08 and max(b[1], b[3]) <= 1.08

    qb, sb = list(qb), list(sb)
    vq, vs = Q.window(qb, _FT), S.window(sb, _FT)
    cur = float(vq @ vs) - _CA2 * (_trim(qb) + _trim(sb))
    for step in (0.04, 0.02, 0.01):
        for _ in range(3):
            moved = False
            for edge in range(8):
                mine, other_box, other_v, pic = (qb, sb, vs, Q) if edge < 4 else (sb, qb, vq, S)
                e = edge % 4
                for d in (-step, step):
                    nb = list(mine)
                    nb[e] += d
                    if not ok(nb):
                        continue
                    nv = pic.window(nb, _FT)
                    val = float(nv @ other_v) - _CA2 * (_trim(nb) + _trim(other_box))
                    if val > cur + 1e-4:
                        cur, mine[e], moved = val, nb[e], True
                        if edge < 4:
                            vq = nv
                        else:
                            vs = nv
            if not moved:
                break
    return cur


def crop_report(s2):
    """Map a precise-fit score onto the 0..1 match scale: below 0.80 nothing is reported."""
    return 0.0 if s2 < 0.80 else 0.60 + min((s2 - 0.80) / 0.15, 1.0) * 0.40


def _crop_picks(qj, qb, dc, n=8):
    """The n most promising ways of laying the scan on one stored photo: [(coarse score, turn, scan box, stored box)]."""
    nj, nq = len(_BOXES_J), len(_BOXES_Q)
    a = (qj @ dc.T) - _CA1 * _TR_DB[None, :]
    b = (qb @ dc[_FULL_DB]) - _CA1 * np.tile(_TR_Q, 4)
    out = []
    flat = a.ravel()
    for k in np.argpartition(-flat, n - 1)[:n]:
        r, w = divmod(int(k), a.shape[1])
        out.append((float(flat[k]), r // nj, _BOXES_J[r % nj], _BOXES_DB[w]))
    for k in np.argpartition(-b, n - 1)[:n]:
        out.append((float(b[k]), int(k) // nq, _BOXES_Q[int(k) % nq], _BOXES_DB[_FULL_DB]))
    out.sort(key=lambda t: -t[0])
    return out[:n]


class Index:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or default_db_path())
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY)")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        self.upgraded = False
        if version < SCHEMA_VERSION:  # older versions used a different matching method
            had = self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            if had:
                self.upgraded = self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0
                self._db.execute("DROP TABLE files")
            self._db.execute("DROP TABLE IF EXISTS thumbs")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL,
                desc BLOB NOT NULL, sha TEXT, cdesc BLOB);
            CREATE TABLE IF NOT EXISTS thumbs (path TEXT PRIMARY KEY, img BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
            """
        )
        if "sha" not in [r[1] for r in self._db.execute("PRAGMA table_info(files)")]:
            self._db.execute("ALTER TABLE files ADD COLUMN sha TEXT")  # index made before exact matching
        self._db.execute("CREATE INDEX IF NOT EXISTS files_sha ON files(sha)")
        self._db.commit()
        self._cache = None
        self.last_cropped = set()
        self.crop_error = None  # set when the cropped-copy check could not run

    # ---- folders ----
    def folders(self):
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT path FROM folders ORDER BY path")]

    def add_folder(self, path):
        path = os.path.normpath(path)
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO folders(path) VALUES (?)", (path,))
            self._db.commit()
        return path

    def remove_folder(self, path):
        with self._lock:
            self._db.execute("DELETE FROM thumbs WHERE path IN (SELECT path FROM files WHERE folder = ?)", (path,))
            self._db.execute("DELETE FROM files WHERE folder = ?", (path,))
            self._db.execute("DELETE FROM folders WHERE path = ?", (path,))
            self._db.commit()
            self._cache = None

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # ---- indexing ----
    def rescan(self, progress=None, cancel=None):
        """Index new/changed files in every folder; drop files that were deleted.

        A folder that can't be reached (e.g. unplugged drive) is skipped and
        left untouched, so its entries are never wiped by mistake.
        """
        summary = {"added": 0, "removed": 0, "failed": [], "unavailable": [], "skipped": 0}
        for folder in self.folders():
            if cancel and cancel.is_set():
                break
            if not os.path.isdir(folder):
                summary["unavailable"].append(folder)
                continue
            with self._lock:
                rows = self._db.execute(
                    "SELECT path, size, mtime, sha FROM files WHERE folder = ?", (folder,)
                ).fetchall()
                known = {p: (s, m) for p, s, m, _h in rows}
                unhashed = {p for p, _s, _m, h in rows if h is None}
            seen, todo, backfill = set(), [], []
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    if not is_image(name):
                        continue
                    p = os.path.normpath(os.path.join(root, name))
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    seen.add(p)
                    if known.get(p) == (st.st_size, st.st_mtime):
                        summary["skipped"] += 1
                        if p in unhashed:
                            backfill.append(p)
                    else:
                        todo.append((p, st.st_size, st.st_mtime))
                if cancel and cancel.is_set():
                    break
            if not (cancel and cancel.is_set()):
                gone = [p for p in known if p not in seen]
                with self._lock:
                    self._db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
                    self._db.executemany("DELETE FROM thumbs WHERE path = ?", [(p,) for p in gone])
                    self._db.commit()
                summary["removed"] += len(gone)

            def work(item):
                if cancel and cancel.is_set():
                    return item, None, "cancelled"
                try:
                    d4, cd, th = describe_full(item[0])
                    return item, (d4.tobytes(), file_hash(item[0]), cd.tobytes(), th), None
                except Exception as e:  # corrupt/unsupported file
                    return item, None, str(e)

            done = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
                for item, desc, err in pool.map(work, todo):
                    done += 1
                    if desc:
                        with self._lock:
                            self._db.execute(
                                "INSERT OR REPLACE INTO files (path, folder, size, mtime, desc, sha, cdesc) VALUES (?,?,?,?,?,?,?)",
                                (item[0], folder, item[1], item[2], desc[0], desc[1], desc[2]),
                            )
                            self._db.execute("INSERT OR REPLACE INTO thumbs (path, img) VALUES (?,?)", (item[0], desc[3]))
                            if done % 500 == 0:
                                self._db.commit()
                        summary["added"] += 1
                    elif err != "cancelled":
                        summary["failed"].append((item[0], err))
                    if progress:
                        progress(done, len(todo), folder)
            # files indexed before exact matching existed only need their hash worked out
            def hash_only(p):
                if cancel and cancel.is_set():
                    return p, None
                try:
                    return p, file_hash(p)
                except OSError:
                    return p, None

            if backfill:
                with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
                    for p, h in pool.map(hash_only, backfill):
                        if h:
                            with self._lock:
                                self._db.execute("UPDATE files SET sha = ? WHERE path = ?", (h, p))
            with self._lock:
                self._db.commit()
                self._cache = None
        return summary

    def add_file(self, path):
        """Add one photo (e.g. a confirmed-new scan) outside any watched folder."""
        path = os.path.normpath(path)
        st = os.stat(path)
        d4, cd, th = describe_full(path)
        sha = file_hash(path)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files (path, folder, size, mtime, desc, sha, cdesc) VALUES (?,?,?,?,?,?,?)",
                (path, None, st.st_size, st.st_mtime, d4.tobytes(), sha, cd.tobytes()),
            )
            self._db.execute("INSERT OR REPLACE INTO thumbs (path, img) VALUES (?,?)", (path, th))
            self._db.commit()
            self._cache = None

    # ---- searching ----
    def find_exact(self, path):
        """Indexed files whose bytes are identical to this file (same SHA-256), other than itself."""
        me = os.path.normcase(os.path.normpath(path))
        sha = file_hash(path)
        with self._lock:
            rows = self._db.execute("SELECT path FROM files WHERE sha = ?", (sha,)).fetchall()
        return [p for (p,) in rows if os.path.normcase(p) != me and os.path.exists(p)]

    def _arrays(self):
        with self._lock:
            if self._cache is None:
                rows = self._db.execute("SELECT path, desc, cdesc FROM files").fetchall()
                paths = [r[0] for r in rows]
                n = len(rows)
                if n:
                    nw = len(DB_WINDOWS)
                    raw = np.empty((n, nw, _T * _T), dtype=np.float16)
                    for i, r in enumerate(rows):
                        raw[i] = np.frombuffer(r[1], dtype=np.float16).reshape(nw, _T * _T)
                    views = [np.ascontiguousarray(raw[:, k, :], dtype=np.float32) for k in range(nw)]
                    del raw
                    nb = len(_BOXES_DB)
                    craw = np.zeros((n, nb, _CT * _CT), dtype=np.float16)
                    for i, r in enumerate(rows):
                        if r[2]:
                            craw[i] = np.frombuffer(r[2], dtype=np.float16).reshape(nb, _CT * _CT)
                    cviews = [np.ascontiguousarray(craw[:, k, :]) for k in range(nb)]  # float16 to save memory
                    del craw
                else:
                    views = [np.zeros((0, _T * _T), np.float32) for _ in DB_WINDOWS]
                    cviews = None
                lookup = {os.path.normcase(p): i for i, p in enumerate(paths)}
                self._cache = (paths, views, lookup, cviews)
            return self._cache

    def _crop_pass(self, path, crop, paths, cviews, own, best):
        """Raise the score of stored photos that are a differently-cropped copy of the scan."""
        bigs, qj, qb = _crop_query(path, crop)
        s1 = _crop_stage1(cviews, qj, qb)
        if own is not None:
            s1[own] = -9.0
        k = min(CROP_K, len(s1))
        cand = np.argpartition(-s1, k - 1)[:k]
        cand = cand[np.argsort(-s1[cand], kind="stable")]
        with self._lock:
            blobs = {}
            for j in range(0, len(cand), 200):
                chunk = [paths[i] for i in cand[j : j + 200]]
                q = ",".join("?" * len(chunk))
                blobs.update(self._db.execute(f"SELECT path, img FROM thumbs WHERE path IN ({q})", chunk).fetchall())
        starts = []   # (rough fit, photo, picture, scan turn, scan box, stored box)
        for i in cand:
            blob = blobs.get(paths[i])
            if blob is None:
                continue
            dc = np.stack([v[i] for v in cviews]).astype(np.float32)
            S = _big_from_blob(blob)
            best_start = None
            for _s, rot, qbox, sbox in _crop_picks(qj, qb, dc):
                raw = float(bigs[rot].window(qbox, _FT) @ S.window(sbox, _FT)) - _CA2 * (_trim(qbox) + _trim(sbox))
                if best_start is None or raw > best_start[0]:
                    best_start = (raw, i, S, rot, qbox, sbox)
            if best_start:
                starts.append(best_start)
        starts.sort(key=lambda t: -t[0])
        for _raw, i, S, rot, qbox, sbox in starts[:CROP_REFINE]:   # only the most promising get the full alignment
            rep = crop_report(_refine(bigs[rot], S, qbox, sbox))
            if rep > best[i]:
                if rep > best[i] + 0.05:  # only call it a crop match when the normal comparison clearly missed it
                    self.last_cropped.add(paths[i])
                best[i] = rep

    def search(self, path, top=5, crop=None, also_whole=False, use_crop=True):
        """Return (matches, already_indexed). matches = [(score 0..1, path)], best first.
        crop: only look at this part of the scan. also_whole: additionally try the whole scan
        and keep whichever fits each stored photo better (used for automatic cropping)."""
        paths, views, lookup, cviews = self._arrays()
        own = lookup.get(os.path.normcase(os.path.normpath(path)))
        if not paths:
            return [], own is not None
        V, scales, shifts = _query(path, crop)
        if crop and also_whole:
            V2, s2, h2 = _query(path)
            V, scales, shifts = np.concatenate([V, V2]), np.concatenate([scales, s2]), np.concatenate([shifts, h2])
        best = _best_scores(V, scales, shifts, views).copy()
        self.last_cropped = set()
        self.crop_error = None
        if cviews is None and use_crop:
            self.crop_error = "the index has no data for cropped copies"
        elif cviews is not None and use_crop:
            try:
                self._crop_pass(path, crop, paths, cviews, own, best)
            except Exception as e:  # noqa: BLE001  (the extra pass must never break a search)
                self.crop_error = f"{type(e).__name__}: {e}"
        if own is not None:
            best[own] = -9.0  # never match a file against itself
        k = min(top, len(best))
        idxs = np.argpartition(-best, k - 1)[:k]
        idxs = idxs[np.argsort(-best[idxs], kind="stable")]
        return [(float(best[i]), paths[i]) for i in idxs if best[i] > -9.0], own is not None


def verdict(score, sensitivity="Normal"):
    dup, maybe = SENSITIVITY[sensitivity]
    if score >= dup:
        return "likely duplicate"
    if score >= maybe:
        return "possible match"
    return "looks new"


def similarity_percent(score):
    return max(0, min(100, round(100 * score)))
