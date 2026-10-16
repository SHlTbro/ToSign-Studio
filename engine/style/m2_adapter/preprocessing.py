"""Versioned image conditioning contracts for the frozen FontDiffuser base.

The historical profile is retained for old adapters. New profiles are explicit
training lineage, never a silent input change to an existing adapter.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

LEGACY_PROFILE = "legacy-pil72-gray-v1"
UPSTREAM_PROFILE = "fontdiffuser-freetype128-blackwhite-v1"

_PROFILES = {
    LEGACY_PROFILE: {
        "schema": "m2-preprocessing/1",
        "profile": LEGACY_PROFILE,
        "content": "Pillow centered 72px glyph on 96px canvas",
        "samples": "alpha-only ink on paper; resize whole canvas to 96px",
        "paper": 245, "ink": 15, "resize": "Pillow bilinear",
        "tensor": "RGB uint8 to float32, x/127.5-1",
    },
    UPSTREAM_PROFILE: {
        "schema": "m2-preprocessing/1",
        "profile": UPSTREAM_PROFILE,
        "content": "pygame.freetype 128px glyph centered on 128px canvas, then resize96",
        "samples": "255-alpha black ink on white; resize whole canvas to 96px",
        "paper": 255, "ink": 0, "resize": "Pillow bilinear",
        "tensor": "RGB uint8 to float32, x/127.5-1",
        "oversize": "reject glyphs exceeding 128px; no unverified resampling fallback",
        "upstream_revision": "7bd89f3932f30e0b0062b4ba852a4270a20fd0e4",
    },
}


def preprocessing_contract(profile=LEGACY_PROFILE):
    if profile not in _PROFILES:
        raise ValueError("unsupported preprocessing profile: " + str(profile))
    body = dict(_PROFILES[profile])
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**body, "sha256": digest,
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def verify_preprocessing_contract(record):
    """Missing historical metadata means historical behavior, never latest."""
    if record is None:
        return preprocessing_contract(LEGACY_PROFILE)
    if not isinstance(record, dict):
        raise ValueError("preprocessing metadata must be an object")
    expected = preprocessing_contract(record.get("profile"))
    if record != expected:
        raise ValueError("preprocessing profile/hash differs from this implementation")
    return expected


def full_range_glyph_image(path):
    # Canonical snapshots store white RGB with ink in alpha. RGB compositing
    # would erase them; alpha is the authoritative coverage channel.
    with Image.open(path) as source:
        alpha = np.asarray(source.convert("RGBA"))[..., 3]
    return Image.fromarray(255 - alpha).convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)


def upstream_content_image(text_label, font_path):
    """The pinned upstream utils.ttf2im non-oversize branch, byte for byte.

    pygame.freetype is already a pinned runtime dependency. No display, network,
    font substitution, or per-character repair is used.
    """
    import pygame.freetype
    import pygame.surfarray

    pygame.freetype.init()
    font = pygame.freetype.Font(str(font_path), size=128)
    surface, _ = font.render(text_label)
    alpha = pygame.surfarray.pixels_alpha(surface).transpose(1, 0).copy()
    height, width = alpha.shape
    if height > 128 or width > 128:
        raise ValueError("E_CONTENT_OVERSIZE: upstream glyph exceeds128; verified resize implementation required")
    canvas = np.full((128, 128), 255, dtype=np.uint8)
    x, y = round((128 - width) / 2), round((128 - height) / 2)
    canvas[y:y + height, x:x + width] = 255 - alpha
    return Image.fromarray(canvas).convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
