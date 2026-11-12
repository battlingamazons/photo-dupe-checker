"""Matching engine: perceptual hashing plus a small on-disk index.

No GUI code lives here so it can be tested without a display.
"""
import os
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
    "Strict": (0.85, 0.68),
    "Normal": (0.80, 0.60),
    "Generous": (0.75, 0.52),
}
SCHEMA_VERSION = 2


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
# stored copy. Each stored photo keeps two views (whole picture + centre crop) so
# a frame on EITHER copy can be cut away before comparing.
# ---------------------------------------------------------------------------
_SZ = 96            # working resolution
_PAD = 48           # edge padding so zooming out has something to sample
_T = 32             # descriptor is _T x _T
DB_WINDOWS = (1.0, 0.82)
QUERY_SCALES = (0.74, 0.8, 0.86, 0.92, 0.96, 1.0, 1.04, 1.09, 1.16, 1.25)
QUERY_OFFSETS = (-0.04, 0.0, 0.04)
_LAM, _MU = 0.25, 0.5   # small penalty so exact alignment wins over a lucky offset


def _gauss_matrix(n, sigma):
    i = np.arange(n)
    m = np.exp(-((i[:, None] - i[None, :]) ** 2) / (2 * sigma**2))
    return (m / m.sum(1, keepdims=True)).astype(np.float32)


_MH, _ML = _gauss_matrix(_T, 0.8), _gauss_matrix(_T, 2.2)
_yy, _xx = np.mgrid[:_T, :_T]
_W = np.exp(-(((_xx + 0.5) / _T - 0.5) ** 2 + ((_yy + 0.5) / _T - 0.5) ** 2) / (2 * 0.3**2)).astype(np.float32)


def _prep(path):
    g = load_image(path).convert("L").resize((_SZ, _SZ), Image.LANCZOS)
    return np.pad(np.asarray(g, dtype=np.float32), _PAD, mode="edge")


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


def _query(path):
    """All the positions a new scan is tried at (4 rotations x zooms x shifts)."""
    P = _prep(path)
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
                desc BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
            """
        )
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
                known = {
                    p: (s, m)
                    for p, s, m in self._db.execute(
                        "SELECT path, size, mtime FROM files WHERE folder = ?", (folder,)
                    )
                }
            seen, todo = set(), []
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
                    return item, describe(item[0]).tobytes(), None
                except Exception as e:  # corrupt/unsupported file
                    return item, None, str(e)

            done = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
                for item, desc, err in pool.map(work, todo):
                    done += 1
                    if desc:
                        with self._lock:
                            self._db.execute(
                                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                                (item[0], folder, item[1], item[2], desc),
                            )
                            if done % 500 == 0:
                                self._db.commit()
                        summary["added"] += 1
                    elif err != "cancelled":
                        summary["failed"].append((item[0], err))
                    if progress:
                        progress(done, len(todo), folder)
            with self._lock:
                self._db.commit()
                self._cache = None
        return summary

    def add_file(self, path):
        """Add one photo (e.g. a confirmed-new scan) outside any watched folder."""
        path = os.path.normpath(path)
        st = os.stat(path)
        desc = describe(path).tobytes()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                (path, None, st.st_size, st.st_mtime, desc),
            )
            self._db.commit()
            self._cache = None

    # ---- searching ----
    def _arrays(self):
        with self._lock:
            if self._cache is None:
                rows = self._db.execute("SELECT path, desc FROM files").fetchall()
                paths = [r[0] for r in rows]
                n = len(rows)
                if n:
                    raw = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(n, len(DB_WINDOWS), _T * _T)
                    views = [np.ascontiguousarray(raw[:, k, :], dtype=np.float32) for k in range(len(DB_WINDOWS))]
                else:
                    views = [np.zeros((0, _T * _T), np.float32) for _ in DB_WINDOWS]
                lookup = {os.path.normcase(p): i for i, p in enumerate(paths)}
                self._cache = (paths, views, lookup)
            return self._cache

    def search(self, path, top=5):
        """Return (matches, already_indexed). matches = [(score 0..1, path)], best first."""
        paths, views, lookup = self._arrays()
        own = lookup.get(os.path.normcase(os.path.normpath(path)))
        if not paths:
            return [], own is not None
        V, scales, shifts = _query(path)
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
