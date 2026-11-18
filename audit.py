"""Collection audit: do the master photo folder, the category folders and the PastPerfect catalogue agree?

Everything is read-only. The audit looks at four sets of files:

  master      the folder that is supposed to hold every photo
  categories  the folder that files photos into category subfolders (optional)
  database    the pictures PastPerfect uses: its Images folder (what a record shows) and its Multimedia
              folder (high-resolution masters), found through the records in the catalogue's .DBF tables

Photos are matched by what they look like (the same matching the checker uses), so a resaved, resized or
differently named copy still counts as the same photo. The result is a spreadsheet of problems for a volunteer
to work through, most important first, with a Status column to fill in. Run again later and the Status and
Notes of problems that are still there are carried over.
"""
import collections
import hashlib
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile

import core
import ppdbf

LINK = 0.85          # at least this similar: the same photo
WEAK = 0.72          # between WEAK and LINK: worth a look by eye, but not assumed
BIG_GROUP = 25       # a group this large is probably unrelated photos chained together
CATALOGS = (("Object", "OBJECTS.DBF"), ("Photo", "PHOTOS.DBF"), ("Archive", "ARCHIVES.DBF"), ("Library item", "LIBRARY.DBF"))
ROLE_LABEL = {"master": "master", "categories": "categories", "images": "database-images", "multimedia": "database-masters"}
STATUSES = ["To do", "In progress", "Fixed", "Not a problem"]


# ------------------------------------------------------------------ finding PastPerfect
def _walk(root, depth):
    root = os.path.abspath(root)
    base = root.count(os.sep)
    for dp, dns, fns in os.walk(root):
        dns.sort()
        yield dp, dns, fns
        if dp.count(os.sep) - base >= depth:
            dns[:] = []


def find_file(folder, name, depth=2):
    for dp, _dns, fns in _walk(folder, depth):
        for f in sorted(fns):
            if f.lower() == name.lower():
                return os.path.join(dp, f)
    return None


def _sibling(parent, names):
    try:
        for entry in sorted(os.listdir(parent)):
            if entry.lower() in names and os.path.isdir(os.path.join(parent, entry)):
                return os.path.join(parent, entry)
    except OSError:
        pass
    return None


def detect_pastperfect(root):
    """Find the Data, Images and Multimedia folders under a PastPerfect project folder.
    Returns {"data":, "images":, "multimedia":, "candidates": [other data folders]} (missing ones are None)."""
    cands = []
    for dp, _dns, fns in _walk(root, 5):
        low = {f.lower() for f in fns}
        if "media.dbf" in low or "photos.dbf" in low or "objects.dbf" in low:
            cands.append(dp)
    out = {"data": None, "images": None, "multimedia": None, "candidates": []}
    if not cands:
        return out
    cands.sort(key=lambda d: (d.count(os.sep), d))
    out["data"], out["candidates"] = cands[0], cands[1:]
    for key, names in (("images", {"images", "image"}), ("multimedia", {"multimedia"})):
        for up in (os.path.dirname(out["data"]), os.path.dirname(os.path.dirname(out["data"])), os.path.abspath(root)):
            found = _sibling(up, names)
            if found:
                out[key] = found
                break
    return out


# ------------------------------------------------------------------ reading the catalogue
class Rec:
    __slots__ = ("catalog", "objectid", "accessno", "title")

    def __init__(self, catalog, objectid, accessno, title):
        self.catalog, self.objectid, self.accessno, self.title = catalog, objectid, accessno, title

    def label(self):
        who = self.objectid or self.accessno
        bits = [who]
        if self.title:
            bits.append(self.title[:60])
        return " - ".join(b for b in bits if b) + f" ({self.catalog})"


class DbImage:
    """One picture a catalogue record points to. kind: 'image' (what the record shows) or 'master' (high-res)."""

    def __init__(self, rec, kind, ref, path, problem):
        self.rec, self.kind, self.ref, self.path, self.problem = rec, kind, ref, path, problem


def _clean(v):
    return "" if v is None else str(v).strip()


