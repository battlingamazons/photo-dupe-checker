"""The collection audit, on a made-up collection: random-noise 'photos' and a hand-built PastPerfect catalogue."""
import os
import struct

import numpy as np
import pytest
from PIL import Image

import audit
import core


def write_dbf(path, fields, rows):
    """A minimal dBase table: fields = [(name, length)], rows = list of dicts (all character fields)."""
    rec_len = 1 + sum(n for _f, n in fields)
    head_len = 32 + 32 * len(fields) + 1
    out = bytearray(struct.pack("<BBBBIHH20x", 3, 126, 1, 1, len(rows), head_len, rec_len))
    for name, n in fields:
        out += name.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([n]) + b"\0" * 15
    out += b"\r"
    for r in rows:
        out += b" "
        for name, n in fields:
            out += str(r.get(name, "")).encode("latin1")[:n].ljust(n, b" ")
    out += b"\x1a"
    with open(path, "wb") as f:
        f.write(out)


def noise(seed, size=(480, 360)):
    rng = np.random.default_rng(seed)
    return Image.fromarray((rng.random((9, 12, 3)) * 255).astype("uint8")).resize(size, Image.BICUBIC)


@pytest.fixture()
def world(tmp_path):
    root = tmp_path / "project"
    master, cats = root / "master", root / "categories"
    data, images, multi = root / "pp5" / "Data", root / "pp5" / "Images", root / "pp5" / "Multimedia"
    for d in (master / "sub", cats / "Schools", cats / "People", data, images / "001", images / "008", multi):
        d.mkdir(parents=True)
    P = {k: noise(i + 1) for i, k in enumerate("ABCDEFGH")}
    # A: in master, in database (smaller jpg), in categories
    P["A"].save(master / "A.tif"); P["A"].resize((240, 180)).save(images / "001" / "A.jpg", quality=85); P["A"].save(cats / "Schools" / "A.tif")
    # B: only in master (never catalogued)
    P["B"].save(master / "sub" / "B.tif")
    # C: catalogued, no master anywhere
    P["C"].resize((240, 180)).save(images / "001" / "C.jpg", quality=85)
    # D: twice in the master folder (a tif and a resaved jpg under another name), catalogued
    P["D"].save(master / "D.tif"); P["D"].save(master / "sub" / "D resaved.jpg", quality=90); P["D"].resize((240, 180)).save(images / "001" / "D.jpg", quality=85)
    # E: master + both category folders (identical), catalogued under two records
    P["E"].save(master / "E.tif"); P["E"].save(cats / "Schools" / "E.tif"); P["E"].save(cats / "People" / "E.tif")
    P["E"].resize((240, 180)).save(images / "001" / "E.jpg", quality=85)
    # F: only in the categories folder
    P["F"].save(cats / "People" / "F.tif")
    # G: its record names the file with a trailing dot and no extension, as PastPerfect does
    P["G"].save(master / "G.tif"); P["G"].resize((240, 180)).save(images / "008" / "2012231", format="JPEG")
    # H: a catalogue record whose picture file is gone
    fields = [("OBJECTID", 20), ("ACCESSNO", 20), ("OBJNAME", 40), ("IMAGEFILE", 60)]
    write_dbf(data / "PHOTOS.DBF", fields, [
        {"OBJECTID": "2001.1", "ACCESSNO": "2001.1", "OBJNAME": "Photo of A", "IMAGEFILE": "001\\A.jpg"},
        {"OBJECTID": "2001.3", "OBJNAME": "Photo of C", "IMAGEFILE": "001\\C.jpg"},
        {"OBJECTID": "2001.4", "OBJNAME": "Photo of D", "IMAGEFILE": "001\\D.jpg"},
        {"OBJECTID": "2001.5", "OBJNAME": "Photo of E", "IMAGEFILE": "001\\E.jpg"},
        {"OBJECTID": "2001.55", "OBJNAME": "E again", "IMAGEFILE": "001\\E.jpg"},
        {"OBJECTID": "2001.7", "OBJNAME": "Photo of G", "IMAGEFILE": "008\\2012231."},
        {"OBJECTID": "2001.8", "OBJNAME": "Photo of H", "IMAGEFILE": "001\\gone.jpg"},
    ])
    write_dbf(data / "MEDIA.DBF", [("OBJECTID", 20), ("IMAGEFILE", 60), ("POSITION", 4)],
              [{"OBJECTID": "2001.1", "IMAGEFILE": "001\\A.jpg", "POSITION": "1"}])
    return {"root": str(root), "master": str(master), "cats": str(cats), "out": str(tmp_path / "report.xlsx")}


def by_issue(rows):
    d = {}
    for r in rows:
        d.setdefault(r.sheet, []).append(r)
    return d


def text(r):
    return " ".join(r.master + r.cats + r.recs + r.dbfiles) + " " + r.issue


def test_detects_the_catalogue_folders(world):
    pp = audit.detect_pastperfect(world["root"])
    assert pp["data"].endswith("Data") and pp["images"].endswith("Images") and pp["candidates"] == []
    assert pp["multimedia"].endswith("Multimedia")


