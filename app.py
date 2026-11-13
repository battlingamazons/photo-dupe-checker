"""Photo Duplicate Checker - a small window for asking "have we scanned this already?"."""
import math
import os
import queue
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
COLORS = {"likely duplicate": "#b00020", "possible match": "#b26a00", "check closest": "#44546a", "looks new": "#1b7f3b"}


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


THUMB_MAX, THUMB_MIN = 300, 90
CARD_X, CARD_Y = 34, 178   # per-card horizontal / vertical overhead (borders, text, buttons)


def shorten(text, n=48):
    return text if len(text) <= n else "..." + text[-(n - 3):]


def load_pil_thumb(path, side=THUMB_MAX):
    img = core.load_image(path, max_side=side * 2).convert("RGB")
    img.thumbnail((side, side))
    return img


def thumbnail(path, size=THUMB):
    img = load_pil_thumb(path, size)
    return ImageTk.PhotoImage(img)


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
        ttk.Label(self, text="Drag on the picture to select the photo itself (leave out blank paper, scanner edges, etc.). "
                  "Drag the edges or inside the box to adjust it.", wraplength=self.MAXW).pack(padx=10, pady=(10, 4))
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
        ttk.Button(bar, text="Reset", command=lambda: self.set_fractions(None)).pack(side="left", padx=6)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.mode = None
        if initial:
            self.set_fractions(initial)
        else:
            self.auto(quiet=True)
        try:
            self.grab_set()
        except tk.TclError:
            pass

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
        self.matches, self.already = [], False
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.query_path = None
        self.query_crop = None
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

        self.results = ttk.Frame(self)
        self.results.pack(fill="both", expand=True, padx=10, pady=6)
        self.results.grid_propagate(False)
        self.results.bind("<Configure>", self._on_results_resize)

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
                s = self.index.rescan(lambda d, t, f: self.q.put(("progress", d, t)), self.cancel)
                self.q.put(("done", s))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, d, t = msg
                    self.progress.config(maximum=max(t, 1), value=d)
                    self.status.set(f"Indexing new photos: {d:,} of {t:,}")
                elif msg[0] == "done":
                    self._finish_scan(msg[1])
                elif msg[0] == "error":
                    self._set_busy(False)
                    messagebox.showerror("Problem while indexing", msg[1])
                elif msg[0] == "search_done":
                    self._search_finished(msg[1], msg[2], msg[3])
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
            self.check_path(self.query_path, crop=dlg.result)

    def check_path(self, path, crop=None):
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
        self.query_crop = crop
        self.searching = True
        self._search_token += 1
        token = self._search_token
        self.btn_check.config(state="disabled")
        self.btn_crop.config(state="disabled")
        self.btn_addfile.config(state="disabled")
        self.verdict_label.config(fg="black")
        self.verdict.set(f"Checking {os.path.basename(path)} against {self.index.count():,} photos...")

        def run():
            try:
                m, already = self.index.search(path, top=5, crop=crop)
                self.q.put(("search_done", token, m, already))
            except Exception as e:  # noqa: BLE001
                self.q.put(("search_error", token, str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _search_finished(self, token, matches, already, error=None):
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
        self.matches, self.already = matches, already
        self.show_results()

    # ---------- results ----------
    def show_results(self):
        sens = self.sens.get()
        self._pil_cache = {k: v for k, v in self._pil_cache.items() if k == self.query_path}
        if not self.matches:
            for w in self.results.winfo_children():
                w.destroy()
            self._cards = []
            self.verdict.set("No other photos in the index to compare against.")
            return
        top = self.matches[0][0]
        v = core.verdict(top, sens)
        if v == "looks new" and top >= core.SENSITIVITY[sens][1] - 0.10:
            v = "check closest"  # nothing convincing, but close enough that a worn copy could hide here
        text = {
            "likely duplicate": "Likely already scanned",
            "possible match": "Possible match - please compare by eye",
            "check closest": "Probably new - but the closest photos below are worth a quick look (a badly worn copy can score low)",
            "looks new": "Looks new - no close match found",
        }[v]
        if v in ("looks new", "check closest") and not self.query_crop:
            try:
                if core.content_box(self._pil(self.query_path)):
                    text += "\nThis scan has blank space around the photo - click Crop... to check just the photo."
            except Exception:  # noqa: BLE001
                pass
        if self.already:
            text += "   (this exact file is already in the index)"
        self.verdict.set(text)
        self.verdict_label.config(fg=COLORS[v])
        self.btn_addfile.config(state="normal" if not self.already else "disabled")
        self._cards = [("THIS SCAN" + (" (cropped)" if self.query_crop else ""), self.query_path)] + [
            (f"{core.similarity_percent(score)}% similar - {core.verdict(score, sens)}", path)
            for score, path in self.matches[:4]
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
        ttk.Label(f, text=heading, font=("Segoe UI", 10, "bold"), wraplength=t, justify="center").pack()
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
        ttk.Label(f, text=shorten(os.path.basename(path), 40), wraplength=t, justify="center").pack()
        if not is_query:
            ttk.Label(f, text=shorten(os.path.dirname(path), 52), wraplength=t, foreground="#666", justify="center").pack()
        row_f = ttk.Frame(f)
        row_f.pack(pady=4)
        side = "left" if t >= 150 else "top"
        ttk.Button(row_f, text="Open", width=7, command=lambda: open_path(path)).pack(side=side, padx=2, pady=1)
        ttk.Button(row_f, text="Show in folder", command=lambda: reveal(path)).pack(side=side, padx=2, pady=1)
        if DND_FILES:
            self._register_drop(f)

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
    else:
        App().mainloop()
