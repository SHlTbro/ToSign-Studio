"""Locate individual cool-color handwritten glyphs on a finished To-sign.

This is a conservative, label-free proposal path for saturated blue/purple
handwriting. It does not recognize Chinese characters or declare ground truth.
Images outside this color/placement envelope fall back to the existing P4
manual box path. Pure Pillow/stdlib keeps the packaged backend self-contained.
"""

from __future__ import annotations

from collections import Counter
from statistics import median

from PIL import Image, ImageChops


MAX_WIDTH = 640
MIN_LINE_HEIGHT = 22
MIN_LINE_INK = 450


def _runs(active: list[bool], max_gap: int) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    start = last = None
    for pos, enabled in enumerate(active):
        if not enabled:
            continue
        if start is None:
            start = pos
        elif pos - last > max_gap + 1:
            intervals.append((start, last + 1))
            start = pos
        last = pos
    if start is not None:
        intervals.append((start, last + 1))
    return intervals


def _color_mask(image: Image.Image, *, lower_only: bool) -> Image.Image:
    hsv = image.convert("HSV")
    hue, saturation, value = hsv.split()
    luminance = image.convert("L")
    upper = int(image.height * 0.57) if lower_only else 0
    hue_data = hue.tobytes()
    saturation_data = saturation.tobytes()
    value_data = value.tobytes()
    luminance_data = luminance.tobytes()
    hue_bins = Counter(
        hue_data[pos] // 5
        for pos in range(upper * image.width, image.width * image.height)
        if 140 <= hue_data[pos] <= 235
        and saturation_data[pos] >= 100
        and value_data[pos] >= 75
        and luminance_data[pos] <= 175
    )
    if not hue_bins:
        return Image.new("L", image.size)
    dominant_hue = hue_bins.most_common(1)[0][0] * 5 + 2
    ink = ImageChops.multiply(
        hue.point(lambda x: 255 if abs(x - dominant_hue) <= 12 else 0),
        saturation.point(lambda x: 255 if x >= 100 else 0),
    )
    ink = ImageChops.multiply(ink, value.point(lambda x: 255 if x >= 75 else 0))
    ink = ImageChops.multiply(ink, luminance.point(lambda x: 255 if x <= 175 else 0))
    if upper:
        ink.paste(0, (0, 0, image.width, upper))
    return ink


def color_stroke_mask(crop: Image.Image) -> Image.Image | None:
    """Try a color-isolated alpha draft for one user-selected candidate box.

    The selected crop is already the user's text location; it has no lower
    page restriction. Return None on weak color evidence so the adjudicated
    traditional separator can continue to handle ordinary ink.
    """
    work = crop.convert("RGB")
    if max(work.size) > MAX_WIDTH:
        work.thumbnail((MAX_WIDTH, MAX_WIDTH), Image.Resampling.LANCZOS)
    mask = _color_mask(work, lower_only=False)
    on = mask.histogram()[255]
    coverage = on / (work.width * work.height)
    if on < 40 or not 0.01 <= coverage <= 0.45:
        return None
    return mask


def _projection(mask: bytes, width: int, y0: int, y1: int) -> list[int]:
    projection = [0] * width
    for y in range(y0, y1):
        row = mask[y * width:(y + 1) * width]
        for x, value in enumerate(row):
            if value:
                projection[x] += 1
    return projection


