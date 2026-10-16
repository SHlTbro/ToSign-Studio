"""Extraction domain service (T59-R1, batch B5-01).

Semantic sources of truth (FROZEN):
- contract-v0.3-patch-D §3.1: extract is a HALF-PRODUCT generator. Every
  response carries ``needs_touchup: true``; there is no pass-through path
  that would let an alpha draft skip human touchup (patch-D §3.1/§8 - the
  old quality enum is abolished entirely).
- patch-D §3.4: detect-text only LOCATES candidate boxes. It never produces
  ground truth, never does semantic judgement, and its boxes are not an
  alpha-quality signal.
- patch-D §8: the frozen error-code table for this domain
  (INVALID_REQUEST/ASSET_NOT_FOUND/NO_STROKES_FOUND/IMAGE_DECODE_FAILED/
  EXTRACT_TIMEOUT/EXTRACT_FAILED/DETECT_FAILED) - mirrored verbatim in
  contracts/style-font-pipeline-v1.json ``error_codes_full_table``.
- patch-D §2.1: 128x128 RGBA glyph normalization (bbox -> fit 112 -> center
  on 128 transparent canvas) applied to the draft alpha output.

ALGORITHM STANCE (red-line declaration, batch-b5-manifest §B5-01):

    [BASELINE — patch-D §3.2 Owner 裁定 2026-09-24：传统加强版方法转正为基线候选]

Owner adjudication (patch-D §11, 2026-09-24): the enhanced traditional
pipeline below is the adjudicated BASELINE binding of the pluggable
Strategy interface (``BackgroundSeparator`` /
``CandidateGenerator``). The baseline binding is a pure-Pillow reduced
re-implementation of the S1-enhanced traditional pipeline idea
(artifacts/style-font/baseline_v2/ 思路: channel selection + local-adaptive
threshold + despeckle + solid-block stroke filter), pinned by the manifest as
the pre-adjudication default. patch-D §3.2(c) forbids treating the traditional
method as the adjudicated algorithm BEFORE the Owner ruling; the compliance
mechanism chosen by the manifest is "pluggable + explicit stance markers (now resolved by the 2026-09-24 Owner adjudication, see patch-D §11)"
# and the former
marking", not refusing to build the endpoint. Swapping in the Owner-adjudicated
algorithm (or PP-OCRv5 for detect, pending its own dependency change order)
is a matter of rebinding the strategy - no endpoint signature changes.

The default candidate generator additionally tries the pure-Pillow cool-color
per-glyph locator first. It returns unlabeled boxes only and falls back to
the adjudicated baseline outside its supported palette/layout. Extract keeps
the mandatory touchup gate even when color isolation creates the alpha draft.

Runtime discipline (CC-19): the backend locked runtime excludes numpy/scipy
(those live in the spike venvs only), so the baseline pipeline is pure
Pillow + stdlib. The local-adaptive threshold is a pure-Pillow reduction of
Sauvola/Niblack (box-blur local mean ± k x local mean-absolute-deviation);
the deviation from the S1 spike numerics is intentional and covered by the
adjudicated baseline stance above (patch-D §11, 2026-09-24).
"""

import io
import os
import time
import uuid
from typing import Protocol

from PIL import Image, ImageChops, ImageFilter, ImageOps
from sqlalchemy.orm import Session

from app.core import storage
from app.core.errors import ApiError
from app.domains.assets_upload.models import Asset
from app.domains.extraction.color_text import color_stroke_mask, detect_color_handwriting
from app.domains.usage.service import storage_root

# patch-D §8: WORKER_TIMEOUT_MS = 10000 [暂定] (T-COST/T40 may revise).
DEFAULT_WORKER_TIMEOUT_MS = 10000
# Extraction-pipeline processing cap (downscale before any pixel work).
MAX_SIDE = 640
# patch-D §3.2(a) proxy criteria P1/P2/P3 (necessary, NOT sufficient - C-59;
# content correctness always needs the human touchup/confirm pass).
COV_MIN, COV_MAX = 0.002, 0.30
CC_MAX = 60
LARGEST_SHARE_MAX = 0.70
# Despeckle floor and solid-block rejection, from the baseline_v2 idea.
DESPECKLE_FLOOR_PX = 16
DESPECKLE_RATE = 0.0005
SOLID_FILL_MAX = 0.9
SOLID_AREA_MIN = 400
# Local-adaptive threshold knobs (pure-Pillow reduction; baseline per patch-D §11).
LOCAL_WINDOW = 31
LOCAL_K = 3  # threshold = mean -/+ LOCAL_K * MAD (MAD is 8-bit, hence the
#             larger multiplier vs Sauvola's 0.2 * std)
DRAFT_CANVAS = 128
DRAFT_TARGET = 112  # 128 - 2*8px padding (patch-D §2.1, 8px [暂定])
# detect candidate score: the baseline candidate generator has no calibrated
# confidence model, so every candidate carries this constant. It MUST NOT be
# read as a detection probability. (U-Net upgrade path: new change order.)
BASELINE_SCORE = 0.5

