"""Bounded apparent-sRGB assignment after all glyph layout/resampling.

No model execution, geometry, outline, opacity or pressure operation occurs here.
Callers own author authorization and the single final RGB source-over operation.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

AUTO_MODE = 'auto-contrast-v1'
SOURCE_MODE = 'source-solid-srgb-v1'
IMPLEMENTATION_VERSION = 'post-layout-solid-srgb/1'
COMPOSITOR_VERSION = 'pillow-rgb-straight-rgba-source-over/1'
COMPOSITE_POLICY = 'Pillow-RGB-paste-straight-RGBA-coverage-once'
ALPHA_POLICY = 'preserve-post-layout-alpha-bytes'
TRANSFER_POLICY = 'encoded-srgb'


def validate_rgb(rgb):
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in rgb):
        raise ValueError('three exact uint8 apparent-sRGB channels required')
    return tuple(rgb)


def recolor_strip(strip, rgb):
    """Return constant RGB with every post-layout alpha byte unchanged."""
    rgb = validate_rgb(rgb)
    if not isinstance(strip, Image.Image) or strip.mode != 'RGBA' or min(strip.size) < 1:
        raise ValueError('completed nonempty RGBA strip required')
    alpha = strip.getchannel('A')
    if alpha.getbbox() is None:
        raise ValueError('empty glyph coverage')
    raw_alpha = alpha.tobytes()
    result = Image.new('RGBA', strip.size, rgb + (255,))
    result.putalpha(alpha)
    if result.getchannel('A').tobytes() != raw_alpha:
        raise AssertionError('color assignment changed glyph coverage')
    return result, {
        'schema': 'ink-color-execution/1',
        'ink_color_mode': SOURCE_MODE,
        'rgb': list(rgb),
        'color_implementation_version': IMPLEMENTATION_VERSION,
        'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'compositor_version': COMPOSITOR_VERSION,
        'composite_policy': COMPOSITE_POLICY,
        'transfer_policy': TRANSFER_POLICY,
        'alpha_policy': ALPHA_POLICY,
        'post_layout_alpha_sha256': hashlib.sha256(raw_alpha).hexdigest(),
        'alpha_dimensions': list(alpha.size),
        'alpha_preserved': True,
        'added_outline': False,
        'erosion': False,
        'thresholding': False,
        'opacity_multiplier': 1,
        'synthetic_texture': False,
        'random_color_variation': False,
        'color_assignment_stage': 'after-all-project-layout-resampling',
        'geometry_producer': 'existing-M2-glyphs-unchanged',
        'physical_pigment_or_opacity_recovery_claimed': False,
    }


def validate_placement(background, strip, xy):
    if not isinstance(background, Image.Image) or background.mode != 'RGB' or not isinstance(strip, Image.Image) or strip.mode != 'RGBA':
        raise ValueError('RGB background and RGBA strip required')
    if not isinstance(xy, (list, tuple)) or len(xy) != 2 or any(type(v) is not int for v in xy):
        raise ValueError('exact integer placement required')
    x, y = xy
    if min(strip.size) < 1 or x < 0 or y < 0 or x + strip.width > background.width or y + strip.height > background.height:
        raise ValueError('placement would clip glyph coverage')
    return x, y


def background_diagnostics(background, strip, xy):
    """Describe actual supported local pixels without a readability verdict.

    Computes, but does not paint, the integer result of one coverage blend.
    Encoded-RGB brightness is a heuristic, not calibrated optical luminance.
    Alpha is only used for selecting support and computing the real blend.
    """
    x, y = validate_placement(background, strip, xy)
    foreground = np.asarray(strip, dtype=np.uint32)
    alpha = foreground[:, :, 3]
    support = alpha > 0
    if not np.any(support):
        raise ValueError('empty glyph coverage')
    crop = background.crop((x, y, x + strip.width, y + strip.height))
    back = np.asarray(crop, dtype=np.uint32)
    a = alpha[:, :, None]
    composed = (foreground[:, :, :3] * a + back * (255 - a) + 127) // 255
    weights = np.array([0.299, 0.587, 0.114])
    before = back[support] @ weights
    after = composed[support] @ weights
    delta = np.abs(after - before)

    def summary(values):
        return {'min': float(np.min(values)), 'p10': float(np.percentile(values, 10)),
                'median': float(np.median(values)), 'p90': float(np.percentile(values, 90)),
                'max': float(np.max(values)), 'mean': float(np.mean(values))}

    return {
        'schema': 'ink-local-background-diagnostics/1',
        'status': 'UNCALIBRATED_REQUIRES_VISUAL_REVIEW',
        'readability_pass': None,
        'warning': 'Inspect the actual photo preview. Local visibility varies; no numerical readability threshold is calibrated.',
        'measurement': 'encoded-RGB brightness heuristic 0.299R+0.587G+0.114B; not optical luminance',
        'placement_xy': [x, y],
        'strip_dimensions': list(strip.size),
        'support_pixel_count': int(np.count_nonzero(support)),
        'supported_background_brightness': summary(before),
        'one_coverage_result_brightness': summary(after),
        'absolute_result_background_brightness_difference': summary(delta),
        'zero_rounded_rgb_change_support_pixels': int(np.count_nonzero(np.all(composed[support] == back[support], axis=1))),
        'background_crop_rgb_sha256': hashlib.sha256(crop.tobytes()).hexdigest(),
        'post_layout_alpha_sha256': hashlib.sha256(strip.getchannel('A').tobytes()).hexdigest(),
        'compositor_version': COMPOSITOR_VERSION,
        'pixels_modified': False,
    }
