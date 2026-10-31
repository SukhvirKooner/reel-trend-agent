"""Frame utilities: cut-rate measurement and perceptual hashes (measured, not guessed)."""
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image


def list_frames(frame_dir):
    """Frames are saved as t<seconds*10:05d>.jpg so lexical order == time order."""
    return sorted(Path(frame_dir).glob("t*.jpg"))


def frame_time(path):
    return int(Path(path).stem[1:]) / 10.0


def _hist(path):
    img = Image.open(path).convert("L").resize((160, 284))
    h, _ = np.histogram(np.asarray(img), bins=64, range=(0, 255))
    return h / max(h.sum(), 1)


def cuts_per_10s(frame_paths, duration_s=None, threshold=0.35):
    """Grayscale-histogram difference between consecutive 1 fps frames; a big jump = a cut.

    Only frames that are ~1s apart are compared (the 2s tail would over-count).
    """
    frames = list(frame_paths)
    if len(frames) < 2:
        return None
    cuts, span = 0, 0.0
    prev_t, prev_h = frame_time(frames[0]), _hist(frames[0])
    for f in frames[1:]:
        t, h = frame_time(f), _hist(f)
        dt = t - prev_t
        # Only ~1 fps pairs. The 2 s tail would count a scene change as a cut.
        # L1 distance between normalised histograms lies in [0, 2].
        if 0.5 <= dt <= 1.5:
            if np.abs(h - prev_h).sum() > threshold:
                cuts += 1
            span += dt
        prev_t, prev_h = t, h
    if span <= 0:
        return None
    span = duration_s or span
    return round(cuts / max(span, 1.0) * 10, 2)


def phash(path):
    return str(imagehash.phash(Image.open(path)))


def phash_distance(a, b):
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
