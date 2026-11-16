"""Find faces in a photo and describe each one with 128 numbers.

Used only to catch copies that are cropped far more than the normal matching can handle: the same
faces appear (as the same pixels) in both photos, so a face that matches very closely tells us
where to look and how big the crop is. The pixels are then compared to be sure.
Runs entirely offline with two small models shipped with the program (YuNet, SFace).
"""
import os
import sys
import threading

import numpy as np

try:
    import cv2

    cv2.setNumThreads(1)
except Exception:  # noqa: BLE001
    cv2 = None

DETECT_SIDE = 1600   # photos are shrunk to this before looking for faces
MIN_FACE = 20        # ignore faces narrower than this many pixels at that size
DET_SCORE = 0.6      # detector confidence
ROT_SCORE = 0.85     # confidence needed for a face found only after turning the photo


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


AVAILABLE = cv2 is not None and os.path.isfile(resource_path("models/yunet.onnx")) and os.path.isfile(resource_path("models/sface.onnx"))
_local = threading.local()


def _models():
    if not hasattr(_local, "det"):
        _local.det = cv2.FaceDetectorYN.create(resource_path("models/yunet.onnx"), "", (320, 320), DET_SCORE)
        _local.rec = cv2.FaceRecognizerSF.create(resource_path("models/sface.onnx"), "")
    return _local.det, _local.rec


def _detect(bgr, min_score):
    det, _ = _models()
    h, w = bgr.shape[:2]
    det.setInputSize((w, h))
    _, found = det.detect(bgr)
    if found is None:
        return []
    return [f for f in found if f[14] >= min_score and f[2] >= MIN_FACE and f[3] >= MIN_FACE]


def turn(rgb, rot):
    """Turn an RGB array the way `rot` says (0 = as is)."""
    if not rot:
        return rgb
    code = {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_90_COUNTERCLOCKWISE, 3: cv2.ROTATE_180}[rot]
    return np.ascontiguousarray(cv2.rotate(rgb, code))


def find(rgb):
    """rgb: uint8 array (H, W, 3), already shrunk to DETECT_SIDE.
    Returns (rot, [(score, (x, y, w, h) as fractions of the turned photo, embedding[128] unit length)])."""
    rgb = np.ascontiguousarray(rgb)
    best, best_rgb, best_rot = _detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), DET_SCORE), rgb, 0
    if not best:  # scans are often rotated: try the other three ways up
        for rot in (1, 2, 3):
            r = turn(rgb, rot)
            got = _detect(cv2.cvtColor(r, cv2.COLOR_RGB2BGR), ROT_SCORE)
            if len(got) > len(best):
                best, best_rgb, best_rot = got, r, rot
    if not best:
        return 0, []
    _, rec = _models()
    bgr = cv2.cvtColor(best_rgb, cv2.COLOR_RGB2BGR)
    H, W = best_rgb.shape[:2]
    out = []
    for f in best:
        e = rec.feature(rec.alignCrop(bgr, f))[0].astype(np.float32)
        e /= max(float(np.linalg.norm(e)), 1e-9)
        out.append((float(f[14]), (float(f[0]) / W, float(f[1]) / H, float(f[2]) / W, float(f[3]) / H), e))
    return best_rot, out
