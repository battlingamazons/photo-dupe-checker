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


class CardGrid(ttk.Frame):
    """A scrolling grid of photo cards that fills its space. Only the cards on screen are built and
    their pictures load in the background, so hundreds of matches stay responsive."""

    THUMB = 190
    CELL_W, CELL_H = THUMB + 40, THUMB + 150

    def __init__(self, parent, on_card=None):
        """cards: [(heading, path, crop, is_query)]; on_card(widget) is called for every card built."""
        super().__init__(parent)
        self.on_card = on_card
        self.cards, self.cols, self.x0 = [], 0, 0
        self._items = {}
        self._live, self._photos, self._thumbs, self._pending = {}, {}, {}, set()
        self._done = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=3)
        self.canvas = tk.Canvas(self, highlightthickness=0, yscrollincrement=60)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.bind_all("<MouseWheel>", self._wheel, add="+")
        self.bind_all("<Button-4>", lambda e: self._wheel_units(-3), add="+")
        self.bind_all("<Button-5>", lambda e: self._wheel_units(3), add="+")
        self.bind("<Destroy>", self._on_destroy)
        self.after(40, self._poll)

    # ---- content ----
    def set_cards(self, cards, reset=True):
        self.cards = list(cards)
        if reset:
            self._clear_cards()
            self.canvas.yview_moveto(0)
        self._layout()

    def see(self, i):
        """Scroll down just far enough that card i's row is on screen."""
        if not self.cols:
            return
        top = (i // self.cols) * self.CELL_H
        view = self.canvas.canvasy(0)
        if top < view or top + self.CELL_H > view + self.canvas.winfo_height():
            total = max(-(-len(self.cards) // self.cols) * self.CELL_H, 1)
            self.canvas.yview_moveto(top / total)
            self._update_visible()

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
        heading, path, crop, is_query = self.cards[i]
        t = self.THUMB
        f = tk.Frame(self.canvas, relief="groove", borderwidth=2)
        f.pack_propagate(False)
        ttk.Label(f, text=heading, font=("Segoe UI", 10, "bold"), wraplength=t, justify="center").pack(pady=(4, 0))
        pic = ttk.Label(f, text="...", anchor="center")
        pic.pack(pady=4)
        ttk.Label(f, text=shorten(os.path.basename(path), 34), wraplength=t, justify="center").pack()
        if not is_query:
            ttk.Label(f, text=shorten(os.path.dirname(path), 42), wraplength=t, foreground="#666", justify="center").pack()
        row = ttk.Frame(f)
        row.pack(pady=4)
        ttk.Button(row, text="Open", width=6, command=lambda: open_path(path)).pack(side="left", padx=2)
        ttk.Button(row, text="Show in folder", command=lambda: reveal(path)).pack(side="left", padx=2)
        r, c = divmod(i, self.cols)
        self._items[i] = self.canvas.create_window(self.x0 + c * self.CELL_W + 4, r * self.CELL_H + 4, window=f, anchor="nw", width=self.CELL_W - 8, height=self.CELL_H - 8)
        self._live[i] = (f, pic)
        if self.on_card:
            self.on_card(f)
        key = (path, crop)
        if key in self._thumbs:
            self._show(i)
        elif key not in self._pending:
            self._pending.add(key)
            self._pool.submit(self._load, path, crop)

    # ---- pictures load in the background ----
    def _load(self, path, crop):
        try:
            side = self.THUMB
            if crop:  # a small part of the scan needs a bigger source picture to look sharp
                side = min(1600, int(self.THUMB / max(min(crop[2] - crop[0], crop[3] - crop[1]), 0.1)) + 1)
            img = load_pil_thumb(path, side)
            if crop:
                img = core.crop_image(img, crop)
                img.thumbnail((self.THUMB, self.THUMB))
        except Exception:  # noqa: BLE001
            img = None
        self._done.put(((path, crop), img))

    def _poll(self):
        if not self.winfo_exists():
            return
        for _ in range(6):
            try:
                key, img = self._done.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(key)
            if len(self._thumbs) > 300:
                self._thumbs.pop(next(iter(self._thumbs)))
            self._thumbs[key] = img
            for i in [i for i in self._live if self.cards[i][1:3] == key]:
                self._show(i)
        self.after(30, self._poll)

    def _show(self, i):
        _f, pic = self._live[i]
        img = self._thumbs.get(self.cards[i][1:3])
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

    def _over(self, e):
        w = e.widget
        while isinstance(w, tk.Misc):
            if w is self:
                return True
            w = w.master
        return False

    def _wheel(self, e):
        if not self._over(e):
            return
        step = -1 * (e.delta // 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step * 3, "units")

    def _wheel_units(self, n):
        try:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return
        while isinstance(w, tk.Misc):
            if w is self:
                self.canvas.yview_scroll(n, "units")
                return
            w = w.master


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
        self._cards = []
        self.entries, self.shown, self.cutoff = [], [], 80
        self._setting = False
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
        self.more_bar = more_bar
        self.btn_more = ttk.Button(more_bar, text="Show more matches", command=self.show_more, state="disabled")
        self.btn_more.pack(side="left")
        ttk.Label(more_bar, text="  or show everything at least").pack(side="left")
        self.minvar = tk.StringVar(value="80")
        self.spin = ttk.Spinbox(more_bar, from_=0, to=100, increment=5, width=4, textvariable=self.minvar, command=self._cutoff_changed, state="disabled")
        self.spin.pack(side="left", padx=4)
        ttk.Label(more_bar, text="% similar").pack(side="left")
        self.minvar.trace_add("write", lambda *a: self._cutoff_changed())
        self.spin.bind("<FocusIn>", lambda e: self.after_idle(lambda: self.spin.selection_range(0, "end")))
        self.spin.bind("<Control-a>", lambda e: (self.spin.selection_range(0, "end"), "break")[1])
        self.more_info = tk.StringVar()
        ttk.Label(more_bar, textvariable=self.more_info, foreground="#555").pack(side="right")
        self._cutoff_job = None

        self.grid_view = CardGrid(self, on_card=self._register_drop if DND_FILES else None)
        self.grid_view.pack(fill="both", expand=True, padx=10, pady=6)
        self.bind("<Configure>", lambda e: e.widget is self and self.verdict_label.config(wraplength=max(300, self.winfo_width() - 30)))

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
        self.spin.config(state="disabled")
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
        if not self.matches and not self.exact:
            self._cards = []
            self.entries, self.shown = [], []
            self.grid_view.set_cards([])
            self.btn_more.config(state="disabled")
            self.spin.config(state="disabled")
            self.more_info.set("")
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
        self.entries = self.more_entries()
        self._start_grid()

    # ---------- the grid in the main window ----------
    def more_entries(self):
        """Everything found as (kind, percent, heading, path): identical copies first, then the rest, most similar first."""
        sens = self.sens.get()
        out = [("exact", 100, "EXACT MATCH - identical file", p) for p in self.exact]
        rest = [("crop", core.similarity_percent(s), f"Same photo, cropped - {core.similarity_percent(s)}%", p) for s, p in self.matches if p in self.cropped]
        rest += [("other", core.similarity_percent(s), f"{core.similarity_percent(s)}% similar - {core.verdict(s, sens)}", p) for s, p in self.matches if p not in self.cropped]
        rest.sort(key=lambda e: -e[1])
        return out + rest

    def _keep_crop_pct(self):
        return core.similarity_percent(core.SENSITIVITY[self.sens.get()][1])

    def _visible(self, cutoff):
        """Indexes into self.entries: identical copies, anything at least `cutoff`% similar, and cropped copies that count as a match."""
        keep = self._keep_crop_pct()
        return [i for i, e in enumerate(self.entries) if e[0] == "exact" or e[1] >= cutoff or (e[0] == "crop" and e[1] >= keep)]

    def _set_cutoff(self, pct):
        self.cutoff = pct
        self._setting = True
        try:
            self.minvar.set(str(pct))
        finally:
            self._setting = False

    def _start_grid(self):
        """First view: identical copies and everything down to 80% similar (at least the 3 best, so there is always something to compare)."""
        cut = 80
        others = [e for e in self.entries if e[0] != "exact"]
        if len([e for e in others if e[1] >= cut]) < 3 and others:
            cut = min(cut, others[min(2, len(others) - 1)][1])
        self._set_cutoff(cut)
        self.shown = self._visible(cut)
        self._refresh_grid(reset=True)

    def _refresh_grid(self, reset=False, first_new=None):
        q_head = "THIS SCAN" + ((" (auto-cropped)" if self.query_auto else " (cropped)") if self.query_crop else "")
        cards = [(q_head, self.query_path, self.query_crop, True)]
        cards += [(self.entries[i][2], self.entries[i][3], None, False) for i in self.shown]
        self._cards = [(c[0], c[1]) for c in cards]
        self.grid_view.set_cards(cards, reset=reset)
        if first_new is not None:
            self.grid_view.see(first_new + 1)
        total = len(self.entries)
        left = total - len(self.shown)
        capped = f" (the best {MORE_MAX} are kept)" if len(self.matches) >= MORE_MAX else ""
        self.more_info.set(f"showing {len(self.shown)} of {total} matches{capped}")
        self.btn_more.config(state="normal" if left > 0 else "disabled")
        self.spin.config(state="normal" if self.entries else "disabled")

    def _cutoff_changed(self):
        if self._setting:
            return
        if self._cutoff_job:
            self.after_cancel(self._cutoff_job)
        self._cutoff_job = self.after(300, self.apply_cutoff)

    def apply_cutoff(self):
        self._cutoff_job = None
        try:
            pct = max(0, min(100, int(float(self.minvar.get()))))
        except ValueError:
            return
        if not self.entries:
            return
        self.cutoff = pct
        self.shown = self._visible(pct)
        self._refresh_grid(reset=True)

    def show_more(self):
        """Bring in the next 12 matches below the ones already shown."""
        if self._cutoff_job:
            self.after_cancel(self._cutoff_job)
            self._cutoff_job = None
        have = set(self.shown)
        rest = [i for i in range(len(self.entries)) if i not in have]
        if not rest:
            return
        take = rest[:12]
        first_new = len(self.shown)
        self.shown = self.shown + take
        self._set_cutoff(min(self.cutoff, self.entries[take[-1]][1]))
        self._refresh_grid(first_new=first_new)

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
        # the first view holds the scan, the identical copy and every match down to 80%
        assert len(app.grid_view.cards) == 1 + len(app.shown) >= 2, (len(app.grid_view.cards), app.shown)
        t0 = time.time()
        while time.time() - t0 < 30 and app.grid_view.pending():
            app.update()
            time.sleep(0.05)
        assert app.grid_view.pending() == 0, "pictures did not load in the main window"
        # 'Show more matches' adds the next ones below, the cutoff box can show everything or less
        some = os.path.join(d, "x.png")
        saved = (app.entries, app.shown, app.cutoff)
        app.entries = [("exact", 100, "E", some)] + [("other", 99 - 3 * k, f"M{k}", some) for k in range(30)]  # 99 ... 12
        app._start_grid()
        assert [app.entries[i][2] for i in app.shown][:2] == ["E", "M0"] and len(app.shown) == 1 + 7, app.shown  # 99..81
        assert len(app.grid_view.cards) == 9 and app.cutoff == 80
        assert str(app.btn_more.cget("state")) == "normal"
        app.btn_more.invoke()
        app.update()
        assert len(app.grid_view.cards) == 9 + 12 and app.minvar.get() == "45", (len(app.grid_view.cards), app.minvar.get())
        assert [c[0] for c in app.grid_view.cards][9] == "M7"  # new ones come after the old ones
        app.btn_more.invoke()
        app.btn_more.invoke()
        app.update()
        assert len(app.grid_view.cards) == 32 and str(app.btn_more.cget("state")) == "disabled", len(app.grid_view.cards)
        app.minvar.set("90")
        app.apply_cutoff()
        assert [c[0] for c in app.grid_view.cards][1:] == ["E", "M0", "M1", "M2", "M3"], [c[0] for c in app.grid_view.cards]
        app.minvar.set("0")
        app.apply_cutoff()
        assert len(app.grid_view.cards) == 32
        app.minvar.set("100")
        app.apply_cutoff()
        assert [c[0] for c in app.grid_view.cards][1:] == ["E"]
        app.entries, app.shown, app.cutoff = saved
        # hundreds of matches must not freeze the window: only the cards on screen are built
        big_win = tk.Toplevel(app)
        big_win.geometry("1000x600")
        big = CardGrid(big_win)
        big.pack(fill="both", expand=True)
        t0 = time.time()
        big.set_cards([(f"M{k}", some, None, False) for k in range(500)])
        big.update()
        assert len(big.cards) == 500 and 0 < len(big._live) < 80, (len(big.cards), len(big._live))
        assert time.time() - t0 < 10, "opening 500 matches was too slow"
        big.canvas.yview_moveto(0.9)
        big.update()
        assert 0 < len(big._live) < 80 and max(big._live) > 300, (len(big._live), max(big._live, default=None))
        big_win.destroy()
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
        # the collection audit must run inside the packaged app too (spreadsheet writer, catalogue reader)
        import struct

        import audit

        proj = os.path.join(d, "audit_project")
        for sub in ("master", os.path.join("pp5", "Data"), os.path.join("pp5", "Images", "001")):
            os.makedirs(os.path.join(proj, sub))
        pa = Image.fromarray((rng.random((9, 12, 3)) * 255).astype("uint8")).resize((480, 360), Image.BICUBIC)
        pb = Image.fromarray((rng.random((9, 12, 3)) * 255).astype("uint8")).resize((480, 360), Image.BICUBIC)
        pa.save(os.path.join(proj, "master", "a.tif"))
        pb.save(os.path.join(proj, "master", "b.tif"))
        pa.resize((240, 180)).save(os.path.join(proj, "pp5", "Images", "001", "a.jpg"), quality=85)
        fields = [("OBJECTID", 20), ("IMAGEFILE", 40)]
        rec = b" " + b"2001.1".ljust(20) + b"001\\a.jpg".ljust(40)
        dbf = bytearray(struct.pack("<BBBBIHH20x", 3, 126, 1, 1, 1, 32 + 32 * 2 + 1, len(rec)))
        for fname, n in fields:
            dbf += fname.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([n]) + b"\0" * 15
        dbf += b"\r" + rec + b"\x1a"
        with open(os.path.join(proj, "pp5", "Data", "PHOTOS.DBF"), "wb") as f:
            f.write(dbf)
        arows, astats, _w = audit.run(os.path.join(proj, "master"), os.path.join(d, "audit.xlsx"), pp_root=proj,
                                      workdir=os.path.join(d, "audit_work"), log=lambda *a: None)
        assert astats["photos matched between master and database"] == 1, astats
        assert [r.sheet for r in arows] == ["Master not in database"], [(r.sheet, r.issue) for r in arows]
        assert "Master not in DB" in audit.read_xlsx(os.path.join(d, "audit.xlsx"))
        msgs.append("audit ok")
        msgs.append("drop ok")

        def fits():
            app.update()
            g = app.grid_view
            cw, ch = g.canvas.winfo_width(), g.canvas.winfo_height()
            assert ch >= 100 and g._live, ("results area too small or empty", cw, ch, len(g._live))
            for c, _pic in g._live.values():
                if c.winfo_y() < ch:  # the first row
                    assert c.winfo_x() >= 0 and c.winfo_x() + c.winfo_width() <= cw + 2, ("card sticks out sideways", cw, c.winfo_x(), c.winfo_width())
            assert app.btn_more.winfo_ismapped() and app.more_bar.winfo_y() + app.more_bar.winfo_height() <= app.winfo_height() + 2, "bottom bar cut off"

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


def audit_cli(argv):
    """Command-line collection audit: master folder vs categories vs the PastPerfect catalogue -> spreadsheet."""
    import argparse

    import audit

    ap = argparse.ArgumentParser(prog="--audit", description="Audit the master photo folder, the category folders and the PastPerfect catalogue. Read-only; writes a spreadsheet.")
    ap.add_argument("--master", required=True, help="the folder that should hold every photo")
    ap.add_argument("--categories", help="the folder that files photos into category subfolders")
    ap.add_argument("--pp", help="the PastPerfect project folder (the Data, Images and Multimedia folders are found inside it)")
    ap.add_argument("--pp-data", help="the PastPerfect Data folder (the .DBF files), if it is not found automatically")
    ap.add_argument("--pp-images", help="the PastPerfect Images folder, if it is not found automatically")
    ap.add_argument("--pp-multimedia", help="the PastPerfect Multimedia folder, if it is not found automatically")
    ap.add_argument("--out", default="collection-audit.xlsx", help="where to write the spreadsheet (default: collection-audit.xlsx here)")
    ap.add_argument("--previous", help="an earlier report: Status and Notes of problems that are still there are carried over")
    ap.add_argument("--workdir", help="where the audit keeps its own index (default: next to the report); keep it to make later runs fast")
    ap.add_argument("--reuse-index", nargs="?", const="default", help="start from the checker's own index (copied, never changed) to save time; "
                    "only helps when the same folders are at the same paths")
    a = ap.parse_args(argv)
    last = [0.0]

    def progress(done, total, what):
        if time.time() - last[0] > 5 or done == total:
            last[0] = time.time()
            print(f"  {what}: {done:,} of {total:,}", flush=True)

    reuse = core.default_db_path() if a.reuse_index == "default" else a.reuse_index
    try:
        audit.run(a.master, a.out, categories=a.categories, pp_root=a.pp, pp_data=a.pp_data, pp_images=a.pp_images,
                  pp_multimedia=a.pp_multimedia, workdir=a.workdir, reuse_index=reuse, previous=a.previous, progress=progress)
    except (ValueError, RuntimeError) as e:
        print("Could not run the audit:", e)
        return 2
    print("Open this in Excel, Numbers or LibreOffice:", os.path.abspath(a.out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--audit":
        sys.exit(audit_cli(sys.argv[2:]))
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