def load_catalogue(data_dir):
    """Return (records, media, warnings): records are Rec + their own IMAGEFILE; media maps OBJECTID -> entries."""
    warnings, records = [], []
    for catalog, fname in CATALOGS:
        path = find_file(data_dir, fname)
        if not path:
            continue
        try:
            t = ppdbf.DBFTable(path)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{fname} could not be read ({e}); its records are not in this audit.")
            continue
        n = 0
        try:
            for r in t.records():
                oid = _clean(r.get("OBJECTID"))
                if not oid:
                    continue
                title = next((_clean(r.get(k)) for k in ("OBJNAME", "TITLE", "OBJNAME2", "TITLEX") if _clean(r.get(k))), "")
                records.append((Rec(catalog, oid, _clean(r.get("ACCESSNO")), title), _clean(r.get("IMAGEFILE"))))
                n += 1
        finally:
            t.close()
        if not n:
            warnings.append(f"{fname} has no records with an OBJECTID.")
    media = collections.defaultdict(list)
    mpath = find_file(data_dir, "MEDIA.DBF")
    if mpath:
        try:
            t = ppdbf.DBFTable(mpath)
            try:
                for r in t.records():
                    oid = _clean(r.get("OBJECTID")).upper()
                    img = _clean(r.get("IMAGEFILE"))
                    hires = next((_clean(r.get(k)) for k in ("OLDHIRES", "HIRESFILE", "LOCHIGHRES")
                                  if _clean(r.get(k)) and _clean(r.get(k)).lower() != "n/a"), "")
                    if oid and (img or hires):
                        pos = r.get("POSITION")
                        media[oid].append((pos if isinstance(pos, (int, float)) else 0, img, hires))
            finally:
                t.close()
            for v in media.values():
                v.sort(key=lambda e: e[0])
        except Exception as e:  # noqa: BLE001
            warnings.append(f"MEDIA.DBF could not be read ({e}); only each record's first picture is audited.")
    else:
        warnings.append("No MEDIA.DBF found; only each record's first picture is audited.")
    return records, media, warnings


def basename(ref):
    return re.split(r"[\\/]", ref.strip())[-1].strip()


def resolve_image(ref, images_dir):
    """A record's IMAGEFILE is relative to the Images folder (backslashes, sometimes a trailing dot).
    Returns (path or None, problem or None)."""
    ref = ref.strip()
    if not ref or not images_dir:
        return None, ("not-set" if not ref else "no-folder")
    parts = [p for p in re.split(r"[\\/]", ref) if p not in ("", ".")]
    if not parts or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]) or ref.startswith(("/", "\\")):
        return None, "outside"
    full = os.path.join(images_dir, *parts)
    if os.path.isfile(full):
        return full, None
    trimmed = full.rstrip(".")
    if trimmed != full and os.path.isfile(trimmed):
        return trimmed, None
    return None, "missing"


def multimedia_names(folder):
    names, dup = {}, set()
    if folder and os.path.isdir(folder):
        for dp, _dns, fns in os.walk(folder):
            for f in fns:
                k = f.lower()
                if k in names:
                    dup.add(k)
                names[k] = os.path.join(dp, f)
    return names, dup


def database_images(records, media, images_dir, multimedia_dir):
    """Every picture the catalogue points to, as DbImage objects."""
    mnames, mdup = multimedia_names(multimedia_dir)
    out = []
    for rec, imagefile in records:
        entries = media.get(rec.objectid.upper())
        if not entries:
            entries = [(0, imagefile, "")] if imagefile else []
        for _pos, img, hires in entries:
            if img:
                path, problem = resolve_image(img, images_dir)
                out.append(DbImage(rec, "image", img, path, problem))
            if hires:
                key = basename(hires).lower()
                if key in mdup:
                    out.append(DbImage(rec, "master", hires, None, "ambiguous"))
                elif key in mnames:
                    out.append(DbImage(rec, "master", hires, mnames[key], None))
                else:
                    out.append(DbImage(rec, "master", hires, None, "missing"))
    return out


