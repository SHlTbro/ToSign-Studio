"""Upload pipeline (patch-C section 11.1/11.2/11.3/11.4, v0.2.1 section 5).

Lossless red line: PNG/JPEG bytes are stored byte-identical (no re-encode,
no JPEG conversion anywhere); HEIC/HEIF is the single allowed transcode and
it lands as lossless PNG. Every gate raises before a single byte is written,
and the quota gate runs against the same six-pool walk that GET /usage
reports, so enforcement and display can never disagree.

Concurrency (C-22 hardening): quota-check -> dedup -> file write -> DB insert
-> commit runs under a process-local lock (deployment is single-process per
dispatch G72-7). Concurrent uploaders therefore serialize: each batch
accounts for every previously committed batch on disk, total stored bytes can
never overshoot LIMIT_BYTES, losers get 413 QUOTA_EXCEEDED, and any failure
mid-batch removes every file (and .part temp) written by that batch so a
rejected request leaves zero residue.
"""
import hashlib
import io
import os
import threading
import uuid
from datetime import datetime

import pillow_heif
from PIL import Image, ImageOps

from app.core.errors import ApiError
from app.domains.usage.service import LIMIT_BYTES, storage_root, total_usage_bytes

pillow_heif.register_heif_opener()

MAX_FILE_BYTES = 50_000_000
MAX_BATCH_COUNT = 20
MAX_PIXELS = 24_000_000
HEIC_EXTENSIONS = {".heic", ".heif"}

# Serializes the quota-critical section across threads of this single-process
# backend; see module docstring. Held only around the short DB/disk section,
# never around decode/validation.
_QUOTA_LOCK = threading.Lock()


def _ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _decode(data: bytes, filename: str) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ApiError(422, "IMAGE_DECODE_FAILED", f"cannot decode image: {filename}")
    return img


def _normalized_size(img: Image.Image) -> tuple:
    """EXIF-normalized (width, height); metadata only, never re-saved."""
    return ImageOps.exif_transpose(img).size


def _check_dimensions(width: int, height: int, filename: str) -> None:
    if width * height > MAX_PIXELS:
        raise ApiError(
            413,
            "FILE_TOO_LARGE",
            f"{filename}: {width}x{height} exceeds {MAX_PIXELS} pixels",
        )
    if width > 3 * height or height > 3 * width:
        raise ApiError(
            422,
            "ASPECT_RATIO_UNSUPPORTED",
            f"{filename}: aspect ratio {width}:{height} outside 1:3..3:1",
        )


def _prepare(filename: str, data: bytes) -> dict:
    """Validate one upload and produce the exact bytes to store."""
    ext = _ext_of(filename)
    img = _decode(data, filename)
    width, height = _normalized_size(img)
    _check_dimensions(width, height, filename)
    if ext in HEIC_EXTENSIONS:
        buf = io.BytesIO()
        img.save(buf, "PNG")
        stored = buf.getvalue()
        ext = ".png"
    else:
        # Lossless red line: store the uploaded bytes untouched.
        stored = data
    return {
        "filename": filename,
        "stored_bytes": stored,
        "ext": ext,
        "sha256": hashlib.sha256(stored).hexdigest(),
        "width": width,
        "height": height,
    }


def _store_locked(session, user_id: str, results: list, new_bytes: int, asset_type: str) -> None:
    """Quota-gate, write, and commit one batch. Caller holds _QUOTA_LOCK."""
    if new_bytes and total_usage_bytes(user_id) + new_bytes > LIMIT_BYTES:
        raise ApiError(
            413,
            "QUOTA_EXCEEDED",
            "upload would exceed the 5 GB per-user quota; free up space first",
        )

    from app.domains.assets_upload.models import Asset

    assets_dir = os.path.join(storage_root(), user_id, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    written: list = []
    try:
        for item in results:
            if item["reused"]:
                continue
            asset_id = uuid.uuid4().hex
            relative_path = os.path.join(user_id, "assets", asset_id + item["ext"])
            abs_path = os.path.join(storage_root(), relative_path)
            tmp_path = abs_path + ".part"
            try:
                with open(tmp_path, "wb") as fh:
                    fh.write(item["stored_bytes"])
                os.replace(tmp_path, abs_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            written.append(abs_path)
            # T58 Phase 2 (critique item 2): 320px thumbnail for gallery list.
            try:
                from PIL import Image as _PILImage

                thumb_dir = os.path.join(storage_root(), user_id, "thumbnails")
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_abs = os.path.join(thumb_dir, asset_id + ".png")
                with _PILImage.open(abs_path) as im:
                    im.thumbnail((320, 320))
                    im.save(thumb_abs, "PNG")
                thumb_rel = os.path.join(user_id, "thumbnails", asset_id + ".png")
            except Exception:
                thumb_rel = None
            asset = Asset(
                asset_id=asset_id,
                user_id=user_id,
                kind=asset_type,
                relative_path=relative_path,
                sha256=item["sha256"],
                width=item["width"],
                height=item["height"],
                bytes=len(item["stored_bytes"]),
                thumbnail_path=thumb_rel,
                uploaded_at=datetime.utcnow(),
            )
            session.add(asset)
            item["asset"] = asset
        session.commit()
    except Exception:
        # Zero half-finished state: roll back the DB rows and remove every
        # file this batch already landed on disk.
        session.rollback()
        for path in written:
            if os.path.exists(path):
                os.remove(path)
        raise


def store_batch(session, user_id: str, files: list, asset_type: str = "cos") -> list:
    """Validate, dedup, quota-gate, then atomically store a batch of uploads.

    ``files`` is a list of (filename, content_bytes). The batch is
    all-or-nothing: every gate for every file passes before any file hits
    disk, so a rejected batch leaves zero residue.
    """
    if asset_type not in {"cos", "sample_source"}:
        raise ApiError(400, "INVALID_REQUEST", "asset_type must be cos or sample_source")
    if not files:
        raise ApiError(400, "INVALID_REQUEST", "empty upload batch")
    if len(files) > MAX_BATCH_COUNT:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"batch has {len(files)} files, max is {MAX_BATCH_COUNT}",
        )
    for filename, data in files:
        if len(data) > MAX_FILE_BYTES:
            raise ApiError(
                413,
                "FILE_TOO_LARGE",
                f"{filename}: {len(data)} bytes exceeds {MAX_FILE_BYTES}",
            )

    prepared = [_prepare(filename, data) for filename, data in files]

    with _QUOTA_LOCK:
        from app.domains.assets_upload.models import Asset

        results = []
        new_bytes = 0
        for item in prepared:
            existing = (
                session.query(Asset)
                .filter(
                    Asset.user_id == user_id,
                    Asset.sha256 == item["sha256"],
                    Asset.kind == asset_type,
                )
                .first()
            )
            if existing is not None:
                item["asset"] = existing
                item["reused"] = True
            else:
                item["asset"] = None
                item["reused"] = False
                new_bytes += len(item["stored_bytes"])
            results.append(item)

        _store_locked(session, user_id, results, new_bytes, asset_type)
    return results
