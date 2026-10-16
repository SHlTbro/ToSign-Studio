"""Versioned slot typography. Missing styles retain the legacy render path."""
import math
import re
from app.core.errors import ApiError

DEFAULT_TEXT_STYLE = {"ordinary_scale": 1.3, "to_ratio": 2.0, "header_gap": 10.0}
LIMITS = {"ordinary_scale": (0.5, 3.0), "to_ratio": (0.5, 3.0), "header_gap": (0.0, 100.0)}

DEFAULT_HANDWRITING = {"size_variation_pct": 0.0, "tilt_variation_deg": 0.0, "seed": 42}

def validate_handwriting(value):
    """Job-level CPU layout adjustment, independent from model training."""
    if not isinstance(value, dict) or set(value) - set(DEFAULT_HANDWRITING):
        raise ApiError(400, "INVALID_REQUEST", "Invalid handwriting adjustment")
    result = dict(DEFAULT_HANDWRITING)
    for key, high in (("size_variation_pct", 20), ("tilt_variation_deg", 8)):
        raw = value.get(key, result[key])
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or not 0 <= raw <= high:
            raise ApiError(400, "INVALID_REQUEST", "Invalid handwriting adjustment", f"{key} must be finite within [0, {high}]")
        result[key] = float(raw)
    seed = value.get("seed", 42)
    if type(seed) is not int or not 0 <= seed <= 0xffffffff:
        raise ApiError(400, "INVALID_REQUEST", "Invalid handwriting seed", "seed must be a uint32 integer")
    result["seed"] = seed
    return result

def validate_text_style(value):
    if not isinstance(value, dict) or set(value) - set(LIMITS):
        raise ApiError(400, "INVALID_REQUEST", "Invalid text size", "text_style needs supported numeric fields")
    result = dict(DEFAULT_TEXT_STYLE)
    for key, raw in value.items():
        low, high = LIMITS[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or not low <= raw <= high:
            raise ApiError(400, "INVALID_REQUEST", "Invalid text size", f"{key} must be finite within [{low}, {high}]")
        result[key] = float(raw)
    return result

def text_segments(text, key):
    """Only the actual To prefix is special; usernames/content remain dynamic."""
    match = re.match(r"^(To(?=\s|[:：]|$)(?:[:：])?)\s*", text, re.IGNORECASE) if key == "to_cn" else None
    return [(match.group(1), True), (text[match.end():], False)] if match else [(text, False)]
