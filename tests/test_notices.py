import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def test_notices_found_next_to_the_mac_app(tmp_path, monkeypatch):
    """On a Mac the notices sit next to the .app, three folders above the executable."""
    exe_dir = tmp_path / "Photo Duplicate Checker.app" / "Contents" / "MacOS"
    exe_dir.mkdir(parents=True)
    (tmp_path / "THIRD_PARTY_NOTICES.txt").write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Photo Duplicate Checker"))
    assert app.find_notices() == str(tmp_path / "THIRD_PARTY_NOTICES.txt")


def test_notices_found_next_to_the_windows_exe(tmp_path, monkeypatch):
    (tmp_path / "THIRD_PARTY_NOTICES.txt").write_text("x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PhotoDupeChecker.exe"))
    assert app.find_notices() == str(tmp_path / "THIRD_PARTY_NOTICES.txt")


def test_notices_found_when_run_from_source():
    assert os.path.basename(app.find_notices()).startswith("THIRD_PARTY_NOTICES")
    assert os.path.exists(app.find_notices())
