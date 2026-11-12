import os
import shutil
import sys

import numpy as np
import pytest
from PIL import Image, ImageEnhance

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def make_photo(seed, size=(400, 300)):
    """Blobby random 'photo' so different seeds look genuinely different."""
    rng = np.random.default_rng(seed)
    small = (rng.random((30, 40, 3)) * 255).astype("uint8")
    return Image.fromarray(small).resize(size, Image.BICUBIC)


@pytest.fixture
def lib(tmp_path):
    folder = tmp_path / "scans"
    folder.mkdir()
    for seed in range(40):
        make_photo(seed).save(folder / f"photo{seed}.jpg", quality=92)
    idx = core.Index(tmp_path / "index.db")
    idx.add_folder(str(folder))
    summary = idx.rescan()
    assert summary["added"] == 40 and not summary["failed"]
    return idx, folder, tmp_path


def best(idx, path):
    matches, _ = idx.search(str(path))
    return matches[0]


def test_resized_recompressed_copy_matches(lib):
    idx, folder, tmp = lib
    q = tmp / "q.png"
    make_photo(7).resize((220, 165)).save(q)
    dist, path = best(idx, q)
    assert path.endswith("photo7.jpg")
    assert core.verdict(dist) == "likely duplicate"


def test_brightness_and_grayscale_still_match(lib):
    idx, folder, tmp = lib
    q = tmp / "q.png"
    img = ImageEnhance.Brightness(make_photo(12)).enhance(0.75).convert("L")
    img.save(q)
    dist, path = best(idx, q)
    assert path.endswith("photo12.jpg")
    assert core.verdict(dist) != "looks new"


def test_sideways_scan_matches(lib):
    idx, folder, tmp = lib
    q = tmp / "q.png"
    make_photo(21).rotate(90, expand=True).save(q)
    dist, path = best(idx, q)
    assert path.endswith("photo21.jpg")
    assert core.verdict(dist) == "likely duplicate"


def test_unrelated_photo_looks_new(lib):
    idx, folder, tmp = lib
    q = tmp / "q.png"
    make_photo(999).save(q)
    dist, _ = best(idx, q)
    assert core.verdict(dist) == "looks new"


def test_incremental_rescan_and_removal(lib):
    idx, folder, tmp = lib
    again = idx.rescan()
    assert again["added"] == 0 and again["skipped"] == 40
    make_photo(500).save(folder / "new.jpg")
    os.remove(folder / "photo0.jpg")
    third = idx.rescan()
    assert third["added"] == 1 and third["removed"] == 1
    assert idx.count() == 40


def test_unavailable_folder_is_not_wiped(lib):
    idx, folder, tmp = lib
    moved = tmp / "offline"
    shutil.move(str(folder), str(moved))
    summary = idx.rescan()
    assert summary["unavailable"] == [str(folder)]
    assert idx.count() == 40


def test_add_file_then_detected_and_self_excluded(lib):
    idx, folder, tmp = lib
    new = tmp / "newscan.png"
    make_photo(777).save(new)
    assert core.verdict(best(idx, new)[0]) == "looks new"
    idx.add_file(str(new))
    matches, already = idx.search(str(new))
    assert already
    assert all(not m[1].endswith("newscan.png") for m in matches)
    copy = tmp / "copy.png"
    make_photo(777).resize((300, 225)).save(copy)
    dist, path = best(idx, copy)
    assert path.endswith("newscan.png") and core.verdict(dist) == "likely duplicate"


def test_corrupt_file_reported_not_fatal(lib):
    idx, folder, tmp = lib
    (folder / "broken.jpg").write_bytes(b"not an image")
    summary = idx.rescan()
    assert len(summary["failed"]) == 1


def test_remove_folder_clears_entries(lib):
    idx, folder, tmp = lib
    idx.remove_folder(str(folder))
    assert idx.count() == 0 and idx.folders() == []
