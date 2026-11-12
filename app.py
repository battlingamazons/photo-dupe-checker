"""Photo Duplicate Checker - a small window for asking "have we scanned this already?"."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

import core

THUMB = 220
COLORS = {"likely duplicate": "#b00020", "possible match": "#b26a00", "looks new": "#1b7f3b"}


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


def thumbnail(path):
    with Image.open(path) as img:
        try:
            img.draft("RGB", (THUMB * 2, THUMB * 2))
        except Exception:
            pass
        img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((THUMB, THUMB))
    return ImageTk.PhotoImage(img)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Photo Duplicate Checker")
        self.geometry("1100x760")
        self.minsize(900, 640)
        self.index = core.Index()
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.query_path = None
        self._images = []

        self._build()
        self._refresh_folders()
        self._poll()

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
        ttk.Label(mid, text="Sensitivity:").pack(side="left", padx=(16, 4))
        self.sens = ttk.Combobox(mid, values=list(core.SENSITIVITY), state="readonly", width=10)
        self.sens.set("Generous")
        self.sens.pack(side="left")
        self.sens.bind("<<ComboboxSelected>>", lambda e: self.query_path and self.show_results())
        self.btn_addfile = ttk.Button(mid, text="Add this photo to the collection", command=self.add_current, state="disabled")
        self.btn_addfile.pack(side="right", padx=8)

        self.verdict = tk.StringVar(value="Add your folders above, then choose a scan to check.")
        self.verdict_label = tk.Label(self, textvariable=self.verdict, font=("Segoe UI", 15, "bold"), anchor="w")
        self.verdict_label.pack(fill="x", padx=12, pady=(6, 0))

        self.results = ttk.Frame(self)
        self.results.pack(fill="both", expand=True, padx=10, pady=6)

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
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish_scan(self, s):
        self._set_busy(False)
        self.progress.config(value=0)
        self._refresh_folders()
        parts = [f"{s['added']:,} added", f"{s['removed']:,} removed", f"{s['skipped']:,} unchanged"]
        self.status.set(f"{self.index.count():,} photos indexed  ({', '.join(parts)}).")
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
            filetypes=[("Photos", "*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.gif *.webp"), ("All files", "*.*")],
        )
        if not path:
            return
        if self.index.count() == 0:
            messagebox.showinfo("Nothing to compare", "The index is empty. Add a folder and let it finish scanning first.")
            return
        self.query_path = path
        try:
            self.matches, self.already = self.index.search(path)
        except Exception as e:
            messagebox.showerror("Could not read that photo", str(e))
            return
        self.show_results()

    def show_results(self):
        for w in self.results.winfo_children():
            w.destroy()
        self._images = []
        sens = self.sens.get()
        if not self.matches:
            self.verdict.set("No other photos in the index to compare against.")
            return
        top = self.matches[0][0]
        v = core.verdict(top, sens)
        text = {"likely duplicate": "Likely already scanned",
                "possible match": "Possible match - please compare by eye",
                "looks new": "Looks new - no close match found"}[v]
        if self.already:
            text += "   (this exact file is already in the index)"
        self.verdict.set(text)
        self.verdict_label.config(fg=COLORS[v])
        self.btn_addfile.config(state="normal" if not self.already else "disabled")

        self._card(0, "THIS SCAN", self.query_path, None)
        shown = [m for m in self.matches if core.verdict(m[0], sens) != "looks new"] or self.matches[:1]
        for i, (dist, path) in enumerate(shown[:4], start=1):
            label = f"{core.similarity_percent(dist)}% similar - {core.verdict(dist, sens)}"
            self._card(i, label, path, dist)
        if v == "looks new":
            ttk.Label(self.results, text="(Closest existing photo shown for reference.)").grid(row=2, column=0, columnspan=5, sticky="w", pady=4)

    def _card(self, col, heading, path, dist):
        f = ttk.Frame(self.results, padding=6, relief="groove")
        f.grid(row=0, column=col, sticky="n", padx=4, pady=4)
        ttk.Label(f, text=heading, font=("Segoe UI", 10, "bold")).pack()
        try:
            img = thumbnail(path)
            self._images.append(img)
            ttk.Label(f, image=img).pack(pady=4)
        except Exception:
            ttk.Label(f, text="(cannot preview)", width=28).pack(pady=40)
        ttk.Label(f, text=os.path.basename(path), wraplength=THUMB).pack()
        if dist is not None:
            ttk.Label(f, text=os.path.dirname(path), wraplength=THUMB, foreground="#666").pack()
        row = ttk.Frame(f)
        row.pack(pady=4)
        ttk.Button(row, text="Open", width=7, command=lambda: open_path(path)).pack(side="left", padx=2)
        ttk.Button(row, text="Show in folder", command=lambda: reveal(path)).pack(side="left", padx=2)

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
    """Used by the Windows build to prove the packaged .exe really starts up."""
    import tempfile

    import numpy as np

    msgs = []
    try:
        tk.Tk().destroy()
        msgs.append("tk ok")
        d = tempfile.mkdtemp()
        rng = np.random.default_rng(1)
        for i in range(5):
            arr = (rng.random((30, 40, 3)) * 255).astype("uint8")
            Image.fromarray(arr).resize((400, 300)).save(os.path.join(d, f"{i}.jpg"))
        idx = core.Index(os.path.join(d, "t.db"))
        idx.add_folder(d)
        s = idx.rescan()
        assert s["added"] == 5, s
        m, _ = idx.search(os.path.join(d, "2.jpg"))
        assert m, "no matches"
        thumbnail(os.path.join(d, "2.jpg"))
        msgs.append("core ok")
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
