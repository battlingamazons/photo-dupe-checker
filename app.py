"""Photo Duplicate Checker - a small window for asking "have we scanned this already?"."""
import math
import os
import queue
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import core

try:  # drag-and-drop support; the app still works (button only) without it
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _Base = TkinterDnD.Tk
except Exception:  # noqa: BLE001
    DND_FILES, _Base = None, tk.Tk

THUMB = 220
COLORS = {"exact": "#b00020", "likely duplicate": "#b00020", "possible match": "#b26a00", "check closest": "#44546a", "looks new": "#1b7f3b"}


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    else:
        subprocess.run(["open", path], check=False)


def reveal(path):
    if sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
    else:
        subprocess.run(["open", "-R", path], check=False)


THUMB_MAX, THUMB_MIN = 300, 60
MORE_MAX = 500             # most matches kept for the 'More matches' window
CARD_X, CARD_Y = 34, 190   # per-card horizontal / vertical overhead (borders, text, buttons)


def app_version():
    """Version of the packaged Mac app (read from its Info.plist); 'dev' when run from source."""
    try:
        import plistlib

        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            plist = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Info.plist")
            with open(plist, "rb") as f:
                return plistlib.load(f).get("CFBundleShortVersionString", "dev")
    except Exception:  # noqa: BLE001
        pass
    return "dev"


def find_notices():
    """Path of the license-notices file that ships with the program (it sits in different places per platform)."""
    here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    folders = [here]
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        # <folder>/Photo Duplicate Checker.app/Contents/MacOS/<exe>: the notices sit next to the .app
        folders.append(os.path.normpath(os.path.join(here, "..", "..", "..")))
        folders.append(os.path.normpath(os.path.join(here, "..", "Resources")))
    for f in folders:
        for name in ("THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.md"):
            p = os.path.join(f, name)
            if os.path.exists(p):
                return p
    return os.path.join(folders[0], "THIRD_PARTY_NOTICES.txt")


def shorten(text, n=48):
    return text if len(text) <= n else "..." + text[-(n - 3):]


def load_pil_thumb(path, side=THUMB_MAX):
    img = core.load_image(path, max_side=side * 2).convert("RGB")
    img.thumbnail((side, side))
    return img


def thumbnail(path, size=THUMB):
    img = load_pil_thumb(path, size)
    return ImageTk.PhotoImage(img)