def test_audit_finds_each_kind_of_problem(world, tmp_path):
    rows, stats, warnings = audit.run(world["master"], world["out"], categories=world["cats"], pp_root=world["root"],
                                      workdir=str(tmp_path / "work"), log=lambda *a: None)
    got = by_issue(rows)
    # master photo nobody catalogued
    assert any("B.tif" in text(r) for r in got["Master not in database"]), got.keys()
    assert not any("A.tif" in text(r) for r in got.get("Master not in database", []))
    assert not any("2012231" in text(r) for r in got.get("Master not in database", []))  # the trailing-dot file resolved
    # catalogued picture with no master
    gone = got["Database not in master"]
    assert len(gone) == 1 and "C.jpg" in text(gone[0]) and "2001.3" in text(gone[0])
    # two versions in the master
    dups = got["Duplicates in master"]
    assert len(dups) == 1 and "D.tif" in text(dups[0]) and "D resaved.jpg" in text(dups[0])
    # same picture on two records
    assert any("E" in text(r) and "2 different" in r.issue for r in got["Database"])
    # only in categories
    assert [r for r in got["Categories"] if "F.tif" in text(r)]
    # not filed in categories
    unfiled = " ".join(text(r) for r in got["Not filed in categories"])
    assert "B.tif" in unfiled and "G.tif" in unfiled and "A.tif" not in unfiled
    # broken link
    assert len(got["Database broken links"]) == 1 and "gone.jpg" in text(got["Database broken links"][0])
    assert stats["photos matched between master and database"] >= 4


def test_report_is_readable_and_status_is_carried_over(world, tmp_path):
    audit.run(world["master"], world["out"], categories=world["cats"], pp_root=world["root"], workdir=str(tmp_path / "work"), log=lambda *a: None)
    book = audit.read_xlsx(world["out"])
    assert "Start here" in book and "DB not in master" in book
    sheet = book["DB not in master"]
    assert sheet[0][:3] == ["#", "Priority", "What's wrong"]
    assert sheet[1][audit.COLS.index("Status")] == "To do"
    # a volunteer marks it, then the audit is run again: the mark stays
    import zipfile, shutil
    marks = audit.previous_marks(world["out"])
    key = sheet[1][audit.COLS.index("Key")]
    assert key in marks
    from audit import write_report
    # rewrite the first report with a note on that row by going through carry
    rows, stats, _ = audit.run(world["master"], world["out"], categories=world["cats"], pp_root=world["root"], workdir=str(tmp_path / "work"), log=lambda *a: None)
    carry = {key: ("Fixed", "Sam", "found the scan")}
    write_report(world["out"], rows, stats, {"warnings": []}, carry)
    again = tmp_path / "again.xlsx"
    audit.run(world["master"], str(again), categories=world["cats"], pp_root=world["root"], workdir=str(tmp_path / "work"),
              previous=world["out"], log=lambda *a: None)
    sheet2 = audit.read_xlsx(str(again))["DB not in master"]
    row = [r for r in sheet2[1:] if r[audit.COLS.index("Key")] == key][0]
    assert row[audit.COLS.index("Status")] == "Fixed" and row[audit.COLS.index("Notes")] == "found the scan"


def test_overlapping_folders_are_refused(world, tmp_path):
    with pytest.raises(ValueError):
        audit.run(world["master"], world["out"], categories=os.path.join(world["master"], "sub"), pp_root=world["root"],
                  workdir=str(tmp_path / "w"), log=lambda *a: None)


def test_existing_index_is_copied_not_changed(world, tmp_path):
    ix = core.Index(str(tmp_path / "theirs.db"))
    ix.add_folder(world["master"])
    ix.rescan(faces=False)
    ix._db.close()
    before = os.path.getsize(tmp_path / "theirs.db"), os.path.getmtime(tmp_path / "theirs.db")
    audit.run(world["master"], world["out"], categories=world["cats"], pp_root=world["root"], workdir=str(tmp_path / "w2"),
              reuse_index=str(tmp_path / "theirs.db"), log=lambda *a: None)
    assert (os.path.getsize(tmp_path / "theirs.db"), os.path.getmtime(tmp_path / "theirs.db")) == before


def test_comparisons_are_remembered_between_runs(world, tmp_path, monkeypatch):
    kw = dict(categories=world["cats"], pp_root=world["root"], workdir=str(tmp_path / "work"), log=lambda *a: None)
    audit.run(world["master"], world["out"], **kw)
    calls = []
    real = core.Index.search

    def spy(self, *a, **k):
        calls.append(a)
        return real(self, *a, **k)

    monkeypatch.setattr(core.Index, "search", spy)
    rows, _stats, _w = audit.run(world["master"], world["out"], **kw)
    assert calls == [], "the second run should not compare anything again"
    assert {r.sheet for r in rows} >= {"Master not in database", "Database not in master"}
    # one new photo: only that one is compared
    noise(99).save(os.path.join(world["master"], "new.tif"))
    audit.run(world["master"], world["out"], **kw)
    assert len(calls) == 1
