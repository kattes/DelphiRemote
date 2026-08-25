"""Visual regression helpers for VCL UI tests.

Combines a perceptual hash (``imagehash.phash``) with a per-pixel diff ratio
so a test can decide whether a render still looks like its baseline. The
perceptual hash catches gross layout/color regressions; the pixel-ratio
catches subtle shifts that don't change the perceptual fingerprint.

Baselines live next to the test in ``baselines/<name>.png`` by default —
override with ``baseline_dir=`` if you keep them elsewhere. New baselines
can be captured by setting the env var ``DELPHI_REMOTE_UPDATE_BASELINES=1``
before running the test; matched baselines are then written rather than
compared.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

UPDATE_BASELINES_ENV = "DELPHI_REMOTE_UPDATE_BASELINES"


@dataclass
class VisualDiff:
    """Quantified comparison between an actual image and its baseline."""
    perceptual_distance: int  # imagehash Hamming distance (lower = closer)
    diff_pixel_ratio: float   # fraction of pixels whose abs-diff > threshold
    max_pixel_diff: int       # max channel-sum delta across all pixels
    width_match: bool
    height_match: bool

    def is_match(self, *, max_perceptual: int = 5,
                 max_pixel_ratio: float = 0.02) -> bool:
        if not (self.width_match and self.height_match):
            return False
        if self.perceptual_distance > max_perceptual:
            return False
        if self.diff_pixel_ratio > max_pixel_ratio:
            return False
        return True


def _open_rgb(path: Path) -> Any:
    from PIL import Image

    return Image.open(path).convert("RGB")


def compare_images(actual: Path | str, baseline: Path | str,
                   *, channel_diff_threshold: int = 30) -> VisualDiff:
    """Quantify how much ``actual`` differs from ``baseline``.

    ``channel_diff_threshold``:
      A pixel counts as "different" if the sum of its absolute RGB channel
      deltas exceeds this. 30 corresponds to a ~10-per-channel shift, which
      is below human-noticeable for natural images but above JPEG noise.
    """
    import imagehash
    from PIL import ImageChops
    import numpy as np

    actual_path = Path(actual)
    baseline_path = Path(baseline)
    if not actual_path.is_file():
        raise FileNotFoundError(f"Actual image not found: {actual_path}")
    if not baseline_path.is_file():
        raise FileNotFoundError(f"Baseline image not found: {baseline_path}")

    a_img = _open_rgb(actual_path)
    b_img = _open_rgb(baseline_path)

    width_match = a_img.size[0] == b_img.size[0]
    height_match = a_img.size[1] == b_img.size[1]

    if not (width_match and height_match):
        # Resize baseline to actual just so the hash comparison is meaningful;
        # is_match() still flags the dimension mismatch.
        b_img_for_hash = b_img.resize(a_img.size)
    else:
        b_img_for_hash = b_img

    phash_actual = imagehash.phash(a_img)
    phash_baseline = imagehash.phash(b_img_for_hash)
    perceptual_distance = int(phash_actual - phash_baseline)

    if width_match and height_match:
        diff = ImageChops.difference(a_img, b_img)
        arr = np.asarray(diff, dtype=np.int16).sum(axis=2)
        nonzero = int((arr > channel_diff_threshold).sum())
        total = int(arr.size)
        diff_ratio = nonzero / total if total else 0.0
        max_diff = int(arr.max()) if total else 0
    else:
        diff_ratio = 1.0
        max_diff = 0

    return VisualDiff(
        perceptual_distance=perceptual_distance,
        diff_pixel_ratio=diff_ratio,
        max_pixel_diff=max_diff,
        width_match=width_match,
        height_match=height_match,
    )


def matches_baseline(actual: Path | str, baseline_name: str,
                     *, baseline_dir: Path | str | None = None,
                     max_perceptual: int = 5,
                     max_pixel_ratio: float = 0.02,
                     channel_diff_threshold: int = 30) -> bool:
    """Compare ``actual`` against a named baseline; True if within tolerance.

    If the baseline file doesn't exist and ``UPDATE_BASELINES_ENV`` is set
    to ``"1"``, the actual image is copied into place and True is returned.
    Otherwise a missing baseline raises ``FileNotFoundError``.
    """
    actual_path = Path(actual)
    base_dir = Path(baseline_dir) if baseline_dir else (actual_path.parent / "baselines")
    base_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = base_dir / baseline_name

    update_mode = os.environ.get(UPDATE_BASELINES_ENV, "") == "1"

    if not baseline_path.is_file():
        if update_mode:
            import shutil

            shutil.copyfile(actual_path, baseline_path)
            log.info("Created baseline %s from %s", baseline_path, actual_path)
            return True
        raise FileNotFoundError(
            f"Baseline {baseline_path} does not exist. "
            f"Set {UPDATE_BASELINES_ENV}=1 to create it from {actual_path}."
        )

    diff = compare_images(actual_path, baseline_path,
                          channel_diff_threshold=channel_diff_threshold)

    if update_mode and not diff.is_match(max_perceptual=max_perceptual,
                                         max_pixel_ratio=max_pixel_ratio):
        import shutil

        shutil.copyfile(actual_path, baseline_path)
        log.info("Updated baseline %s (was: %s)", baseline_path, diff)
        return True

    return diff.is_match(max_perceptual=max_perceptual,
                         max_pixel_ratio=max_pixel_ratio)


def capture_window(window: Any, output_path: Path | str) -> Path:
    """Save a PNG of the given pywinauto window to ``output_path``.

    Uses ``mss`` to grab the screen region the window occupies — this works
    even for windows that render via GDI/Direct2D where pywinauto's own
    capture would return an empty bitmap. Returns the saved path.
    """
    import mss
    from PIL import Image

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rect = window.rectangle()
    region = {"left": rect.left, "top": rect.top,
              "width": rect.width(), "height": rect.height()}
    with mss.mss() as sct:
        grab = sct.grab(region)
        img = Image.frombytes("RGB", grab.size, grab.rgb)
        img.save(output_path)
    return output_path