class MoreWindow(tk.Toplevel):
    """Every match found, as a scrolling grid that fills the window: identical copies first, then
    differently cropped versions, then the other close matches - all those at least as similar as
    the chosen cutoff. Only the cards on screen are built and their pictures load in the
    background, so hundreds of matches stay responsive."""

    THUMB = 190
    CELL_W, CELL_H = THUMB + 40, THUMB + 150

    def __init__(self, parent, entries, name, default_min):
        """entries: [(kind, percent, heading, path)] with kind 'exact' | 'crop' | 'other'."""
        super().__init__(parent)
        self.title("More matches")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1100, sw - 60)}x{min(700, sh - 150)}")
        self.entries, self.name = entries, name
        self.cards, self.cols, self.x0 = [], 0, 0
        self._items = {}
        self._live, self._photos, self._thumbs, self._pending = {}, {}, {}, set()
        self._done = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._job = None
        self.summary = tk.StringVar()
        ttk.Label(self, textvariable=self.summary, font=("Segoe UI", 11, "bold"), wraplength=1000, justify="left").pack(fill="x", padx=12, pady=(10, 2))
        bar_row = ttk.Frame(self)
        bar_row.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Label(bar_row, text="Show matches that are at least").pack(side="left")
        self.minvar = tk.StringVar(value=str(default_min))
        self.spin = ttk.Spinbox(bar_row, from_=0, to=100, increment=5, width=4, textvariable=self.minvar, command=self._changed)
        self.spin.pack(side="left", padx=4)
        ttk.Label(bar_row, text=f"% similar   (identical copies are always shown; 0 shows everything, up to the best {MORE_MAX})", foreground="#555").pack(side="left")
        self.minvar.trace_add("write", lambda *a: self._changed())
        self.spin.bind("<FocusIn>", lambda e: self.after_idle(lambda: self.spin.selection_range(0, "end")))
        self.spin.bind("<Control-a>", lambda e: (self.spin.selection_range(0, "end"), "break")[1])
        self.bind("<Return>", lambda e: self.rebuild())
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=6)
        self.canvas = tk.Canvas(body, highlightthickness=0, yscrollincrement=60)
        self.bar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        self.bind("<Prior>", lambda e: self.canvas.yview_scroll(-8, "units"))
        self.bind("<Next>", lambda e: self.canvas.yview_scroll(8, "units"))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(0, 8))
        self.rebuild()
        self.after(60, self._take_focus)
        self.after(40, self._poll)

    # ---- the cutoff ----
    def _take_focus(self):
        try:
            self.lift()
            self.focus_force()
            self.spin.focus_set()
        except tk.TclError:
            pass

    def minimum(self):
        try:
            return max(0, min(100, int(float(self.minvar.get()))))
        except ValueError:
            return None

    def _changed(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(300, self.rebuild)

    def set_min(self, pct):
        self.minvar.set(str(pct))
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.rebuild()

    def rebuild(self):
        self._job = None
        lo = self.minimum()
        if lo is None:
            return
        shown = [x for x in self.entries if x[0] == "exact" or x[1] >= lo]
        self.cards = [(h, p) for _k, _pc, h, p in shown]
        n_ex = sum(1 for x in shown if x[0] == "exact")
        n_cr = sum(1 for x in shown if x[0] == "crop")
        parts = []
        if n_ex:
            parts.append(f"{n_ex} identical cop{'y' if n_ex == 1 else 'ies'}")
        if n_cr:
            parts.append(f"{n_cr} differently cropped")
        parts.append(f"{len(shown) - n_ex - n_cr} other matches")
        hidden = len(self.entries) - len(shown)
        self.summary.set(f"{self.name}: " + ", ".join(parts) + (f"  ({hidden} below {lo}% hidden)" if hidden else ""))
        self._clear_cards()
        self.canvas.yview_moveto(0)
        self._layout()

    # ---- the grid: only what is on screen exists ----
    def _clear_cards(self):
        for f, _pic in self._live.values():
            f.destroy()
        self.canvas.delete("all")
        self._live, self._photos, self._items = {}, {}, {}

    def _layout(self):
        if not self.winfo_exists():
            return
        width = self.canvas.winfo_width()
        cols = max(1, width // self.CELL_W)
        if cols != self.cols:
            self.cols = cols
            self._clear_cards()
        x0 = max(0, (width - cols * self.CELL_W) // 2)  # leftover width is split evenly on both sides
        if x0 != self.x0:
            self.x0 = x0
            for i, item in self._items.items():
                r, c = divmod(i, self.cols)
                self.canvas.coords(item, self.x0 + c * self.CELL_W + 4, r * self.CELL_H + 4)
        rows = -(-len(self.cards) // cols)
        self.canvas.configure(scrollregion=(0, 0, max(width, cols * self.CELL_W), max(rows * self.CELL_H, 1)))
        self._update_visible()

    def _on_scroll(self, lo, hi):
        self.bar.set(lo, hi)
        self._update_visible()

    def _update_visible(self):
        if not self.cols:
            return
        top = self.canvas.canvasy(0)
        height = max(self.canvas.winfo_height(), 1)
        r0 = max(0, int(top // self.CELL_H) - 1)
        r1 = int((top + height) // self.CELL_H) + 1
        want = set(range(r0 * self.cols, min(len(self.cards), (r1 + 1) * self.cols)))
        for i in [i for i in self._live if i not in want]:
            self._live.pop(i)[0].destroy()
            self._photos.pop(i, None)
            self.canvas.delete(self._items.pop(i, None))
        for i in sorted(want):
            if i not in self._live:
                self._make_card(i)

    def _make_card(self, i):
        heading, path = self.cards[i]
        t = self.THUMB
        f = tk.Frame(self.canvas, relief="groove", borderwidth=2)
        f.pack_propagate(False)
        ttk.Label(f, text=heading, font=("Segoe UI", 10, "bold"), wraplength=t, justify="center").pack(pady=(4, 0))
        pic = ttk.Label(f, text="...", anchor="center")
        pic.pack(pady=4)
        ttk.Label(f, text=shorten(os.path.basename(path), 34), wraplength=t, justify="center").pack()
        ttk.Label(f, text=shorten(os.path.dirname(path), 42), wraplength=t, foreground="#666", justify="center").pack()
        row = ttk.Frame(f)
        row.pack(pady=4)
        ttk.Button(row, text="Open", width=6, command=lambda: open_path(path)).pack(side="left", padx=2)
        ttk.Button(row, text="Show in folder", command=lambda: reveal(path)).pack(side="left", padx=2)
        r, c = divmod(i, self.cols)
        self._items[i] = self.canvas.create_window(self.x0 + c * self.CELL_W + 4, r * self.CELL_H + 4, window=f, anchor="nw", width=self.CELL_W - 8, height=self.CELL_H - 8)
        self._live[i] = (f, pic)
        if path in self._thumbs:
            self._show(i)
        elif path not in self._pending:
            self._pending.add(path)
            self._pool.submit(self._load, path)

    # ---- pictures load in the background ----
    def _load(self, path):
        try:
            img = load_pil_thumb(path, self.THUMB)
        except Exception:  # noqa: BLE001
            img = None
        self._done.put((path, img))

    def _poll(self):
        if not self.winfo_exists():
            return
        for _ in range(6):
            try:
                path, img = self._done.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(path)
            if len(self._thumbs) > 300:
                self._thumbs.pop(next(iter(self._thumbs)))
            self._thumbs[path] = img
            for i in [i for i in self._live if self.cards[i][1] == path]:
                self._show(i)
        self.after(30, self._poll)

    def _show(self, i):
        _f, pic = self._live[i]
        img = self._thumbs.get(self.cards[i][1])
        if img is None:
            pic.config(text="(cannot preview)")
            self._photos[i] = None
            return
        photo = ImageTk.PhotoImage(img)
        self._photos[i] = photo
        pic.config(image=photo, text="")

    def pending(self):
        """How many cards on screen are still waiting for their picture."""
        return sum(1 for i in self._live if i not in self._photos)

    def _on_destroy(self, e):
        if e.widget is self:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _wheel(self, e):
        step = -1 * (e.delta // 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step * 3, "units")


class CropDialog(tk.Toplevel):
    """Drag a rectangle over the scan; result = (l, t, r, b) fractions, or None if cancelled."""

    MAXW, MAXH, GRAB = 820, 520, 10

    def __init__(self, parent, path, initial=None):
        super().__init__(parent)
        self.title("Crop the scan - drag over the part that is the photo")
        self.transient(parent)
        self.result = None
        self.done = False
        img = core.load_image(path, max_side=1200).convert("RGB")
        self.full = img
        sc = min(self.MAXW / img.width, self.MAXH / img.height, 1.0)
        self.dw, self.dh = max(1, int(img.width * sc)), max(1, int(img.height * sc))
        self.photo = ImageTk.PhotoImage(img.resize((self.dw, self.dh)))
        ttk.Label(self, text="The box was placed automatically around the photo. Drag its edges (or the box itself) to adjust it, "
                  "or drag a new box. Then click Check this part.", wraplength=self.MAXW).pack(padx=10, pady=(10, 4))
        self.canvas = tk.Canvas(self, width=self.dw, height=self.dh, highlightthickness=1, cursor="crosshair")
        self.canvas.pack(padx=10, pady=4)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.rect = None      # (x0, y0, x1, y1) in canvas pixels
        self.shade = []
        self.outline = None
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(4, 10))
        self.btn_ok = ttk.Button(bar, text="Check this part", command=self.accept, state="disabled")
        self.btn_ok.pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="right", padx=6)
        ttk.Button(bar, text="Auto-trim blank edges", command=self.auto).pack(side="left")
        ttk.Button(bar, text="Whole scan", command=lambda: self.set_fractions((0, 0, 1, 1))).pack(side="left", padx=6)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.mode = None
        if initial:
            self.set_fractions(initial)
        else:
            self.auto(quiet=True)
        self.bind("<Return>", lambda e: self.accept() if self.rect and self.btn_ok.instate(["!disabled"]) else None)
        self.bind("<Escape>", lambda e: self.cancel())
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.lift()
        self.focus_force()
        self.btn_ok.focus_set()

    # --- rectangle <-> fractions
    def set_fractions(self, fr):
        if not fr:
            self.rect = None
        else:
            l, t, r, b = fr
            self.rect = (l * self.dw, t * self.dh, r * self.dw, b * self.dh)
        self._draw()

    def fractions(self):
        if not self.rect:
            return None
        x0, y0, x1, y1 = self.rect
        return (x0 / self.dw, y0 / self.dh, x1 / self.dw, y1 / self.dh)

    def auto(self, quiet=False):
        box = core.content_box(self.full)
        if box:
            self.set_fractions(box)
        elif not quiet:
            messagebox.showinfo("Auto-trim", "No blank edges found - drag a box by hand instead.", parent=self)

    def _draw(self):
        c = self.canvas
        for i in self.shade:
            c.delete(i)
        self.shade = []
        if self.outline:
            c.delete(self.outline)
            self.outline = None
        if not self.rect:
            self.btn_ok.config(state="disabled")
            return
        x0, y0, x1, y1 = self.rect
        for box in ((0, 0, self.dw, y0), (0, y1, self.dw, self.dh), (0, y0, x0, y1), (x1, y0, self.dw, y1)):
            self.shade.append(c.create_rectangle(*box, fill="black", stipple="gray50", outline=""))
        self.outline = c.create_rectangle(x0, y0, x1, y1, outline="#00a0ff", width=2)
        ok = (x1 - x0) >= 12 and (y1 - y0) >= 12
        self.btn_ok.config(state="normal" if ok else "disabled")

    # --- mouse
    def _clamp(self, x, y):
        return min(max(x, 0), self.dw), min(max(y, 0), self.dh)

    def _press(self, e):
        x, y = self._clamp(e.x, e.y)
        self.start = (x, y)
        self.mode = None
        if self.rect:
            x0, y0, x1, y1 = self.rect
            g = self.GRAB
            if x0 - g <= x <= x1 + g and y0 - g <= y <= y1 + g:
                edges = (abs(x - x0) <= g, abs(x - x1) <= g, abs(y - y0) <= g, abs(y - y1) <= g)
                if any(edges):
                    self.mode = ("edge", edges)
                else:
                    self.mode = ("move", self.rect)
        if not self.mode:
            self.mode = ("new", None)
            self.rect = (x, y, x, y)
        self._draw()

    def _drag(self, e):
        if not self.mode:
            return
        x, y = self._clamp(e.x, e.y)
        kind, data = self.mode
        if kind == "new":
            sx, sy = self.start
            self.rect = (min(sx, x), min(sy, y), max(sx, x), max(sy, y))
        elif kind == "edge":
            x0, y0, x1, y1 = self.rect
            L, R, T, B = data
            if L:
                x0 = min(x, x1 - 1)
            if R:
                x1 = max(x, x0 + 1)
            if T:
                y0 = min(y, y1 - 1)
            if B:
                y1 = max(y, y0 + 1)
            self.rect = (x0, y0, x1, y1)
        else:
            x0, y0, x1, y1 = data
            dx = min(max(x - self.start[0], -x0), self.dw - x1)
            dy = min(max(y - self.start[1], -y0), self.dh - y1)
            self.rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        self._draw()

    def _release(self, e):
        self.mode = None

    def accept(self):
        self.result = self.fractions()
        self.done = True
        self.destroy()

    def cancel(self):
        self.result = None
        self.done = True
        self.destroy()


class App(_Base):
    def __init__(self, index=None):
        super().__init__()
        self.title("Photo Duplicate Checker")
        self.icon_ok = False
        try:
            self.iconbitmap(default=resource_path("icon.ico"))
            self.icon_ok = True
        except Exception:  # noqa: BLE001  (e.g. not Windows)
            pass
        w = min(1100, self.winfo_screenwidth() - 40)
        h = min(760, self.winfo_screenheight() - 100)
        self.geometry(f"{w}x{h}+10+10")
        self.minsize(min(900, w), min(640, h))
        self.index = index or core.Index()
        self.matches, self.already, self.exact = [], False, []
        self.cropped = set()
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.query_path = None
        self.query_crop = None
        self.query_auto = False
        self.searching = False
        self._search_token = 0
        self._images = []
        self._pil_cache = {}
        self._cards = []
        self._layout_job = None
        self._layout_size = (0, 0)
        self._reindex_note = False

        self._build()
        self._refresh_folders()
        self._enable_drop()
        self._poll()
        self.after(400, self._maybe_reindex)

    def _maybe_reindex(self):
        """After an update that changed how photos are matched, rebuild the index once."""
        if self.index.folders() and self.index.count() == 0 and not self.busy:
            if self.index.upgraded:
                self._reindex_note = True
                self.verdict.set("Matching was improved in this version - rebuilding your index (one time).")
            self.rescan()
        elif self.index.folders() and not self.busy and self.index.needs_faces():
            self.verdict.set("Matching was improved: looking for faces in your photos (one time) so very tightly cropped copies are caught. "
                             "Please wait for it to finish (you can press Cancel and it will continue next time).")
            self.rescan()

    # ---------- drag and drop ----------
    def _enable_drop(self):
        if not DND_FILES:
            return
        self._register_drop(self)

    def _register_drop(self, widget):
        """Make a widget and everything inside it accept dropped files."""
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # noqa: BLE001
            pass
        for child in widget.winfo_children():
            self._register_drop(child)

    def _on_drop(self, event):
        paths = [p for p in self.tk.splitlist(event.data)]
        files = [p for p in paths if os.path.isfile(p)]
        dirs = [p for p in paths if os.path.isdir(p)]
        if files:
            if len(files) > 1:
                self.status.set(f"{len(files)} files dropped - checking the first one ({os.path.basename(files[0])}).")
            self.check_path(files[0])
        elif dirs:
            names = "\n".join(dirs[:5])
            if messagebox.askyesno(
                "Add folder?",
                f"You dropped a folder:\n\n{names}\n\nAdd it to the folders of photos already scanned?\n"
                "(To check a single photo, drop the photo file itself.)",
            ):
                for d in dirs:
                    self.index.add_folder(d)
                self._refresh_folders()
                self.rescan()
        return event.action

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        top = ttk.LabelFrame(self, text="1. Folders of photos already scanned")
        top.pack(fill="x", **pad)
        self.folder_list = tk.Listbox(top, height=4, activestyle="none")
        self.folder_list.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        btns = ttk.Frame(top)
        btns.pack(side="right", padx=8, pady=8)
        self.btn_add = ttk.Button(btns, text="Add folder...", command=self.add_folder)
        self.btn_rm = ttk.Button(btns, text="Remove selected", command=self.remove_folder)
        self.btn_scan = ttk.Button(btns, text="Rescan folders", command=self.rescan)
        for b in (self.btn_add, self.btn_rm, self.btn_scan):
            b.pack(fill="x", pady=2)

        status = ttk.Frame(self)
        status.pack(fill="x", padx=10)
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(status, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="right", padx=(8, 0))
        ttk.Button(status, text="About & licenses", command=self.about).pack(side="right", padx=(8, 0))
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=10, pady=(2, 6))

        mid = ttk.LabelFrame(self, text="2. Check a photo")
        mid.pack(fill="x", **pad)
        self.btn_check = ttk.Button(mid, text="Choose a scan to check...", command=self.check)
        self.btn_check.pack(side="left", padx=8, pady=8)
        ttk.Label(mid, text="or drag a photo anywhere onto this window", foreground="#555").pack(side="left", padx=(10, 0))
        ttk.Label(mid, text="Sensitivity:").pack(side="left", padx=(24, 4))
        self.sens = ttk.Combobox(mid, values=list(core.SENSITIVITY), state="readonly", width=10)
        self.sens.set("Generous")
        self.sens.pack(side="left")
        self.sens.bind("<<ComboboxSelected>>", lambda e: self.matches and self.show_results())
        self.btn_crop = ttk.Button(mid, text="Crop...", command=self.crop, state="disabled")
        self.btn_crop.pack(side="left", padx=(24, 0))
        self.btn_addfile = ttk.Button(mid, text="Add this photo to the collection", command=self.add_current, state="disabled")
        self.btn_addfile.pack(side="right", padx=8)

        self.verdict = tk.StringVar(value="Add your folders above, then drag a scan onto this window to check it.")
        self.verdict_label = tk.Label(self, textvariable=self.verdict, font=("Segoe UI", 14, "bold"), anchor="w", justify="left", wraplength=1000)
        self.verdict_label.pack(fill="x", padx=12, pady=(6, 0))

        more_bar = ttk.Frame(self)
        more_bar.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        ttk.Label(more_bar, text="Only the best few matches fit above. The button lists all of them, down to a similarity you choose.", foreground="#555").pack(side="left")
        self.btn_more = ttk.Button(more_bar, text="Show more matches...", command=self.show_more, state="disabled")
        self.btn_more.pack(side="right")

        self.results = ttk.Frame(self)
        self.results.pack(fill="both", expand=True, padx=10, pady=6)
        self.results.grid_propagate(False)
        self.results.bind("<Configure>", self._on_results_resize)

    def about(self):
        notes = find_notices()
        if messagebox.askyesno(
            "About Photo Duplicate Checker",
            f"Photo Duplicate Checker {app_version()} - free, open-source software (MIT license).\n"
            "It is built with other open-source components - including GPL/LGPL libraries for HEIC and RAW "
            "support - whose licenses are acknowledged in the notices file.\n\n"
            "Source code: https://github.com/battlingamazons/photo-dupe-checker\n\n"
            "Open the license notices now?",
        ):
            try:
                open_path(notes)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Could not open", f"{notes}\n\n{e}")

    # ---------- folders / indexing ----------
    def _refresh_folders(self):
        self.folder_list.delete(0, "end")
        for f in self.index.folders():
            self.folder_list.insert("end", f)
        self.status.set(f"{self.index.count():,} photos indexed.")

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_add, self.btn_rm, self.btn_scan, self.btn_check):
            b.config(state=state)
        self.btn_cancel.config(state="normal" if busy else "disabled")

    def add_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of already-scanned photos")
        if path:
            self.index.add_folder(path)
            self._refresh_folders()
            self.rescan()

    def remove_folder(self):
        sel = self.folder_list.curselection()
        if not sel:
            return
        path = self.folder_list.get(sel[0])
        if messagebox.askyesno("Remove folder", f"Stop checking against:\n{path}\n\n(Your photo files are not touched.)"):
            self.index.remove_folder(path)
            self._refresh_folders()

    def rescan(self):
        if self.busy:
            return
        if not self.index.folders():
            messagebox.showinfo("No folders", "Add at least one folder first.")
            return
        self.cancel.clear()
        self._set_busy(True)
        self.progress.config(value=0)
        self.status.set("Looking for new or changed photos...")

        def run():
            try:
                s = self.index.rescan(lambda d, t, f: self.q.put(("progress", d, t, f)), self.cancel)
                self.q.put(("done", s))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, d, t, what = msg
                    self.progress.config(maximum=max(t, 1), value=d)
                    if what == "faces":
                        self.status.set(f"Finding faces, so even very tightly cropped copies are caught (one time): {d:,} of {t:,}")
                    else:
                        self.status.set(f"Indexing new photos: {d:,} of {t:,}")
                elif msg[0] == "done":
                    self._finish_scan(msg[1])
                elif msg[0] == "error":
                    self._set_busy(False)
                    messagebox.showerror("Problem while indexing", msg[1])
                elif msg[0] == "search_done":
                    self._search_finished(msg[1], msg[2], msg[3], crop=msg[4], auto=msg[5], exact=msg[6], cropped=msg[7])
                elif msg[0] == "search_error":
                    self._search_finished(msg[1], [], False, error=msg[2])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish_scan(self, s):
        self._set_busy(False)
        self.progress.config(value=0)
        self._refresh_folders()
        parts = [f"{s['added']:,} added", f"{s['removed']:,} removed", f"{s['skipped']:,} unchanged"]
        self.status.set(f"{self.index.count():,} photos indexed  ({', '.join(parts)}).")
        if self._reindex_note:
            self._reindex_note = False
            self.verdict.set("Index rebuilt with the improved matching. Drag a scan onto this window to check it.")
        notes = []
        if s["unavailable"]:
            notes.append("These folders could not be reached and were left as they were:\n" + "\n".join(s["unavailable"]))
        if s["failed"]:
            notes.append(f"{len(s['failed'])} file(s) could not be read (skipped), e.g.:\n" + "\n".join(p for p, _ in s["failed"][:5]))
        if notes:
            messagebox.showwarning("Scan finished with notes", "\n\n".join(notes))

    # ---------- checking ----------
    def check(self):
        path = filedialog.askopenfilename(
            title="Choose the new scan",
            filetypes=[("Photos and scans", " ".join(f"*{e}" for e in sorted(core.EXTS))), ("All files", "*.*")],
        )
        if path:
            self.check_path(path)

    def crop(self):
        """Let the user mark just the photo part of the scan, then check that part."""
        if not self.query_path or self.searching or self.busy:
            return
        try:
            dlg = CropDialog(self, self.query_path, self.query_crop)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not open the scan", str(e))
            return
        self.wait_window(dlg)
        if dlg.result:
            l, t, r, b = dlg.result
            if l <= 0.01 and t <= 0.01 and r >= 0.99 and b >= 0.99:
                self.check_path(self.query_path, auto=False)  # user chose the whole scan
            else:
                self.check_path(self.query_path, crop=dlg.result, auto=False)

    def check_path(self, path, crop=None, auto=True):
        """Compare one photo (chosen or dropped) against the index and show the result."""
        if not core.is_image(path):
            messagebox.showinfo("Not a supported photo", f"{os.path.basename(path)} is not a photo or scan type this program can read.")
            return
        if self.busy:
            messagebox.showinfo("Still indexing", "Please wait for indexing to finish, then check the photo again.")
            return
        if self.index.count() == 0:
            messagebox.showinfo("Nothing to compare", "The index is empty. Add a folder and let it finish scanning first.")
            return
        if self.searching:
            messagebox.showinfo("Still checking", "Please wait a moment - the previous photo is still being checked.")
            return
        self.query_path = path
        self.query_crop = None
        self.query_auto = False
        self.searching = True
        self._search_token += 1
        token = self._search_token
        self.btn_check.config(state="disabled")
        self.btn_crop.config(state="disabled")
        self.btn_more.config(state="disabled")
        self.btn_addfile.config(state="disabled")
        self.verdict_label.config(fg="black")
        self.verdict.set(f"Checking {os.path.basename(path)} against {self.index.count():,} photos...")

        def run():
            try:
                use, was_auto = crop, False
                if use is None and auto:
                    use = core.auto_crop(path)
                    was_auto = use is not None
                exact = self.index.find_exact(path)
                m, already = self.index.search(path, top=MORE_MAX + len(exact), crop=use, also_whole=was_auto)
                m = [x for x in m if x[1] not in exact][:MORE_MAX]
                self.q.put(("search_done", token, m, already, use, was_auto, exact, set(self.index.last_cropped)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("search_error", token, str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _search_finished(self, token, matches, already, error=None, crop=None, auto=False, exact=(), cropped=()):
        if token != self._search_token:
            return
        self.searching = False
        self.btn_crop.config(state="normal")
        if not self.busy:
            self.btn_check.config(state="normal")
        if error:
            self.verdict.set("Could not read that photo.")
            messagebox.showerror("Could not read that photo", error)
            return
        self.matches, self.already, self.exact = matches, already, list(exact)
        self.cropped = set(cropped)  # matches found because one scan is cropped differently from the other
        self.query_crop, self.query_auto = crop, auto
        self.show_results()

    # ---------- results ----------
    def show_results(self):
        sens = self.sens.get()
        self._pil_cache = {k: v for k, v in self._pil_cache.items() if k == self.query_path}
        if not self.matches and not self.exact:
            for w in self.results.winfo_children():
                w.destroy()
            self._cards = []
            self.verdict.set("No other photos in the index to compare against.")
            return
        top = self.matches[0][0] if self.matches else 0.0
        v = core.verdict(top, sens)
        if v == "looks new" and top >= core.SENSITIVITY[sens][1] - 0.10:
            v = "check closest"  # nothing convincing, but close enough that a worn copy could hide here
        if self.exact:
            v = "exact"
        text = {
            "exact": "Exact copy already in the collection - the file is identical",
            "likely duplicate": "Likely already scanned",
            "possible match": "Possible match - please compare by eye",
            "check closest": "Probably new - but the closest photos below are worth a quick look (a badly worn copy can score low)",
            "looks new": "Looks new - no close match found",
        }[v]
        # differently cropped versions of this photo: one card per distinct picture (copies of it in other
        # folders score identically), and always given a slot so identical copies can't push it out of view
        crops, seen = [], set()
        for s, p in self.matches:
            if p in self.cropped and core.verdict(s, sens) != "looks new" and round(s, 3) not in seen:
                seen.add(round(s, 3))
                crops.append((s, p))
        crops = crops[:2]
        if self.matches and self.matches[0][1] in self.cropped and v not in ("looks new", "exact"):
            text += "   - the closest one is the same photo cropped differently"
        elif crops:
            text += "   - the collection also holds a differently cropped version of this photo"
        if v == "exact" and len(self.exact) > 1:
            text = text.replace("the file is identical", f"the file is identical ({len(self.exact)} copies)")
        if self.index.crop_error:
            text += f"   [cropped-copy check did not run: {self.index.crop_error}]"
        if self.already:
            text += "   (this exact file is already in the index)"
        self.verdict.set(text)
        self.verdict_label.config(fg=COLORS.get(v, COLORS["likely duplicate"]))
        self.btn_addfile.config(state="normal" if not self.already else "disabled")
        self.btn_more.config(state="normal")
        exact_cards = self.exact[:2]
        slots = max(1, 4 - len(exact_cards))
        crop_paths = {p for _s, p in crops}
        others = [(s, p) for s, p in self.matches if p not in self.cropped][: max(0, slots - len(crops))] if crops else [
            (s, p) for s, p in self.matches
        ][:slots]
        shown = sorted(others + crops, key=lambda t: -t[0])[:slots]
        self._cards = [("THIS SCAN" + ((" (auto-cropped)" if self.query_auto else " (cropped)") if self.query_crop else ""), self.query_path)]
        self._cards += [("EXACT MATCH - identical file", p) for p in exact_cards]
        self._cards += [
            (f"Same photo, cropped - {core.similarity_percent(s)}%" if p in crop_paths else f"{core.similarity_percent(s)}% similar - {core.verdict(s, sens)}", p)
            for s, p in shown
        ]
        self._layout_cards()

    def _on_results_resize(self, event):
        if (event.width, event.height) == self._layout_size:
            return
        if self._layout_job:
            self.after_cancel(self._layout_job)
        self._layout_job = self.after(150, self._layout_cards)

    def _layout_cards(self):
        """Pick columns/rows and thumbnail size so every card fits on screen without scrolling."""
        self._layout_job = None
        for w in self.results.winfo_children():
            w.destroy()
        self._images = []
        if not self._cards:
            return
        self.update_idletasks()
        W = self.results.winfo_width()
        H = self.results.winfo_height()
        if W < 50 or H < 50:  # not laid out yet
            W, H = max(self.winfo_width() - 20, 600), max(self.winfo_height() - 380, 260)
        self._layout_size = (W, H)
        self.verdict_label.config(wraplength=max(300, self.winfo_width() - 30))
        n = len(self._cards)
        best = None
        for cols in range(n, 0, -1):  # prefer one row; wrap only if that clearly gives bigger photos
            rows = math.ceil(n / cols)
            t = min((W - 8) // cols - CARD_X, (H - 8) // rows - CARD_Y, THUMB_MAX)
            if best is None or t > best[0] + 4:
                best = (t, cols)
        t, cols = best
        t = max(t, THUMB_MIN)
        for c in range(cols):
            self.results.columnconfigure(c, weight=1)
        for i, (heading, path) in enumerate(self._cards):
            self._card(i // cols, i % cols, heading, path, t, is_query=(i == 0))

    def _pil(self, path):
        if path not in self._pil_cache:
            self._pil_cache[path] = load_pil_thumb(path)
        return self._pil_cache[path]

    def _card(self, row, col, heading, path, t, is_query=False):
        f = ttk.Frame(self.results, padding=6, relief="groove")
        f.grid(row=row, column=col, sticky="n", padx=4, pady=4)
        ttk.Label(f, text=heading, font=("Segoe UI", 10, "bold"), wraplength=max(t, 160), justify="center").pack()
        try:
            img = self._pil(path).copy()
            if is_query:
                img = core.crop_image(img, self.query_crop)
            img.thumbnail((t, t))
            photo = ImageTk.PhotoImage(img)
            self._images.append(photo)
            ttk.Label(f, image=photo).pack(pady=4)
        except Exception:  # noqa: BLE001
            ttk.Label(f, text="(cannot preview)", width=max(10, t // 8)).pack(pady=30)
        ttk.Label(f, text=shorten(os.path.basename(path), 40), wraplength=max(t, 160), justify="center").pack()
        if not is_query:
            ttk.Label(f, text=shorten(os.path.dirname(path), 52), wraplength=max(t, 160), foreground="#666", justify="center").pack()
        row_f = ttk.Frame(f)
        row_f.pack(pady=4)
        side = "left" if t >= 150 else "top"
        ttk.Button(row_f, text="Open", width=7, command=lambda: open_path(path)).pack(side=side, padx=2, pady=1)
        ttk.Button(row_f, text="Show in folder", command=lambda: reveal(path)).pack(side=side, padx=2, pady=1)
        if DND_FILES:
            self._register_drop(f)

    def more_entries(self):
        """Everything found as (kind, percent, heading, path): identical copies, cropped versions, other matches."""
        sens = self.sens.get()
        out = [("exact", 100, "EXACT MATCH - identical file", p) for p in self.exact]
        out += [("crop", core.similarity_percent(s), f"Same photo, cropped - {core.similarity_percent(s)}%", p) for s, p in self.matches if p in self.cropped]
        out += [("other", core.similarity_percent(s), f"{core.similarity_percent(s)}% similar - {core.verdict(s, sens)}", p) for s, p in self.matches if p not in self.cropped]
        return out

    def show_more(self):
        entries = self.more_entries()
        if not entries:
            return
        maybe = core.SENSITIVITY[self.sens.get()][1]
        if getattr(self, "more", None) is not None and self.more.winfo_exists():
            self.more.destroy()
        self.more = MoreWindow(self, entries, os.path.basename(self.query_path), core.similarity_percent(maybe))

    def add_current(self):
        if not self.query_path:
            return
        try:
            self.index.add_file(self.query_path)
        except Exception as e:
            messagebox.showerror("Could not add", str(e))
            return
        self.btn_addfile.config(state="disabled")
        self.status.set(f"{self.index.count():,} photos indexed. Added {os.path.basename(self.query_path)}.")
        messagebox.showinfo(
            "Added",
            "Added to the index. Note: it is remembered by its current location, "
            "so keep the file there (or move it into one of the watched folders).",
        )


def selftest(out_path):
    """Used by the Windows build to prove the packaged app really works end to end."""
    import tempfile
    from types import SimpleNamespace

    import numpy as np

    msgs = []
    try:
        d = tempfile.mkdtemp()
        rng = np.random.default_rng(1)
        base = Image.fromarray((rng.random((30, 40, 3)) * 255).astype("uint8")).resize((400, 300))
        for ext in (".jpg", ".png", ".tif", ".bmp", ".gif", ".webp", ".jp2", ".tga"):
            base.save(os.path.join(d, "x" + ext))
        base.save(os.path.join(d, "x.pdf"))
        if core.pillow_heif:
            import pillow_heif

            pillow_heif.from_pillow(base).save(os.path.join(d, "x.heic"))
        idx = core.Index(os.path.join(d, "t.db"))
        idx.add_folder(d)
        s = idx.rescan()
        assert not s["failed"], s["failed"]
        msgs.append(f"{s['added']} files ok")
        assert core.pypdfium2 and core.pillow_heif and core.rawpy and core.tifffile, "optional reader missing"
        assert DND_FILES, "drag-and-drop library missing"
        msgs.append("dnd lib ok")

        app = App(index=idx)  # builds the whole window (also keeps a Tk root alive)
        app.update()
        if sys.platform.startswith("win"):
            assert app.icon_ok, "window icon failed to load"
            msgs.append("icon ok")
        # simulate dropping a file whose path contains a space (Tk wraps it in braces)
        spaced = os.path.join(d, "my photo.png")
        base.save(spaced)
        app._on_drop(SimpleNamespace(data="{" + spaced + "}", action="copy"))
        t0 = time.time()
        while time.time() - t0 < 60:  # the check runs in the background
            app.update()
            if not app.searching:
                break
            time.sleep(0.05)
        app.update()
        assert app.query_path == spaced and app.matches, "drop did not trigger a check"
        assert app.matches[0][0] >= 0.9, app.matches[:1]
        assert [os.path.basename(p) for p in app.exact] == ["x.png"], app.exact  # byte-identical copy
        assert app._cards[1][0].startswith("EXACT MATCH"), app._cards[:2]
        msgs.append("exact ok")
        # extremely cropped copies are found through their faces: needs the models and OpenCV inside the build
        assert core.faces.AVAILABLE, "face models or OpenCV missing from the build"
        face_src = core.faces.resource_path("selfdata/solvay_1927.jpg")
        if not os.path.isfile(face_src):
            face_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "data", "solvay_1927.jpg")
        fimg = Image.open(face_src).convert("RGB")
        fw_, fh_ = fimg.size
        tight = os.path.join(d, "tight crop.jpg")
        fimg.crop((int(fw_ * .25), int(fh_ * .45), int(fw_ * .7), int(fh_ * .75))).resize((int(fw_ * .45 * 1.4), int(fh_ * .3 * 1.4))).save(tight, quality=88)
        fpath = os.path.join(d, "faces source.jpg")
        fimg.save(fpath, quality=95)
        idx.add_file(fpath)
        assert idx._db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] > 5, "no faces found in the test photo"
        found, _ = idx.search(tight, top=3)
        assert idx.crop_error is None, idx.crop_error
        assert found and found[0][1] == os.path.normpath(fpath) and found[0][0] >= 0.8, found[:2]
        msgs.append("faces ok")
        app.update()
        app.btn_more.invoke() if str(app.btn_more.cget("state")) == "normal" else None
        app.update()
        assert str(app.btn_more.cget("state")) == "normal", "more-matches button not enabled after a check"
        total = len(app.exact) + len(app.matches)
        assert getattr(app, "more", None) is not None, "more-matches window did not open"
        app.more.set_min(0)  # everything
        assert len(app.more.cards) == total >= 2, (len(app.more.cards), total)
        t0 = time.time()
        while time.time() - t0 < 30 and app.more.pending():
            app.update()
            time.sleep(0.05)
        assert app.more.pending() == 0, "pictures did not load in the more-matches window"
        app.more.destroy()
        # the similarity cutoff: identical copies always stay, others need to reach it
        some = os.path.join(d, "x.png")
        probe = MoreWindow(app, [("exact", 100, "E", some), ("other", 90, "A", some), ("crop", 72, "B", some), ("other", 40, "C", some)], "probe", 60)
        assert len(probe.cards) == 3, probe.cards  # 100, 90, 72 at 60%
        probe.set_min(95)
        assert [h for h, _p in probe.cards] == ["E"], probe.cards
        probe.set_min(0)
        assert len(probe.cards) == 4
        probe.set_min(100)
        assert [h for h, _p in probe.cards] == ["E"], probe.cards
        probe.destroy()
        # hundreds of matches must not freeze the window: only the cards on screen are built
        many = [("exact", 100, "E", some)] + [("other", 99 - (k % 60), f"M{k}", some) for k in range(499)]
        t0 = time.time()
        big = MoreWindow(app, many, "stress", 0)
        big.update()
        assert len(big.cards) == 500 and 0 < len(big._live) < 80, (len(big.cards), len(big._live))
        assert time.time() - t0 < 10, "opening 500 matches was too slow"
        big.canvas.yview_moveto(0.9)
        big.update()
        assert 0 < len(big._live) < 80 and max(big._live) > 300, (len(big._live), max(big._live, default=None))
        big.destroy()
        msgs.append("more matches ok")
        # the cropped-copy check must really run inside the packaged app (its errors are otherwise silent)
        sm = Image.fromarray((rng.random((9, 12)) * 255).astype("uint8")).resize((640, 480), Image.BICUBIC)
        sm_path, sm_crop = os.path.join(d, "smooth.png"), os.path.join(d, "smooth crop.png")
        sm.save(sm_path)
        sm.crop((96, 48, 576, 384)).save(sm_crop)
        idx.add_file(sm_path)
        found, _ = idx.search(sm_crop, top=3)
        assert idx.crop_error is None, idx.crop_error
        assert found and found[0][1] == os.path.normpath(sm_path) and found[0][0] >= 0.8, found[:2]
        msgs.append("crop pass ok")
        msgs.append("drop ok")

        def fits():
            app.update()
            app._layout_cards()
            app.update()
            rw, rh = app.results.winfo_width(), app.results.winfo_height()
            cards = app.results.winfo_children()
            assert len(cards) >= 5, f"only {len(cards)} cards shown"
            for c in cards:
                assert c.winfo_x() >= 0 and c.winfo_x() + c.winfo_width() <= rw + 2, ("card sticks out sideways", rw, c.winfo_x(), c.winfo_width())
                assert c.winfo_y() >= 0 and c.winfo_y() + c.winfo_height() <= rh + 2, ("card sticks out below", rh, c.winfo_y(), c.winfo_height())

        # a scan with a big blank strip under the photo is cropped automatically
        strip = os.path.join(d, "strip scan.png")
        tall = Image.new("RGB", (400, 600), (248, 248, 246))
        tall.paste(base, (0, 0))
        tall.save(strip)
        app.check_path(strip)
        t0 = time.time()
        while time.time() - t0 < 60 and (app.searching or app.query_path != strip):
            app.update()
            time.sleep(0.05)
        app.update()
        assert app.query_auto and app.query_crop and app.query_crop[3] < 0.7, (app.query_auto, app.query_crop)
        msgs.append("auto-crop ok")
        app.check_path(spaced)
        t0 = time.time()
        while time.time() - t0 < 60 and app.searching:
            app.update()
            time.sleep(0.05)
        app.update()
        fits()
        msgs.append("layout fits")
        # crop dialog: draw a box with real mouse events, accept, and re-check the cropped part
        dlg = CropDialog(app, spaced)
        dlg.update()
        dlg.set_fractions(None)
        dlg.canvas.event_generate("<ButtonPress-1>", x=10, y=10)
        dlg.canvas.event_generate("<B1-Motion>", x=dlg.dw // 2, y=dlg.dh // 2)
        dlg.canvas.event_generate("<ButtonRelease-1>", x=dlg.dw // 2, y=dlg.dh // 2)
        dlg.update()
        fr = dlg.fractions()
        assert fr and 0.4 < fr[2] < 0.6, fr
        dlg.accept()
        assert dlg.result == fr
        app.check_path(spaced, crop=fr)
        t0 = time.time()
        while time.time() - t0 < 60 and app.searching:
            app.update()
            time.sleep(0.05)
        app.update()
        assert app.query_crop == fr and app.matches, "cropped check failed"
        msgs.append("crop ok")
        app.check_path(spaced)
        t0 = time.time()
        while time.time() - t0 < 60 and app.searching:
            app.update()
            time.sleep(0.05)
        app.update()
        app.geometry("900x640")
        app.update()
        fits()
        msgs.append("small window fits")
        thumbnail(os.path.join(d, "x.pdf"))
        app.destroy()
        result = "OK " + " ".join(msgs)
    except Exception as e:  # noqa: BLE001
        result = f"FAIL {type(e).__name__}: {e}"
    with open(out_path, "w") as f:
        f.write(result)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "--check-index":
        # headless diagnostic: check one photo against your real index (read-only)
        _t = time.time()
        _ix = core.Index()
        print("version:", app_version(), "| photos indexed:", f"{_ix.count():,}")
        _q = sys.argv[2]
        _use = core.auto_crop(_q)
        print("auto-crop of this scan:", _use)
        print("identical files in index:", _ix.find_exact(_q))
        _m, _own = _ix.search(_q, top=10, crop=_use, also_whole=_use is not None)
        print("this exact file is in the index:", _own)
        for _s, _p in _m:
            print(f"  {_s:.3f}  {'(cropped copy) ' if _p in _ix.last_cropped else ''}{_p}")
        print("crop check error:", _ix.crop_error, "| took", round(time.time() - _t, 1), "s")
    elif len(sys.argv) == 4 and sys.argv[1] == "--check":
        # headless diagnostic: index photo A in memory, then check photo B against it
        _ix = core.Index(":memory:")
        _ix.add_file(sys.argv[2])
        _m, _ = _ix.search(sys.argv[3])
        print("version:", app_version())
        print("matches:", [(round(s, 3), os.path.basename(p)) for s, p in _m])
        print("flagged as cropped:", sorted(os.path.basename(p) for p in _ix.last_cropped))
        print("crop check error:", _ix.crop_error)
    else:
        App().mainloop()
