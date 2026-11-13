"""Matching engine: perceptual hashing plus a small on-disk index.

No GUI code lives here so it can be tested without a display.
"""
import os
import hashlib
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
SCHEMA_VERSION = 5


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
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL,
                desc BLOB NOT NULL, sha TEXT);
            CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
            """
        )
        if "sha" not in [r[1] for r in self._db.execute("PRAGMA table_info(files)")]:
            self._db.execute("ALTER TABLE files ADD COLUMN sha TEXT")  # index made before exact matching
        self._db.execute("CREATE INDEX IF NOT EXISTS files_sha ON files(sha)")
        self._db.commit()
        self._cache = None

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
                    self._db.commit()
                summary["removed"] += len(gone)

            def work(item):
                if cancel and cancel.is_set():
                    return item, None, "cancelled"
                try:
                    return item, (describe(item[0]).tobytes(), file_hash(item[0])), None
                except Exception as e:  # corrupt/unsupported file
                    return item, None, str(e)

            done = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
                for item, desc, err in pool.map(work, todo):
                    done += 1
                    if desc:
                        with self._lock:
                            self._db.execute(
                                "INSERT OR REPLACE INTO files (path, folder, size, mtime, desc, sha) VALUES (?,?,?,?,?,?)",
                                (item[0], folder, item[1], item[2], desc[0], desc[1]),
                            )
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
        desc = describe(path).tobytes()
        sha = file_hash(path)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files (path, folder, size, mtime, desc, sha) VALUES (?,?,?,?,?,?)",
                (path, None, st.st_size, st.st_mtime, desc, sha),
            )
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
                rows = self._db.execute("SELECT path, desc FROM files").fetchall()
                paths = [r[0] for r in rows]
                n = len(rows)
                if n:
                    nw = len(DB_WINDOWS)
                    raw = np.empty((n, nw, _T * _T), dtype=np.float16)
                    for i, r in enumerate(rows):
                        raw[i] = np.frombuffer(r[1], dtype=np.float16).reshape(nw, _T * _T)
                    views = [np.ascontiguousarray(raw[:, k, :], dtype=np.float32) for k in range(nw)]
                    del raw
                else:
                    views = [np.zeros((0, _T * _T), np.float32) for _ in DB_WINDOWS]
                lookup = {os.path.normcase(p): i for i, p in enumerate(paths)}
                self._cache = (paths, views, lookup)
            return self._cache

    def search(self, path, top=5, crop=None, also_whole=False):
        """Return (matches, already_indexed). matches = [(score 0..1, path)], best first.
        crop: only look at this part of the scan. also_whole: additionally try the whole scan
        and keep whichever fits each stored photo better (used for automatic cropping)."""
        paths, views, lookup = self._arrays()
        own = lookup.get(os.path.normcase(os.path.normpath(path)))
        if not paths:
            return [], own is not None
        V, scales, shifts = _query(path, crop)
        if crop and also_whole:
            V2, s2, h2 = _query(path)
            V, scales, shifts = np.concatenate([V, V2]), np.concatenate([scales, s2]), np.concatenate([shifts, h2])
        best = _best_scores(V, scales, shifts, views).copy()
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
