# -*- coding: utf-8 -*-
"""S11 glyph_provider — personal-style per-character glyph provider (render side).

Consumes the S10 artifact registry (app/domains/styles, READ-ONLY: tables +
runstore helpers + GPU-lock functions) and turns an approved personal style
into per-character transparent glyph layers for the render core.

Per-character fallback chain (card S11):
    M2 (bound artifact glyph, hash-verified)
      -> M1 (frozen-basis inference via the engine/style/m1_adapter.runner
             SUBPROCESS - the API process never imports the research stack;
             GPU locked through .ai-hub/parallel-lines/line-b/gpu.lock)
      -> M0 (existing system-font rendering path)
    generation failure + strict_glyph -> the char blocks the item
    (GLYPH_MISSING, human must PATCH/reject); without strict the char falls
    to M0 and the downgrade is recorded, never passed off as personal style.

Every char record carries source (M2|M1|M0) + version (artifact engine
version / M1 model_version / M0 font name) + the fallback reason, is written
to a sidecar JSON next to the output PNG (render item metadata on disk) and
summarized into item.warnings (already exposed by the review payload).

Determinism (card point 3): the M1 seed is derived stably from
(artifact_id, engine_version, char) - same input -> same seed -> same pixels;
the generated-png cache dir is keyed by artifact_id + sanitized engine
version, so a style version switch can never cross-contaminate the cache.
Approved/enabled state is NEVER cached: every render re-resolves the profile
and re-verifies the artifact hash chain (S10 handover requirement).

Anti-fabrication red lines:
- a tampered artifact (any recomputed hash mismatch) is refused and the
  render falls back to M0 with reason "artifact_hash_mismatch";
- pending_review / disabled profiles are never consumed (reason recorded);
- a degenerate glyph (empty alpha, or a near-fully-opaque block = black-box /
  inverted alpha polarity) is rejected, never stamped.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue as queue_mod
import re
import subprocess
import threading
import time
from pathlib import Path
from engine.style.m2_adapter.trainer_resume_protocol import native_path

from app.core.errors import ApiError
from app.core.storage import ensure_within_root
from app.domains.styles import runstore
from app.domains.styles.models import StyleProfile, StyleRun, StyleRunArtifact
from .selection_contract import SOURCE_POLICIES, REQUIRED_M2_POLICIES, character_requires_m2

PROJ = Path(__file__).resolve().parents[3]

SOURCE_M2 = "M2"
SOURCE_M1 = "M1"
SOURCE_M0 = "M0"

M2_SOURCE_TYPE = "M2_ADAPTER"  # mirrors styles.service.M2_SOURCE_TYPE (read-only mirror)

# M1 subprocess environment (same conventions as app/domains/styles/worker.py).
M1_DEFAULT_PYTHON = PROJ / ".venv-ml" / "Scripts" / "python.exe"
M1_DEFAULT_CKPT_DIR = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio" / "ckpt"
M1_DEFAULT_TTF = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio" / "ttf" / "KaiXinSongA.ttf"

M1_TIMEOUT_S = 240.0          # per-char subprocess deadline (YY_RENDER_M1_TIMEOUT_S)
# alpha>=128 over this share of the canvas = block, not a glyph. Real CJK
# handwriting glyphs stay far below (~10-45%); a black block or an
# inverted-polarity glyph (opaque background, transparent strokes) is >=60%.
GLYPH_BLOCK_MAX_INK_RATIO = 0.60
TRACKING_DIVISOR = 40         # inter-char tracking = size // 40 (mirrors stroke proportion)

_SUBPROC_LOG_CAP = 64 * 1024  # bounded subprocess stdout copy (anti-deadlock)

# In-process verified-glyph cache: (artifact_id, file_sha256) -> RGBA image.
# Caches FILE CONTENT keyed by content hash - never approval state.
_img_cache: dict = {}
_img_cache_lock = threading.Lock()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _m1_python() -> str:
    return os.environ.get("YY_STYLE_M1_PYTHON") or str(M1_DEFAULT_PYTHON)


def _m1_ckpt_dir() -> Path:
    return Path(os.environ.get("YY_RENDER_M1_CKPT_DIR") or M1_DEFAULT_CKPT_DIR)


def _m1_content_ttf() -> Path:
    return Path(os.environ.get("YY_RENDER_M1_CONTENT_TTF") or M1_DEFAULT_TTF)


def _m1_timeout_s() -> float:
    return _env_float("YY_RENDER_M1_TIMEOUT_S", M1_TIMEOUT_S)


def _gpu_lock_wait_s() -> float:
    return _env_float("YY_STYLE_GPU_LOCK_WAIT_S", 0.0)


def _stable_seed(artifact_id: int, engine_version: str, char: str) -> int:
    """Deterministic per-(artifact version, char) seed: same input -> same
    seed -> pixel-identical previews and formal renders (M1 is seed-pinned,
    verified bit-identical by the B-ENGINE S04 wave)."""
    digest = hashlib.sha256(
        f"yy-render-glyph|{int(artifact_id)}|{engine_version}|{char}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big")


def _version_slug(engine_version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(engine_version))[:48] or "unknown"


# ---------------------------------------------------------------------------
# artifact resolution (S10 registry consumption, read-only, non-raising)
# ---------------------------------------------------------------------------

def resolve_personal_binding(session, user_id: int, style_id, *, observe_environment: bool = True) -> dict:
    """Resolve an approved personal profile to its verified active artifact.

    Mirrors the S10 publish verification recipe exactly (styles.service.
    _load_verified_manifest): manifest readable, manifest file sha256 ==
    registry row, every bound file + the metrics receipt recompute to their
    recorded hashes. NEVER raises for render purposes - any failure returns
    state=unavailable with a reason so the render can fall back to M0 while
    saying why (create-time gates for unapproved styles stay in service.py).
    """
    sid = int(style_id)
    profile = (
        session.query(StyleProfile)
        .filter(StyleProfile.style_id == sid, StyleProfile.user_id == user_id)
        .first()
    )
    if profile is None:
        return _unavailable(sid, "style_not_found", "profile does not resolve for this user")
    if profile.status != "approved":
        return _unavailable(sid, "style_not_approved", f"profile status is {profile.status}")
    if not profile.enabled:
        return _unavailable(sid, "style_disabled", "profile is disabled for rendering")
    receipt_id = profile.receipt_id or ""
    if receipt_id.startswith('imported_inference/'):
        try:
            from app.domains.styles.imported_inference import resolve
            return resolve(session, user_id, profile, observe_environment=observe_environment)
        except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
            return _unavailable(sid, 'imported_inference_unavailable', str(exc))
    if not receipt_id.startswith("style_run_artifacts/"):
        return _unavailable(sid, "artifact_not_bound", "profile carries no artifact binding")
    try:
        artifact_id = int(receipt_id[len("style_run_artifacts/"):])
    except ValueError:
        return _unavailable(sid, "artifact_not_bound", "profile artifact binding is malformed")
    artifact = (
        session.query(StyleRunArtifact)
        .filter(StyleRunArtifact.artifact_id == artifact_id, StyleRunArtifact.user_id == user_id)
        .first()
    )
    if artifact is None:
        return _unavailable(sid, "artifact_not_bound", f"artifact {artifact_id} does not resolve")
    if artifact.profile_id != sid:
        return _unavailable(sid, "artifact_profile_mismatch", "registry artifact belongs to a different profile")
    owned_run = session.get(StyleRun, artifact.run_id)
    if owned_run is None or owned_run.user_id != user_id:
        return _unavailable(sid, "artifact_run_owner_mismatch", "registry run ownership differs")
    if artifact.quality_status == 'OWNER_DIRECT_VISUAL_ACCEPTED':
        # Queued renders and cached glyphs must recheck acceptance too; creation
        # time approval cannot authorize later consumption of changed evidence.
        from app.domains.styles.service import resolve_style_artifact
        try:
            resolve_style_artifact(session,user_id,sid)
        except (ApiError,OSError,ValueError,KeyError,TypeError) as exc:
            return _unavailable(sid,'owner_acceptance_stale',str(exc))

    base_dir = runstore.run_dir(user_id, artifact.run_id)
    manifest_path = runstore.artifact_manifest_path(user_id, artifact.run_id)
    if not native_path(manifest_path).is_file():
        return _unavailable(sid, "artifact_missing", "artifact manifest file missing on disk")
    # registry row hash must match the manifest file bytes (tamper guard)
    if runstore.sha256_file(manifest_path) != artifact.sha256:
        return _unavailable(sid, "artifact_hash_mismatch", "manifest file hash != registry sha256")
    manifest = runstore.read_json(manifest_path)
    if not isinstance(manifest, dict):
        return _unavailable(sid, "artifact_invalid", "artifact manifest is not readable JSON")
    for key in ("engine_mode", "engine_id", "engine_version", "files",
                "metrics_receipt_relative_path", "metrics_receipt_sha256"):
        if key not in manifest:
            return _unavailable(sid, "artifact_invalid", f"manifest missing field: {key}")

    deployment = None
    if manifest.get('engine_mode') == 'M2_ADAPTER':
        try:
            from engine.style.m2_adapter.preserved_training_deployment import lookup as lookup_preserved
            from engine.style.m2_adapter.deployment import lookup_deployment
            deployment = lookup_preserved(PROJ,base_dir,user_id=user_id)
            if deployment is None:deployment = lookup_deployment(PROJ, base_dir, artifact.sha256)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _unavailable(sid, 'm2_deployment_invalid', str(exc))
    from engine.style.m2_adapter.windows_scr_deployment import is_windows
    windows_deployed=bool(deployment and is_windows(deployment.get('deployment_binding')))

    problems: list[str] = []
    files_by_rel: dict[str, dict] = {}
    for entry in manifest["files"]:
        rel = str(entry.get("relative_path", ""))
        if windows_deployed and rel not in ('artifact/m2_adapter.pt','m2-receipt.json'):
            # The original full manifest remains byte-pinned. Optimizer/best/last
            # training exports are not inputs to this explicitly qualified old
            # terminal adapter deployment; this path cannot resume training.
            continue
        try:
            path = ensure_within_root(base_dir / rel, base_dir)
        except Exception:  # noqa: BLE001 - containment: refuse the entry
            return _unavailable(sid, "artifact_invalid", f"artifact file path escapes the run dir: {rel}")
        if not native_path(path).is_file():
            return _unavailable(sid, "artifact_missing", f"artifact file missing: {rel}")
        if runstore.sha256_file(path) != entry.get("sha256"):
            # a tampered bound file poisons the whole artifact: refuse to
            # consume ANY of it (mirrors the S10 publish verification verdict)
            return _unavailable(sid, "artifact_hash_mismatch", f"artifact hash mismatch: {rel}")
        files_by_rel[rel] = entry
    receipt_rel = str(manifest["metrics_receipt_relative_path"])
    receipt_file = base_dir / receipt_rel
    if not native_path(receipt_file).is_file() or runstore.sha256_file(receipt_file) != manifest["metrics_receipt_sha256"]:
        return _unavailable(sid, "artifact_hash_mismatch", "metrics receipt missing or hash mismatch")

    active_snapshot = deployment['snapshot_binding'] if deployment else manifest.get('snapshot_binding')
    active_runtime = deployment['runtime_binding'] if deployment else manifest.get('runtime_binding')
    chars, style_refs = _build_glyph_index(
        base_dir, manifest, files_by_rel, problems,
        base_dir / str(manifest["metrics_receipt_relative_path"]))
    if manifest.get('engine_mode')=='M2_ADAPTER':
        from app.domains.styles.m2_topology import validated_topology
        try:validated_topology(manifest)
        except (ValueError,KeyError,TypeError) as exc:return _unavailable(sid,'m2_topology_invalid',str(exc))
        try:style_refs=_snapshot_style_refs(active_snapshot or {},reference_label=(manifest.get('inference_defaults') or {}).get('reference_label','永'),allow_env=active_runtime is None)
        except Exception as exc:return _unavailable(sid,'m2_snapshot_invalid',str(exc))
    return {
        "state": "bound",
        "style_id": sid,
        "artifact_id": artifact_id,
        "run_id": artifact.run_id,
        "engine_mode": str(manifest["engine_mode"]),
        "engine_id": str(manifest["engine_id"]),
        "engine_version": str(manifest["engine_version"]),
        "manifest_sha256": artifact.sha256,
        "base_dir": str(base_dir),
        "chars": chars,
        "style_refs": style_refs,
        "problems": problems,
        "m2_adapter": ({"path": str(base_dir / "artifact/m2_adapter.pt"),
                        "sha256": files_by_rel["artifact/m2_adapter.pt"]["sha256"]}
                       if "artifact/m2_adapter.pt" in files_by_rel else None),
        "snapshot_binding": active_snapshot,
        "base_ckpt_sha256": manifest.get("base_ckpt_sha256"),
        "runtime_manifest_path": manifest.get("runtime_manifest_path"),
        "runtime_identity_sha256": manifest.get("runtime_identity_sha256"),
        "runtime_binding": active_runtime,
        "deployment_binding": deployment['deployment_binding'] if deployment else None,
        "deployment_user_id": user_id if deployment else None,
        "deployment_version": deployment.get('deployment_version') if deployment else None,
        "training_runtime_binding": manifest.get("runtime_binding") if deployment else None,
        "training_snapshot_binding": manifest.get("snapshot_binding") if deployment else None,
        "preprocessing": manifest.get("preprocessing"),
        "training_objective": manifest.get("training_objective"),
        "training_recipe": manifest.get("training_recipe"),
        "training_budget": manifest.get("training_budget"),
        **{key:manifest[key] for key in ('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity') if key in manifest},
    }


def _snapshot_style_refs(binding,reference_label="永",allow_env=True):
    from engine.style.dataset.binding import verify_snapshot_binding
    root=Path((os.environ.get('YY_RENDER_M2_SNAPSHOT_DIR') if allow_env else None) or binding['snapshot_dir'])
    canonical=verify_snapshot_binding(root,binding['snapshot_sha256'])
    for k in ('snapshot_id','style_id','index_sha256','provenance_sha256'):
        if canonical.get(k)!=binding.get(k):raise ValueError('M1 reference snapshot binding mismatch')
    split=json.loads((root/'split.json').read_text(encoding='utf-8'))['assignments']
    rows=[json.loads(l) for l in (root/'samples.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    rows=sorted((r for r in rows if split[r['sample_id']]=='train'),key=lambda r:(r['label']!=reference_label,r['sample_id']))
    if not rows:raise ValueError('no verified training-side style reference')
    return [str((root/r['snapshot_file']).resolve()) for r in rows]


def _unavailable(style_id: int, reason: str, detail: str) -> dict:
    return {"state": "unavailable", "style_id": style_id, "reason": reason, "detail": detail}


def _historical_m2_lineage_matches(receipt: dict, manifest: dict, files_by_rel: dict) -> bool:
    """A historical receipt cannot borrow the selected adapter's M2 identity.

    Original receipts remain untouched. Incomplete/mismatched historical
    evidence is unavailable for current M2 replay, never silently upgraded.
    Registered variants never use this legacy replay path at all.
    """
    if manifest.get("runtime_binding") is not None:
        return False
    adapter = files_by_rel.get("artifact/m2_adapter.pt") or {}
    digest = adapter.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return False
    expected = {"source": SOURCE_M2, "engine_mode": "M2_ADAPTER", "adapter_applied": True,
                "adapter_sha256": digest, "model_version": manifest.get("engine_version"),
                "engine_id": manifest.get("engine_id"),
                "dataset_snapshot_sha256": manifest.get("dataset_snapshot_sha256"),
                "base_ckpt_sha256": manifest.get("base_ckpt_sha256"),
                "runtime_identity_sha256": manifest.get("runtime_identity_sha256"),
                "preprocessing": manifest.get("preprocessing")}
    if (any(value is None for value in expected.values())
            or receipt.get("adapter_applied") is not True
            or any(receipt.get(key) != value for key, value in expected.items())):
        return False
    from engine.style.dataset.binding import verify_snapshot_binding
    from engine.style.dataset.schema import DatasetError
    try:
        stored = manifest["snapshot_binding"]
        canonical = verify_snapshot_binding(stored["snapshot_dir"], manifest["dataset_snapshot_sha256"])
        if canonical != stored or (receipt.get("snapshot_binding") is not None and receipt["snapshot_binding"] != canonical):
            return False
        inputs = receipt.get("inputs") or {}
        ref = Path(inputs["style_ref_path"]).resolve()
        refs = _snapshot_style_refs(canonical, allow_env=False)
        if str(ref) not in refs or runstore.sha256_file(ref) != inputs.get("style_ref_sha256"):
            return False
    except (OSError, ValueError, KeyError, TypeError, DatasetError):
        return False
    return True


def _build_glyph_index(base_dir: Path, manifest: dict, files_by_rel: dict,
                       problems: list[str], metrics_receipt_path: Path) -> tuple[dict, list[str]]:
    """char -> {"relative_path", "sha256", "seed"?} + M1 style-ref paths.

    Two honest sources, both hash-verified:
    - manifest["glyph_index"]: explicit {char: {relative_path, sha256}}
      extension (S05/M2 or fixture artifacts may declare it);
    - the hash-bound engine receipt's ``steps`` (the M1 worker shape - the
      manifest itself carries no steps; the metrics receipt, whose sha256 was
      verified by resolve_personal_binding, lists each step's own receipt file
      with its sha256; that receipt carries inputs.target_char and
      output.png, which must match a hash-verified manifest.files entry).
    """
    chars: dict[str, dict] = {}
    style_refs: list[str] = []

    index = manifest.get("glyph_index")
    if isinstance(index, dict):
        for ch, entry in index.items():
            if not isinstance(ch, str) or len(ch) != 1 or not isinstance(entry, dict):
                problems.append("glyph_index_invalid_entry")
                continue
            rel = str(entry.get("relative_path", ""))
            try:
                path = ensure_within_root(base_dir / rel, base_dir)
            except Exception:  # noqa: BLE001
                problems.append(f"glyph_index_path_escape:{rel}")
                continue
            bound = files_by_rel.get(rel)
            if bound is None:
                problems.append(f"glyph_index_file_unverified:{rel}")
                continue
            if entry.get("sha256") and entry["sha256"] != bound["sha256"]:
                problems.append(f"glyph_index_hash_conflict:{rel}")
                continue
            # An unlabelled cached PNG is not evidence of M2 generation.
            record = {"relative_path": rel, "sha256": bound["sha256"], "source": None}
            receipt_rel = entry.get("receipt_relative_path")
            receipt_bound = files_by_rel.get(receipt_rel) if isinstance(receipt_rel, str) else None
            if receipt_bound:
                receipt_path = ensure_within_root(base_dir / receipt_rel, base_dir)
                generation = runstore.read_json(receipt_path) or {}
                if (runstore.sha256_file(receipt_path) == receipt_bound["sha256"]
                        and generation.get("ok") is True
                        and generation.get("inputs", {}).get("target_char") == ch
                        and generation.get("output", {}).get("png_sha256") == bound["sha256"]):
                    mode = generation.get("engine_mode")
                    origin = (SOURCE_M2 if mode == "M2_ADAPTER" and _historical_m2_lineage_matches(generation, manifest, files_by_rel)
                              else SOURCE_M1 if mode == "M1_FROZEN" else None)
                    if mode == "M2_ADAPTER" and origin is None:
                        problems.append(f"historical_m2_lineage_unverified:{ch}")
                    record.update({"source": origin, "engine_mode": mode,
                                   "engine_version": generation.get("model_version")})
            chars[ch] = record

    steps = manifest.get("steps")
    if not isinstance(steps, list) and native_path(metrics_receipt_path).is_file():
        engine_receipt = runstore.read_json(metrics_receipt_path) or {}
        steps = engine_receipt.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            receipt_rel = str(step.get("receipt_relative_path", ""))
            receipt_sha = step.get("receipt_sha256")
            if not isinstance(receipt_sha, str) or re.fullmatch(r"[0-9a-f]{64}", receipt_sha) is None:
                problems.append(f"step_receipt_digest_missing:{receipt_rel}")
                continue
            try:
                if not receipt_rel or Path(receipt_rel).is_absolute():
                    raise ValueError("relative contained receipt required")
                receipt_path = ensure_within_root(base_dir / receipt_rel, base_dir)
            except (OSError, ValueError, ApiError):
                problems.append(f"step_receipt_path_escape:{receipt_rel}")
                continue
            if not native_path(receipt_path).is_file():
                problems.append(f"step_receipt_missing:{receipt_rel}")
                continue
            if runstore.sha256_file(receipt_path) != receipt_sha:
                problems.append(f"step_receipt_hash_mismatch:{receipt_rel}")
                continue
            receipt = runstore.read_json(receipt_path)
            if not isinstance(receipt, dict) or receipt.get("ok") is not True:
                problems.append(f"step_receipt_invalid:{receipt_rel}")
                continue
            inputs = receipt.get("inputs") or {}
            output = receipt.get("output") or {}
            ch = inputs.get("target_char")
            png_out = output.get("png")
            if not isinstance(ch, str) or len(ch) != 1 or not png_out:
                problems.append(f"step_receipt_no_char:{receipt_rel}")
                continue
            rel = None
            for cand, entry in files_by_rel.items():
                if os.path.normcase(str((base_dir / cand).resolve())) == os.path.normcase(str(Path(png_out).resolve())):
                    rel = cand
                    break
            if rel is None:
                problems.append(f"step_png_unverified:{ch}")
                continue
            if output.get("png_sha256") != files_by_rel[rel]["sha256"]:
                problems.append(f"step_png_digest_mismatch:{ch}")
                continue
            ref = inputs.get("style_ref_path")
            if isinstance(ref, str) and ref and ref not in style_refs and native_path(ref).is_file():
                style_refs.append(ref)
            mode = receipt.get("engine_mode")
            origin = (SOURCE_M2 if mode == "M2_ADAPTER" and _historical_m2_lineage_matches(receipt, manifest, files_by_rel)
                      else SOURCE_M1 if mode == "M1_FROZEN" else None)
            if mode == "M2_ADAPTER" and origin is None:
                problems.append(f"historical_m2_lineage_unverified:{ch}")
            record = {"relative_path": rel, "sha256": files_by_rel[rel]["sha256"],
                      "source": origin, "engine_mode": mode,
                      "engine_version": receipt.get("model_version") or receipt.get("engine_version")}
            if isinstance(inputs.get("seed"), int):
                record["receipt_seed"] = inputs["seed"]
            chars.setdefault(ch, record)

    return chars, style_refs


# ---------------------------------------------------------------------------
# glyph image handling (polarity guard, colorize, layout)
# ---------------------------------------------------------------------------

def _polarity_problem(img) -> str | None:
    """Anti-black-block / anti-inverted-polarity guard: a usable glyph has
    some opaque ink and is not a near-fully-opaque rectangle."""
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    total = img.width * img.height
    ink = sum(hist[128:])
    if ink == 0:
        return "glyph_empty"
    if ink / total > GLYPH_BLOCK_MAX_INK_RATIO:
        return "glyph_block"
    return None


def _load_glyph_image(path: Path, cache_key: tuple, expected_sha: str | None = None) -> tuple[object, str | None]:
    from PIL import Image

    with _img_cache_lock:
        hit = _img_cache.get(cache_key)
    if hit is not None:
        return hit, None
    try:
        import io
        data = native_path(path).read_bytes()
        if expected_sha is not None and hashlib.sha256(data).hexdigest() != expected_sha:
            return None, "glyph_hash_mismatch"
        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001 - unreadable file -> unusable glyph
        return None, "glyph_unreadable"
    problem = _polarity_problem(img)
    if problem is not None:
        return None, problem
    with _img_cache_lock:
        _img_cache[cache_key] = img
    return img, None


def _colorized_cell(img, size: int, fill, stroke_fill, *, add_outline: bool = True, preserve_canvas: bool = False):
    """Scale the glyph to height=size and colorize: stroke color through the
    dilated alpha (under), fill color through the glyph alpha (over) - the
    stamped char honours the luminance-based black/white + stroke policy."""
    from PIL import Image, ImageChops, ImageFilter

    scale = size / img.height
    w2 = max(1, round(img.width * scale))
    h2 = max(1, size)
    alpha = img.getchannel("A").resize((w2, h2), Image.LANCZOS)
    if not add_outline:
        bbox = alpha.getbbox()
        if bbox is None:
            return None
        native = Image.new("RGBA", (w2, h2), tuple(fill) + (255,))
        native.putalpha(alpha)
        return native if preserve_canvas else native.crop(bbox)
    k = 3 if size < 60 else 5
    dilated = alpha.filter(ImageFilter.MaxFilter(k))
    stroke_mask = ImageChops.subtract(dilated, alpha)
    stroke_layer = Image.new("RGBA", (w2, h2), tuple(stroke_fill) + (255,))
    stroke_layer.putalpha(stroke_mask)
    fill_layer = Image.new("RGBA", (w2, h2), tuple(fill) + (255,))
    fill_layer.putalpha(alpha)
    cell = Image.alpha_composite(stroke_layer, fill_layer)
    bbox = dilated.getbbox()
    if bbox is None:
        return None
    return cell.crop(bbox)


def _m0_cell(char: str, font_path: str, size: int, fill, stroke_fill, *, identity=None):
    """Render one char with the (post-fallback) system font onto a transparent
    cell - same visual policy parameters the legacy draw.text call uses."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        if identity is None:
            font = ImageFont.truetype(font_path, size=size)
        else:
            from .mixed_fonts import load_font
            font = load_font(font_path, size, identity)
    except Exception:  # noqa: BLE001
        return None
    stroke_width = max(1, size // 40)
    probe = Image.new("RGBA", (size * 3 + 8, size * 3 + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(probe)
    d.text((size, size), char, font=font, fill=tuple(fill) + (255,),
           stroke_width=stroke_width, stroke_fill=tuple(stroke_fill) + (255,))
    bbox = probe.getbbox()
    if bbox is None:
        return None
    return probe.crop(bbox)


def _compose_strip(cells: list, size: int):
    """Horizontal strip, all ink boxes bottom-aligned (stable anchor for CJK
    single chars - no per-char baseline drift), fixed tracking."""
    from PIL import Image

    if not cells:
        return None
    tracking = max(0, size // TRACKING_DIVISOR)
    total_w = sum(c.width for c in cells) + tracking * (len(cells) - 1)
    strip_h = max([size] + [c.height for c in cells])
    strip = Image.new("RGBA", (max(1, total_w), strip_h), (0, 0, 0, 0))
    x = 0
    for cell in cells:
        strip.paste(cell, (x, strip_h - cell.height), cell)
        x += cell.width + tracking
    return strip


# ---------------------------------------------------------------------------
# M1 subprocess (runner contract 0/2/3; GPU lock; bounded drain)
# ---------------------------------------------------------------------------

def _m1_gate() -> str | None:
    """Cheap availability precheck. Returns a failure reason or None."""
    if not native_path(_m1_python()).exists():
        return "m1_interpreter_missing"
    ckpt = _m1_ckpt_dir()
    for name in ("content_encoder.pth", "style_encoder.pth", "unet.pth"):
        if not native_path(ckpt / name).is_file():
            return "m1_ckpt_missing"
    if not native_path(_m1_content_ttf()).is_file():
        return "m1_ckpt_missing"
    return None


def _m1_cache_path(user_id: int, artifact_id: int, engine_version: str, char: str, seed: int) -> Path:
    """Disk cache key = artifact_id + engine version + codepoint + seed: a
    style version switch changes the directory, never reuses old glyphs."""
    root = runstore.storage_root()
    d = root / "users" / str(int(user_id)) / "styles" / "render-glyph-cache" / (
        f"{int(artifact_id)}-{_version_slug(engine_version)}")
    return d / f"{ord(char):04X}_{int(seed)}.png"


def _drain_bounded(proc, deadline: float) -> tuple[int, str]:
    """Read the child's merged stdout with a hard cap (no pipe-fill deadlock)
    and the per-char deadline (no whole-batch stall)."""
    from engine.style.subprocess_io import drain_bounded
    from engine.style.m2_adapter.inference_diagnostics import active_capture
    capture = active_capture()
    if capture is not None:
        return drain_bounded(proc, deadline, _SUBPROC_LOG_CAP,
                             on_stdout=capture.on_stdout, on_stop=capture.on_stop)
    return drain_bounded(proc, deadline, _SUBPROC_LOG_CAP)


def _cache_attempt_dirs(canonical: Path):
    """Locate preserved retry attempts; callers still verify every identity/hash.

    Only this key and generated UUID siblings are candidates. Do not follow
    arbitrary directory names or symlinks to files outside the cache root.
    """
    if native_path(canonical).is_dir() and not native_path(canonical).is_symlink():
        yield canonical
    pattern = re.compile(re.escape(canonical.name) + r"-[0-9a-f]{32}")
    for native_candidate in sorted(native_path(canonical.parent).glob(canonical.name + "-*")):
        candidate = canonical.parent / native_candidate.name
        if pattern.fullmatch(candidate.name) and native_candidate.is_dir() and not native_candidate.is_symlink():
            yield candidate


def _m1_generate(session_user_id: int, artifact_id: int, engine_version: str,
                 char: str, style_ref: Path) -> tuple[Path | None, str | None, dict]:
    """Legacy real runner with content/code-bound, independently verified cache."""
    from app.domains.styles.worker import gpu_lock_acquire, gpu_lock_release
    from engine.style.m1_adapter.receipts import (
        ENGINE_ID, RUNNER_VERSION, STYLE_PREPROCESSING_PROFILE,
        application_code_hashes, model_version_from_ckpt)

    seed = _stable_seed(artifact_id, engine_version, char)
    extra = {"seed": seed}
    try:
        ckpt_dir = _m1_ckpt_dir().resolve()
        content_ttf = _m1_content_ttf().resolve()
        style_ref = style_ref.resolve()
        model_version, ckpt_detail = model_version_from_ckpt(ckpt_dir)
        identity = {
            "style_ref_path": str(style_ref), "style_ref_sha256": runstore.sha256_file(style_ref),
            "content_ttf": str(content_ttf), "content_ttf_sha256": runstore.sha256_file(content_ttf),
            "ckpt_dir": str(ckpt_dir), "ckpt_detail": ckpt_detail,
            "runner_version": RUNNER_VERSION, "application_code_sha256": application_code_hashes(),
            "style_preprocessing_profile": STYLE_PREPROCESSING_PROFILE,
            "target_char": char, "seed": seed, "device": "cuda:0",
        }
    except OSError as exc:
        return None, "m1_input_missing", {**extra, "detail": str(exc)[:160]}
    # Older cache locations are historical evidence: never accept or overwrite
    # them after a normalization/model/font/code change.
    prior_path = _m1_cache_path(session_user_id, artifact_id, engine_version, char, seed)
    cache_dir = prior_path.parent / runstore.canonical_sha256(identity)
    cache_png = cache_dir / prior_path.name
    receipt_path = cache_png.with_suffix(".receipt.json")

    def valid_receipt(png_path, receipt_file):
        receipt = runstore.read_json(receipt_file) or {}
        if not isinstance(receipt, dict): return None
        inputs = receipt.get("inputs") or {}
        output = receipt.get("output") or {}
        if not isinstance(inputs, dict) or not isinstance(output, dict): return None
        if (receipt.get("ok") is True and receipt.get("engine_mode") == "M1_FROZEN"
                and receipt.get("engine_id") == ENGINE_ID
                and receipt.get("runner_version") == RUNNER_VERSION
                and receipt.get("model_version") == model_version
                and receipt.get("ckpt_detail") == ckpt_detail
                and receipt.get("application_code_sha256") == identity["application_code_sha256"]
                and all(inputs.get(key) == identity[key] for key in (
                    "target_char", "seed", "device", "style_ref_path", "style_ref_sha256",
                    "content_ttf", "content_ttf_sha256", "style_preprocessing_profile"))
                and native_path(png_path).is_file() and not native_path(png_path).is_symlink()
                and runstore.sha256_file(png_path) == output.get("png_sha256")):
            return receipt
        return None

    def evidence(receipt, receipt_file):
        return {**extra, "engine_mode": "M1_FROZEN", "model_version": receipt["model_version"],
                "style_preprocessing_profile": STYLE_PREPROCESSING_PROFILE,
                "inference_receipt_sha256": runstore.sha256_file(receipt_file)}

    if os.environ.get("YY_RENDER_GLYPH_CACHE", "1") != "0":
        for attempt in _cache_attempt_dirs(cache_dir):
            attempt_png = attempt / prior_path.name
            attempt_receipt = attempt_png.with_suffix(".receipt.json")
            cached = valid_receipt(attempt_png, attempt_receipt)
            if cached:
                return attempt_png, None, evidence(cached, attempt_receipt)
    ok, lock_reason = gpu_lock_acquire(f"render_glyph/{artifact_id}", _gpu_lock_wait_s())
    if not ok:
        return None, "gpu_busy", extra
    try:
        if native_path(cache_dir).exists():
            import uuid
            cache_dir = cache_dir.parent / (cache_dir.name + "-" + uuid.uuid4().hex)
            cache_png = cache_dir / prior_path.name
            receipt_path = cache_png.with_suffix(".receipt.json")
        native_path(cache_dir).mkdir(parents=True, exist_ok=True)
        req_path = cache_png.with_suffix(".req.json")
        runstore.write_json_atomic(req_path, {
            "style_ref": str(style_ref), "target_char": char, "seed": seed,
            "out_png": str(cache_png), "out_receipt": str(receipt_path),
            "ckpt_dir": str(ckpt_dir), "content_ttf": str(content_ttf), "device": identity["device"],
        })
        argv = [_m1_python(), "-X", "utf8", "-m", "engine.style.m1_adapter.runner", str(req_path)]
        try:
            proc = subprocess.Popen(
                [str(a) for a in argv], cwd=str(PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as exc:
            return None, "m1_spawn_failed", {**extra, "detail": repr(exc)[:160]}
        deadline = time.monotonic() + _m1_timeout_s()
        code, tail = _drain_bounded(proc, deadline)
        if code == 0 and time.monotonic() > deadline:
            code = -9
        if code != 0:
            receipt = runstore.read_json(receipt_path) if native_path(receipt_path).is_file() else None
            runner_code = (receipt or {}).get("failure", {}).get("code") if isinstance(receipt, dict) else None
            return None, ("m1_timeout" if code == -9 else "m1_failed"), {
                **extra, "runner_exit": code, "runner_code": runner_code, "log_tail": tail[-200:]}
        receipt = valid_receipt(cache_png, receipt_path)
        if receipt is None:
            return None, "m1_receipt_invalid", extra
        return cache_png, None, evidence(receipt, receipt_path)
    finally:
        gpu_lock_release()


from engine.style.m2_adapter.personal_runtime_policy import operation_entry


@operation_entry
def _m2_generate(user_id: int, binding: dict, char: str, *, seed_override=None):
    diagnostic_started = time.monotonic()
    from engine.style.m2_adapter.windows_scr_execution import OLD0178
    runtime = binding.get('runtime_binding')
    if isinstance(runtime,dict) and runtime.get('variant_id') == OLD0178:
        from engine.style.m2_adapter.personal_runtime_policy import execution_scope
        with execution_scope(PROJ,runtime,'inference'):
            return _verified_runtime_generate(user_id,binding,char,use_adapter=True,seed_override=seed_override,
                                              diagnostic_started=diagnostic_started)
    return _verified_runtime_generate(user_id,binding,char,use_adapter=True,seed_override=seed_override,
                                      diagnostic_started=diagnostic_started)


def _m1_verified_generate(user_id: int, binding: dict, char: str):
    return _verified_runtime_generate(user_id,binding,char,use_adapter=False)


def _verified_runtime_generate(user_id: int, binding: dict, char: str, *, use_adapter: bool, seed_override=None, diagnostic_started=None):
    """Subprocess boundary; cache acceptance requires a matching real receipt."""
    if diagnostic_started is None:diagnostic_started=time.monotonic()
    from engine.style.m2_adapter.release_runtime_policy import assert_m2_execution_allowed
    from engine.style.m2_adapter.runtime_variants import RuntimeUnavailable
    try:assert_m2_execution_allowed(Path(__file__).resolve().parents[3],binding=binding.get('runtime_binding'),purpose='inference')
    except RuntimeUnavailable as exc:return None,exc.code,{"detail":str(exc)}
    from app.domains.styles.worker import gpu_lock_acquire, gpu_lock_release
    runtime_binding=binding.get("runtime_binding")
    deployment = None
    if binding.get("deployment_binding") is not None:
        try:
            from engine.style.m2_adapter import preserved_training_deployment as preserved
            from engine.style.m2_adapter.deployment import resolve_deployment, verify_deployment_request
            is_preserved=binding['deployment_binding'].get('schema')==preserved.BINDING_SCHEMA
            if is_preserved:
                deployment=preserved.resolve(PROJ,binding['deployment_binding'],user_id=user_id)
                if Path(deployment['source']['run_dir']).resolve()!=Path(binding['base_dir']).resolve() or deployment['source']['manifest_sha256']!=binding['manifest_sha256']:
                    raise ValueError('Preserved deployment differs from owned artifact')
                verify_request=preserved.verify_request
            else:
                deployment = resolve_deployment(PROJ, binding["deployment_binding"],
                    expected_run_dir=binding.get("base_dir"), expected_manifest_sha256=binding.get("manifest_sha256"))
                verify_request=verify_deployment_request
            verify_request(deployment,
                adapter_path=(binding.get("m2_adapter") or {}).get("path"),
                adapter_sha256=(binding.get("m2_adapter") or {}).get("sha256"),
                snapshot_dir=(binding.get("snapshot_binding") or {}).get("snapshot_dir"),
                snapshot_sha256=(binding.get("snapshot_binding") or {}).get("snapshot_sha256"),
                runtime_binding=runtime_binding)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, "m2_deployment_invalid", {"detail": str(exc)}
    adapter = binding.get("m2_adapter") or {}
    snapshot = binding.get("snapshot_binding") or {}
    if not adapter or not snapshot.get("snapshot_sha256") or not snapshot.get("snapshot_dir"):
        return None, "m2_snapshot_binding_missing", {}
    try:
        from engine.style.dataset.binding import verify_snapshot_binding
        verify_snapshot_binding((os.environ.get("YY_RENDER_M2_SNAPSHOT_DIR") if runtime_binding is None else None) or snapshot["snapshot_dir"], snapshot["snapshot_sha256"])
        verified_refs=_snapshot_style_refs(snapshot,allow_env=runtime_binding is None)
        selected_ref=(binding.get("style_refs") or verified_refs)[0]
        if str(Path(selected_ref).resolve()) not in verified_refs:raise ValueError("reference is not canonical training-side glyph")
    except Exception as exc:
        return None, "m2_snapshot_invalid", {"detail": str(exc)}
    from engine.style.m2_adapter.runtime import runtime_python,manifest_location,verify_runtime
    selected=None
    runtime_path=manifest_location()
    if not native_path(runtime_path).is_file() and not os.environ.get("YY_M2_RUNTIME_MANIFEST"):
        runtime_path=Path(binding.get("runtime_manifest_path") or runtime_path)
    try:
        if runtime_binding is not None:
            from engine.style.m2_adapter.runtime_variants import resolve_binding, USER_SCR_CPU
            from engine.style.m2_adapter.windows_user_scr import VARIANTS as WINDOWS_VARIANTS
            from engine.style.m2_adapter.windows_scr_deployment import is_windows
            from engine.style.m2_adapter.preserved_training_deployment import BINDING_SCHEMA as PRESERVED_SCHEMA
            selected=(deployment['execution'] if deployment and (is_windows(deployment.get('deployment_binding')) or deployment.get('deployment_binding',{}).get('schema')==PRESERVED_SCHEMA) else
                      resolve_binding(PROJ,runtime_binding,user_scr_purpose='inference') if runtime_binding.get('variant_id') in (USER_SCR_CPU,*WINDOWS_VARIANTS) else resolve_binding(PROJ,runtime_binding))
            runtime_path=Path(selected['manifest_path'])
            verified={'identity_sha256':selected['asset_identity_sha256'],'fd_repo':selected['fd_repo'],'engine_id':selected['engine_id']}
        else:verified=verify_runtime(runtime_path,os.environ.get("YY_RENDER_M2_BASE_REPO"))
        if verified['identity_sha256']!=binding.get('runtime_identity_sha256'):
            return None,"m2_runtime_identity_mismatch",{}
    except (OSError,ValueError,KeyError) as exc:
        return None,"m2_runtime_invalid",{"detail":str(exc)}
    from engine.style.m2_adapter.preprocessing import verify_preprocessing_contract
    try:
        preprocessing = verify_preprocessing_contract(binding.get("preprocessing"))
    except ValueError as exc:
        return None, "m2_preprocessing_identity_mismatch", {"detail": str(exc)}
    interpreter=selected["python"] if selected else str(runtime_python(runtime_path))
    if not native_path(interpreter).is_file():return None,"m2_interpreter_missing",{}
    runtime_manifest=str(runtime_path)
    repo=Path(verified['fd_repo'])
    font = Path((os.environ.get("YY_RENDER_M2_CONTENT_TTF") if runtime_binding is None else None) or repo / "ttf/KaiXinSongA.ttf")
    seed = _stable_seed(binding["artifact_id"], binding["engine_version"], char)
    if seed_override is not None:
        from .v3_layout import ADAPTER_SHA, model_seed
        if adapter.get('sha256') != ADAPTER_SHA or seed_override != model_seed(char):
            return None, 'v3_inference_seed_identity_mismatch', {}
        seed = seed_override
    req = {"adapter_path": adapter["path"], "adapter_sha256": adapter["sha256"],
           "snapshot_dir": (os.environ.get("YY_RENDER_M2_SNAPSHOT_DIR") if runtime_binding is None else None) or snapshot["snapshot_dir"],
           "snapshot_sha256": snapshot["snapshot_sha256"], "fd_repo": str(repo),
           "base_npz_sha256": binding.get("base_ckpt_sha256"), "content_ttf": str(font),
           "device": selected["device"] if selected else "cpu", "cpu_threads": 2,
           "target_char": char, "seed": seed, "steps": 20, "use_adapter": use_adapter,
           "style_ref": selected_ref,
           "runtime_manifest_path": runtime_manifest, "sampler": "dpm", "guidance_scale": 7.5,
           "preprocessing_profile": preprocessing["profile"]}
    if runtime_binding is not None:req["runtime_binding"]=runtime_binding
    if deployment is not None:
        req["deployment_binding"]=deployment["deployment_binding"]
        req['deployment_user_id']=user_id
    from app.domains.styles.m2_topology import inference_identity,validate_topology_lineage
    try:req.update(inference_identity(binding))
    except (ValueError,KeyError,TypeError) as exc:
        return None,'m2_topology_identity_mismatch',{'detail':str(exc)}
    from engine.style.m2_adapter.inference import application_code_hashes
    code_hashes=application_code_hashes(user_scr=True) if binding.get('recipe_id') in ('m2-scr2n-user-snapshot-v1','m2-scr2n-user-windows-spatial-v1') else application_code_hashes()
    identity = {**req, "preprocessing": preprocessing, "application_code_sha256":code_hashes,"inference_code_sha256": runstore.sha256_file(PROJ / "engine/style/m2_adapter/inference.py"),
                "content_ttf_sha256": runstore.sha256_file(font) if native_path(font).is_file() else None,
                "style_ref_sha256":runstore.sha256_file(selected_ref)}
    key = runstore.canonical_sha256(identity)
    cache = runstore.storage_root() / "users" / str(int(user_id)) / "styles" / ("m2-glyph-cache" if use_adapter else "m1-verified-glyph-cache") / key
    png = cache / "glyph.png"; receipt_path = cache / "inference.json"
    def accepted(candidate_png, candidate_receipt):
        if native_path(candidate_receipt).is_symlink() or not native_path(candidate_receipt).is_file():return None
        receipt = runstore.read_json(candidate_receipt) or {}
        if not isinstance(receipt, dict): return None
        inp = receipt.get("inputs") or {}; output = receipt.get("output") or {}
        calls = receipt.get("adapter_hook_calls") or {}
        if not isinstance(inp, dict) or not isinstance(output, dict) or not isinstance(calls, dict): return None
        try:
            validate_topology_lineage(binding,receipt)
            from engine.style.m2_adapter.topology_dispatch import validate_inference_topology_execution
            validate_inference_topology_execution(receipt)
        except (ValueError,KeyError,TypeError):return None
        if use_adapter and any(count!=req['steps'] for count in [*calls.values(),*(receipt.get('offset_projection_hook_calls') or {}).values(),*(receipt.get('spatial_offset_hook_calls') or {}).values()]):return None
        if runtime_binding is not None:
            from engine.style.m2_adapter.windows_scr_deployment import is_windows,verify_execution_receipt as verify_windows_execution
            from engine.style.m2_adapter.runtime_variants import verify_execution_receipt
            try:
                if deployment and is_windows(deployment.get('deployment_binding')):verify_windows_execution(deployment,receipt)
                else:verify_execution_receipt(runtime_binding,receipt,'inference')
            except (ValueError,KeyError,TypeError):return None
        if deployment is not None:
            from engine.style.m2_adapter import preserved_training_deployment as preserved
            from engine.style.m2_adapter.deployment import verify_deployment_receipt
            try:
                if deployment.get('deployment_binding',{}).get('schema')==preserved.BINDING_SCHEMA:
                    if use_adapter:preserved.verify_inference_receipt(deployment,receipt)
                    elif any(receipt.get(k)!=v for k,v in preserved.inference_identity(deployment).items()):return None
                else:verify_deployment_receipt(deployment, receipt)
            except (ValueError, KeyError, TypeError):return None
        elif receipt.get("deployment_binding") is not None:
            return None
        good = (receipt.get("ok") is True and receipt.get("source") == ("M2" if use_adapter else "M1")
                and receipt.get("engine_mode") == ("M2_ADAPTER" if use_adapter else "M1_FROZEN")
                and receipt.get("model_version") == (binding["engine_version"] if use_adapter else "frozen-npz-base")
                and receipt.get("engine_id") == verified["engine_id"]
                and receipt.get("application_code_sha256")==code_hashes
                and receipt.get("preprocessing") == preprocessing
                and receipt.get("adapter_applied") is use_adapter and receipt.get("quality_eligible") is True and receipt.get("adapter_sha256") == (adapter["sha256"] if use_adapter else None)
                and receipt.get("dataset_snapshot_sha256") == snapshot["snapshot_sha256"]
                and receipt.get("base_ckpt_sha256") == binding.get("base_ckpt_sha256")
                and receipt.get("runtime_identity_sha256") == verified["identity_sha256"]
                and inp.get("target_char") == char and inp.get("seed") == seed
                and inp.get("steps")==req["steps"] and inp.get("sampler")==req["sampler"]
                and (runtime_binding is None or (inp.get("device")==req["device"] and inp.get("cpu_threads")==req["cpu_threads"]))
                and inp.get("guidance_scale")==req["guidance_scale"]
                and inp.get("style_ref_path")==str(Path(selected_ref).resolve())
                and inp.get("style_ref_sha256")==identity["style_ref_sha256"]
                and inp.get("content_ttf_sha256") == identity["content_ttf_sha256"]
                and native_path(candidate_png).is_file() and not native_path(candidate_png).is_symlink()
                and runstore.sha256_file(candidate_png) == output.get("png_sha256"))
        return receipt if good else None
    try:
        base_hashes = binding.get("base_ckpt_sha256")
        if not isinstance(base_hashes, dict) or set(base_hashes) != {"unet.npz", "style_encoder.npz", "content_encoder.npz"}:
            return None, "m2_base_hashes_missing", {}
        for name, expected in base_hashes.items():
            if runstore.sha256_file(repo / "ckpt" / name) != expected:
                return None, "m2_base_hash_mismatch", {}
    except OSError:
        return None, "m2_base_missing", {}
    for attempt in _cache_attempt_dirs(cache):
        attempt_png = attempt / "glyph.png"; attempt_receipt = attempt / "inference.json"
        hit = accepted(attempt_png, attempt_receipt)
        if hit:
            return attempt_png, None, {"seed": seed, "adapter_sha256": adapter["sha256"] if use_adapter else None,
                               "model_version":hit["model_version"],"engine_mode":hit["engine_mode"],"runtime_identity_sha256":hit["runtime_identity_sha256"],
                               "unseen_in_training": hit["inputs"].get("unseen_in_training"),
                               "preprocessing": hit["preprocessing"], "runtime_binding":runtime_binding,
                               "deployment_binding": binding.get("deployment_binding"),
                               "deployment_version": hit.get("deployment_version"),
                               "inference_receipt_sha256": runstore.sha256_file(attempt_receipt), **inference_identity(hit)}
    cpu_lease=None
    from engine.style.m2_adapter.windows_user_scr import VARIANTS as WINDOWS_VARIANTS
    from engine.style.m2_adapter.windows_scr_execution import OLD0178
    from engine.style.m2_adapter.windows_user_scr import CPU as WINDOWS_CPU
    old0178=isinstance(runtime_binding,dict) and runtime_binding.get('variant_id')==OLD0178
    windows_binding=isinstance(runtime_binding,dict) and runtime_binding.get('variant_id') in (*WINDOWS_VARIANTS,OLD0178)
    windows_cpu=isinstance(runtime_binding,dict) and runtime_binding.get('variant_id') in (WINDOWS_CPU,OLD0178)
    if windows_cpu:
        from engine.style.m2_adapter.windows_training_runtime import acquire_cpu_lease
        try:cpu_lease=acquire_cpu_lease(PROJ,f"m2_render/{binding['artifact_id']}")
        except (OSError,ValueError) as exc:return None,'cpu_busy',{'detail':str(exc)}
    else:
        ok, reason = gpu_lock_acquire(f"m2_render/{binding['artifact_id']}", _gpu_lock_wait_s())
        if not ok:return None, "gpu_busy", {"detail": reason}
    proc=None
    try:
        # Do not overwrite prior failed/tampered evidence. A fresh attempt has
        # its own directory; only verified output can be consumed.
        if native_path(cache).exists():
            import uuid
            cache = cache.parent / (key + "-" + uuid.uuid4().hex)
            png = cache / "glyph.png"; receipt_path = cache / "inference.json"
        native_path(cache).mkdir(parents=True,exist_ok=True)
        req.update({"out_png": str(png), "out_receipt": str(receipt_path)})
        request_path = cache / "request.json"
        runstore.write_json_atomic(request_path, req)
        try:
            proc = subprocess.Popen([interpreter, "-X", "utf8", "-m", "engine.style.m2_adapter.inference_runner", str(request_path)],
                                    cwd=str(PROJ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=__import__('engine.style.m2_adapter.windows_training_runtime',fromlist=['_env'])._env(cache/'private-env',WINDOWS_CPU if old0178 else runtime_binding['variant_id']) if windows_binding else
                                    __import__("engine.style.m2_adapter.runtime_variants",fromlist=["bound_subprocess_env"]).bound_subprocess_env(runtime_binding))
        except OSError as exc:return None, "m2_spawn_failed", {"detail":str(exc)}
        budget = _env_float("YY_RENDER_M2_TIMEOUT_S",240.0)
        deadline = time.monotonic()+budget
        from engine.style.m2_adapter.inference_diagnostics import capture_child
        with capture_child(cache, proc, budget_seconds=budget,
                           parent_prepare_seconds=time.monotonic()-diagnostic_started) as capture:
            code, tail = _drain_bounded(proc, deadline)
            capture.complete(code)
        receipt = accepted(png, receipt_path)
        if code != 0 or receipt is None:
            failed = runstore.read_json(receipt_path) or {}
            return None, "m2_timeout" if capture.facts['stop_reason'] == 'TIME_LIMIT' else "m2_inference_failed", {"runner_exit":code,
                    "runner_code":(failed.get("failure") or {}).get("code") or capture.facts.get('runner_failure_code'),"log_tail":tail[-200:],
                    **{k:capture.facts[k] for k in ('stop_reason','execution_end_confirmed','diagnostic_io_error_type') if k in capture.facts}}
        return png, None, {"seed":seed,"adapter_sha256":adapter["sha256"] if use_adapter else None,
                           "model_version":receipt["model_version"],"engine_mode":receipt["engine_mode"],"runtime_identity_sha256":receipt["runtime_identity_sha256"],
                           "unseen_in_training":receipt["inputs"].get("unseen_in_training"),
                           "preprocessing": receipt["preprocessing"], "runtime_binding":runtime_binding,
                           "deployment_binding": binding.get("deployment_binding"),
                           "deployment_version": receipt.get("deployment_version"),
                           "inference_receipt_sha256":runstore.sha256_file(receipt_path), **inference_identity(receipt)}
    finally:
        # A drain/termination exception does not prove that the owned child ended.
        # Keep the same lease until its process identity can be independently checked.
        execution_ended=proc is None or getattr(proc,'returncode',None) is not None
        if execution_ended:
            if cpu_lease is not None:
                from engine.style.m2_adapter.windows_training_runtime import release_cpu_lease
                release_cpu_lease(cpu_lease)
            else:gpu_lock_release()


# ---------------------------------------------------------------------------
# GlyphSession - one render job's personal-glyph context
# ---------------------------------------------------------------------------

class GlyphSession:
    """Per-job session: fresh resolution + hash re-verification per render
    (approved/enabled state is never cached), per-char fallback chain, and
    the provenance records that become the item's glyph metadata."""

    def __init__(self, user_id: int, job_id: str, requested: dict, resolved: dict):
        self.user_id = int(user_id)
        self.job_id = job_id
        self.requested = requested  # binding frozen into the style snapshot at create
        self.resolved = resolved    # FRESH resolution at render time
        self._m1_gate_reason = False  # False=not evaluated; None=gate ok; str=reason
        self.last_meta: dict | None = None
        self.source_policy = "fallback_allowed" if requested.get("glyph_source_policy") is None else requested["glyph_source_policy"]
        if self.source_policy not in SOURCE_POLICIES:
            raise ValueError("invalid frozen glyph source policy")
        self.mixed_fonts = None
        if 'non_cjk_fonts' in requested or 'non_cjk_font_binding' in requested:
            from .mixed_fonts import resolve_frozen
            self.mixed_fonts = resolve_frozen(requested)
        self.output_blocked = False
        self.blocked_reason = None
        self.render_font_bindings = {}
        self.layout_executions = []
        self.slot_executions = []

    def begin_item(self) -> None:
        """Reset item-local provenance once, never at each slot boundary."""
        self.output_blocked = False
        self.blocked_reason = None
        self.render_font_bindings = {}
        self.layout_executions = []
        self.slot_executions = []
        self.last_meta = None

    @property
    def bound(self) -> bool:
        return self.resolved.get("state") == "bound"

    # ---- metadata assembly -------------------------------------------------

    def meta_payload(self, item, records: list, warnings: list) -> dict:
        res = self.resolved
        payload = {
            "schema": "render-glyph-meta/1",
            "item_id": item.item_id,
            "job_id": self.job_id,
            "render_version": int(item.render_version),
            "mode": "personal" if self.bound else "personal_unavailable",
            "style_id": self.requested.get("style_id"),
            "requested_binding": {
                k: self.requested.get(k)
                for k in ("style_id", "artifact_id", "engine_version", "manifest_sha256")
                if k in self.requested
            },
            "resolved": (
                {
                    "artifact_id": res.get("artifact_id"),
                    "engine_mode": res.get("engine_mode"),
                    "engine_version": res.get("engine_version"),
                    "manifest_sha256": res.get("manifest_sha256"),
                    "runtime_binding": res.get("runtime_binding"),
                    "deployment_binding": res.get("deployment_binding"),
                    "deployment_version": res.get("deployment_version"),
                    "training_runtime_binding": res.get("training_runtime_binding"),
                    "problems": res.get("problems") or [],
                }
                if self.bound
                else {"reason": res.get("reason"), "detail": res.get("detail")}
            ),
            "chars": records,
            "glyph_source_policy": self.source_policy,
            "layout_profile": self.requested.get("layout_profile", "baseline-v1"),
            "layout_seed": self.requested.get("layout_seed"),
            "layout_binding": self.requested.get("layout_binding"),
            "layout_executions": self.layout_executions,
            "slot_executions": self.slot_executions,
            "outcome": "not_rendered" if self.output_blocked else "rendered",
            "blocked_reason": self.blocked_reason,
            "fallback_count": 0 if self.output_blocked else sum(1 for r in records if r.get("reason") and r.get("source") in (SOURCE_M1, SOURCE_M0)),
            "attempted_fallback_count": sum(1 for r in records if r.get("reason") and r.get("source") in (SOURCE_M1, SOURCE_M0)),
            "created_at": _now(),
        }
        return payload

    def warning_lines(self, records: list) -> list[str]:
        res = self.resolved
        lines: list[str] = []
        if not self.bound:
            lines.append(f"GLYPH_STYLE_UNAVAILABLE reason={res.get('reason')} "
                         f"detail={str(res.get('detail'))[:120]}")
            return lines
        shown = records[:32]
        summary = ",".join(f"{r['char']}:{r['source']}" for r in shown)
        more = "" if len(records) <= 32 else f",+{len(records) - 32} more"
        lines.append(
            f"GLYPH_STYLE artifact={res.get('artifact_id')} "
            f"version={res.get('engine_version')} chars={summary}{more}")
        for r in records:
            if r.get("reason"):
                lines.append(
                    f"GLYPH_STYLE_FALLBACK char={r['char']} reason={r['reason']} "
                    f"resolved={r['source']}:{r['version']}")
        return lines

    # ---- per-char chain ----------------------------------------------------

    def render_text(self, text: str, *, w: int, h: int, font_path: str, font_name: str,
                    fill, stroke_fill, size_hint: int, strict: bool, text_style=None, slot_key=None):
        """Render one slot text per character. Returns (strip|None, records,
        blocker_char|None). strip None -> caller uses the legacy draw.text
        path (records still describe what happened). blocker set -> strict
        mode refuses to pass a non-personal char off as styled output."""
        # Blank secondary slots are intentional no-ops, not failed glyphs.
        # In particular they must neither poison success nor clear a prior
        # nonblank failure from this item's other slots.
        try:
            self.render_font_bindings[str(slot_key or 'default')]={'id':font_name,'sha256':runstore.sha256_file(Path(font_path))}
            if self.mixed_fonts is not None:
                for role,font in self.mixed_fonts[0]['fonts'].items():
                    self.render_font_bindings['mixed:'+role]={'id':font['id'],'sha256':font['sha256']}
        except (OSError,ValueError):pass  # rendering remains unchanged; incomplete evidence cannot be accepted
        if not text.strip():
            return None, [], None
        records: list[dict] = []
        cells: list = []
        blocker = None
        if not self.bound:
            reason = self.resolved.get("reason")
            for ch in text:
                if not ch.strip():
                    continue
                records.append({"char": ch, "source": SOURCE_M0, "version": font_name,
                                "reason": reason})
            if self.source_policy in REQUIRED_M2_POLICIES:
                for record in records:
                    record.update(source=None, generation_kind="not_rendered",
                                  required_source="M2" if character_requires_m2(self.source_policy, record["char"]) else "M0")
                    if self.source_policy == "m2_cjk_required":record["version"] = None
            slot_blocked = bool(strict or self.source_policy in REQUIRED_M2_POLICIES)
            self.output_blocked = self.output_blocked or slot_blocked
            return None, records, (next((ch for ch in text if ch.strip()), None) if slot_blocked else None)

        from .selection_contract import is_personal_punctuation_unsupported
        unsupported = [ch for ch in text if is_personal_punctuation_unsupported(ch)]
        if self.source_policy == "m2_required" and unsupported:
            self.output_blocked = True
            self.blocked_reason = "PERSONAL_PUNCTUATION_UNSUPPORTED"
            return None, [{"char": ch, "source": None, "version": None, "generation_kind": "not_rendered", "required_source": "M2", "reason": "personal_punctuation_unsupported"} for ch in unsupported], unsupported[0]
        if self.requested.get("layout_profile") == "natural-layout-v1":
            try:
                from .natural_authorization import resolve_author_profile
                if not isinstance(self.requested.get("layout_binding"),dict):raise ValueError("missing frozen layout binding")
                resolve_author_profile(self.user_id,self.resolved,frozen=self.requested['layout_binding'])
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.output_blocked=True;self.blocked_reason="NATURAL_LAYOUT_UNAVAILABLE"
                self.layout_executions.append({'schema':'natural-layout-failure/1','reason':str(exc)})
                return None,[],next((ch for ch in text if not ch.isspace()),None)
        try:
            from .selection_contract import frozen_ink_mode
            ink_mode=frozen_ink_mode(self.requested)
            if ink_mode == "source-solid-srgb-v1":
                from .ink_authorization import resolve_author_palette
                resolve_author_palette(self.user_id,self.resolved,self.requested['ink_palette_id'],
                    layout_profile=self.requested.get('layout_profile'),render_source_policy=self.source_policy,
                    frozen=self.requested['ink_color_binding'])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self.output_blocked=True;self.blocked_reason="INK_COLOR_UNAVAILABLE"
            return None,[],next((ch for ch in text if not ch.isspace()),None)
        from .v3_layout import selected as is_v3
        if is_v3(self.requested):
            observed_cells=[]
            try:
                from .v3_layout import render_line, binding as v3_binding, ADAPTER_SHA
                if (self.requested.get('v3_layout_binding') != v3_binding()
                        or (self.resolved.get('m2_adapter') or {}).get('sha256') != ADAPTER_SHA
                        or self.mixed_fonts is None):
                    raise ValueError('V3 model/font/layout authority unavailable')
                frozen_fonts, paths = self.mixed_fonts
                def chinese_cell(char):
                    cell, record = self._char_cell(char, self.resolved.get('chars') or {}, 96, font_path, font_name, fill, stroke_fill)
                    observed_cells.append(record)
                    return cell, record
                strip, records, execution = render_line(text, slot_key, binding=frozen_fonts, paths=paths,
                    chinese_cell=chinese_cell, width=w, height=h, text_style=text_style or {},
                    handwriting=self.requested.get('handwriting') or {}, fill=fill)
                self.layout_executions.append(execution)
                return strip, records, None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.output_blocked=True;self.blocked_reason='V3_RENDER_UNAVAILABLE'
                self.layout_executions.append({'schema':'accepted-v3-layout-failure/1','reason':str(exc),
                    'm2_failures':[row for row in observed_cells if row.get('m2_failure')]})
                return None, observed_cells, next((ch for ch in text if not ch.isspace()),None)
        chars_index = self.resolved.get("chars") or {}
        size = max(12, int(h * 0.6))
        styled_prefix = 0
        prefix_flags = []
        if text_style is not None:
            from app.core.text_style import validate_text_style,text_segments
            text_style = validate_text_style(text_style)
            size = max(6,round(h * 0.6 * text_style['ordinary_scale']))
            segments = text_segments(text,slot_key)
            styled_prefix = len(segments[0][0]) if segments[0][1] else 0
        for char_index,ch in enumerate(text):
            if not ch.strip():
                continue
            cell, record = self._char_cell(
                ch, chars_index, max(6,round(size * text_style['to_ratio'])) if text_style is not None and char_index < styled_prefix else size,
                font_path, font_name, fill, stroke_fill)
            if text_style is not None:
                record = dict(record,size_px=max(6,round(size * text_style['to_ratio'])) if char_index < styled_prefix else size)
            records.append(record)
            if cell is not None:
                cells.append(cell)
                prefix_flags.append(char_index < styled_prefix)
            if character_requires_m2(self.source_policy, ch) and (record["source"] != SOURCE_M2 or record.get("generation_kind") != "adapter_inference"):
                blocker = ch
            if self.source_policy == "m2_cjk_required" and cell is None:
                blocker = ch
            if strict and record["source"] == SOURCE_M0 and self.source_policy != "m2_cjk_required":
                # strict: no personal glyph (M2 missing/degenerate, M1
                # unavailable/failed) -> the char blocks the item for a human
                # decision; never silently stamp M0 as if it were styled.
                blocker = ch
        if blocker is not None or not cells:
            self.output_blocked = True
            return None, records, blocker
        handwriting = self.requested.get('handwriting')
        if handwriting is not None:
            from .text_layout import adjust_handwriting
            cells = adjust_handwriting(cells, records, handwriting, text=text, slot_key=slot_key)
        if self.requested.get("layout_profile") == "natural-layout-v1":
            try:
                from .natural_layout_v1 import compose_wrapped
                from .natural_authorization import resolve_author_profile
                qualified=resolve_author_profile(self.user_id,self.resolved,frozen=self.requested.get("layout_binding"))
                if not isinstance(self.requested.get("layout_binding"),dict):raise ValueError("missing frozen layout binding")
                strip,layout=compose_wrapped(cells,records,text=text,width=w,height=h,seed=self.requested.get("layout_seed"),profile=qualified['profile'],style_id=self.requested.get("style_id"),**({'text_style':text_style,'prefix_flags':prefix_flags} if text_style is not None else {}))
                if text_style is not None:layout['frozen_text_style']=text_style
                if handwriting is not None:layout['user_handwriting_adjustment']=dict(handwriting)
                self.layout_executions.append(layout)
                return strip,records,None
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.output_blocked=True;self.blocked_reason="NATURAL_LAYOUT_UNAVAILABLE"
                self.layout_executions.append({'schema':'natural-layout-failure/1','reason':str(exc)})
                return None,records,next((ch for ch in text if not ch.isspace()),None)
        if text_style is not None:
            from .text_layout import compose_scaled_cells
            return compose_scaled_cells(cells,prefix_flags,w=w,h=h,size=size,gap=text_style['header_gap'],records=records),records,None
        # deterministic fit: shrink while the strip overflows the region width
        strip = _compose_strip(cells, size)
        while strip is not None and strip.width > w and size > 12:
            size = max(12, int(size * w / strip.width * 0.95))
            cells = [self._rescale(c, size) for c in cells]
            strip = _compose_strip(cells, size)
        if strip is not None and strip.width > w:
            # one final squeeze below 12, mirroring the legacy fitting tail
            size = max(6, int(size * w / strip.width * 0.95))
            cells = [self._rescale(c, size) for c in cells]
            strip = _compose_strip(cells, size)
        if strip is None:
            self.output_blocked = True
            self.blocked_reason = "GLYPH_COMPOSITION_FAILED"
            return None, records, next(ch for ch in text if ch.strip())
        return strip, records, blocker

    def _rescale(self, cell, size: int):
        from PIL import Image

        scale = size / cell.height
        return cell.resize((max(1, round(cell.width * scale)), max(1, size)), Image.LANCZOS)

    def _char_cell(self, ch, chars_index: dict, size: int, font_path: str, font_name: str,
                   fill, stroke_fill):
        """M2 -> M1 -> M0 for one char. Returns (RGBA cell|None, record)."""
        record = {"char": ch, "source": None, "version": None}
        attempt_note: list[str] = []
        if self.source_policy == "m2_cjk_required" and not character_requires_m2(self.source_policy, ch):
            # Intentional source selection, never an M2/M1 attempt or fallback.
            if self.mixed_fonts is not None:
                from .mixed_fonts import font_role, char_missing
                binding, paths = self.mixed_fonts
                role = font_role(ch, binding)
                selected_font = binding['fonts'][role]
                font_path, font_name = paths[role], selected_font['id']
                record.update(font_role=role, font_id=selected_font['id'], font_sha256=selected_font['sha256'])
                cell = None if char_missing(font_path,ch,selected_font) else _m0_cell(ch,font_path,size,fill,stroke_fill, identity=selected_font)
            else:
                cell = _m0_cell(ch, font_path, size, fill, stroke_fill)
            record.update(source=SOURCE_M0 if cell is not None else None,
                          version=font_name if cell is not None else None,
                          generation_kind="system_font" if cell is not None else "not_rendered",
                          required_source="M0", selection_reason="non_cjk_system_font", attempted=[])
            if cell is None:record["reason"] = "system_font_glyph_unavailable"
            return cell, record
        from .selection_contract import is_personal_punctuation_unsupported
        if is_personal_punctuation_unsupported(ch):
            if self.source_policy == "m2_required":
                record.update(generation_kind="not_rendered", required_source="M2", reason="personal_punctuation_unsupported")
                return None, record
            # Explicit, honest fallback. Failed punctuation cannot be replayed
            # from the genuine-but-visually-incorrect M2 cache.
            record.update(source=SOURCE_M0, version=font_name, generation_kind="system_font", reason="personal_punctuation_unsupported", attempted=[])
            return _m0_cell(ch, font_path, size, fill, stroke_fill), record

        # ---- M2: genuinely load the adapter for this character first ----
        if self.resolved.get("m2_adapter"):
            from .v3_layout import selected as is_v3, model_seed
            override = model_seed(ch) if is_v3(self.requested) else None
            png, reason, extra = _m2_generate(self.user_id, self.resolved, ch, **({'seed_override':override} if override is not None else {}))
            if png is not None:
                actual_sha = runstore.sha256_file(png)
                img, problem = _load_glyph_image(png, ("m2-inference", actual_sha), expected_sha=actual_sha)
                if img is not None:
                    natural = self.requested.get("layout_profile") == "natural-layout-v1" or is_v3(self.requested)
                    cell = _colorized_cell(img, size, fill, stroke_fill, add_outline=not natural, preserve_canvas=natural)
                    if cell is not None:
                        record.update({"source": SOURCE_M2, "version": self.resolved["engine_version"],
                                       "artifact_id": self.resolved["artifact_id"],
                                       "engine_mode": "M2_ADAPTER", "png_sha256": actual_sha,
                                       "generation_kind": "adapter_inference", **extra})
                        return cell, record
                reason = problem or "glyph_empty"
            attempt_note.append(f"M2:{reason}")
            record['m2_failure']={'reason':reason, **{k:extra[k] for k in
                ('runner_exit','runner_code','stop_reason','execution_end_confirmed','diagnostic_io_error_type') if k in extra}}

        if character_requires_m2(self.source_policy, ch):
            record.update(source=None, generation_kind="not_rendered", required_source="M2",
                          reason="trained_adapter_required", attempted=attempt_note or ["M2:adapter_missing"])
            return None, record

        # ---- Historical bound-glyph path; does not prove unseen inference ----
        entry = None if self.resolved.get("runtime_binding") is not None else chars_index.get(ch)
        if entry is not None and entry.get("source")==SOURCE_M2:
            from engine.style.m2_adapter.release_runtime_policy import assert_m2_execution_allowed
            from engine.style.m2_adapter.runtime_variants import RuntimeUnavailable
            try:assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
            except RuntimeUnavailable as exc:
                attempt_note.append('replay:'+exc.code);entry=None
        if entry is not None and entry.get("source") not in (SOURCE_M1, SOURCE_M2):
            attempt_note.append("replay:missing_generation_provenance")
            entry = None
        if entry is not None:
            path = Path(self.resolved["base_dir"]) / entry["relative_path"]
            img, problem = _load_glyph_image(
                path, (self.resolved["artifact_id"], entry["sha256"]), expected_sha=entry["sha256"])
            if img is not None:
                cell = _colorized_cell(img, size, fill, stroke_fill)
                if cell is not None:
                    record.update({
                        "source": entry["source"],
                        "version": entry.get("engine_version") or "unknown",
                        "artifact_id": self.resolved["artifact_id"],
                        "engine_mode": entry.get("engine_mode"),
                        "file": entry["relative_path"],
                        "generation_kind": "bound_glyph_replay",
                        "png_sha256": entry["sha256"],
                    })
                    if attempt_note:
                        record["reason"] = ";".join(attempt_note)
                    return cell, record
                problem = "glyph_empty"
            attempt_note.append(f"M2:{problem or 'glyph_unreadable'}")

        # ---- Same verified NPZ base without trained adapter, honest M1 ----
        if self.resolved.get('m2_adapter') and self.resolved.get('runtime_identity_sha256'):
            png,reason,extra=_m1_verified_generate(self.user_id,self.resolved,ch)
            if png is not None:
                digest=runstore.sha256_file(png)
                img,problem=_load_glyph_image(png,('m1-verified',digest),expected_sha=digest)
                cell=_colorized_cell(img,size,fill,stroke_fill) if img is not None else None
                if cell is not None:
                    record.update({'source':SOURCE_M1,'version':extra.get('model_version'),'engine_mode':'M1_FROZEN',
                                   'artifact_id':self.resolved['artifact_id'],'png_sha256':digest,
                                   'generation_kind':'verified_base_inference','reason':';'.join(attempt_note),**extra})
                    return cell,record
                reason=problem or 'glyph_empty'
            attempt_note.append(f'M1_VERIFIED:{reason}')

        # ---- M1: frozen-basis subprocess generation (GPU-locked) ----
        gate = "bound_runtime_forbids_legacy_environment_fallback" if self.resolved.get("runtime_binding") is not None else self._m1_gate_reason
        if gate is False:
            gate = self._m1_gate_reason = _m1_gate()
        if gate is not None:
            attempt_note.append(f"M1:{gate}")
        else:
            style_ref = self._pick_style_ref()
            if style_ref is None:
                attempt_note.append("M1:m1_style_ref_unavailable")
            else:
                png, reason, extra = _m1_generate(
                    self.user_id, self.resolved["artifact_id"],
                    self.resolved["engine_version"], ch, style_ref)
                if png is not None:
                    img, problem = _load_glyph_image(
                        png, ("m1-inference", runstore.sha256_file(png)), expected_sha=runstore.sha256_file(png))
                    if img is None:
                        attempt_note.append(f"M1:{problem}")
                    else:
                        cell = _colorized_cell(img, size, fill, stroke_fill)
                        if cell is not None:
                            record.update({
                                "source": SOURCE_M1,
                                "version": extra.get("model_version") or "unknown",
                                "artifact_id": self.resolved["artifact_id"],
                                "engine_mode": extra.get("engine_mode") or "M1_FROZEN",
                                "seed": extra.get("seed"),
                                "png_sha256": runstore.sha256_file(png),
                                "cached_png": str(png.name),
                                "generation_kind": "legacy_base_inference",
                                "style_preprocessing_profile": extra.get("style_preprocessing_profile"),
                                "inference_receipt_sha256": extra.get("inference_receipt_sha256"),
                            })
                            if attempt_note:
                                record["reason"] = ";".join(attempt_note)
                            return cell, record
                        attempt_note.append("M1:glyph_empty")
                else:
                    attempt_note.append(f"M1:{reason}")

        # ---- M0: system font (existing render path semantics) ----
        cell = _m0_cell(ch, font_path, size, fill, stroke_fill)
        if attempt_note:
            reason = ";".join(attempt_note)
        elif entry is None:
            reason = "not_covered_by_artifact"
        else:
            reason = "glyph_unusable"
        record.update({
            "source": SOURCE_M0,
            "version": font_name,
            "reason": reason,
            "attempted": attempt_note,
        })
        return cell, record

    def _pick_style_ref(self) -> Path | None:
        for ref in self.resolved.get("style_refs") or []:
            p = Path(ref)
            if native_path(p).is_file():
                return p
        return None


def open_job_session(session, job) -> GlyphSession | None:
    """Wiring point: build the job's glyph session from the frozen style
    snapshot. Returns None for system-font jobs (zero behavior change); a
    personal snapshot always yields a session whose render-time resolution is
    FRESH (disable/unapprove/tamper after create is honoured at render)."""
    snap = {}
    def blocked(reason,detail):
        # Retain an explicit new policy even when its frozen binding fails.
        policy = "m2_cjk_required" if isinstance(snap,dict) and snap.get("glyph_source_policy") == "m2_cjk_required" else "m2_required"
        result = GlyphSession(job.user_id, job.job_id, {"glyph_source_policy": policy},
                              {"state": "unavailable", "reason": reason, "detail": detail})
        if reason == "INK_COLOR_UNAVAILABLE":result.blocked_reason = reason
        return result
    try:
        snap = json.loads(job.style_snapshot or "{}")
    except (TypeError, ValueError):
        return blocked("frozen_selection_invalid", "Malformed frozen selection")
    if not isinstance(snap, dict):
        return blocked("frozen_selection_invalid", "Frozen selection must be an object")
    from .selection_contract import frozen_ink_mode
    try: ink_mode=frozen_ink_mode(snap)
    except (ValueError,TypeError) as exc:return blocked("INK_COLOR_UNAVAILABLE",str(exc))
    if 'non_cjk_fonts' in snap or 'non_cjk_font_binding' in snap:
        from app.core.errors import ApiError
        from .mixed_fonts import resolve_frozen
        try:resolve_frozen(snap)
        except (OSError,ValueError,KeyError,TypeError,ApiError) as exc:
            return blocked('non_cjk_font_unavailable',str(exc))
    from .v3_layout import selected as is_v3, verify as verify_v3
    if is_v3(snap) or 'v3_layout_binding' in snap:
        try:verify_v3(snap)
        except (OSError,ValueError,KeyError,TypeError) as exc:return blocked('v3_frozen_identity_invalid',str(exc))
    mode=snap.get("style_mode")
    if mode not in (None,"legacy_inherited","profile","system"):
        return blocked("frozen_selection_invalid", "Unknown frozen source mode")
    if "glyph_source_policy" in snap and snap["glyph_source_policy"] not in SOURCE_POLICIES:
        return blocked("frozen_selection_invalid", "Unknown frozen source policy")
    if mode=="profile" and (type(snap.get("style_id")) is not int or snap['style_id']<1):
        return blocked("frozen_selection_invalid", "Profile mode requires a positive integer identity")
    layout=snap.get("layout_profile","baseline-v1");seed=snap.get("layout_seed")
    if layout not in ("baseline-v1","natural-layout-v1") or (layout=="baseline-v1" and seed is not None):
        return blocked("frozen_selection_invalid", "Unknown or inconsistent frozen layout")
    if layout=="natural-layout-v1" and (snap.get("glyph_source_policy")!="m2_required" or type(seed) is not int or not 0<=seed<=0xffffffff or not isinstance(snap.get("layout_binding"),dict)):
        return blocked("frozen_selection_invalid", "Natural layout needs its explicit frozen policy, seed and authority binding")
    if mode=="system" and (snap.get("style_id") is not None or snap.get("source") not in (None,"M0_SYSTEM","SYSTEM_FONT")):
        return blocked("frozen_selection_inconsistent", "System selection contains a personal source")
    pb = snap.get("personal_binding")
    if not isinstance(pb, dict) or not pb.get("style_id"):
        personal=(snap.get("source")=="M2_ADAPTER" or snap.get("glyph_source_policy") in REQUIRED_M2_POLICIES
                  or (mode=="profile" and snap.get("source") not in ("M0_SYSTEM","SYSTEM_FONT"))
                  or "personal_binding" in snap)
        if personal:
            return blocked("required_frozen_binding_missing", "Personal job lacks a valid frozen binding")
        return None
    if type(pb["style_id"]) is not int or pb["style_id"]<1:
        return blocked("required_frozen_binding_invalid", "Invalid frozen profile identity")
    sid=pb["style_id"]
    # Existing well-formed legacy snapshots omit mode/source; when present,
    # all outer selection fields must agree with the nested personal binding.
    mismatched = (("style_id" in snap and (type(snap["style_id"]) is not int or snap["style_id"] != sid))
                  or snap.get("style_mode") not in (None, "legacy_inherited", "profile")
                  or ("source" in snap and snap["source"] != "M2_ADAPTER"))
    if mismatched:
        if snap.get("glyph_source_policy") == "m2_cjk_required":
            return blocked("frozen_selection_inconsistent", "Outer selection and nested personal binding disagree")
        return GlyphSession(job.user_id, job.job_id, {"glyph_source_policy": "m2_required", "style_id": snap.get("style_id")},
                            {"state": "unavailable", "reason": "frozen_selection_inconsistent", "detail": "Outer selection and nested personal binding disagree"})
    resolved = resolve_personal_binding(session, job.user_id, sid)
    if resolved.get("state") == "bound":
        keys = ("artifact_id", "engine_version", "manifest_sha256", "runtime_binding", "deployment_binding", "deployment_version")
        if snap.get("glyph_source_policy") == "m2_cjk_required":
            keys += ("engine_mode", "m2_adapter", "runtime_identity_sha256", "snapshot_binding", "base_ckpt_sha256")
            if resolved.get("engine_mode") != "M2_ADAPTER" or not resolved.get("m2_adapter"):
                return blocked("required_trained_adapter_missing", "CJK personal rendering needs a bound trained adapter")
        if pb.get("state") != "bound" or any(pb.get(k) != resolved.get(k) for k in keys):
            resolved = _unavailable(sid, "frozen_artifact_binding_changed", "fresh artifact/version/runtime differs from the immutable job request")
    if layout=="natural-layout-v1":
        from .natural_authorization import resolve_author_profile
        try:
            qualified=resolve_author_profile(job.user_id,resolved,frozen=snap['layout_binding'])
            if snap.get("layout_profile_sha256")!=qualified['binding']['profile_sha256']:raise ValueError("frozen layout profile digest differs")
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return blocked("natural_layout_unavailable",str(exc))
    if ink_mode == "source-solid-srgb-v1":
        try:
            from .ink_authorization import resolve_author_palette
            resolve_author_palette(job.user_id,resolved,snap['ink_palette_id'],layout_profile=layout,
                render_source_policy=snap.get('glyph_source_policy'),frozen=snap['ink_color_binding'],session=session)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return blocked("INK_COLOR_UNAVAILABLE",str(exc))
    requested = {
        "ink_color_mode": ink_mode,
        **({"ink_color_rgb":list(snap['ink_color_rgb'])} if ink_mode=='custom-solid-srgb-v1' else {}),
        "ink_palette_id": snap.get("ink_palette_id"),
        "ink_color_binding": snap.get("ink_color_binding"),
        "ink_color_binding_sha256": snap.get("ink_color_binding_sha256"),
        "style_id": sid,
        "artifact_id": pb.get("artifact_id"),
        "engine_version": pb.get("engine_version"),
        "manifest_sha256": pb.get("manifest_sha256"),
        "glyph_source_policy": snap.get("glyph_source_policy"),
        "layout_profile": snap.get("layout_profile") or "baseline-v1",
        "layout_seed": snap.get("layout_seed"),
        "layout_binding": snap.get("layout_binding"),
        "layout_profile_sha256": snap.get("layout_profile_sha256"),
    }
    if 'handwriting' in snap:
        from app.core.text_style import validate_handwriting
        from app.core.errors import ApiError
        try:requested['handwriting'] = validate_handwriting(snap['handwriting'])
        except ApiError as exc:return blocked('frozen_selection_invalid',str(exc))
    if 'non_cjk_fonts' in snap:
        requested.update(non_cjk_fonts=snap['non_cjk_fonts'],non_cjk_font_binding=snap['non_cjk_font_binding'])
    if 'v3_layout_binding' in snap:
        requested['v3_layout_binding']=snap['v3_layout_binding']
    return GlyphSession(job.user_id, job.job_id, requested, resolved)


# ---------------------------------------------------------------------------
# sidecar metadata (render item metadata on disk, next to the output PNG)
# ---------------------------------------------------------------------------

def glyph_meta_relative_path(job, item) -> Path:
    rel_dir = Path(str(job.user_id)) / "outputs" / job.job_id
    return rel_dir / f"{item.item_id}_v{item.render_version}.glyphs.json"


def write_glyph_meta(job, item, session_obj: GlyphSession, records: list, warnings: list) -> str | None:
    """Atomic sidecar write; returns the storage-relative path (or None if the
    write failed - metadata loss must never fail a finished render)."""
    try:
        payload = session_obj.meta_payload(item, records, warnings)
        try:
            from engine.style.visual_review.output_evidence import produce
            payload['owner_output_producer']=produce(job,item,payload,session_obj.render_font_bindings,(session_obj.resolved.get('m2_adapter') or {}).get('sha256'))
        except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
            payload['owner_output_evidence_blocker']=str(exc)
        rel = glyph_meta_relative_path(job, item)
        abs_path = runstore.storage_root() / rel
        native_path(abs_path.parent).mkdir(parents=True, exist_ok=True)
        runstore.write_json_atomic(abs_path, payload)
        session_obj.last_meta = payload
        return rel.as_posix()
    except Exception:  # noqa: BLE001 - containment: never fail the render here
        return None