# ------------------------------------------------------------------ the index of all four sets
def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _inside(path, folder):
    path, folder = _norm(path), _norm(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def build_index(workdir, roles, reuse=None, progress=None, cancel=None, extra_files=()):
    """roles: {role: folder}. The audit has its own index in workdir, so the checker's index is never touched.
    reuse: an existing checker index to start from (copied; the original is only read)."""
    os.makedirs(workdir, exist_ok=True)
    db = os.path.join(workdir, "audit-index.db")
    if reuse and not os.path.exists(db) and os.path.isfile(reuse):
        shutil.copyfile(reuse, db)
    ix = core.Index(db)
    wanted = {os.path.normpath(os.path.abspath(f)) for f in roles.values() if f}
    for f in ix.folders():
        if f not in wanted:
            ix.remove_folder(f)
    for f in wanted:
        ix.add_folder(f)
    summary = ix.rescan(progress, cancel, faces=False)
    have = {r[0] for r in ix._db.execute("SELECT path FROM files")}
    have_n = {_norm(p) for p in have}
    for p in extra_files:  # files the folder scan skipped (for example names with no extension)
        if _norm(p) not in have_n and os.path.isfile(p):
            try:
                ix.add_file(p, faces=False)
            except Exception as e:  # noqa: BLE001
                summary["failed"].append((p, str(e)))
    return ix, summary


def role_of(path, roles):
    best = None
    for role, folder in roles.items():
        if folder and _inside(path, folder) and (best is None or len(folder) > len(roles[best])):
            best = role
    return best


# ------------------------------------------------------------------ grouping the same photo together
def cluster(ix, progress=None, cancel=None):
    """Group files that are the same photo: identical bytes first, then near copies (resaved, resized...).

    Comparing every photo with every other is the slow part (about a fifth of a second per photo for a collection
    of tens of thousands), so each result is kept in the audit's own index: an interrupted run carries on where it
    stopped, and a later run only compares photos it has not seen. Returns (clusters, weak, sha_of):
    clusters = [[paths]], weak = {sha: [(score, other sha)]} for 'maybe' matches, sha_of = {path: sha}."""
    ix._db.executescript(
        "CREATE TABLE IF NOT EXISTS audit_edges (a TEXT, b TEXT, score REAL, PRIMARY KEY (a, b));"
        "CREATE TABLE IF NOT EXISTS audit_done (sha TEXT PRIMARY KEY);")
    rows = ix._db.execute("SELECT path, sha FROM files").fetchall()
    by_sha = collections.defaultdict(list)
    sha_of = {}
    for p, s in rows:
        key = s or p
        by_sha[key].append(p)
        sha_of[p] = key
    done = {r[0] for r in ix._db.execute("SELECT sha FROM audit_done")}
    todo = [k for k in by_sha if k not in done]
    for n, key in enumerate(todo, 1):
        if cancel and cancel.is_set():
            break
        paths = by_sha[key]
        try:
            found, _own = ix.search(paths[0], top=len(paths) + 12, use_crop=False)
        except Exception:  # noqa: BLE001  (an unreadable file is simply left on its own)
            found = []
        for score, p in found:
            other = sha_of.get(p)
            if other is None or other == key or score < WEAK:
                continue
            for a, b in ((key, other), (other, key)):  # the match is the same whichever photo was the query
                ix._db.execute("INSERT INTO audit_edges (a, b, score) VALUES (?,?,?) ON CONFLICT(a, b) DO UPDATE SET score = MAX(score, excluded.score)",
                               (a, b, float(score)))
        ix._db.execute("INSERT OR REPLACE INTO audit_done (sha) VALUES (?)", (key,))
        if n % 200 == 0:
            ix._db.commit()
        if progress:
            progress(n, len(todo), "comparing photos")
    ix._db.commit()
    parent = {k: k for k in by_sha}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    weak = collections.defaultdict(list)
    for a, b, score in ix._db.execute("SELECT a, b, score FROM audit_edges ORDER BY score DESC"):
        if a not in by_sha or b not in by_sha:
            continue  # a photo that has since gone
        if score >= LINK:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        elif len(weak[a]) < 3:
            weak[a].append((score, b))
    groups = collections.defaultdict(list)
    for key, paths in by_sha.items():
        groups[find(key)].extend(paths)
    return [sorted(v) for v in groups.values()], weak, sha_of


# ------------------------------------------------------------------ turning groups into problems
class Row:
    def __init__(self, sheet, priority, issue, action, key_parts, master=(), cats=(), recs=(), dbfiles=(), maybe="", detail=""):
        self.sheet, self.priority, self.issue, self.action = sheet, priority, issue, action
        self.master, self.cats, self.recs, self.dbfiles, self.maybe, self.detail = list(master), list(cats), list(recs), list(dbfiles), maybe, detail
        digest = hashlib.sha1("|".join(sorted(key_parts)).encode("utf-8", "replace")).hexdigest()[:16]
        self.key = f"{issue[:24]}|{digest}"


def _rel(path, roles):
    role = role_of(path, roles)
    if not role:
        return path
    rel = os.path.relpath(path, roles[role]).replace(os.sep, "/")
    return f"{ROLE_LABEL[role]}/{rel}"


def _cap(items, n=6):
    items = list(items)
    return items if len(items) <= n else items[:n] + [f"... and {len(items) - n} more"]


def analyse(ix, roles, db_items, clusters, weak, sha_of, have_categories=True):
    """Compare the groups with what the catalogue points to. Returns (rows, stats)."""
    by_path = collections.defaultdict(list)
    for it in db_items:
        if it.path:
            by_path[_norm(it.path)].append(it)
    sizes = {}
    for p, s in ix._db.execute("SELECT path, size FROM files"):
        sizes[p] = s or 0
    rows, st = [], collections.Counter()
    covered = set()
    sha_members = collections.defaultdict(list)
    for p, k in sha_of.items():
        sha_members[k].append(p)

    def recs_of(paths):
        seen, out = set(), []
        for p in paths:
            for it in by_path.get(_norm(p), []):
                lab = it.rec.label()
                if lab not in seen:
                    seen.add(lab)
                    out.append(lab)
        return out

    def best_maybe(paths, want_roles):
        """Closest 'maybe' match among files with these roles, as text."""
        best = None
        for p in paths:
            for score, other_sha in weak.get(sha_of.get(p), []):
                for other in sha_members.get(other_sha, []):
                    if role_of(other, roles) in want_roles and (best is None or score > best[0]):
                        best = (score, other)
        return f"{round(best[0] * 100)}% similar: {_rel(best[1], roles)}" if best else ""

    for group in clusters:
        G = [p for p in group if role_of(p, roles) == "master"]
        C = [p for p in group if role_of(p, roles) == "categories"]
        P = [p for p in group if role_of(p, roles) in ("images", "multimedia")]
        Pref = [p for p in P if _norm(p) in by_path]
        for p in Pref:
            covered.add(_norm(p))
        n_rec = len(recs_of(Pref))
        rel = lambda ps: [_rel(p, roles) for p in ps]  # noqa: E731
        if len(group) > BIG_GROUP:
            rows.append(Row("Check by eye", 2, f"A group of {len(group)} files that look alike - probably not all the same photo",
                            "Check by eye; this group was too big to trust automatically.", rel(group),
                            master=_cap(rel(G)), cats=_cap(rel(C)), recs=_cap(recs_of(Pref)), dbfiles=_cap(rel(Pref))))
            st["big groups"] += 1
            continue
        if G and Pref:
            st["photos matched between master and database"] += 1
            if n_rec > 1:
                rows.append(Row("Database", 3, f"The same photo is attached to {n_rec} different catalogue records",
                                "Check whether these records are the same item catalogued twice.", rel(group),
                                master=rel(G), cats=_cap(rel(C)), recs=_cap(recs_of(Pref)), dbfiles=_cap(rel(Pref))))
                st["one photo, several records"] += 1
        elif G and not Pref:
            maybe = best_maybe(G, ("images", "multimedia"))
            rows.append(Row("Master not in database", 1 if not maybe else 2, "In the master folder, but no catalogue record uses it",
                            "Check whether it is catalogued under a different picture; if not, catalogue it or note why not." if maybe
                            else "Catalogue it, or note why it is not catalogued.", rel(G),
                            master=_cap(rel(G)), cats=_cap(rel(C)), maybe=maybe))
            st["master photos not in database"] += 1
        elif Pref and not G:
            maybe = best_maybe(Pref, ("master",))
            where = " A copy is in the categories folder." if C else ""
            rows.append(Row("Database not in master", 1, "A catalogue record uses this picture, but there is no matching file in the master folder",
                            ("Check the possible match; if it is the same, nothing is missing." if maybe else
                             "Put the best scan into the master folder (or find it)." ) + where, rel(group),
                            cats=_cap(rel(C)), recs=_cap(recs_of(Pref)), dbfiles=_cap(rel(Pref)), maybe=maybe))
            st["database pictures not in master"] += 1
        elif C and not G and not P:
            maybe = best_maybe(C, ("master", "images", "multimedia"))
            rows.append(Row("Categories", 2, "Only in the categories folder - not in the master folder or the database",
                            "Check the possible match; otherwise add it to the master folder.", rel(group),
                            cats=_cap(rel(C)), maybe=maybe))
            st["category-only photos"] += 1
        if len(G) > 1:
            keep = max(G, key=lambda p: sizes.get(p, 0))
            ident = len({ix._db.execute("SELECT sha FROM files WHERE path = ?", (p,)).fetchone()[0] for p in G}) == 1
            rows.append(Row("Duplicates in master", 2 if ident else 1,
                            f"{len(G)} {'identical' if ident else 'versions of the same photo'} in the master folder",
                            ("Keep one and remove the others." if ident else
                             f"Different versions: probably keep the largest ({_rel(keep, roles)}) - compare before deleting."),
                            rel(G), master=_cap(rel(G)), cats=_cap(rel(C)), recs=_cap(recs_of(Pref)), dbfiles=_cap(rel(Pref))))
            st["duplicate sets in master"] += 1
        if G and not C and have_categories:
            rows.append(Row("Not filed in categories", 4, "In the master folder, but not filed in any category",
                            "File it in the right category folder.", rel(G), master=_cap(rel(G))))
            st["master photos not filed in categories"] += 1
    for it in db_items:
        if it.problem in ("missing", "outside", "ambiguous", "no-folder"):
            what = {"missing": "names a picture that is not on disk", "outside": "has a picture reference that cannot be used (it points outside the Images folder)",
                    "ambiguous": "names a master that exists in more than one place under Multimedia",
                    "no-folder": "has a picture reference but the Images folder was not found"}[it.problem]
            rows.append(Row("Database broken links", 1, f"A catalogue record {what}",
                            "Find the file and fix the link in PastPerfect.", [it.rec.objectid, it.kind, it.ref],
                            recs=[it.rec.label()], dbfiles=[f"{it.kind}: {it.ref}"]))
            st["broken picture links"] += 1
    unused = [p for p in ix._db.execute("SELECT path FROM files").fetchall() if role_of(p[0], roles) in ("images", "multimedia") and _norm(p[0]) not in covered]
    st["database picture files no record uses"] = len(unused)
    rows.sort(key=lambda r: (r.priority, r.sheet, r.issue, r.master[:1], r.dbfiles[:1]))
    return rows, st


# ------------------------------------------------------------------ the spreadsheet
COLS = ["#", "Priority", "What's wrong", "Suggested next step", "Master file(s)", "Category copies", "Database record(s)",
        "Database picture(s)", "Closest possible match", "Status", "Done by", "Notes", "Key"]
SHEET_ORDER = ["Database not in master", "Master not in database", "Duplicates in master", "Database broken links",
               "Database", "Categories", "Not filed in categories", "Check by eye"]
SHEET_TITLE = {"Database not in master": "DB not in master", "Master not in database": "Master not in DB",
               "Duplicates in master": "Duplicates in master", "Database broken links": "Broken DB links",
               "Database": "DB oddities", "Categories": "Categories only", "Not filed in categories": "Not in categories",
               "Check by eye": "Check by eye"}
PRIORITY_NAME = {1: "High", 2: "Medium", 3: "Low", 4: "Low"}


def read_xlsx(path):
    """Tiny reader for the spreadsheets this module writes: {sheet name: [[cell text, ...], ...]}."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out = {}
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                strings.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sh in wb.find("m:sheets", ns):
            rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rels[rid].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter("{%s}row" % ns["m"]):
                cells = {}
                for c in row.findall("m:c", ns):
                    col = 0
                    for ch in re.match(r"[A-Z]+", c.get("r")).group():
                        col = col * 26 + ord(ch) - 64
                    t = c.get("t")
                    v = c.find("m:v", ns)
                    if t == "s" and v is not None:
                        val = strings[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    cells[col - 1] = val
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
            out[sh.get("name")] = rows
    return out


def previous_marks(path):
    """Status / Done by / Notes already filled in by the volunteer, keyed by the problem's Key."""
    marks = {}
    try:
        book = read_xlsx(path)
    except Exception:  # noqa: BLE001
        return marks
    for _name, rows in book.items():
        if not rows or "Key" not in rows[0]:
            continue
        head = rows[0]
        ki, si, di, ni = (head.index(h) if h in head else None for h in ("Key", "Status", "Done by", "Notes"))
        for r in rows[1:]:
            get = lambda i: (r[i] if i is not None and i < len(r) else "")  # noqa: E731
            if get(ki):
                marks[get(ki)] = (get(si), get(di), get(ni))
    return marks


def write_report(path, rows, stats, info, carry=None):
    import xlsxwriter

    carry = carry or {}
    wb = xlsxwriter.Workbook(path)
    bold = wb.add_format({"bold": True})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    head = wb.add_format({"bold": True, "bg_color": "#DDE6F0", "border": 1, "text_wrap": True, "valign": "top"})
    title = wb.add_format({"bold": True, "font_size": 16})
    pri = {1: wb.add_format({"valign": "top", "bg_color": "#F8D7DA"}), 2: wb.add_format({"valign": "top", "bg_color": "#FFF3CD"}),
           3: wb.add_format({"valign": "top"}), 4: wb.add_format({"valign": "top"})}

    ws = wb.add_worksheet("Start here")
    ws.set_column(0, 0, 4)
    ws.set_column(1, 1, 95)
    ws.write(0, 1, "Collection audit", title)
    line = 2
    for text in info.get("intro", []):
        ws.write(line, 1, text, wrap)
        line += 1
    line += 1
    ws.write(line, 1, "What was compared", bold)
    line += 1
    for text in info.get("compared", []):
        ws.write(line, 1, text, wrap)
        line += 1
    line += 1
    ws.write(line, 1, "What the audit found", bold)
    line += 1
    counts = collections.Counter(r.sheet for r in rows)
    for name in SHEET_ORDER:
        if counts.get(name):
            ws.write(line, 1, f"{SHEET_TITLE[name]}: {counts[name]} to look at", wrap)
            line += 1
    for k, v in sorted(stats.items()):
        ws.write(line, 1, f"{k}: {v}", wrap)
        line += 1
    line += 1
    ws.write(line, 1, "Notes and warnings", bold)
    line += 1
    for text in info.get("warnings", []) or ["(none)"]:
        ws.write(line, 1, text, wrap)
        line += 1
    line += 1
    ws.write(line, 1, "How to work through it", bold)
    line += 1
    for text in ("Start with the first sheet and work down. Rows are in order of importance (High first).",
                 "Paths start with the folder they came from (master, categories, database-images, database-masters) so you can find the file.",
                 "Set Status as you go (To do / In progress / Fixed / Not a problem) and add Notes. Use the filter arrows to hide finished rows.",
                 "'Closest possible match' is a guess below the confidence needed to say two files are the same photo - always look at both.",
                 "Nothing is changed by this audit. Every fix is made by hand in the folders or in PastPerfect.",
                 "Run the audit again after fixing things. Rows that are still a problem keep their Status and Notes; fixed ones disappear."):
        ws.write(line, 1, "- " + text, wrap)
        line += 1

    for name in SHEET_ORDER:
        mine = [r for r in rows if r.sheet == name]
        if not mine:
            continue
        sh = wb.add_worksheet(SHEET_TITLE[name][:31])
        for c, h in enumerate(COLS):
            sh.write(0, c, h, head)
        widths = [5, 9, 38, 38, 42, 34, 42, 42, 34, 13, 12, 30, 8]
        for c, w in enumerate(widths):
            sh.set_column(c, c, w, None, {"hidden": True} if COLS[c] == "Key" else {})
        for i, r in enumerate(mine, 1):
            status, who, notes = carry.get(r.key, ("To do", "", ""))
            vals = [i, PRIORITY_NAME[r.priority], r.issue, r.action, "\n".join(r.master), "\n".join(r.cats), "\n".join(r.recs),
                    "\n".join(r.dbfiles), r.maybe, status or "To do", who, notes, r.key]
            for c, v in enumerate(vals):
                sh.write(i, c, v, pri[r.priority] if c < 2 else wrap)
        sh.freeze_panes(1, 3)
        sh.autofilter(0, 0, len(mine), len(COLS) - 1)
        sh.data_validation(1, COLS.index("Status"), len(mine), COLS.index("Status"), {"validate": "list", "source": STATUSES})
    wb.close()


# ------------------------------------------------------------------ the whole thing
def run(master, out_path, categories=None, pp_root=None, pp_data=None, pp_images=None, pp_multimedia=None,
        workdir=None, reuse_index=None, previous=None, progress=None, cancel=None, log=print):
    """Run the audit and write the spreadsheet. Returns (rows, stats, warnings)."""
    t0 = time.time()
    warnings = []
    if not master or not os.path.isdir(master):
        raise ValueError(f"The master folder was not found: {master}")
    if categories and not os.path.isdir(categories):
        raise ValueError(f"The categories folder was not found: {categories}")
    pp = {"data": pp_data, "images": pp_images, "multimedia": pp_multimedia, "candidates": []}
    if pp_root:
        found = detect_pastperfect(pp_root)
        for k in ("data", "images", "multimedia"):
            pp[k] = pp[k] or found[k]
        pp["candidates"] = found["candidates"]
    for label, folder in (("PastPerfect Data", pp["data"]), ("PastPerfect Images", pp["images"]), ("PastPerfect Multimedia", pp["multimedia"])):
        log(f"  {label} folder: {folder or 'not found'}")
    if not pp["data"]:
        raise ValueError("No PastPerfect data (.DBF files) found. Give the project folder, or the Data folder with --pp-data.")
    if pp["candidates"]:
        warnings.append("More than one folder with PastPerfect tables was found; this audit used " + pp["data"]
                        + ". Others: " + "; ".join(pp["candidates"][:3]))
    if not pp["images"]:
        warnings.append("The PastPerfect Images folder was not found, so record pictures could not be checked. Give it with --pp-images.")
    if not pp["multimedia"]:
        warnings.append("The PastPerfect Multimedia folder was not found, so high-resolution masters were not checked. Give it with --pp-multimedia.")
    roles = {"master": os.path.abspath(master)}
    if categories:
        roles["categories"] = os.path.abspath(categories)
    if pp["images"]:
        roles["images"] = os.path.abspath(pp["images"])
    if pp["multimedia"]:
        roles["multimedia"] = os.path.abspath(pp["multimedia"])
    folders = list(roles.items())
    for i, (ra, fa) in enumerate(folders):
        for rb, fb in folders[i + 1:]:
            if _inside(fa, fb) or _inside(fb, fa):
                raise ValueError(f"The {ra} folder and the {rb} folder overlap, so files would be counted in both. Point them at separate folders.")
    log("Reading the catalogue ...")
    records, media, warn = load_catalogue(pp["data"])
    warnings += warn
    items = database_images(records, media, roles.get("images"), roles.get("multimedia"))
    log(f"  {len(records):,} catalogue records, {len(items):,} pictures they point to")
    log("Indexing the photo folders (the first run takes a while; later runs only look at what changed) ...")
    workdir = workdir or os.path.join(os.path.dirname(os.path.abspath(out_path)), "audit-work")
    ix, summary = build_index(workdir, roles, reuse_index, progress, cancel, [it.path for it in items if it.path])
    if cancel and cancel.is_set():
        raise RuntimeError("Cancelled.")
    if summary["failed"]:
        warnings.append(f"{len(summary['failed'])} files could not be read as photos and were left out (for example {os.path.basename(summary['failed'][0][0])}).")
    if summary["unavailable"]:
        warnings.append("These folders could not be reached: " + "; ".join(summary["unavailable"]))
    log(f"  {ix.count():,} photos indexed")
    log("Working out which files are the same photo ...")
    clusters, weak, sha_of = cluster(ix, progress, cancel)
    if cancel and cancel.is_set():
        raise RuntimeError("Cancelled.")
    log("Comparing ...")
    rows, stats = analyse(ix, roles, items, clusters, weak, sha_of, have_categories=bool(categories))
    stats["catalogue records"] = len(records)
    stats["pictures the catalogue points to"] = len(items)
    stats["photos in the index"] = ix.count()
    carry = previous_marks(previous) if previous and os.path.isfile(previous) else {}
    info = {
        "intro": ["A read-only comparison of the master photo folder, the category folders and the PastPerfect catalogue. The master folder is treated as the source of truth.",
                  f"Made {time.strftime('%Y-%m-%d %H:%M')}. A photo counts as 'the same' when it looks the same (a resaved, resized or renamed copy still matches), not when the file names agree."],
        "compared": [f"master: {os.path.basename(roles['master'])}"] + ([f"categories: {os.path.basename(roles['categories'])}"] if "categories" in roles else [])
                    + [f"database pictures: {len(items):,} from {len(records):,} records"],
        "warnings": warnings,
    }
    write_report(out_path, rows, stats, info, carry)
    log(f"Done in {time.time() - t0:.0f} s. Report: {out_path}")
    return rows, stats, warnings
