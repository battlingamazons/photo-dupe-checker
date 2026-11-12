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

# Combined distance is out of 128 bits (64 from each of two hashes).
SENSITIVITY = {
    "Strict": (8, 20),
    "Normal": (12, 28),
    "Generous": (16, 36),
}

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _dct_matrix(n=32):
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2)
    return m


_DCT = _dct_matrix()


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


def _open_gray(path):
    """Return an auto-contrasted grayscale copy of the photo."""
    return ImageOps.autocontrast(load_image(path).convert("L"), cutoff=1)


def _pack(bits):
    return np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes()


def hash_gray(g):
    """Return (phash, dhash) as 8-byte strings each."""
    a = np.asarray(g.resize((32, 32), Image.LANCZOS), dtype=np.float64)
    low = (_DCT @ a @ _DCT.T)[:8, :8].flatten()
    ph = low > np.median(low[1:])
    s = np.asarray(g.resize((9, 8), Image.LANCZOS), dtype=np.float64)
    dh = (s[:, 1:] > s[:, :-1]).flatten()
    return _pack(ph), _pack(dh)


def hash_file(path):
    return hash_gray(_open_gray(path))


def hash_file_rotations(path):
    """Hashes for the photo at 0/90/180/270 degrees (scans are often sideways)."""
    g = _open_gray(path)
    rots = [
        g,
        g.transpose(Image.ROTATE_90),
        g.transpose(Image.ROTATE_180),
        g.transpose(Image.ROTATE_270),
    ]
    return [hash_gray(r) for r in rots]


def _arr(blobs):
    return np.frombuffer(b"".join(blobs), dtype=np.uint8).reshape(-1, 8)


class Index:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or default_db_path())
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL,
                ph BLOB NOT NULL, dh BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
            """
        )
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
                    return item, hash_file(item[0]), None
                except Exception as e:  # corrupt/unsupported file
                    return item, None, str(e)

            done = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2))) as pool:
                for item, hashes, err in pool.map(work, todo):
                    done += 1
                    if hashes:
                        with self._lock:
                            self._db.execute(
                                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                                (item[0], folder, item[1], item[2], hashes[0], hashes[1]),
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
        ph, dh = hash_file(path)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                (path, None, st.st_size, st.st_mtime, ph, dh),
            )
            self._db.commit()
            self._cache = None

    # ---- searching ----
    def _arrays(self):
        with self._lock:
            if self._cache is None:
                rows = self._db.execute("SELECT path, ph, dh FROM files").fetchall()
                paths = [r[0] for r in rows]
                self._cache = (paths, _arr([r[1] for r in rows]), _arr([r[2] for r in rows]))
            return self._cache

    def search(self, path, top=5):
        """Return (matches, already_indexed). matches = [(distance, path)], best first."""
        paths, P, D = self._arrays()
        norm = os.path.normcase(os.path.normpath(path))
        already = any(os.path.normcase(p) == norm for p in paths)
        if not paths:
            return [], already
        best = None
        for qp, qd in hash_file_rotations(path):
            qp = np.frombuffer(qp, dtype=np.uint8)
            qd = np.frombuffer(qd, dtype=np.uint8)
            dist = _POP[P ^ qp].sum(axis=1, dtype=np.int32) + _POP[D ^ qd].sum(axis=1, dtype=np.int32)
            best = dist if best is None else np.minimum(best, dist)
        order = np.argsort(best, kind="stable")
        out = []
        for i in order:
            if os.path.normcase(paths[i]) == norm:
                continue
            out.append((int(best[i]), paths[i]))
            if len(out) >= top:
                break
        return out, already


def verdict(distance, sensitivity="Normal"):
    dup, maybe = SENSITIVITY[sensitivity]
    if distance <= dup:
        return "likely duplicate"
    if distance <= maybe:
        return "possible match"
    return "looks new"


def similarity_percent(distance):
    return max(0, round(100 * (1 - distance / 128)))
