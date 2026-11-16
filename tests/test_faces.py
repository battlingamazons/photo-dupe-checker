"""Extremely cropped copies are found through their faces. Uses one public-domain photo
(the 1927 Solvay Conference, Wikimedia Commons)."""
import os

import pytest
from PIL import Image

import core

SRC = os.path.join(os.path.dirname(__file__), "data", "solvay_1927.jpg")
pytestmark = pytest.mark.skipif(not core.faces.AVAILABLE, reason="face models / OpenCV not available")


@pytest.fixture(scope="module")
def photo():
    return Image.open(SRC).convert("RGB")


def crop(photo, tmp_path, name, box, scale=1.0):
    w, h = photo.size
    c = photo.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
    c = c.resize((int(c.width * scale), int(c.height * scale)), Image.LANCZOS)
    p = tmp_path / name
    c.save(p, quality=85)
    return str(p)


def make_index(tmp_path, *paths):
    ix = core.Index(str(tmp_path / "t.db"))
    for p in paths:
        ix.add_file(p)
    return ix


# crops of about 5-15% of the photo, several of them enlarged or shrunk: far beyond the normal 25% per side
EXTREME = {
    "front_row": ((0.25, 0.45, 0.70, 0.75), 1.4),
    "left_part": ((0.00, 0.30, 0.35, 0.70), 1.0),
    "small_block": ((0.40, 0.40, 0.65, 0.65), 1.7),
}


@pytest.mark.parametrize("name", EXTREME)
def test_extreme_crop_is_found_when_the_stored_photo_is_the_full_one(tmp_path, photo, name):
    box, scale = EXTREME[name]
    ix = make_index(tmp_path, SRC)
    scan = crop(photo, tmp_path, name + ".jpg", box, scale)
    found, _ = ix.search(scan)
    assert found and found[0][1] == os.path.normpath(SRC) and found[0][0] >= 0.8, found
    assert os.path.normpath(SRC) in ix.last_cropped
    assert ix.crop_error is None


def test_full_photo_is_found_when_the_stored_one_is_the_extreme_crop(tmp_path, photo):
    box, scale = EXTREME["front_row"]
    stored = crop(photo, tmp_path, "stored.jpg", box, scale)
    ix = make_index(tmp_path, stored)
    found, _ = ix.search(SRC)
    assert found and found[0][1] == os.path.normpath(stored) and found[0][0] >= 0.8, found


def test_different_faces_from_the_same_photo_do_not_match(tmp_path, photo):
    """Two crops showing different people of one photo are not copies of each other."""
    a = crop(photo, tmp_path, "a.jpg", (0.00, 0.0, 0.30, 0.45))
    b = crop(photo, tmp_path, "b.jpg", (0.70, 0.45, 1.00, 0.95))
    ix = make_index(tmp_path, a)
    found, _ = ix.search(b)
    assert not found or found[0][0] < 0.66, found


def test_index_made_before_faces_existed_is_filled_in_without_a_rebuild(tmp_path):
    ix = make_index(tmp_path, SRC)
    ix._db.execute("DELETE FROM faces")
    ix._db.execute("UPDATE files SET fdone = NULL")
    ix._db.commit()
    assert ix.needs_faces() == 1
    seen = []
    ix.scan_faces(lambda d, t, f: seen.append((d, t, f)))
    assert ix.needs_faces() == 0 and seen[-1] == (1, 1, "faces")
    assert ix._db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] > 5
    assert ix._db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1  # nothing rebuilt


def test_deleted_photo_loses_its_faces(tmp_path, photo):
    folder = tmp_path / "lib"
    folder.mkdir()
    p = folder / "x.jpg"
    photo.save(p, quality=90)
    ix = core.Index(str(tmp_path / "t.db"))
    ix.add_folder(str(folder))
    ix.rescan()
    assert ix._db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] > 5
    os.remove(p)
    ix.rescan()
    assert ix._db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