BOX_KEYS = ("x_norm", "y_norm", "w_norm", "h_norm")


class BackgroundSeparator(Protocol):
    """Strategy interface: crop image -> alpha mask (PIL 'L', 255=stroke).

    Rebindable; the default binding below is the adjudicated baseline (patch-D §11) and is
    not an algorithm ruling (see module docstring).
    """

    def separate(self, img: Image.Image) -> Image.Image:  # pragma: no cover
        ...


class CandidateGenerator(Protocol):
    """Strategy interface: image -> [{box_norm, score}, ...].

    patch-D §3.4: locate only. The default binding is the baseline candidate generator until
    PP-OCRv5 lands via its own dependency change order.
    """

    def detect(self, img: Image.Image) -> list:  # pragma: no cover
        ...


def _box_blur(im: Image.Image, radius: int) -> Image.Image:
    return im.filter(ImageFilter.BoxBlur(radius))


def _local_adaptive_mask(channel: Image.Image, polarity: str) -> Image.Image:
    """Pure-Pillow local-adaptive threshold (Sauvola/Niblack 精神简化).

    t = mean -/+ k * MAD with MAD = blur(|x - mean|). 8-bit ops only, so it
    runs inside the CC-19 locked runtime. (Baseline knobs; U-Net upgrade path via new change order.)
    """
    mean = _box_blur(channel, LOCAL_WINDOW // 2)
    dev = ImageChops.difference(channel, mean)
    mad = _box_blur(dev, LOCAL_WINDOW // 2)
    k_mad = mad.point(lambda v: min(255, v * LOCAL_K))
    if polarity == "dark":
        t = ImageChops.subtract(mean, k_mad)  # stroke darker than local t
        d = ImageChops.subtract(t, channel)
    else:
        t = ImageChops.add(mean, k_mad)  # stroke lighter than local t
        d = ImageChops.subtract(channel, t)
    return d.point(lambda v: 255 if v > 0 else 0, mode="L")


def _otsu_threshold(channel: Image.Image) -> int:
    """Global Otsu from the 8-bit histogram (pure Python, 256 bins)."""
    hist = channel.histogram()
    total = sum(hist)
    sum_all = sum(i * c for i, c in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best_t, best_var = 0, -1.0
    for t, count in enumerate(hist):
        w_b += count
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * count
        mu_b = sum_b / w_b
        mu_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (mu_b - mu_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _global_mask(channel: Image.Image, polarity: str, t: int) -> Image.Image:
    lut = [255 if v < t else 0 for v in range(256)] if polarity == "dark" else [
        255 if v > t else 0
        for v in range(256)
    ]
    return channel.point(lut, mode="L")


def _opened(mask: Image.Image) -> Image.Image:
    """3x3 opening (MinFilter then MaxFilter), same as the T76/S1 spikes."""
    return mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))


def _connected_components(mask: Image.Image):
    """4-connected component labeling (pure Python BFS; the mask bytes double
    as the visited array). Same scheme as the T76 spike (spike_c47.py).

    Returns [(area, x0, y0, x1, y1, [pixel offsets])...] sorted by area desc.
    """
    w, h = mask.size
    data = bytearray(mask.tobytes())
    comps = []
    stack = []
    for start in range(w * h):
        if not data[start]:
            continue
        area = 0
        x0 = y0 = 10**9
        x1 = y1 = -1
        pixels = []
        stack.append(start)
        data[start] = 0
        while stack:
            pos = stack.pop()
            area += 1
            pixels.append(pos)
            y, x = divmod(pos, w)
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            if y < y0:
                y0 = y
            if y > y1:
                y1 = y
            if x > 0 and data[pos - 1]:
                data[pos - 1] = 0
                stack.append(pos - 1)
            if x < w - 1 and data[pos + 1]:
                data[pos + 1] = 0
                stack.append(pos + 1)
            if y > 0 and data[pos - w]:
                data[pos - w] = 0
                stack.append(pos - w)
            if y < h - 1 and data[pos + w]:
                data[pos + w] = 0
                stack.append(pos + w)
        comps.append((area, x0, y0, x1, y1, pixels))
    comps.sort(key=lambda c: -c[0])
    return comps


def _filter_components(comps, min_area: int):
    """Despeckle (area floor) + solid-block rejection (baseline_v2 idea)."""
    kept = []
    for area, x0, y0, x1, y1, pixels in comps:
        if area < min_area:
            continue
        bbox_area = (x1 - x0 + 1) * (y1 - y0 + 1)
        if area / max(bbox_area, 1) > SOLID_FILL_MAX and area > SOLID_AREA_MIN:
            continue
        kept.append((area, x0, y0, x1, y1, pixels))
    return kept


def _proxy_pass(cov: float, cc: int, max_share: float) -> bool:
    """patch-D §3.2(a) proxy criteria - necessary, NOT sufficient (C-59)."""
    return bool(COV_MIN <= cov <= COV_MAX and 1 <= cc <= CC_MAX and max_share <= LARGEST_SHARE_MAX)


def _channels(im: Image.Image) -> dict:
    rgb = im.convert("RGB")
    r, g, b = rgb.split()
    _, s, v = rgb.convert("HSV").split()
    return {"gray": rgb.convert("L"), "R": r, "G": g, "B": b, "S": s, "V": v}


class S1EnhancedBaselineSeparator:
    """Default BackgroundSeparator binding.

    [BASELINE — patch-D §3.2 Owner 裁定 2026-09-24：传统加强版转正为基线候选]

    Pure-Pillow reduced re-implementation of the S1-enhanced traditional
    pipeline idea (artifacts/style-font/baseline_v2/ 思路): channel selection,
    local-adaptive threshold + global Otsu fallback (dark/light), 3x3
    opening, despeckle, solid-block stroke filtering; config selection via
    the patch-D §3.2(a) proxy criteria (max kept components among passing).
    """

    name = "s1_enhanced_baseline"

    def separate(self, img: Image.Image) -> Image.Image:
        scale = min(1.0, MAX_SIDE / max(img.size))
        work = img if scale >= 1.0 else img.resize(
            (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
            Image.BILINEAR,
        )
        area = work.size[0] * work.size[1]
        min_area = max(DESPECKLE_FLOOR_PX, int(DESPECKLE_RATE * area))
        channels = _channels(work)
        best_mask, best_cc = None, 0
        for name in ("gray", "V", "S", "R", "G", "B"):
            channel = channels[name]
            for polarity in ("dark", "light"):
                mask = _opened(_local_adaptive_mask(channel, polarity))
                cand = self._score(mask, area, min_area)
                if cand is not None and cand[1] > best_cc:
                    best_mask, best_cc = cand[0], cand[1]
            # Global Otsu fallback (dark polarity), as in baseline_v2.
            mask = _opened(_global_mask(channel, "dark", _otsu_threshold(channel)))
            cand = self._score(mask, area, min_area)
            if cand is not None and cand[1] > best_cc:
                best_mask, best_cc = cand[0], cand[1]
        if best_mask is None:
            # No configuration passed the proxy: fall back to Otsu-gray so the
            # caller can still answer NO_STROKES_FOUND from an empty mask.
            fallback = _opened(_global_mask(channels["gray"], "dark", _otsu_threshold(channels["gray"])))
            return _rebuild(fallback, _connected_components(fallback), min_area)
        return best_mask

    def _score(self, mask: Image.Image, area: int, min_area: int):
        """Return (filtered_mask, cc) when the proxy passes, else None."""
        on = area - mask.histogram()[0]  # C-side count, cheap prefilter
        cov = on / area
        if not (0.001 <= cov <= 0.60):  # cheap prefilter before BFS
            return None
        comps = _filter_components(_connected_components(mask), min_area)
        cc = len(comps)
        if cc == 0 or cc > CC_MAX:
            return None
        max_share = comps[0][0] / max(on, 1)
        if not _proxy_pass(cov, cc, max_share):
            return None
        return _rebuild(mask, comps, min_area), cc


def _rebuild(mask: Image.Image, comps, min_area: int) -> Image.Image:
    """Rebuild a mask image from the kept components (despeckle applied)."""
    out = Image.new("L", mask.size, 0)
    px = out.load()
    for _area, _x0, _y0, _x1, _y1, pixels in comps:
        for pos in pixels:
            y, x = divmod(pos, mask.size[0])
            px[x, y] = 255
    return out


class BaselineCandidateGenerator:
    """Default CandidateGenerator binding for detect-text.

    [BASELINE — patch-D §3.2 Owner 裁定 2026-09-24；detect 候选生成器沿用基线；
    PP-OCRv5 真实现待依赖变更单，不在本批]

    Reuses the baseline separator to propose whole-image candidate boxes
    from kept components. Locate-only (patch-D §3.4): boxes are hints for
    the P4 quick-select UI, never ground truth, and the score is a constant
    non-semantic value.
    """

    name = "baseline_candidate_generator"

    def detect(self, img: Image.Image) -> list:
        mask = DEFAULT_SEPARATOR.separate(img)
        area = mask.size[0] * mask.size[1]
        min_area = max(DESPECKLE_FLOOR_PX, int(DESPECKLE_RATE * area))
        comps = _filter_components(_connected_components(mask), min_area)
        w, h = mask.size
        items = []
        for area_px, x0, y0, x1, y1, _pixels in comps:
            items.append(
                {
                    "box_norm": {
                        "x_norm": round(x0 / w, 6),
                        "y_norm": round(y0 / h, 6),
                        "w_norm": round((x1 - x0 + 1) / w, 6),
                        "h_norm": round((y1 - y0 + 1) / h, 6),
                    },
                    "score": BASELINE_SCORE,
                }
            )
        return items


class ColorAwareCandidateGenerator:
    """Use per-glyph color proposals when this To-sign palette is present.

    The color path only locates boxes. Unrecognized color or layout continues
    through the existing adjudicated baseline, and users can always re-box.
    """

    name = "color_aware_candidate_generator"

    def detect(self, img: Image.Image) -> list:
        items = detect_color_handwriting(img)
        if 3 <= len(items) <= 80:
            return items
        return BaselineCandidateGenerator().detect(img)


# Default bindings (rebindable; see module docstring). The color-aware
# candidate generator falls back to the adjudicated baseline; it does not
# assign character identity or change the human-confirmation contract.
DEFAULT_SEPARATOR = S1EnhancedBaselineSeparator()
DEFAULT_GENERATOR = ColorAwareCandidateGenerator()


def _worker_timeout_ms() -> int:
    raw = os.environ.get("WORKER_TIMEOUT_MS")
    try:
        return int(raw) if raw is not None else DEFAULT_WORKER_TIMEOUT_MS
    except (TypeError, ValueError):
        return DEFAULT_WORKER_TIMEOUT_MS


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _validate_box_norm(box) -> dict:
    if not isinstance(box, dict) or set(box) != set(BOX_KEYS):
        raise ApiError(
            400,
            "INVALID_REQUEST",
            "box_norm must be an object with exactly x_norm/y_norm/w_norm/h_norm",
        )
    values = {}
    for key in BOX_KEYS:
        value = box[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ApiError(400, "INVALID_REQUEST", f"box_norm.{key} must be a number")
        values[key] = float(value)
    if not (0 <= values["x_norm"] <= 1 and 0 <= values["y_norm"] <= 1):
        raise ApiError(400, "INVALID_REQUEST", "box_norm.x_norm/y_norm must be within [0,1]")
    if not (0 < values["w_norm"] <= 1 and 0 < values["h_norm"] <= 1):
        raise ApiError(400, "INVALID_REQUEST", "box_norm.w_norm/h_norm must be within (0,1]")
    if values["x_norm"] + values["w_norm"] > 1 + 1e-9 or values["y_norm"] + values["h_norm"] > 1 + 1e-9:
        raise ApiError(400, "INVALID_REQUEST", "box_norm exceeds the image bounds")
    return values


def _load_asset_image(
    session: Session,
    user_id: int,
    asset_id,
    *,
    decode_code: str = "IMAGE_DECODE_FAILED",
    decode_retryable: bool = False,
) -> Image.Image:
    """Asset lookup + decode. 404 ASSET_NOT_FOUND covers missing AND foreign
    rows (anti-enumeration, patch-B section 3 spirit); undecodable bytes map
    to the caller's contract error: 422 IMAGE_DECODE_FAILED for extract
    (patch-D §8) and 422 DETECT_FAILED for detect (the frozen detect error
    set - contract ``new_implementation_pending`` detect entry - has no
    dedicated decode code)."""
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ApiError(400, "INVALID_REQUEST", "asset_id must be a non-empty string")
    row = session.get(Asset, asset_id)
    if row is None or str(row.user_id) != str(user_id):
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found")
    if not row.enabled:
        # P05 lifecycle semantics (2026-09-27): soft-deleted assets are not
        # extractable. Same 404 shape as missing/foreign so one uniform
        # unreachable answer covers all three.
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found")
    abs_path = os.path.join(storage_root(), row.relative_path)
    if not os.path.exists(abs_path):
        # Same answer as the P2 asset file endpoint (assets_upload/router.py).
        raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found")
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ApiError(
            422,
            decode_code,
            "Image decode failed" if decode_code == "IMAGE_DECODE_FAILED" else "Detect failed",
            "asset bytes are corrupt, truncated, or in an unsupported format",
            retryable=decode_retryable,
        )
    # Match browser rendering for EXIF-rotated JPEGs so normalized detection
    # boxes and the P4 source image share the same coordinate system.
    return ImageOps.exif_transpose(img).convert("RGB")


def _crop_norm(img: Image.Image, values: dict) -> Image.Image:
    w, h = img.size
    x0 = int(round(values["x_norm"] * w))
    y0 = int(round(values["y_norm"] * h))
    x1 = min(w, int(round((values["x_norm"] + values["w_norm"]) * w)))
    y1 = min(h, int(round((values["y_norm"] + values["h_norm"]) * h)))
    x1 = max(x1, x0 + 1)
    y1 = max(y1, y0 + 1)
    return img.crop((x0, y0, x1, y1))


def _normalize_glyph(alpha: Image.Image) -> Image.Image:
    """patch-D §2.1: bbox of the alpha -> fit 112 -> centered 128 canvas.

    Strokes render as white pixels with the mask as the alpha channel, so the
    P4 dark-canvas preview shows 白字 on 黑底.
    """
    bbox = alpha.getbbox()
    canvas = Image.new("RGBA", (DRAFT_CANVAS, DRAFT_CANVAS), (255, 255, 255, 0))
    if bbox is None:
        return canvas
    glyph = alpha.crop(bbox)
    scale = min(DRAFT_TARGET / glyph.width, DRAFT_TARGET / glyph.height)
    if abs(scale - 1.0) > 1e-9:
        glyph = glyph.resize(
            (max(1, int(glyph.width * scale)), max(1, int(glyph.height * scale))),
            Image.BILINEAR,
        )
    rgba = Image.merge(
        "RGBA",
        (
            Image.new("L", glyph.size, 255),
            Image.new("L", glyph.size, 255),
            Image.new("L", glyph.size, 255),
            glyph,
        ),
    )
    # RGBA already carries straight alpha; a self mask would square coverage.
    canvas.paste(
        rgba,
        ((DRAFT_CANVAS - rgba.width) // 2, (DRAFT_CANVAS - rgba.height) // 2),
    )
    return canvas


def _store_draft(session: Session, user_id: int, glyph: Image.Image) -> str:
    """Persist the half-product alpha draft under the caller's style drafts
    space; returns the storage-relative path (forward slashes).

    D01 wiring (b2-extraction-data 2026-09-27, external-audit 2.3): the draft
    lands through app/core/storage.py like every other user write - the
    filename is a generated hex name validated by the contained draft_path
    builder, the exact byte count is quota-gated under the shared QUOTA_LOCK
    (the same number GET /usage walks), and the write is atomic
    temp+replace. A quota rejection (413 QUOTA_EXCEEDED) or a disk failure
    therefore leaves zero residue: no half PNG at the final path, no .part
    file behind. The response/contract path shape
    ``users/{uid}/styles/drafts/<hex16>.png`` is unchanged.
    """
    buf = io.BytesIO()
    glyph.save(buf, format="PNG")
    data = buf.getvalue()
    name = uuid.uuid4().hex[:16] + ".png"
    abs_path = storage.draft_path(user_id, name)
    with storage.QUOTA_LOCK:
        storage.enforce_quota(user_id, len(data))
        storage.atomic_write_bytes(abs_path, data)
    return "/".join(("users", str(user_id), "styles", "drafts", name))


def detect_text(session: Session, user_id: int, payload: dict) -> dict:
    """POST /api/v1/detect-text (patch-A §3.1 + patch-D §3.4).

    Manual ``boxes`` are the contract-level evergreen path: they are echoed
    back as candidates untouched (no auto run). Otherwise the color-aware
    generator proposes candidates with baseline fallback. Locate-only: never
    ground truth.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")
    asset_id = payload.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ApiError(400, "INVALID_REQUEST", "asset_id must be a non-empty string")
    boxes = payload.get("boxes")
    if boxes is not None:
        if not isinstance(boxes, list):
            raise ApiError(400, "INVALID_REQUEST", "boxes must be a list of box_norm objects")
        items = []
        for box in boxes:
            _validate_box_norm(box)
            items.append({"box_norm": {k: float(box[k]) for k in BOX_KEYS}, "score": 1.0})
        # 2026-09-27 (B1 P05 cross-domain finding): the evergreen manual path
        # previously echoed boxes without touching the asset at all - a
        # soft-deleted/foreign/missing asset got a 200. Load it through the
        # same gate (ownership + enabled + existence + decode) before echoing.
        _load_asset_image(
            session, user_id, asset_id, decode_code="DETECT_FAILED", decode_retryable=True
        )
        return {"items": items}
    try:
        img = _load_asset_image(
            session, user_id, asset_id, decode_code="DETECT_FAILED", decode_retryable=True
        )
        candidates = DEFAULT_GENERATOR.detect(img)
    except ApiError:
        raise
    except Exception:
        raise ApiError(
            422,
            "DETECT_FAILED",
            "Detect failed",
            "candidate generation raised an internal error; retry may succeed",
            retryable=True,
        )
    return {"items": candidates}


def extract(session: Session, user_id: int, payload: dict) -> dict:
    """POST /api/v1/extract (patch-D §3.1).

    Half-product generator: box-located background separation -> draft alpha
    PNG. EVERY success response carries needs_touchup=true (patch-D §3.1:
    no direct-registration path exists). Errors follow patch-D §8 verbatim.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "request body must be a JSON object")
    asset_id = payload.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ApiError(400, "INVALID_REQUEST", "asset_id must be a non-empty string")
    values = _validate_box_norm(payload.get("box_norm"))

    timeout_ms = _worker_timeout_ms()
    deadline = time.monotonic() + timeout_ms / 1000.0
    try:
        img = _load_asset_image(session, user_id, asset_id)
        if _deadline_exceeded(deadline):
            raise _timeout()
        crop = _crop_norm(img, values)
        if _deadline_exceeded(deadline):
            raise _timeout()
        mask = color_stroke_mask(crop)
        if mask is None:
            mask = DEFAULT_SEPARATOR.separate(crop)
        if _deadline_exceeded(deadline):
            raise _timeout()
    except ApiError:
        raise
    except Exception:
        raise ApiError(
            422,
            "EXTRACT_FAILED",
            "Extract failed",
            "background separation raised an internal error; retry may succeed",
            retryable=True,
        )

    min_area = max(DESPECKLE_FLOOR_PX, int(DESPECKLE_RATE * mask.size[0] * mask.size[1]))
    comps = _filter_components(_connected_components(mask), min_area)
    if not comps:
        # patch-D §8: the box simply has no extractable strokes - "请重框",
        # an execution-outcome answer, not an internal error.
        raise ApiError(
            422,
            "NO_STROKES_FOUND",
            "No strokes found",
            "no valid connected component survived separation inside the box; re-select the box",
        )
    alpha = _rebuild(mask, comps, min_area)
    if _deadline_exceeded(deadline):
        raise _timeout()
    glyph = _normalize_glyph(alpha)
    stroke_path = _store_draft(session, user_id, glyph)
    # patch-D §3.1: needs_touchup is unconditionally true. There is no
    # quality field and no pass-through path anywhere in this domain.
    return {
        "stroke_path": stroke_path,
        "draft_alpha_path": stroke_path,
        "needs_touchup": True,
    }


def _timeout() -> ApiError:
    return ApiError(
        422,
        "EXTRACT_TIMEOUT",
        "Extract timeout",
        f"single-run execution exceeded WORKER_TIMEOUT_MS={_worker_timeout_ms()} [暂定]",
        retryable=True,
    )