def _line_bands(mask: bytes, width: int, height: int) -> list[tuple[int, int]]:
    rows = [sum(mask[y * width:(y + 1) * width]) // 255 for y in range(height)]
    threshold = max(16, int(width * 0.032))
    smooth = [sum(rows[max(0, y - 3):min(height, y + 4)]) / 7 for y in range(height)]
    active = [count >= threshold for count in smooth]
    raw_bands = []
    for start, end in _runs(active, max_gap=7):
        if end - start < MIN_LINE_HEIGHT or sum(rows[start:end]) < MIN_LINE_INK:
            continue
        raw_bands.append((start, end))
    bands = []
    for index, (start, end) in enumerate(raw_bands):
        previous_end = raw_bands[index - 1][1] if index else 0
        next_start = raw_bands[index + 1][0] if index + 1 < len(raw_bands) else height
        bands.append((max(previous_end + (start - previous_end) // 2, start - 10),
                      min(end + 10, end + (next_start - end) // 2)))
    return bands


def _split_wide(start: int, end: int, projection: list[int], typical: float) -> list[tuple[int, int]]:
    width = end - start
    if typical <= 0 or width < max(typical * 1.65, 35):
        return [(start, end)]
    parts = [(start, end)]
    minimum_piece = max(15, round(typical * 0.55))
    # Ink-free valleys are stronger evidence than the average glyph width:
    # one naturally wide character must not be cut into several fragments.
    while len(parts) < 4:
        options = []
        for index, (left, right) in enumerate(parts):
            if right - left < max(typical * 1.65, 35):
                continue
            center = (left + right) / 2
            for cut in range(left + minimum_piece, right - minimum_piece + 1):
                if projection[cut] <= 2:
                    options.append((abs(cut - center), index, cut))
        if not options:
            break
        _, index, cut = min(options)
        left, right = parts[index]
        parts[index:index + 1] = [(left, cut), (cut, right)]
    while len(parts) < 4:
        wide = [(right - left, index) for index, (left, right) in enumerate(parts)
                if right - left >= typical * 1.8]
        if not wide:
            break
        _, index = max(wide)
        left, right = parts[index]
        center = (left + right) / 2
        radius = max(4, round(typical * 0.35))
        lo = max(left + minimum_piece, round(center - radius))
        hi = min(right - minimum_piece, round(center + radius))
        if lo > hi:
            break
        cut = min(range(lo, hi + 1), key=lambda x: (projection[x], abs(x - center)))
        parts[index:index + 1] = [(left, cut), (cut, right)]
    return parts


def detect_color_handwriting(image: Image.Image) -> list[dict]:
    """Return normalized, unlabeled per-glyph boxes for the supported palette.

    Each score is a location hint only. Empty means that this branch found no
    trustworthy candidates; callers may use their ordinary fallback.
    """
    original_width, original_height = image.size
    if original_width < 100 or original_height < 100:
        return []
    work = image.convert("RGB")
    if work.width > MAX_WIDTH:
        work.thumbnail((MAX_WIDTH, round(work.height * MAX_WIDTH / work.width)), Image.Resampling.LANCZOS)
    width, height = work.size
    mask = _color_mask(work, lower_only=True).tobytes()
    bands = _line_bands(mask, width, height)
    line_clusters = []
    ordinary_widths = []
    for y0, y1 in bands:
        projection = _projection(mask, width, y0, y1)
        active = [n >= 2 for n in projection]
        clusters = []
        for x0, x1 in _runs(active, max_gap=4):
            mass = sum(projection[x0:x1])
            if x1 - x0 < 10 or mass < 70:
                continue
            clusters.append((x0, x1))
            if 16 <= x1 - x0 <= 90:
                ordinary_widths.append(x1 - x0)
        line_clusters.append((y0, y1, projection, clusters))
    if not ordinary_widths:
        return []
    typical = median(ordinary_widths)
    candidates = []
    for y0, y1, projection, clusters in line_clusters:
        for x0, x1 in clusters:
            for a, b in _split_wide(x0, x1, projection, typical):
                xs = []
                ys = []
                for y in range(y0, y1):
                    row = mask[y * width + a:y * width + b]
                    for dx, value in enumerate(row):
                        if value:
                            xs.append(a + dx)
                            ys.append(y)
                if len(xs) < 70:
                    continue
                left, right = max(0, min(xs) - 2), min(width, max(xs) + 3)
                top, bottom = max(0, min(ys) - 2), min(height, max(ys) + 3)
                glyph_width, glyph_height = right - left, bottom - top
                # A legitimate 一 can be only a few pixels tall after
                # thumbnailing. Keep a dense horizontal stroke, while still
                # rejecting tiny isolated specks.
                flat_stroke = glyph_height >= 5 and glyph_width >= 20 and glyph_width >= glyph_height * 2.5
                if glyph_width < max(10, glyph_height * 0.2) or (glyph_height < 18 and not flat_stroke):
                    continue
                candidates.append({
                    "box_norm": {
                        "x_norm": round(left / width, 6),
                        "y_norm": round(top / height, 6),
                        "w_norm": round((right - left) / width, 6),
                        "h_norm": round((bottom - top) / height, 6),
                    },
                    "score": 0.5,
                })
    return candidates
