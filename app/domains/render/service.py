"""Render domain service (contract v0.2 section 1.6/4/5/6/7/9, patch-C CC-12/13).

Execution model (T75, D-T10R-1/D-T10R-5): create/retry enqueue the job and a
single resident worker thread (started by create_app) runs it against the PRD
section 5.7 two-lane queue - user jobs first, draft jobs yielded one slot
every DRAFT_FAIRNESS user dispatches so drafts stay bounded-starvation. The
endpoints quiesce briefly (RENDER_WAIT_SECONDS) so small jobs answer with
their terminal state exactly like the old synchronous path, while large jobs
return in under 500ms with the queued/running state. run_job stays a
synchronous service function (probes drive it directly). All file writes are
temp-file + os.replace (section 9); output is lossless PNG.
"""

import hashlib
import copy
import json
import os
import secrets
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.audit import AuditLog, audit
from app.core.db import get_engine
from app.core.errors import ApiError
from app.domains.render.models import ExportArtifact, RenderItem, RenderJob
from app.domains.templates.models import LayoutTemplate
from .selection_contract import REQUIRED_M2_POLICIES
from engine.style.m2_adapter.personal_runtime_policy import operation_entry

DRAFT_TTL_SECONDS = 86400  # section 1.6 draft constraint 4 (default 24h)
EXPORT_SINGLE_CAP = 56_000_000  # patch-C section 11.6 (EXPORT_TOO_LARGE)
_RETRY_EXECUTION_FIELDS = ('runtime_binding','deployment_binding','deployment_version','deployment_user_id')
SLOT_TEXT_VARS = ("{cn}", "{blessing}", "{blogger}")
FALLBACK_FONT = "simkai"

# T75 queue machinery (D-T10R-1/D-T10R-5). sqlite is a single writer, so one
# worker thread with its own short-lived sessions; the busy timeout in
# get_engine covers the cross-writer contention.
RENDER_WAIT_SECONDS = 0.40  # response quiescence window; POST latency when
# timing out lands ~0.42-0.47s incl. HTTP overhead, inside the 500ms G75-03
# bound while small renders still answer terminal (t10render compat)
DRAFT_FAIRNESS = 5  # one draft slot per 4 user dispatches (PRD section 5.7)

_queue_cv = threading.Condition()
_user_queue: deque = deque()
_draft_queue: deque = deque()
_dispatch_count = 0
_done_events: dict = {}
_done_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_last_heartbeat: str | None = None  # B1-06: refreshed each worker-loop pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


def storage_root() -> Path:
    return Path(os.environ["YY_STORAGE_ROOT"])


def _load_weak_fonts() -> dict:
    """Use the same optional, validated catalog as the listing API."""
    from app.domains.font_presets.service import available_font_paths
    return available_font_paths()


def _resolve_font(style_snapshot: dict, *, verified_registered_content_font: dict | None = None) -> tuple[str, str]:
    """(font_path, font_name) from the style snapshot font_url (CC-11) or the
    M0 system stack (simkai on Windows; D-T10R-4)."""
    # Layout/coverage must use the explicitly selected runtime's verified font.
    # This authorizes no glyph or profile state: the fresh glyph session still
    # resolves the active artifact and records M2/M1/M0 provenance independently.
    personal = style_snapshot.get("personal_binding")
    if verified_registered_content_font is not None:
        # Capability reads pass this only from this request's fresh owned
        # binding. Persisted/HTTP snapshots never activate this explicit path.
        try:
            font = verified_registered_content_font
            if (not isinstance(personal, dict) or personal.get('state') != 'bound'
                or personal.get('artifact_registry_kind') != 'imported_inference'
                or font is not personal.get('registered_content_font')
                or font['asset_identity_sha256'] != personal.get('runtime_identity_sha256')
                or font['runtime_binding_sha256'] != personal['runtime_binding']['binding_sha256']
                or font['deployment_sha256'] != personal['deployment_binding']['deployment_sha256']):
                raise ValueError('request-local registered font identity differs')
            from installer.unpublished_artifacts.unpublished_artifact_import import regular, sha_file
            content = regular(font['path'], e_only=True)
            if sha_file(content) != font['sha256']:
                raise ValueError('request-local registered font bytes changed')
            return str(content), 'registered-runtime-content-font'
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ApiError(409, "M2_RUNTIME_UNAVAILABLE", "Selected runtime font unavailable", str(exc), False) from exc
    if isinstance(personal, dict) and personal.get("runtime_binding") is not None:
        try:
            from engine.style.m2_adapter.runtime_variants import resolve_binding
            from engine.style.m2_adapter.windows_scr_deployment import is_windows,resolve_deployment as resolve_windows
            selected = (resolve_windows(Path(__file__).resolve().parents[3],personal['deployment_binding'])['execution']
                        if is_windows(personal.get('deployment_binding')) else
                        resolve_binding(Path(__file__).resolve().parents[3], personal["runtime_binding"]))
            if selected["asset_identity_sha256"] != personal.get("runtime_identity_sha256"):
                raise ValueError("personal artifact and selected runtime asset identities differ")
            content = Path(selected["fd_repo"]) / "ttf/KaiXinSongA.ttf"
            # resolve_binding verifies the official asset closure, including
            # the exact content font hash; never accept an arbitrary path or
            # an environment-variable font override for a registered runtime.
            return str(content), "registered-runtime-content-font"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ApiError(409, "M2_RUNTIME_UNAVAILABLE", "Selected runtime font unavailable", str(exc), False) from exc
    url = style_snapshot.get("font_url") or ""
    presets = _load_weak_fonts()
    # font_url points at /api/v1/style-presets/{preset_id}/download
    if url.startswith("/api/v1/style-presets/") and url.endswith("/download"):
        pid = url.split("/")[-2]
        if pid in presets:
            return str(presets[pid]), pid
    simkai = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simkai.ttf"
    if simkai.exists():
        return str(simkai), "simkai"
    if presets:
        first = next(iter(presets.values()))
        return str(first), "preset-fallback"
    # A verified runtime addon supplies its own licensed content font for
    # layout/coverage. This is not any of the missing historical presets.
    try:
        from engine.style.m2_adapter.runtime import verify_runtime
        runtime=verify_runtime()
        content=Path(runtime['fd_repo'])/'ttf/KaiXinSongA.ttf'
        return str(content), 'runtime-content-font'
    except (OSError,ValueError,KeyError):pass
    raise ApiError(500, "EXPORT_FAILED", "No render font available", "", True)


def render_capabilities(session: Session, user_id: int, style_id: int | None = None, *, include_author_bound: bool = True) -> dict:
    """Read current source/asset/runtime prerequisites; never runs a model."""
    result = {"schema": "render-style-capabilities/1", "style_mode": "system" if style_id is None else "profile",
              "style_id": style_id, "source_type": "M0_SYSTEM", "renderable": False, "font_ready": False,
              "reason": None, "source_policies": [], "artifact": None,
              "source_policy_details": {"m2_cjk_required": {
                  "personal_scope": "Han ideographs including ideographic zero and CJK compatibility ideographs",
                  "other_characters": "M0 system font directly; no M2 or M1 attempts",
                  "personal_failure": "block item; no fallback",
                  "supported_layouts": ["baseline-v1"], "supported_ink_modes": ["auto-contrast-v1","custom-solid-srgb-v1"]}},
              "ink_colors": {"default_mode":"auto-contrast-v1", "custom_solid":{"available":True}, "source_solid":{"available":False,"reason":"Exact owned trained author palette required","palettes":[]},
                             "supported_layouts":["natural-layout-v1"],"supported_source_policies":["m2_required"],"automatic_recoloring":False},
              "layouts": [{"id": "baseline-v1", "available": False, "reason": None, "profile_sha256": None},
                          {"id": "natural-layout-v1", "available": False,
                           "reason": "Author-bound experimental layout has not completed acceptance", "profile_sha256": None}]}
    from .mixed_fonts import capabilities as mixed_font_capabilities
    result["non_cjk_fonts"] = mixed_font_capabilities()
    snapshot = {"font_url": None, "source": "M0_SYSTEM"}
    try:
        if style_id is not None:
            from app.domains.styles.models import StyleProfile
            from . import glyph_provider
            profile = session.query(StyleProfile).filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id).first()
            if profile is None:
                raise ApiError(404, "STYLE_NOT_FOUND", "Style not found")
            result["source_type"] = profile.source_type
            if profile.status != "approved" or not profile.enabled:
                raise ApiError(409, "STYLE_NOT_APPROVED", "Selected profile is not approved and enabled")
            if profile.source_type not in ("M0_SYSTEM", "SYSTEM_FONT", "M2_ADAPTER"):
                raise ApiError(409, "STYLE_NOT_RENDERABLE", "Input-only or unsupported render profile")
            snapshot = {"font_url": profile.font_url, "source": profile.source_type, "style_id": profile.style_id}
            if profile.source_type == "M2_ADAPTER":
                from app.domains.styles.service import resolve_style_artifact
                if not (getattr(profile, 'receipt_id', None) or '').startswith('imported_inference/'):
                    resolve_style_artifact(session, user_id, style_id)
                binding = glyph_provider.resolve_personal_binding(
                    session, user_id, style_id, observe_environment=False)
                if binding.get("state") != "bound":
                    raise ApiError(409, "ARTIFACT_INVALID", binding.get("reason", "Selected artifact unavailable"))
                snapshot["personal_binding"] = binding
                result["artifact"] = {k: binding[k] for k in ("artifact_id", "engine_version", "manifest_sha256")}
        verified_font = (snapshot.get('personal_binding') or {}).get('registered_content_font')
        if verified_font is not None:
            _resolve_font(snapshot, verified_registered_content_font=verified_font)
        else:
            _resolve_font(snapshot)
        result.update(renderable=True, font_ready=True, source_policies=["fallback_allowed"])
        binding = snapshot.get("personal_binding") or {}
        if binding.get("engine_mode") == "M2_ADAPTER" and binding.get("m2_adapter"):
            result["source_policies"].extend(("m2_required", "m2_cjk_required"))
        from .v3_layout import ADAPTER_SHA, PAIR, binding as v3_binding
        result['accepted_v3'] = {'available': (binding.get('m2_adapter') or {}).get('sha256') == ADAPTER_SHA
            and all(any(font['id'] == font_id and font['available'] for font in result['non_cjk_fonts']['fonts']) for font_id in PAIR.values()),
            'fonts': PAIR, 'binding': v3_binding(), 'seed': 2026100401, 'rgb': [140,48,68]}
        result["layouts"][0]["available"] = True
        if binding.get("engine_mode") == "M2_ADAPTER":
            if not include_author_bound:
                result["layouts"][1].update(available=False, reason="选择自然排版后再核验", deferred=True)
                result['ink_colors']['source_solid'] = {'available': False, 'reason': '选择训练原图取色后再核验', 'palettes': [], 'deferred': True}
            else:
                from .natural_authorization import resolve_author_profile
                qualified = None
                try:
                    qualified=resolve_author_profile(user_id,binding)
                    result["layouts"][1].update(available=True,reason=None,deferred=False,profile_sha256=qualified['binding']['profile_sha256'],qualification=qualified['binding']['qualification'],binding=qualified['binding'])
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    qualified = None
                    result["layouts"][1].update(available=False, reason=str(exc), deferred=False)
                from .ink_authorization import _discover_author_palettes
                if qualified is None:
                    result['ink_colors']['source_solid'] = {'available': False, 'reason': result["layouts"][1]["reason"], 'palettes': [], 'deferred': False}
                else:
                    result['ink_colors']['source_solid']=_discover_author_palettes(user_id,binding,
                        layout_profile='natural-layout-v1',render_source_policy='m2_required',session=session,
                        verified_layout=qualified)
    except ApiError as exc:
        if exc.status == 404:
            raise
        result["reason"] = exc.code + ": " + exc.title
        result["layouts"][0]["reason"] = result["reason"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = "RENDER_PREREQUISITE_UNAVAILABLE: " + str(exc)
        result["layouts"][0]["reason"] = result["reason"]
    return result


def _selected_missing(font_path, texts, snapshot):
    if 'non_cjk_fonts' not in snapshot:
        return _char_set_ok(font_path, texts)
    from .mixed_fonts import resolve_frozen, font_role, char_missing
    from .selection_contract import is_cjk_personal_character
    binding, paths = resolve_frozen(snapshot)
    return {ch for text in texts for ch in text if not ch.isspace()
            and not is_cjk_personal_character(ch) and char_missing(paths[font_role(ch, binding)], ch, binding['fonts'][font_role(ch, binding)])}


def _char_set_ok(font_path: str, texts: list[str]) -> set:
    """Characters missing from the font cmap (fontTools, v0.2 line 196)."""
    cmap = _cmap_for(font_path)
    if cmap is None:
        return set()  # cannot check -> no glyph gate (logged as warning upstream)
    missing = set()
    for text in texts:
        for ch in text:
            if ch.strip() and ord(ch) not in cmap:
                missing.add(ch)
    return missing


@lru_cache(maxsize=32)
def _cmap_for(font_path: str, face_index: int = 0):
    """Process-wide cmap cache: the precheck and the item loop re-open the
    same font files repeatedly; parsing them once keeps small renders well
    inside the RENDER_WAIT_SECONDS response window."""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(font_path, fontNumber=face_index, lazy=True) as font:
            return font.getBestCmap()
    except Exception:
        return None  # cannot check -> no glyph gate (logged as warning upstream)


def _stored_cn(text: str, style_snapshot: dict) -> str:
    """Natural-layout whitespace is authored content; preserve legacy trimming otherwise."""
    return text if style_snapshot.get("layout_profile") == "natural-layout-v1" else text.strip()


def _expand_slot_text(slot: dict, cn: str, blessing: str | None, blogger: str | None) -> str:
    # Disabling a signature omits the entire slot, including template literals.
    # Shared expansion keeps create/coverage/patch preflight aligned with render.
    if slot.get("key") == "signature" and not blogger:
        return ""
    tpl = slot.get("text_template") or ""
    text = tpl
    for var, val in (("{cn}", cn), ("{blessing}", blessing or ""), ("{blogger}", blogger or "")):
        text = text.replace(var, val)
    if not text:
        key = slot.get("key")
        text = {"to_cn": cn, "blessing": blessing or "", "signature": blogger or ""}.get(key, "")
    return text


def _snapshot_template(tpl: LayoutTemplate) -> dict:
    return {
        "template_id": tpl.template_id,
        "version": tpl.version,
        "slots": json.loads(tpl.slots),
        "base_asset_id": tpl.base_asset_id,
        "canvas_width": tpl.canvas_width,
        "canvas_height": tpl.canvas_height,
    }


def create_job(session: Session, user_id: int, payload: dict, idempotency_key: str | None) -> dict:
    """v0.2 section 1.6 POST: idempotent create + glyph precheck + snapshots
    (section 6) + CC-12 signature gate + CC-13 draft semantics, then the
    synchronous worker runs the items and finalizes the state machine."""
    if idempotency_key:
        existing = (
            session.query(RenderJob)
            .filter(RenderJob.idempotency_key == idempotency_key, RenderJob.user_id == user_id)
            .first()
        )
        if existing is not None:
            # section 7: same key returns the same job_id; hit is audited.
            audit(session, action="render_idempotency_hit", actor=str(user_id), object_ref=existing.job_id, result="ok")
            session.commit()
            return {"job": existing, "items": list(existing_items(session, existing.job_id)), "idempotent": True}

    template_id = payload.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "template_id is required")
    tpl = (
        session.query(LayoutTemplate)
        .filter(LayoutTemplate.template_id == template_id, LayoutTemplate.user_id == user_id)
        .first()
    )
    if tpl is None or tpl.status != "approved":
        raise ApiError(404 if tpl is None else 409, "TEMPLATE_NOT_APPROVED" if tpl is not None else "INVALID_REQUEST", "Template not available")

    items_in = payload.get("items")
    if not isinstance(items_in, list) or not items_in:
        # section 3 line 196: 0-item jobs are INVALID_REQUEST.
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items must be a non-empty list")
    if len(items_in) > 200:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "too many items (max 200)")

    draft = bool(payload.get("draft", False))
    strict_glyph = bool(payload.get("strict_glyph", False))
    sign_blogger = bool(payload.get("sign_blogger", False))
    blogger_name = payload.get("blogger_name")

    slots = json.loads(tpl.slots)
    slot_keys = {s.get("key") for s in slots}
    if sign_blogger and "signature" not in slot_keys:
        # patch-C section 5 (CC-12): signature on but template has no slot.
        raise ApiError(422, "SIGNATURE_SLOT_MISSING", "Signature slot missing", "template has no signature slot")
    if sign_blogger and not isinstance(blogger_name, str) or (sign_blogger and not blogger_name):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "blogger_name required when sign_blogger is true")

    # Style snapshot: referenced style row (approved only) or M0 defaults.
    from .selection_contract import choices, verify_selected
    selected = choices(payload, tpl.style_id)
    style_snapshot = {"font_url": None, "source": "M0_SYSTEM", **selected}
    style_id = selected["style_id"]
    if style_id:
        from app.domains.styles.models import StyleProfile

        sp = (
            session.query(StyleProfile)
            .filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id)
            .first()
        )
        if sp is None or sp.status != "approved" or not sp.enabled:
            # section 1.4 rule 4 / patch-C section 9.4: pending_review is unapproved.
            # P10-GAP-1 (2026-09-27): a user-disabled style must not start new jobs.
            if sp is not None and sp.status == "approved" and not sp.enabled:
                raise ApiError(409, "STYLE_DISABLED", "Style is disabled")
            raise ApiError(409, "STYLE_NOT_APPROVED", "Style not approved")
        style_snapshot = {"font_url": sp.font_url, "source": sp.source_type, **selected, "style_id": sp.style_id}
        if selected["style_mode"] == "profile" and sp.source_type not in ("M0_SYSTEM", "SYSTEM_FONT", "M2_ADAPTER"):
            raise ApiError(409, "STYLE_NOT_RENDERABLE", "Selected profile is an input or unsupported render source")
        # S11: a personal (M2_ADAPTER) profile additionally freezes its artifact
        # binding (hash-verified at create; re-resolved + re-verified at render
        # time - approved/enabled state is never cached). Any resolution failure
        # is recorded as personal_unavailable and starts the M2->M1->M0 fallback
        # chain instead of blocking the job (the approval gates above are
        # unchanged).
        if sp.source_type == "M2_ADAPTER":
            from app.domains.render import glyph_provider

            style_snapshot["personal_binding"] = glyph_provider.resolve_personal_binding(
                session, user_id, sp.style_id)

    verify_selected(style_snapshot,user_id,session=session)
    verified_font = (style_snapshot.get('personal_binding') or {}).get('registered_content_font')
    if verified_font is not None:
        font_path, font_name = _resolve_font(style_snapshot, verified_registered_content_font=verified_font)
    else:
        font_path, font_name = _resolve_font(style_snapshot)

    # P13 per-row overrides (E01/E13 2026-09-27): items[] rows may carry
    # optional asset_id / template_id / sign_blogger; a missing field falls
    # back to the batch value, so every pre-existing payload behaves exactly
    # as before. PRD v0.5 line 88 "单行 Cos 图/祝福语覆盖"; the template is the
    # asset lever (a layout template is bound to one base asset - a per-row
    # image must ride its own template or regions mis-scale, P13 "不同底图匹配
    # 本身template不套错比例"); per-row sign_blogger closes the E13 "经 some 变
    # 全局" wire collapse (blogger_name stays batch-level - one blogger per batch).
    from app.domains.assets_upload.models import Asset

    def _load_template(tid: str):
        return (
            session.query(LayoutTemplate)
            .filter(LayoutTemplate.template_id == tid, LayoutTemplate.user_id == user_id)
            .first()
        )

    batch_sign = sign_blogger
    item_ids = [_uuid() for _ in items_in]
    ov_templates: dict = {}  # template_id -> snapshot (section 6, frozen at create)
    ov_items: dict = {}  # item_id -> {"template_id"?, "sign_blogger"?} delta vs batch
    all_texts: list = []
    tpl_cache = {template_id: tpl}
    for idx, it in enumerate(items_in):
        if isinstance(it,dict) and any(k in it for k in ('ink_color_mode','ink_palette_id','ink_color_binding','ink_color_binding_sha256','ink_color_rgb')):
            raise ApiError(400,'INVALID_REQUEST','Ink selection is job-level and cannot be overridden per row')
        if not isinstance(it, dict) or not isinstance(it.get("cn"), str) or not it["cn"].strip():
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "each item needs a non-empty cn")
        row_tid = it.get("template_id")
        if row_tid is not None and (not isinstance(row_tid, str) or not row_tid):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].template_id must be a non-empty string")
        eff_tpl = tpl
        if row_tid and row_tid != template_id:
            eff_tpl = tpl_cache.get(row_tid) or _load_template(row_tid)
            tpl_cache[row_tid] = eff_tpl
            if eff_tpl is None:
                raise ApiError(404, "INVALID_REQUEST", "Template not available", f"items[{idx}].template_id not found")
            if eff_tpl.status != "approved":
                raise ApiError(409, "TEMPLATE_NOT_APPROVED", "Template not available", f"items[{idx}].template_id not approved")
        row_asset = it.get("asset_id")
        if row_asset is not None:
            if not isinstance(row_asset, str) or not row_asset:
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].asset_id must be a non-empty string")
            asset_row = session.get(Asset, row_asset)
            if asset_row is None or str(asset_row.user_id) != str(user_id):
                raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found", f"items[{idx}].asset_id not found")
            if asset_row.status != "approved" or not asset_row.enabled:
                raise ApiError(409, "ASSET_NOT_APPROVED", "Asset not approved", f"items[{idx}].asset_id not usable")
            if row_asset != eff_tpl.base_asset_id:
                # "换底图沿用旧template" wrong-ratio guard: the rendered image
                # comes from the template binding, so a disagreeing asset_id
                # would silently render the template's own base image.
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"items[{idx}].asset_id does not match the effective template's base asset; a per-row image must use its own template")
        row_sign = it.get("sign_blogger")
        if row_sign is not None and not isinstance(row_sign, bool):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].sign_blogger must be a boolean")
        eff_sign = batch_sign if row_sign is None else row_sign
        eff_slots = json.loads(eff_tpl.slots)
        if eff_sign and "signature" not in {s.get("key") for s in eff_slots}:
            raise ApiError(422, "SIGNATURE_SLOT_MISSING", "Signature slot missing", f"items[{idx}] template has no signature slot")
        if eff_sign and (not isinstance(blogger_name, str) or not blogger_name):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "blogger_name required when sign_blogger is true")
        # Glyph precheck (section 6): item text expands the item's own template
        # vars; the signature text only counts for rows that actually sign
        # (mirrors _render_one, precheck == formal truth).
        for slot in eff_slots:
            all_texts.append(_expand_slot_text(slot, it["cn"], it.get("blessing"), blogger_name if eff_sign else None))
        ov: dict = {}
        if eff_tpl is not tpl:
            if eff_tpl.template_id not in ov_templates:
                ov_templates[eff_tpl.template_id] = _snapshot_template(eff_tpl)
            ov["template_id"] = eff_tpl.template_id
        if eff_sign != batch_sign:
            ov["sign_blogger"] = eff_sign
        if ov:
            ov_items[item_ids[idx]] = ov

    from .selection_contract import verify_personal_text
    verify_personal_text(style_snapshot, all_texts)
    missing = _selected_missing(font_path, all_texts, style_snapshot)
    if missing and strict_glyph:
        # section 3 line 196 (rev. patch-E E-4): strict_glyph missing-glyph rejects the whole job.
        raise ApiError(422, "GLYPH_MISSING", "Missing glyphs", f"missing glyphs: {''.join(sorted(missing))}")

    base_asset = session.get(Asset, tpl.base_asset_id)
    if base_asset is None:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "template base asset missing")

    job_snapshot = _snapshot_template(tpl)
    if ov_items:
        # section 6 snapshot fidelity for per-row rows: the effective template
        # snapshots freeze at create exactly like the batch template does.
        job_snapshot["item_overrides"] = {"templates": ov_templates, "items": ov_items}
    now = _now()
    job = RenderJob(
        job_id=_uuid(),
        user_id=user_id,
        status="queued",
        draft=draft,
        idempotency_key=idempotency_key,
        ttl_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=DRAFT_TTL_SECONDS)).isoformat() if draft else None,
        template_snapshot=json.dumps(job_snapshot, ensure_ascii=False),
        style_snapshot=json.dumps(style_snapshot, ensure_ascii=False),
        base_asset_id=tpl.base_asset_id,
        base_checksum=base_asset.sha256,
        strict_glyph=strict_glyph,
        sign_blogger=sign_blogger,
        blogger_name=blogger_name,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    items = []
    for idx, it in enumerate(items_in):
        item = RenderItem(
            item_id=item_ids[idx],
            job_id=job.job_id,
            user_id=user_id,
            status="pending",
            cn=_stored_cn(it["cn"],style_snapshot),
            blessing=it.get("blessing"),
            template_id=ov_items.get(item_ids[idx], {}).get("template_id") or template_id,
            style_id=style_id,
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        items.append(item)
    audit(
        session,
        action="draft_job_created" if draft else "render_job_created",
        actor=str(user_id),
        object_ref=job.job_id,
        result="ok",
    )
    session.commit()

    # D-T10R-1: enqueue and return; the resident worker executes the job.
    enqueue_job(job.job_id, draft)
    return {"job": job, "items": list(existing_items(session, job.job_id)), "idempotent": False}


def coverage_check(session: Session, user_id: int, payload: dict) -> dict:
    """P16 coverage precheck (E05/E06 gap 2026-09-27: the frontend registry
    declares POST /api/v1/coverage/check, the app had no implementation).
    Thin read-only route over the SAME font resolution, template slot
    expansion and cmap gate the render core uses (_resolve_font /
    _expand_slot_text / _char_set_ok - exactly what create_job and
    _render_one run), so precheck == formal fallback truth. Creates nothing.

    Response: font (resolved), missing (chars absent from the primary font),
    fallback_map (missing chars the FALLBACK_FONT covers - what _render_one
    would substitute), unresolved (chars no available font covers - those
    items fail at render), ok (every char resolves via primary or fallback),
    strict_would_reject (strict_glyph jobs with any missing char are rejected
    at create, mirroring the GLYPH_MISSING 422)."""
    template_id = payload.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "template_id is required")
    tpl = (
        session.query(LayoutTemplate)
        .filter(LayoutTemplate.template_id == template_id, LayoutTemplate.user_id == user_id)
        .first()
    )
    if tpl is None or tpl.status != "approved":
        raise ApiError(404 if tpl is None else 409, "TEMPLATE_NOT_APPROVED" if tpl is not None else "INVALID_REQUEST", "Template not available")

    items_in = payload.get("items")
    if not isinstance(items_in, list) or not items_in:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items must be a non-empty list")
    if len(items_in) > 200:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "too many items (max 200)")

    sign_blogger = bool(payload.get("sign_blogger", False))
    blogger_name = payload.get("blogger_name")
    slots = json.loads(tpl.slots)
    if sign_blogger and "signature" not in {s.get("key") for s in slots}:
        raise ApiError(422, "SIGNATURE_SLOT_MISSING", "Signature slot missing", "template has no signature slot")
    if sign_blogger and not isinstance(blogger_name, str) or (sign_blogger and not blogger_name):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "blogger_name required when sign_blogger is true")

    # Style resolution mirrors create_job (approved style row or M0 defaults).
    from .selection_contract import choices, verify_selected
    selected = choices(payload, tpl.style_id)
    style_id = selected["style_id"]
    style_snapshot = {"font_url": None, "source": "M0_SYSTEM", **selected}
    if style_id:
        from app.domains.styles.models import StyleProfile

        sp = (
            session.query(StyleProfile)
            .filter(StyleProfile.style_id == style_id, StyleProfile.user_id == user_id)
            .first()
        )
        if sp is None or sp.status != "approved" or not sp.enabled:
            # P10-GAP-1 (2026-09-27): disabled styles are unusable for new jobs
            # (same gate as the create path above).
            if sp is not None and sp.status == "approved" and not sp.enabled:
                raise ApiError(409, "STYLE_DISABLED", "Style is disabled")
            raise ApiError(409, "STYLE_NOT_APPROVED", "Style not approved")
        style_snapshot = {"font_url": sp.font_url, "source": sp.source_type, **selected, "style_id": sp.style_id}
        if selected["style_mode"] == "profile" and sp.source_type not in ("M0_SYSTEM", "SYSTEM_FONT", "M2_ADAPTER"):
            raise ApiError(409, "STYLE_NOT_RENDERABLE", "Selected profile is an input or unsupported render source")
        if sp.source_type == "M2_ADAPTER":
            from app.domains.render import glyph_provider
            style_snapshot["personal_binding"] = glyph_provider.resolve_personal_binding(session, user_id, sp.style_id)

    verify_selected(style_snapshot,user_id,session=session)
    verified_font = (style_snapshot.get('personal_binding') or {}).get('registered_content_font')
    if verified_font is not None:
        font_path, font_name = _resolve_font(style_snapshot, verified_registered_content_font=verified_font)
    else:
        font_path, font_name = _resolve_font(style_snapshot)
    # Coverage must use the same owned, approved effective row context as
    # create_job. A retained batch name must not revive a disabled signature;
    # row templates and sign overrides decide which literal text is inspected.
    from app.domains.assets_upload.models import Asset

    tpl_cache = {template_id: tpl}
    all_texts: list[str] = []
    for idx, it in enumerate(items_in):
        if isinstance(it,dict) and any(k in it for k in ('ink_color_mode','ink_palette_id','ink_color_binding','ink_color_binding_sha256','ink_color_rgb')):
            raise ApiError(400,'INVALID_REQUEST','Ink selection is job-level and cannot be overridden per row')
        if not isinstance(it, dict) or not isinstance(it.get("cn"), str) or not it["cn"].strip():
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "each item needs a non-empty cn")
        row_tid = it.get("template_id")
        if row_tid is not None and (not isinstance(row_tid, str) or not row_tid):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].template_id must be a non-empty string")
        eff_tpl = tpl
        if row_tid and row_tid != template_id:
            eff_tpl = tpl_cache.get(row_tid) or (
                session.query(LayoutTemplate)
                .filter(LayoutTemplate.template_id == row_tid, LayoutTemplate.user_id == user_id)
                .first()
            )
            tpl_cache[row_tid] = eff_tpl
            if eff_tpl is None:
                raise ApiError(404, "INVALID_REQUEST", "Template not available", f"items[{idx}].template_id not found")
            if eff_tpl.status != "approved":
                raise ApiError(409, "TEMPLATE_NOT_APPROVED", "Template not available", f"items[{idx}].template_id not approved")
        row_asset = it.get("asset_id")
        if row_asset is not None:
            if not isinstance(row_asset, str) or not row_asset:
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].asset_id must be a non-empty string")
            asset_row = session.get(Asset, row_asset)
            if asset_row is None or str(asset_row.user_id) != str(user_id):
                raise ApiError(404, "ASSET_NOT_FOUND", "Asset not found", f"items[{idx}].asset_id not found")
            if asset_row.status != "approved" or not asset_row.enabled:
                raise ApiError(409, "ASSET_NOT_APPROVED", "Asset not approved", f"items[{idx}].asset_id not usable")
            if row_asset != eff_tpl.base_asset_id:
                # "换底图沿用旧template" wrong-ratio guard: the rendered image
                # comes from the template binding, so a disagreeing asset_id
                # would silently render the template's own base image.
                raise ApiError(400, "INVALID_REQUEST", "Invalid request", f"items[{idx}].asset_id does not match the effective template's base asset; a per-row image must use its own template")
        row_sign = it.get("sign_blogger")
        if row_sign is not None and not isinstance(row_sign, bool):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "items[].sign_blogger must be a boolean")
        eff_sign = sign_blogger if row_sign is None else row_sign
        eff_slots = json.loads(eff_tpl.slots)
        if eff_sign and "signature" not in {s.get("key") for s in eff_slots}:
            raise ApiError(422, "SIGNATURE_SLOT_MISSING", "Signature slot missing", f"items[{idx}] template has no signature slot")
        if eff_sign and (not isinstance(blogger_name, str) or not blogger_name):
            raise ApiError(400, "INVALID_REQUEST", "Invalid request", "blogger_name required when sign_blogger is true")
        # Glyph precheck (section 6): item text expands the item's own template
        # vars; the signature text only counts for rows that actually sign
        # (mirrors _render_one, precheck == formal truth).
        for slot in eff_slots:
            all_texts.append(_expand_slot_text(slot, it["cn"], it.get("blessing"), blogger_name if eff_sign else None))

    from .selection_contract import verify_personal_text
    verify_personal_text(style_snapshot, all_texts)
    missing = _selected_missing(font_path, all_texts, style_snapshot)
    fallback_map: dict = {}
    unresolved: set = set()
    if missing:
        # Judge the fallback with the same fonts _render_one would pick.
        fb_path = _load_weak_fonts().get(FALLBACK_FONT) or _win_font(FALLBACK_FONT)
        if fb_path is None:
            unresolved = set(missing)
        else:
            fb_missing = _char_set_ok(fb_path, all_texts)
            fallback_map = {ch: FALLBACK_FONT for ch in sorted(missing) if ch not in fb_missing}
            unresolved = {ch for ch in missing if ch in fb_missing}
    strict = bool(payload.get("strict_glyph", False))
    return {
        "font": font_name,
        "scope": "font_cmap_only",
        **({"non_cjk_fonts": style_snapshot["non_cjk_fonts"], "non_cjk_font_binding": style_snapshot["non_cjk_font_binding"]} if "non_cjk_fonts" in style_snapshot else {}),
        "m2_inference_guaranteed": False,
        "style_mode": selected["style_mode"],
        "glyph_source_policy": selected["glyph_source_policy"],
        "layout_profile": selected["layout_profile"],
        "strict_glyph": strict,
        "missing": sorted(missing),
        "fallback_map": fallback_map,
        "unresolved": sorted(unresolved),
        "ok": not unresolved,
        "strict_would_reject": bool(missing) and strict,
    }


def existing_items(session: Session, job_id: str) -> list:
    return session.query(RenderItem).filter(RenderItem.job_id == job_id).all()


def _finalize_job(session: Session, job: RenderJob) -> None:
    """Section 5 terminal-state predicate; cancelled outranks partial."""
    statuses = [i.status for i in existing_items(session, job.job_id)]
    if statuses and all(s == "cancelled" for s in statuses):
        job.status = "cancelled"
    elif job.cancel_requested and any(s == "cancelled" for s in statuses):
        job.status = "cancelled"
    elif statuses and all(s == "completed" for s in statuses):
        job.status = "succeeded"
    elif statuses and all(s == "failed" for s in statuses):
        job.status = "failed"
    elif any(s in ("failed", "completed_with_warning") for s in statuses):
        job.status = "partial"
    else:
        job.status = "partial"
    job.updated_at = _now()


def run_job(session: Session, job: RenderJob) -> None:
    """Job execution body (section 7): runs on the worker thread with its own
    session, and is still callable synchronously (probe G10R-7 drives it
    directly). The collaborative-cancel flag is re-read per item so a cancel
    request that lands mid-run is honoured; state machine finalized per
    section 5."""
    job.status = "running"
    job.updated_at = _now()
    session.commit()
    tpl = json.loads(job.template_snapshot)
    font_path, font_name = _resolve_font(json.loads(job.style_snapshot))
    # S11: per-job personal glyph session (None for system-font jobs - the
    # pre-existing path). Fresh resolution/hash re-verification per render.
    from app.domains.render import glyph_provider

    # Resolve again at every item boundary; a mid-batch disable/revocation
    # must not inherit approval or outcome state from the previous item.
    # P13 per-row overrides: rows created with their own template/sign ride the
    # snapshots frozen at create (job.template_snapshot.item_overrides); jobs
    # without overrides (all pre-existing rows) resolve to the batch values.
    ov_root = tpl.get("item_overrides") or {}
    ov_templates = ov_root.get("templates") or {}
    ov_items = ov_root.get("items") or {}
    for item in existing_items(session, job.job_id):
        if item.status != "pending":
            continue
        session.expire_all()
        glyph_session = glyph_provider.open_job_session(session, job)
        session.expire(job, ["cancel_requested"])
        if job.cancel_requested:
            item.status = "cancelled"
            item.updated_at = _now()
            session.commit()
            continue
        ov = ov_items.get(item.item_id)
        eff_tpl = ov_templates[ov["template_id"]] if ov and ov.get("template_id") in ov_templates else tpl
        _render_one(
            session,
            job,
            item,
            eff_tpl,
            font_path,
            font_name,
            base_asset_id=eff_tpl.get("base_asset_id"),
            signed=ov.get("sign_blogger") if ov else None,
            glyph_session=glyph_session,
        )
    _finalize_job(session, job)
    session.commit()


@operation_entry
def _render_one(
    session: Session,
    job: RenderJob,
    item: RenderItem,
    tpl: dict,
    font_path: str,
    font_name: str,
    base_asset_id: str | None = None,
    signed: bool | None = None,
    glyph_session=None,
) -> None:
    """P13: base_asset_id/signed default to the batch job values, so every
    pre-existing caller (sync path, patch re-render, probes driving run_job)
    behaves exactly as before; per-row override rows pass their own frozen
    template binding and per-row sign decision.
    S11: glyph_session (None = system-font job, byte-identical legacy path)
    renders slot text per character through the M2->M1->M0 personal fallback
    chain and records per-char source/version/reason provenance."""
    from PIL import Image, ImageDraw, ImageFont, ImageStat

    item.status = "running"
    session.commit()
    warnings: list[str] = []
    fallback_used = False
    fallback_map: dict = {}
    glyph_records: list = []
    slot_meta = None
    frozen_settings = json.loads(job.style_snapshot)
    typography_slots = []
    custom_color_slots = []
    def color_strip(strip,index,key):
        if frozen_settings.get('ink_color_mode') != 'custom-solid-srgb-v1':return strip
        from .custom_ink import apply_custom_ink
        colored,execution = apply_custom_ink(strip,frozen_settings)
        custom_color_slots.append({'slot_index':index,'slot_key':key,'execution':execution})
        if slot_meta is not None:slot_meta['ink_color_execution']=execution
        return colored
    def record_failure(reason):
        if glyph_session is None:
            return
        glyph_session.output_blocked = True
        glyph_session.blocked_reason = glyph_session.blocked_reason or reason
        if slot_meta is not None and slot_meta["outcome"] == "not_attempted":
            slot_meta.update(outcome="not_rendered", reason=reason)
        _glyph_provider_module().write_glyph_meta(job, item, glyph_session, glyph_records, warnings)

    try:
        from .selection_contract import frozen_ink_mode
        frozen_ink_mode(frozen_settings)
        if glyph_session is not None:
            glyph_session.begin_item()
            glyph_session.slot_executions = [
                {"slot_index": index, "slot_key": slot.get("key"), "outcome": "not_attempted"}
                for index, slot in enumerate(tpl["slots"])
            ]
        from app.domains.assets_upload.models import Asset

        asset = session.get(Asset, base_asset_id or job.base_asset_id)
        base_path = storage_root() / asset.relative_path
        img = Image.open(base_path).convert("RGB")
        background = img.copy()
        W, H = img.size
        draw = ImageDraw.Draw(img)
        missing = _char_set_ok(font_path, ["x"])  # placeholder; per-slot below
        for slot_index, slot in enumerate(tpl["slots"]):
            # Overlay absolute values onto a copy; immutable template/job snapshots
            # are never multiplied or edited when previewing, retrying or patching.
            if 'text_style' in frozen_settings or 'handwriting' in frozen_settings:
                from app.core.text_style import validate_text_style
                slot = {**slot, 'text_style': validate_text_style({**slot.get('text_style', {}),
                    **frozen_settings.get('text_style', {})})}
            key = slot.get("key")
            region = slot.get("region", {})
            override = json.loads(item.region_override) if item.region_override else None
            if override and key in override:
                region = override[key]
            blogger = job.blogger_name if (job.sign_blogger if signed is None else signed) else None
            text = _expand_slot_text(slot, item.cn, item.blessing, blogger)
            if glyph_session is not None:
                slot_meta = glyph_session.slot_executions[slot_index]
                slot_meta.update(text=text, char_record_range=[len(glyph_records), len(glyph_records)],
                                 layout_execution_range=[len(glyph_session.layout_executions), len(glyph_session.layout_executions)])
            if not text.strip():
                if slot_meta is not None:
                    slot_meta.update(outcome="skipped", reason=("signature_disabled" if key == "signature" and not blogger
                                                               else "empty_text" if not text else "blank_only"))
                continue
            miss = _selected_missing(font_path, [text], json.loads(job.style_snapshot))
            use_font_path = font_path
            if miss:
                if job.strict_glyph:
                    item.status = "failed"
                    item.last_error = "GLYPH_MISSING"
                    record_failure(item.last_error)
                    item.updated_at = _now()
                    session.commit()
                    return
                fallback_used = True
                for ch in miss:
                    fallback_map[ch] = FALLBACK_FONT
                warnings.append(f"GLYPH_MISSING fallback to {FALLBACK_FONT}: {''.join(sorted(miss))}")
                use_font_path = str(_load_weak_fonts().get(FALLBACK_FONT) or _win_font(FALLBACK_FONT) or font_path)
                miss2 = _char_set_ok(use_font_path, [text])
                if miss2:
                    item.status = "failed"
                    item.last_error = "GLYPH_MISSING"
                    record_failure(item.last_error)
                    item.updated_at = _now()
                    session.commit()
                    return
            x = int(region.get("x_norm", 0) * W)
            y = int(region.get("y_norm", 0) * H)
            w = int(region.get("w_norm", 0.5) * W)
            h = int(region.get("h_norm", 0.2) * H)
            size = max(12, int(h * 0.6))
            font = ImageFont.truetype(use_font_path, size=size)
            text_box = draw.textbbox((0, 0), text, font=font)
            text_width = text_box[2] - text_box[0]
            while text_width > w and size > 12:
                size = max(12, int(size * w / text_width * 0.95))
                font = ImageFont.truetype(use_font_path, size=size)
                text_box = draw.textbbox((0, 0), text, font=font)
                text_width = text_box[2] - text_box[0]
            if text_width > w:
                size = max(6, int(size * w / text_width * 0.95))
                font = ImageFont.truetype(use_font_path, size=size)

            sample = ImageStat.Stat(background.crop((x, y, x + w, y + h))).mean
            luminance = 0.2126 * sample[0] + 0.7152 * sample[1] + 0.0722 * sample[2]
            fill = (255, 255, 255) if luminance < 140 else (0, 0, 0)
            stroke_fill = (0, 0, 0) if luminance < 140 else (255, 255, 255)
            stroke_width = max(1, size // 40)
            # S11 wiring point: personal-style per-character stamping. The
            # provider owns the M2->M1->M0 chain; the drawing, region math and
            # color policy above stay the render core's own.
            strip = None
            if glyph_session is not None:
                layout_start = len(glyph_session.layout_executions)
                strip, records, blocker = glyph_session.render_text(
                    text, w=w, h=h, font_path=use_font_path, font_name=font_name,
                    fill=fill, stroke_fill=stroke_fill, size_hint=size,
                    strict=bool(job.strict_glyph),
                    **({"text_style": slot["text_style"], "slot_key": key} if "text_style" in slot else {}))
                for record in records:
                    record.update(slot_index=slot_index, slot_key=key)
                typography_slots.append({'slot_index':slot_index,'slot_key':key,
                    'text_style':slot.get('text_style'),'chars':records})
                glyph_records.extend(records)
                for execution in glyph_session.layout_executions[layout_start:]:
                    execution.update(slot_index=slot_index, slot_key=key)
                slot_meta["char_record_range"][1] = len(glyph_records)
                slot_meta["layout_execution_range"][1] = len(glyph_session.layout_executions)
                missing_required_strip = strip is None and glyph_session.source_policy in REQUIRED_M2_POLICIES
                if (glyph_session.output_blocked or missing_required_strip) and blocker is None:
                    # A nonblank provider failure can never drift into draw.text.
                    blocker = next(ch for ch in text if ch.strip())
                if blocker is not None:
                    # strict + personal glyph unavailable for a char: mark the
                    # item for a human decision (PATCH text / reject) instead
                    # of silently substituting the system font.
                    warnings.extend(glyph_session.warning_lines(glyph_records))
                    item.warnings = json.dumps(warnings, ensure_ascii=False) or None
                    item.status = "failed"
                    item.last_error = getattr(glyph_session,"blocked_reason",None) or ("M2_REQUIRED_UNAVAILABLE" if glyph_session.source_policy in REQUIRED_M2_POLICIES else "GLYPH_MISSING")
                    slot_meta["blocker"] = blocker
                    record_failure(item.last_error)
                    item.retryable = glyph_session.source_policy in REQUIRED_M2_POLICIES
                    item.updated_at = _now()
                    session.commit()
                    return
                if strip is not None:
                    from .selection_contract import frozen_ink_mode
                    if frozen_ink_mode(json.loads(job.style_snapshot)) == 'source-solid-srgb-v1':
                        from .ink_render_provenance import apply_source_ink
                        try:
                            strip, ink_execution = apply_source_ink(session,job,item,glyph_session,asset,img,strip,(x,y),slot_index,key)
                        except (OSError,ValueError,KeyError,TypeError) as exc:
                            raise ApiError(409,'INK_COLOR_UNAVAILABLE','Current source ink cannot be rendered',str(exc)) from exc
                        slot_meta['ink_color_execution'] = ink_execution
                        warnings.append('INK_COLOR_PHOTO_REVIEW_REQUIRED source-derived sRGB; local visibility is not calibrated')
                    strip = color_strip(strip,slot_index,key)
                    img.paste(strip, (x, y), strip)
                    slot_meta["outcome"] = "composed"
                    continue
            if "text_style" in slot:
                from app.domains.render.text_layout import render_scaled_text
                styled_strip, records, _ = render_scaled_text(text,key,slot["text_style"],w=w,h=h,
                    font_path=use_font_path,font_name=font_name,fill=fill,stroke_fill=stroke_fill,
                    handwriting=frozen_settings.get('handwriting'))
                typography_slots.append({'slot_index':slot_index,'slot_key':key,
                    'text_style':slot['text_style'],'chars':records,
                    'output_size':list(styled_strip.size) if styled_strip is not None else None})
                if styled_strip is not None:
                    styled_strip = color_strip(styled_strip,slot_index,key)
                    img.paste(styled_strip,(x,y),styled_strip)
                if slot_meta is not None:
                    slot_meta.update(outcome="composed",text_style=slot["text_style"])
                continue
            if frozen_settings.get('ink_color_mode') == 'custom-solid-srgb-v1':
                legacy_strip=Image.new('RGBA',(w,h))
                ImageDraw.Draw(legacy_strip).text((0,0),text,fill=tuple(fill)+(255,),font=font,
                    stroke_width=stroke_width,stroke_fill=tuple(stroke_fill)+(255,))
                legacy_strip=color_strip(legacy_strip,slot_index,key)
                img.paste(legacy_strip,(x,y),legacy_strip)
                continue
            draw.text(
                (x, y), text, fill=fill, font=font,
                stroke_width=stroke_width, stroke_fill=stroke_fill,
            )
            if slot_meta is not None:
                slot_meta["outcome"] = "composed"
        # S11: finalize per-char provenance (warnings summary + sidecar JSON
        # next to the output PNG). Any char that resolved below its M2 target
        # sets fallback_used, so the review UI shows an honest downgrade.
        if glyph_session is not None:
            warnings.extend(glyph_session.warning_lines(glyph_records))
            if any(r.get("reason") for r in glyph_records):
                fallback_used = True
        out_dir = storage_root() / str(job.user_id) / "outputs" / job.job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"{item.item_id}_v{item.render_version}.png"
        out_path = out_dir / out_name
        tmp_path = out_path.with_suffix(".png.part")
        cjk_policy = glyph_session is not None and glyph_session.source_policy == "m2_cjk_required"
        if job.draft or cjk_policy or 'text_style' in frozen_settings or 'handwriting' in frozen_settings or custom_color_slots:
            from PIL.PngImagePlugin import PngInfo

            meta = PngInfo()
            if job.draft:meta.add_text("draft", "true")  # section 1.6 draft constraint 2
            if custom_color_slots:
                from .custom_ink import color_metadata
                meta.add_itxt('render_ink_color',json.dumps(color_metadata(frozen_settings,custom_color_slots),
                    ensure_ascii=False,sort_keys=True,allow_nan=False))
            if 'text_style' in frozen_settings or 'handwriting' in frozen_settings:
                meta.add_itxt('render_typography', json.dumps({'schema':'render-typography/1',
                    'text_style':frozen_settings.get('text_style'), 'handwriting':frozen_settings.get('handwriting'),
                    'slots':typography_slots},ensure_ascii=False,sort_keys=True,allow_nan=False))
            if cjk_policy:
                # Review/export hash the PNG bytes. Embedding this evidence
                # carries the explicit mixed-source contract into exact-copy
                # exports, even when a consumer has no adjacent sidecar.
                from .natural_layout_v1 import digest
                provenance = {"schema": "render-glyph-source-provenance/1",
                              "job_id": job.job_id, "item_id": item.item_id,
                              "render_version": item.render_version,
                              "glyph_source_policy": glyph_session.source_policy,
                              "style_snapshot_sha256": digest(json.loads(job.style_snapshot)),
                              "requested_binding": glyph_session.meta_payload(item,glyph_records,warnings)["requested_binding"],
                              "chars": glyph_records, "slot_executions": glyph_session.slot_executions}
                meta.add_text("glyph_source_policy", glyph_session.source_policy)
                meta.add_itxt("render_glyph_provenance", json.dumps(provenance,ensure_ascii=False,sort_keys=True,allow_nan=False))
            img.save(tmp_path, "PNG", pnginfo=meta)
        else:
            img.save(tmp_path, "PNG")
        os.replace(tmp_path, out_path)
        rel = str(out_path.relative_to(storage_root())).replace("\\", "/")
        item.output_relative_path = rel
        item.fallback_used = fallback_used
        item.fallback_map = json.dumps(fallback_map, ensure_ascii=False) if fallback_map else None
        item.warnings = json.dumps(warnings, ensure_ascii=False) if warnings else None
        if glyph_session is not None:
            # per-char provenance sidecar (S11 metadata on disk; meta loss must
            # never fail a finished render, hence the containment inside)
            meta_path = _glyph_provider_module().write_glyph_meta(
                job, item, glyph_session, glyph_records, warnings)
            if cjk_policy and not meta_path:
                item.output_relative_path = None
                raise ApiError(409,'GLYPH_OUTPUT_PROVENANCE_INVALID','Required mixed-source glyph metadata could not be saved')
            if cjk_policy:
                try:
                    # A provenance SELECT must not autoflush unfinished output
                    # fields and keep SQLite's writer through model/font hashes.
                    with session.no_autoflush:
                        proof = _cjk_output_evidence(session, job, item)
                    audit(session, action='render_item_cjk_output_binding', actor=str(job.user_id),
                          object_ref=item.item_id, result=json.dumps(proof, sort_keys=True, allow_nan=False))
                except (OSError, ValueError, KeyError, TypeError, ApiError) as exc:
                    item.output_relative_path = None
                    raise ApiError(409,'GLYPH_OUTPUT_PROVENANCE_INVALID','Mixed-source output could not be anchored',str(exc)) from exc
            from .selection_contract import frozen_ink_mode
            if frozen_ink_mode(json.loads(job.style_snapshot)) == 'source-solid-srgb-v1':
                from .ink_render_provenance import finalize_ink_output
                try:
                    if not meta_path:raise ValueError('Required glyph/color sidecar was not saved')
                    with session.no_autoflush:
                        finalize_ink_output(session,job,item)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    item.output_relative_path = None
                    raise ApiError(409,'INK_OUTPUT_PROVENANCE_INVALID','Color output evidence could not be finalized',str(exc)) from exc
        item.status = "completed_with_warning" if warnings else "completed"
        item.updated_at = _now()
        session.commit()
    except ApiError as exc:
        record_failure(exc.code)
        raise
    except Exception as exc:
        item.status = "failed"
        item.last_error = str(exc)[:400]
        record_failure(item.last_error)
        item.retryable = True
        item.updated_at = _now()
        session.commit()


def _glyph_provider_module():
    """Lazy import so the render core keeps loading without the provider's
    subprocess/GPU-lock dependency chain in system-font-only deployments."""
    from app.domains.render import glyph_provider

    return glyph_provider


def _win_font(name: str) -> str | None:
    p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / f"{name}.ttf"
    return str(p) if p.exists() else None


def get_job(session: Session, user_id: int, job_id: str) -> RenderJob:
    job = (
        session.query(RenderJob)
        .filter(RenderJob.job_id == job_id, RenderJob.user_id == user_id)
        .first()
    )
    if job is None:
        raise ApiError(404, "JOB_NOT_FOUND", "Job not found")
    if job.draft and job.ttl_expires_at and datetime.fromisoformat(job.ttl_expires_at) <= datetime.now(timezone.utc):
        raise ApiError(404, "JOB_NOT_FOUND", "Job not found", "draft TTL expired")
    return job


def cancel_job(session: Session, user_id: int, job_id: str) -> RenderJob:
    """Section 7 collaborative cancel: flag the job; the item loop honours it.
    Terminal jobs answer 409 JOB_NOT_CANCELLABLE (section 3)."""
    job = get_job(session, user_id, job_id)
    if job.status not in ("queued", "running"):
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "Job not cancellable", f"status={job.status}")
    job.cancel_requested = True
    job.updated_at = _now()
    pending = [i for i in existing_items(session, job_id) if i.status in ("pending", "running")]
    for item in pending:
        item.status = "cancelled"
        item.updated_at = _now()
    _finalize_job(session, job)
    audit(session, action="render_cancel", actor=str(user_id), object_ref=job_id, result="ok")
    session.commit()
    return job


# P15 retry scopes (E13/E06 gap 2026-09-27: the frontend registry declares
# retry-cancelled and per-item retry, the router had neither). One retry core,
# three explicit scopes: the job-level retry never auto re-runs a user
# cancellation (P15 risk "cancelled 误自动重试") - cancelled items need the
# dedicated retry_cancelled_job route; a per-item retry accepts one explicitly
# targeted item. GLYPH_MISSING stays non-retriable everywhere: the text must
# be fixed via PATCH first (the glyph gate would fail the new job again).
RETRY_JOB_STATUSES = ("failed", "completed_with_warning")
RETRY_ITEM_STATUSES = ("failed", "cancelled", "completed_with_warning")


def _retry_gate(session: Session, user_id: int, job_id: str) -> RenderJob:
    job = get_job(session, user_id, job_id)
    if job.status not in ("partial", "failed", "cancelled"):
        raise ApiError(409, "JOB_NOT_CANCELLABLE", "Job not retriable", f"status={job.status}")
    return job


def _retriable_items(session: Session, job_id: str, statuses: tuple, item_id: str | None = None) -> list:
    rows = [
        i
        for i in existing_items(session, job_id)
        if i.status in statuses and i.last_error != "GLYPH_MISSING"
    ]
    if item_id is not None:
        rows = [i for i in rows if i.item_id == item_id]
    return rows


def _stable_retry_personal_binding(binding):
    """Only execution namespace may migrate; model/data/font identities remain."""
    stable=copy.deepcopy(binding)
    for key in _RETRY_EXECUTION_FIELDS:stable.pop(key,None)
    font=stable.get('registered_content_font')
    if isinstance(font,dict):
        for key in ('runtime_binding_sha256','deployment_sha256'):font.pop(key,None)
    return stable


def _retry_identity(job: RenderJob, item: RenderItem, *, stable_execution: bool = False, stable_layout: bool = False) -> str:
    """Identity of one frozen user request, independent of retry IDs."""
    template = json.loads(job.template_snapshot)
    overrides = template.pop('item_overrides', {}) or {}
    job_identity={key: getattr(job,key) for key in ('user_id','draft','style_snapshot','base_asset_id','base_checksum','strict_glyph','sign_blogger','blogger_name')}
    if stable_execution:
        snapshot=json.loads(job.style_snapshot)
        if isinstance(snapshot.get('personal_binding'),dict):snapshot['personal_binding']=_stable_retry_personal_binding(snapshot['personal_binding'])
        if stable_layout:snapshot.pop('v3_layout_binding',None)
        job_identity['style_snapshot']=snapshot
    payload = {'job': job_identity,
               'template': template, 'override_templates': overrides.get('templates') or {},
               'item_override': (overrides.get('items') or {}).get(item.item_id),
               'item': {key: getattr(item, key) for key in ('user_id', 'cn', 'blessing', 'region_override', 'template_id', 'style_id')}}
    if stable_layout:payload['item'].pop('region_override',None)
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _retry_execution_snapshot(session,user_id,job):
    snapshot=json.loads(job.style_snapshot);old=snapshot.get('personal_binding')
    if not isinstance(old,dict) or old.get('engine_mode')!='M2_ADAPTER':return job.style_snapshot,None
    if old.get('state')!='bound' or type(old.get('style_id')) is not int or old['style_id']<1 or snapshot.get('style_id',old['style_id'])!=old['style_id']:
        raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Original personal source identity is invalid')
    from .glyph_provider import resolve_personal_binding
    current=resolve_personal_binding(session,user_id,old['style_id'])
    for binding in (old,current):
        owner=binding.get('deployment_user_id')
        if owner is not None and (type(owner) is not int or owner!=user_id):raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Execution owner differs')
    if current.get('state')!='bound' or _stable_retry_personal_binding(old)!=_stable_retry_personal_binding(current):
        raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Current qualified execution cannot preserve the original model, data, fonts and parameters')
    if current==old:return job.style_snapshot,None
    def sha(value):return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
    adapter=old.get('m2_adapter');snapshot_binding=old.get('snapshot_binding');original_font=old.get('registered_content_font')
    base=old.get('base_ckpt_sha256')
    base_complete=sha(base) or (isinstance(base,dict)
        and set(base)=={'content_encoder.npz','style_encoder.npz','unet.npz'}
        and all(sha(value) for value in base.values()))
    if (not isinstance(adapter,dict) or not sha(adapter.get('sha256')) or not sha(old.get('manifest_sha256'))
        or not base_complete or not isinstance(old.get('engine_version'),str) or not old['engine_version']
        or not isinstance(snapshot_binding,dict) or not sha(snapshot_binding.get('snapshot_sha256'))
        or not isinstance(original_font,dict) or not sha(original_font.get('sha256')) or not sha(original_font.get('asset_identity_sha256'))):
        raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Original model, S03, base and font evidence is incomplete')
    if not isinstance(current.get('runtime_binding'),dict) or not isinstance(current.get('deployment_binding'),dict):
        raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Execution migration requires a verified deployment')
    verified_font=current.get('registered_content_font')
    if verified_font is not None:_resolve_font({'personal_binding':current},verified_registered_content_font=verified_font)
    snapshot['personal_binding']=copy.deepcopy(current)
    def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return json.dumps(snapshot,ensure_ascii=False), {'old_personal_binding_sha256':digest(old),'new_personal_binding_sha256':digest(current),
        'old_execution':{key:old.get(key) for key in _RETRY_EXECUTION_FIELDS},
        'new_execution':{key:current.get(key) for key in _RETRY_EXECUTION_FIELDS}}


def _retry_layout_record(user_id,parent,source,child,target,requested,requested_override=None):
    old=json.loads(parent.style_snapshot);new=json.loads(child.style_snapshot)
    old_binding=old.get('v3_layout_binding');new_binding=new.get('v3_layout_binding')
    if old_binding is not None or new_binding is not None:
        from .historical_cjk_output import validate_v3_binding
        try:validate_v3_binding(old);validate_v3_binding(new)
        except (ValueError,TypeError,KeyError) as exc:raise ApiError(409,'RETRY_BINDING_INVALID','Unknown V3 layout identity') from exc
    def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    before=json.loads(source.region_override) if source.region_override else None
    after=json.loads(target.region_override) if target.region_override else None
    if not requested and requested_override is not None:raise ApiError(409,'RETRY_BINDING_INVALID','Unexpected region request evidence')
    if requested:
        _retry_validate_region(parent,source,requested_override)
        expected={**(before or {}),**requested_override}
        _retry_validate_region(parent,source,expected)
        if after!=expected:raise ApiError(409,'RETRY_BINDING_INVALID','Explicit region changes differ from frozen target')
    return {'schema':'render-retry-layout-migration/1','owner_user_id':user_id,'source_job_id':parent.job_id,'source_item_id':source.item_id,'job_id':child.job_id,'item_id':target.item_id,
        'old_layout_binding':old_binding,'new_layout_binding':new_binding,'old_region_override':before,'new_region_override':after,
        'old_region_override_sha256':digest(before),'new_region_override_sha256':digest(after),'region_override_requested':requested,
        'requested_region_override':requested_override,'requested_region_override_sha256':digest(requested_override)}


def _retry_validate_region(job,item,region_override):
    if region_override is None:return
    from app.domains.templates.service import _validate_slots
    template=json.loads(job.template_snapshot);overrides=template.get('item_overrides') or {}
    row=(overrides.get('items') or {}).get(item.item_id) or {}
    effective=(overrides.get('templates') or {}).get(row.get('template_id'),template)
    slots=copy.deepcopy(effective.get('slots') or [])
    keys={slot.get('key') for slot in slots}
    if not isinstance(region_override,dict) or not region_override or not set(region_override)<=keys:
        raise ApiError(400,'INVALID_REQUEST','Region override must target existing slots')
    for key,region in region_override.items():
        if not isinstance(region,dict) or set(region)!={'x_norm','y_norm','w_norm','h_norm'} or any(type(value) not in (int,float) for value in region.values()):
            raise ApiError(400,'INVALID_REQUEST','Region override requires four numeric normalized bounds')
        for slot in slots:
            if slot['key']==key:slot['region']=copy.deepcopy(region)
    _validate_slots(slots)


def _retry_binding(session, user_id, new_job_id):
    rows = session.query(AuditLog).filter(AuditLog.action=='render_retry_binding', AuditLog.actor==str(user_id), AuditLog.object_ref==new_job_id).all()
    if len(rows)!=1:raise ApiError(409,'RETRY_BINDING_INVALID','Retry binding absent or conflicting')
    try:record=json.loads(rows[0].result)
    except (ValueError,TypeError) as exc:raise ApiError(409,'RETRY_BINDING_INVALID','Retry binding malformed') from exc
    if not isinstance(record,dict) or set(record)!={'schema','owner_user_id','source_job_id','source_item_id','job_id','item_id','frozen_identity_sha256'} or record['schema'] not in ('render-retry-binding/1','render-retry-binding/2','render-retry-binding/3') or type(record['owner_user_id']) is not int or record['owner_user_id']!=user_id or record['job_id']!=new_job_id or any(not isinstance(record[k],str) or not record[k] for k in ('source_job_id','source_item_id','job_id','item_id')) or not isinstance(record['frozen_identity_sha256'],str) or len(record['frozen_identity_sha256'])!=64:
        raise ApiError(409,'RETRY_BINDING_INVALID','Retry binding differs')
    parent=get_job(session,user_id,record['source_job_id']); child=get_job(session,user_id,new_job_id)
    source=session.get(RenderItem,record['source_item_id']); target=session.get(RenderItem,record['item_id'])
    layout=record['schema']=='render-retry-binding/3'
    stable=record['schema'] in ('render-retry-binding/2','render-retry-binding/3')
    if source is None or target is None or source.user_id!=user_id or target.user_id!=user_id or source.job_id!=parent.job_id or target.job_id!=child.job_id or child.retry_of_job_id!=parent.job_id or _retry_identity(parent,source,stable_execution=stable,stable_layout=layout)!=record['frozen_identity_sha256'] or _retry_identity(child,target,stable_execution=stable,stable_layout=layout)!=record['frozen_identity_sha256']:
        raise ApiError(409,'RETRY_BINDING_INVALID','Retry frozen source identity differs')
    if layout:
        rows_layout=session.query(AuditLog).filter(AuditLog.action=='render_retry_layout_migration',AuditLog.actor==str(user_id),AuditLog.object_ref==new_job_id).all()
        if len(rows_layout)!=1:raise ApiError(409,'RETRY_BINDING_INVALID','Layout migration evidence missing or conflicting')
        try:layout_record=json.loads(rows_layout[0].result)
        except (ValueError,TypeError) as exc:raise ApiError(409,'RETRY_BINDING_INVALID','Layout migration malformed') from exc
        requested=layout_record.get('region_override_requested') if isinstance(layout_record,dict) else None
        if type(requested) is not bool or layout_record!=_retry_layout_record(user_id,parent,source,child,target,requested,layout_record.get('requested_region_override')):
            raise ApiError(409,'RETRY_BINDING_INVALID','Layout migration evidence changed')
        if not requested and source.region_override!=target.region_override:raise ApiError(409,'RETRY_BINDING_INVALID','Region changed without explicit request')
        if requested:_retry_validate_region(parent,source,json.loads(target.region_override))
    old=json.loads(parent.style_snapshot).get('personal_binding');new=json.loads(child.style_snapshot).get('personal_binding')
    if old!=new:
        if not isinstance(old,dict) or not isinstance(new,dict):raise ApiError(409,'RETRY_BINDING_INVALID','Execution migration source malformed')
        for binding in (old,new):
            owner=binding.get('deployment_user_id')
            if owner is not None and (type(owner) is not int or owner!=user_id):raise ApiError(409,'RETRY_BINDING_INVALID','Execution migration owner differs')
        migrations=session.query(AuditLog).filter(AuditLog.action=='render_retry_execution_migration',AuditLog.actor==str(user_id),AuditLog.object_ref==new_job_id).all()
        if not stable or len(migrations)!=1:raise ApiError(409,'RETRY_BINDING_INVALID','Execution migration authority missing')
        migration=json.loads(migrations[0].result)
        def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        expected={'schema':'render-retry-execution-migration/1','owner_user_id':user_id,'source_job_id':parent.job_id,'source_item_id':source.item_id,'job_id':child.job_id,'item_id':target.item_id,
          'old_personal_binding_sha256':digest(old),'new_personal_binding_sha256':digest(new),
          'old_execution':{key:old.get(key) for key in _RETRY_EXECUTION_FIELDS},
          'new_execution':{key:new.get(key) for key in _RETRY_EXECUTION_FIELDS}}
        if migration!=expected:raise ApiError(409,'RETRY_BINDING_INVALID','Execution migration evidence changed')
    return record,target,rows[0].id


def _retry_core(session: Session, user_id: int, job: RenderJob, retriable: list, idempotency_key: str | None = None, region_override: dict | None = None) -> dict:
    """Shared retry body: a new job with retry_of_job_id set; the original is
    immutable (section 7). The clone goes through the same worker queue.
    P13: per-row overrides travel with their item - the frozen snapshots are
    cloned verbatim and re-keyed from the old item_ids to the new ones."""
    now = _now()
    retry_style,migration=_retry_execution_snapshot(session,user_id,job)
    retry_snapshot=json.loads(retry_style)
    layout_migration=region_override is not None
    if retry_snapshot.get('v3_layout_binding') is not None:
        from .historical_cjk_output import validate_v3_binding
        from .v3_layout import binding as current_v3_binding
        try:validate_v3_binding(retry_snapshot)
        except (ValueError,TypeError,KeyError) as exc:raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Original V3 layout identity is unknown') from exc
        current_layout=current_v3_binding()
        if retry_snapshot['v3_layout_binding']!=current_layout:
            retry_snapshot['v3_layout_binding']=current_layout;retry_style=json.dumps(retry_snapshot,ensure_ascii=False);layout_migration=True
    if layout_migration and len(retriable)!=1:raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Layout migration requires an explicit single-item retry')
    if region_override is not None:_retry_validate_region(job,retriable[0],region_override)
    if migration is not None and len(retriable)!=1:raise ApiError(409,'RETRY_EXECUTION_INCOMPATIBLE','Execution migration requires an explicit single-item retry')
    old_snap = json.loads(job.template_snapshot)
    old_ov = old_snap.get("item_overrides") or {}
    old_ov_items = old_ov.get("items") or {}
    new_job = RenderJob(
        job_id=_uuid(),
        user_id=user_id,
        status="queued",
        draft=job.draft,
        retry_of_job_id=job.job_id,
        idempotency_key=idempotency_key,
        template_snapshot=job.template_snapshot,
        style_snapshot=retry_style,
        base_asset_id=job.base_asset_id,
        base_checksum=job.base_checksum,
        strict_glyph=job.strict_glyph,
        sign_blogger=job.sign_blogger,
        blogger_name=job.blogger_name,
        created_at=now,
        updated_at=now,
    )
    session.add(new_job)
    new_items = []
    new_ov_items: dict = {}
    for old in retriable:
        item = RenderItem(
            item_id=_uuid(),
            job_id=new_job.job_id,
            user_id=user_id,
            status="pending",
            cn=old.cn,
            blessing=old.blessing,
            region_override=json.dumps({**(json.loads(old.region_override) if old.region_override else {}),**region_override},ensure_ascii=False,sort_keys=True,allow_nan=False) if region_override is not None else old.region_override,
            template_id=old.template_id,
            style_id=old.style_id,
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        new_items.append(item)
        if old.item_id in old_ov_items:
            new_ov_items[item.item_id] = old_ov_items[old.item_id]
    if new_ov_items:
        new_snap = dict(old_snap)
        new_snap["item_overrides"] = {"templates": old_ov.get("templates") or {}, "items": new_ov_items}
        new_job.template_snapshot = json.dumps(new_snap, ensure_ascii=False)
    if len(retriable)==1:
        source,target=retriable[0],new_items[0]
        audit(session,action='render_retry_binding',actor=str(user_id),object_ref=new_job.job_id,
              result=json.dumps({'schema':'render-retry-binding/3' if layout_migration else 'render-retry-binding/2','owner_user_id':user_id,
                'source_job_id':job.job_id,'source_item_id':source.item_id,'job_id':new_job.job_id,
                'item_id':target.item_id,'frozen_identity_sha256':_retry_identity(job,source,stable_execution=True,stable_layout=layout_migration)},sort_keys=True))
        if layout_migration:
            audit(session,action='render_retry_layout_migration',actor=str(user_id),object_ref=new_job.job_id,
                result=json.dumps(_retry_layout_record(user_id,job,source,new_job,target,region_override is not None,region_override),sort_keys=True,allow_nan=False))
        if migration is not None:
            audit(session,action='render_retry_execution_migration',actor=str(user_id),object_ref=new_job.job_id,
                  result=json.dumps({'schema':'render-retry-execution-migration/1','owner_user_id':user_id,'source_job_id':job.job_id,
                    'source_item_id':source.item_id,'job_id':new_job.job_id,'item_id':target.item_id,**migration},sort_keys=True))
    audit(session, action="render_retry", actor=str(user_id), object_ref=job.job_id, result=f"new:{new_job.job_id}")
    session.commit()
    # D-T10R-1: the retry job goes through the same worker queue.
    enqueue_job(new_job.job_id, new_job.draft)
    return {"job": new_job, "items": new_items, "retried_item_ids": [i.item_id for i in retriable]}


def retry_job(session: Session, user_id: int, job_id: str) -> dict:
    """POST /{job_id}/retry: retry failed / completed_with_warning items.
    Cancelled items are deliberately NOT auto-retried (P15): reviving a
    cancellation is the explicit retry_cancelled_job route."""
    job = _retry_gate(session, user_id, job_id)
    retriable = _retriable_items(session, job_id, RETRY_JOB_STATUSES)
    if not retriable:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "no retriable items")
    return _retry_core(session, user_id, job, retriable)


def retry_cancelled_job(session: Session, user_id: int, job_id: str) -> dict:
    """POST /{job_id}/retry-cancelled (P15 gap): retry ONLY the cancelled items
    of a terminal job - explicit user intent to re-run what was cancelled."""
    job = _retry_gate(session, user_id, job_id)
    retriable = _retriable_items(session, job_id, ("cancelled",))
    if not retriable:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "no cancelled items")
    return _retry_core(session, user_id, job, retriable)


def retry_item(session: Session, user_id: int, job_id: str, item_id: str, idempotency_key: str | None = None, region_override: dict | None = None) -> dict:
    """POST /{job_id}/items/{item_id}/retry (P15 gap): retry one explicitly
    targeted item (failed / cancelled / completed_with_warning)."""
    # Same owned row lock as review/export serializes SQLite retry creation.
    get_job(session,user_id,job_id)
    _lock_review_job(session,user_id,job_id)
    if idempotency_key is not None:
        if not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=128 or any(ord(c)<33 or ord(c)>126 for c in idempotency_key):
            raise ApiError(400,'INVALID_REQUEST','Invalid idempotency key')
        existing=session.query(RenderJob).filter(RenderJob.idempotency_key==idempotency_key).first()
        if existing is not None:
            if existing.user_id!=user_id:raise ApiError(409,'IDEMPOTENCY_CONFLICT','Idempotency key belongs to a different request')
            binding,target,_=_retry_binding(session,user_id,existing.job_id)
            if binding['source_job_id']!=job_id or binding['source_item_id']!=item_id:
                raise ApiError(409,'IDEMPOTENCY_CONFLICT','Idempotency key targets a different item')
            source=session.get(RenderItem,item_id)
            requested=False
            saved_override=None
            if binding['schema']=='render-retry-binding/3':
                entry=session.query(AuditLog).filter(AuditLog.action=='render_retry_layout_migration',AuditLog.actor==str(user_id),AuditLog.object_ref==existing.job_id).one()
                replay_layout=json.loads(entry.result);requested=replay_layout['region_override_requested'];saved_override=replay_layout['requested_region_override']
            if requested!=(region_override is not None) or saved_override!=region_override:
                raise ApiError(409,'IDEMPOTENCY_CONFLICT','Idempotency key has different region override')
            if region_override is not None:_retry_validate_region(get_job(session,user_id,job_id),source,region_override)
            session.commit()
            return {'job':existing,'items':[target],'retried_item_ids':[item_id],'idempotent':True}
    job = _retry_gate(session, user_id, job_id)
    retriable = _retriable_items(session, job_id, RETRY_ITEM_STATUSES, item_id=item_id)
    if not retriable:
        raise ApiError(404, "JOB_NOT_FOUND", "Item not found", "not retriable in this state")
    return _retry_core(session, user_id, job, retriable, idempotency_key=idempotency_key,region_override=region_override)


def _lock_review_job(session: Session, user_id: int, job_id: str) -> None:
    """Serialize output edits, review decisions and export through one job row.

    The no-value-change UPDATE takes SQLite's write transaction (or the job
    row's write lock) before authority reads; refreshing avoids stale ORM data.
    The caller commits/rolls back its normal request transaction.
    """
    session.execute(update(RenderJob).where(RenderJob.job_id == job_id, RenderJob.user_id == user_id)
                    .values(updated_at=RenderJob.updated_at))
    session.expire_all()


def patch_item(session: Session, user_id: int, job_id: str, item_id: str, payload: dict) -> RenderItem:
    """Section 7 item-level fix: new render version, old output kept, audit
    records before/after."""
    if any(k in payload for k in ("style_mode", "style_id", "glyph_source_policy", "layout_profile", "layout_seed", "ink_color_mode", "ink_color_rgb", "ink_palette_id", "ink_color_binding", "ink_color_binding_sha256", "non_cjk_fonts", "non_cjk_font_binding")):
        raise ApiError(400, "INVALID_REQUEST", "Render selection and layout policy are immutable; create a new job")
    _lock_review_job(session, user_id, job_id)
    job = get_job(session, user_id, job_id)
    if job.status not in ("succeeded", "partial", "failed"):
        raise ApiError(409, "RENDER_NOT_EDITABLE", "Wait for the render to finish before editing", "Queued/running output is immutable; cancelled jobs use explicit retry")
    item = (
        session.query(RenderItem)
        .filter(RenderItem.item_id == item_id, RenderItem.job_id == job_id)
        .first()
    )
    if item is None:
        raise ApiError(404, "JOB_NOT_FOUND", "Item not found")
    before = {"cn": item.cn, "blessing": item.blessing, "render_version": item.render_version,
              "output_relative_path": item.output_relative_path}
    updates = {}
    if "cn" in payload:
        if not isinstance(payload["cn"],str) or not payload["cn"].strip():
            raise ApiError(400,"INVALID_REQUEST","cn must contain non-whitespace text; omit it to keep the current value")
        updates["cn"] = _stored_cn(payload["cn"],json.loads(job.style_snapshot))
    if "blessing" in payload:
        updates["blessing"] = payload.get("blessing")
    if "region_override" in payload and isinstance(payload.get("region_override"), dict):
        updates["region_override"] = json.dumps(payload["region_override"], ensure_ascii=False)
    if not updates:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "nothing to update")
    from .selection_contract import verify_personal_text
    pre_tpl=json.loads(job.template_snapshot)
    pre_root=pre_tpl.get("item_overrides") or {}
    pre_override=(pre_root.get("items") or {}).get(item.item_id)
    pre_effective=((pre_root.get("templates") or {}).get(pre_override["template_id"])
                   if pre_override and pre_override.get("template_id") in (pre_root.get("templates") or {}) else pre_tpl)
    pre_signed=pre_override.get("sign_blogger") if pre_override else None
    pre_name=job.blogger_name if (job.sign_blogger if pre_signed is None else pre_signed) else None
    pre_texts=[_expand_slot_text(slot,updates.get("cn",item.cn),updates.get("blessing",item.blessing),pre_name)
               for slot in pre_effective["slots"]]
    verify_personal_text(json.loads(job.style_snapshot), pre_texts)
    # A removed/tampered selected runtime must reject before a terminal item
    # is edited or committed. Preserve its prior text/version/output/review.
    render_font = (_resolve_font(json.loads(job.style_snapshot))
                   if job.status in ("succeeded", "partial", "failed") else None)
    for key, value in updates.items():
        setattr(item, key, value)
    item.render_version += 1
    # Old bytes remain on disk/audit/export history; they are not the output
    # of this new version if inference fails before a replacement is written.
    item.output_relative_path = None
    # A prior review approves a specific output version, never future edits.
    item.reviewed_by = None
    item.reviewed_at = None
    item.review_batch_id = None
    item.comment = None
    item.status = "pending"
    item.last_error = None
    item.updated_at = _now()
    audit(
        session,
        action="render_item_patch",
        actor=str(user_id),
        object_ref=item_id,
        result=f"before={json.dumps(before, ensure_ascii=False)} after={json.dumps({'cn': item.cn, 'blessing': item.blessing}, ensure_ascii=False)}",
    )
    # Claim the new revision atomically with its edit before releasing the
    # write transaction. A second PATCH/worker cannot consume an older version.
    job.status = "running"
    job.updated_at = _now()
    session.commit()
    # Re-render the claimed item; unexpected setup failure must terminalize it.
    try:
        tpl = json.loads(job.template_snapshot)
        font_path, font_name = render_font
        # P13: a per-row item re-renders with its own frozen template/sign.
        ov_root = tpl.get("item_overrides") or {}
        ov = (ov_root.get("items") or {}).get(item.item_id)
        eff_tpl = (
            (ov_root.get("templates") or {}).get(ov["template_id"])
            if ov and ov.get("template_id") in (ov_root.get("templates") or {})
            else tpl
        )
        item.status = "pending"
        session.commit()
        # S11: fresh glyph session for the re-render (re-resolves the binding).
        glyph_session = _glyph_provider_module().open_job_session(session, job)
        _render_one(
            session,
            job,
            item,
            eff_tpl,
            font_path,
            font_name,
            base_asset_id=eff_tpl.get("base_asset_id"),
            signed=ov.get("sign_blogger") if ov else None,
            glyph_session=glyph_session,
        )
        _finalize_job(session, job)
        session.commit()
    except Exception as exc:
        session.rollback()
        session.refresh(item)
        session.refresh(job)
        item.status = "failed"
        item.output_relative_path = None
        item.last_error = exc.code if isinstance(exc, ApiError) else "RENDER_FAILED"
        item.updated_at = _now()
        audit(session, action="render_item_patch_failed", actor=str(user_id), object_ref=item_id,
              result="failed", error_code=item.last_error)
        _finalize_job(session, job)
        session.commit()
    session.refresh(item)
    return item


def _cjk_output_evidence(session, job, item, *, output_bytes=None):
    """Recompute current CJK source evidence; only the producer may anchor it."""
    import io
    from PIL import Image
    from app.core.storage import ensure_within_root
    from .selection_contract import character_requires_m2, frozen_ink_mode
    from .natural_layout_v1 import digest
    from .ink_render_provenance import _plan, _context
    from . import glyph_provider as provider

    snapshot = json.loads(job.style_snapshot)
    if (not isinstance(snapshot,dict) or snapshot.get('glyph_source_policy') != 'm2_cjk_required'
            or snapshot.get('layout_profile', 'baseline-v1') != 'baseline-v1'
            or frozen_ink_mode(snapshot) not in ('auto-contrast-v1','custom-solid-srgb-v1')):
        raise ValueError('CJK output requires its exact frozen baseline/automatic policy')
    binding = snapshot['personal_binding']
    from .v3_layout import selected as is_v3, verify as verify_v3, binding as v3_binding
    v3 = is_v3(snapshot)
    output_rel = (Path(str(job.user_id)) / 'outputs' / job.job_id / f'{item.item_id}_v{item.render_version}.png').as_posix()
    if item.output_relative_path != output_rel:
        raise ValueError('CJK output path differs from current item/version')
    root = storage_root().resolve()
    raw = ensure_within_root(root / output_rel, root / str(job.user_id) / 'outputs' / job.job_id).read_bytes()
    if output_bytes is not None and (not isinstance(output_bytes, bytes) or output_bytes != raw):
        raise ValueError('CJK exported bytes differ from current output')
    glyph_rel = provider.glyph_meta_relative_path(job, item).as_posix()
    glyph_raw = ensure_within_root(root / glyph_rel, root / str(job.user_id) / 'outputs' / job.job_id).read_bytes()
    selected = provider.open_job_session(session, job)
    historical = selected is None or not selected.bound or selected.source_policy != 'm2_cjk_required'
    if historical:
        # A completed producer-anchored image is evidence, not a new model run.
        # This context cannot generate glyphs and retains the original runtime.
        from .historical_cjk_output import resolve as resolve_historical
        selected = resolve_historical(session, job, item, snapshot,
            output_bytes=raw, sidecar_bytes=glyph_raw,
            render_context=_context(job, item, snapshot),
            stable_binding=_stable_retry_personal_binding)
    if v3:
        if historical:expected_v3_binding = selected.v3_binding
        else:
            verify_v3(snapshot)
            expected_v3_binding = v3_binding()
    meta = json.loads(glyph_raw)
    requested = {k: selected.requested[k] for k in ('style_id', 'artifact_id', 'engine_version', 'manifest_sha256')}
    expected_meta = {'schema': 'render-glyph-meta/1', 'item_id': item.item_id, 'job_id': job.job_id,
                     'render_version': item.render_version, 'style_id': binding['style_id'],
                     'mode': 'personal', 'glyph_source_policy': 'm2_cjk_required',
                     'outcome': 'rendered', 'blocked_reason': None, 'requested_binding': requested}
    if not isinstance(meta, dict) or any(digest(meta.get(k)) != digest(v) for k, v in expected_meta.items()):
        raise ValueError('CJK glyph sidecar identity or outcome changed')
    chars, slots = meta.get('chars'), meta.get('slot_executions')
    executions = meta.get('layout_executions')
    if (not isinstance(chars, list) or not isinstance(slots, list)
            or not isinstance(executions, list) or (not v3 and executions != [])):
        raise ValueError('CJK glyph/slot evidence is incomplete')
    _, plans = _plan(job, item)
    if len(slots) != len(plans):
        raise ValueError('CJK slot evidence differs from frozen template')
    offset = 0
    layout_offset = 0
    custom_slots = {}
    if v3 and frozen_ink_mode(snapshot) == 'custom-solid-srgb-v1':
        from .custom_ink import validate_custom_output
        validate_custom_output(snapshot, raw)
        with Image.open(io.BytesIO(raw)) as image:
            custom_slots = {row['slot_index']: row['execution'] for row in json.loads(image.info['render_ink_color'])['slots']}
    for slot, plan in zip(slots, plans):
        visible = [] if plan['skip_reason'] is not None else [ch for ch in plan['text'] if not ch.isspace()]
        end = offset + len(visible)
        layout_end = layout_offset + (1 if v3 and plan['skip_reason'] is None else 0)
        expected = {'slot_index': plan['slot_index'], 'slot_key': plan['slot_key'], 'text': plan['text'],
                    'char_record_range': [offset, end], 'layout_execution_range': [layout_offset, layout_end],
                    'outcome': 'skipped' if plan['skip_reason'] is not None else 'composed'}
        if plan['slot_index'] in custom_slots:expected['ink_color_execution'] = custom_slots[plan['slot_index']]
        if plan['skip_reason'] is not None:expected['reason'] = plan['skip_reason']
        if not isinstance(slot, dict) or digest(slot) != digest(expected):
            raise ValueError('CJK slot source ranges or outcome changed')
        if layout_end > layout_offset:
            if layout_end > len(executions):raise ValueError('V3 layout execution missing')
            execution = executions[layout_offset]
            if (execution.get('schema') != 'accepted-v3-layout-execution/1'
                    or execution.get('binding') != expected_v3_binding or execution.get('text') != plan['text']
                    or execution.get('slot_key') != plan['slot_key'] or execution.get('slot_index') != plan['slot_index']
                    or execution.get('hidden_fit_down') is not False):
                raise ValueError('V3 layout producer identity changed')
            from app.core.text_style import validate_text_style, validate_handwriting
            effective_slot = _plan(job, item)[0]['slots'][plan['slot_index']]
            if (execution.get('text_style') != validate_text_style({**effective_slot.get('text_style', {}), **snapshot.get('text_style', {})})
                    or execution.get('handwriting') != validate_handwriting(snapshot.get('handwriting', {}))):
                raise ValueError('V3 render parameters differ from the frozen job')
        records = chars[offset:end]
        if any(not isinstance(r, dict) for r in records) or [r.get('char') for r in records] != visible:
            raise ValueError('CJK character evidence differs from current text')
        for record in records:
            if (record.get('slot_index') != plan['slot_index'] or record.get('slot_key') != plan['slot_key']
                    or record.get('reason')):
                raise ValueError('CJK character slot/source identity changed')
            if character_requires_m2('m2_cjk_required', record['char']):
                expected_source = {'source': 'M2', 'generation_kind': 'adapter_inference',
                                   'engine_mode': 'M2_ADAPTER', 'artifact_id': binding['artifact_id'],
                                   'version': binding['engine_version'], 'adapter_sha256': binding['m2_adapter']['sha256']}
                if binding.get('runtime_binding') is not None:expected_source['runtime_binding'] = binding['runtime_binding']
                png_hash = record.get('png_sha256')
                if not isinstance(png_hash,str) or len(png_hash) != 64 or any(c not in '0123456789abcdef' for c in png_hash):
                    raise ValueError('CJK model glyph lacks its verified PNG digest')
            else:
                expected_source = {'source': 'M0', 'generation_kind': 'system_font', 'required_source': 'M0',
                                   'selection_reason': 'non_cjk_system_font', 'attempted': []}
                if selected.mixed_fonts is not None:
                    from .mixed_fonts import font_role
                    role=font_role(record['char'], selected.mixed_fonts[0]);font=selected.mixed_fonts[0]['fonts'][role]
                    expected_source.update(version=font['id'],font_role=role,font_id=font['id'],font_sha256=font['sha256'])
                    if v3:expected_source.update(font_face_index=font.get('face_index',0),font_variation_axes=font.get('variation_axes',{}))
                if not isinstance(record.get('version'),str) or not record['version']:
                    raise ValueError('CJK system character lacks its font identity')
            if any(digest(record.get(k)) != digest(v) for k, v in expected_source.items()):
                raise ValueError('CJK character violates the required source selection')
        offset = end
        layout_offset = layout_end
    if offset != len(chars):raise ValueError('CJK sidecar contains orphan character evidence')
    if layout_offset != len(executions):raise ValueError('CJK sidecar contains orphan layout evidence')
    expected_embedded = {'schema': 'render-glyph-source-provenance/1', 'job_id': job.job_id,
                         'item_id': item.item_id, 'render_version': item.render_version,
                         'glyph_source_policy': 'm2_cjk_required', 'style_snapshot_sha256': digest(snapshot),
                         'requested_binding': requested, 'chars': chars, 'slot_executions': slots}
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != 'PNG' or image.info.get('glyph_source_policy') != 'm2_cjk_required':
            raise ValueError('CJK output lacks its explicit PNG source policy')
        embedded = json.loads(image.info.get('render_glyph_provenance', 'null'))
    if digest(embedded) != digest(expected_embedded):
        raise ValueError('CJK embedded provenance differs from current sidecar/context')
    return {'schema': 'render-cjk-output-proof/1', 'owner_user_id': job.user_id, 'job_id': job.job_id,
            'item_id': item.item_id, 'render_version': item.render_version, 'glyph_source_policy': 'm2_cjk_required',
            'render_context_sha256': digest(_context(job, item, snapshot)),
            'output_relative_path': output_rel, 'output_sha256': hashlib.sha256(raw).hexdigest(),
            'glyph_sidecar_relative_path': glyph_rel, 'glyph_sidecar_sha256': hashlib.sha256(glyph_raw).hexdigest(),
            'embedded_provenance_sha256': digest(embedded)}


def _validate_cjk_output(session, job, item, *, output_bytes=None):
    """Existing producer history prevents a modern output becoming legacy."""
    from .natural_layout_v1 import digest
    rows = session.query(AuditLog).filter(AuditLog.action == 'render_item_cjk_output_binding',
            AuditLog.actor == str(job.user_id), AuditLog.object_ref == item.item_id).order_by(AuditLog.id).all()
    snapshot = json.loads(job.style_snapshot or '{}')
    if not isinstance(snapshot,dict):raise ValueError('Frozen render selection must be an object')
    if snapshot.get('glyph_source_policy') != 'm2_cjk_required':
        if rows:raise ValueError('CJK producer authority cannot downgrade to another source policy')
        return None
    if item.status not in ('completed', 'completed_with_warning'):
        raise ValueError('CJK output is not complete')
    proof = _cjk_output_evidence(session, job, item, output_bytes=output_bytes)
    current = []
    for row in rows:
        value = json.loads(row.result)
        if (not isinstance(value,dict) or value.get('schema') != 'render-cjk-output-proof/1'
                or type(value.get('render_version')) is not int
                or any(digest(value.get(k)) != digest(proof[k]) for k in ('owner_user_id','job_id','item_id'))):
            raise ValueError('CJK producer anchor identity is invalid')
        if value['render_version'] == item.render_version:current.append(value)
    if not current or digest(current[-1]) != digest(proof):
        raise ValueError('CJK output has no matching server producer anchor')
    return proof


def review_items(session: Session, user_id: int, job_id: str, item_ids: list, decision: str, comment: str | None, reviewer: str) -> list:
    job = get_job(session, user_id, job_id)
    if job.draft:
        # section 1.6 draft constraint 3: drafts never enter review.
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "draft jobs are not reviewable")
    if decision not in ("approved", "rejected"):
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "decision must be approved|rejected")
    if decision == "rejected" and not comment:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "reject requires a comment")
    # Validate costly provenance before owning SQLite's single writer. Recheck
    # the exact frozen context, producer authority and bytes under a short lock.
    def fields(row):
        return {column.key: getattr(row, column.key) for column in row.__table__.columns}
    def anchors(iid):
        return [(row.id, row.action, row.result) for row in session.query(AuditLog)
                .filter(AuditLog.actor == str(user_id), AuditLog.object_ref == iid)
                .order_by(AuditLog.id).all()]
    job_fields = fields(job)
    prepared = []
    for iid in item_ids:
        item = session.query(RenderItem).filter(RenderItem.item_id == iid, RenderItem.job_id == job_id).first()
        if item is None:
            continue
        if item.status not in ("completed", "completed_with_warning"):
            continue
        item_fields = fields(item)
        item_anchors = anchors(iid)
        output_sha256 = None
        ink_proof = None
        cjk_proof = None
        if decision == "approved":
            try:
                from app.core.storage import ensure_within_root
                output = ensure_within_root(storage_root() / (item.output_relative_path or ""), storage_root())
                if not output.is_file():
                    raise ValueError("render output missing")
                output_bytes = output.read_bytes()
                output_sha256 = hashlib.sha256(output_bytes).hexdigest()
                cjk_proof = _validate_cjk_output(session,job,item,output_bytes=output_bytes)
                from .ink_render_provenance import validate_ink_output
                ink_proof = validate_ink_output(session,job,item)
            except (OSError, ValueError, KeyError, TypeError, ApiError) as exc:
                raise ApiError(409, "ITEM_NOT_APPROVED", "Current output cannot be approved", str(exc)) from exc
        file_bindings = []
        for proof in (cjk_proof, ink_proof):
            if proof is None:
                continue
            for path_key, sha_key in (("glyph_sidecar_relative_path", "glyph_sidecar_sha256"),
                                      ("sidecar_relative_path", "sidecar_sha256")):
                if proof.get(path_key) is not None:
                    file_bindings.append((proof[path_key], proof[sha_key]))
        prepared.append((iid, item_fields, item_anchors, output_sha256, ink_proof, cjk_proof, file_bindings))
    session.commit()
    _lock_review_job(session, user_id, job_id)
    job = get_job(session, user_id, job_id)
    if fields(job) != job_fields:
        raise ApiError(409, "ITEM_NOT_APPROVED", "Render context changed during review")
    batch = _uuid()
    out = []
    for iid, item_fields, item_anchors, output_sha256, ink_proof, cjk_proof, file_bindings in prepared:
        item = session.query(RenderItem).filter(RenderItem.item_id == iid, RenderItem.job_id == job_id).first()
        if item is None or fields(item) != item_fields or anchors(iid) != item_anchors:
            raise ApiError(409, "ITEM_NOT_APPROVED", "Render authority changed during review")
        if decision == "approved":
            from app.core.storage import ensure_within_root
            for relative, expected in [(item.output_relative_path, output_sha256), *file_bindings]:
                try:
                    path = ensure_within_root(storage_root() / relative, storage_root())
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("render evidence changed during review")
                except (OSError, ValueError, TypeError) as exc:
                    raise ApiError(409, "ITEM_NOT_APPROVED", "Render evidence changed during review", str(exc)) from exc
        item.reviewed_by = reviewer
        item.reviewed_at = _now()
        item.comment = comment
        item.review_batch_id = batch
        item.updated_at = _now()
        audit(session, action=f"render_item_{decision}", actor=str(user_id), object_ref=iid, result="ok")
        review_binding={"schema": "render-output-review/1", "decision": decision,
                  "job_id": job_id, "item_id": iid, "render_version": item.render_version,
                  "output_relative_path": item.output_relative_path, "output_sha256": output_sha256,
                  "reviewed_by": reviewer, "reviewed_at": item.reviewed_at,
                  "review_batch_id": batch}
        if ink_proof is not None:
            review_binding.update(schema='render-output-review/2',ink_color_proof=ink_proof)
        if cjk_proof is not None:
            review_binding.update(schema='render-output-review/3',cjk_source_proof=cjk_proof)
        audit(session, action="render_item_review_binding", actor=str(user_id), object_ref=iid,
              result=json.dumps(review_binding, sort_keys=True))
        out.append(item)
    session.commit()
    return out


def _current_output_review(session: Session, user_id: int, item: RenderItem, *, output_bytes: bytes | None = None) -> str | None:
    """A server-written explicit decision bound to these exact current bytes.

    Historical username-only records remain historical and need a fresh normal
    review; no approval is inferred or migrated. Rejection never authorizes an
    export. An edited/replaced output invalidates even a genuine old approval.
    """
    if not item.reviewed_by or item.reviewed_by == "pending" or not item.review_batch_id or not item.reviewed_at:
        return None
    q = session.query(AuditLog).filter(AuditLog.actor == str(user_id), AuditLog.object_ref == item.item_id)
    decision_row = q.filter(AuditLog.action.in_(("render_item_approved", "render_item_rejected")), AuditLog.result == "ok").order_by(AuditLog.id.desc()).first()
    bound_row = q.filter(AuditLog.action == "render_item_review_binding").order_by(AuditLog.id.desc()).first()
    if decision_row is None or bound_row is None or bound_row.id <= decision_row.id:
        return None
    try:
        record = json.loads(bound_row.result)
        keys = {"schema", "decision", "job_id", "item_id", "render_version", "output_relative_path", "output_sha256", "reviewed_by", "reviewed_at", "review_batch_id"}
        if isinstance(record,dict) and record.get('schema') == 'render-output-review/2':keys.add('ink_color_proof')
        if isinstance(record,dict) and record.get('schema') == 'render-output-review/3':keys.add('cjk_source_proof')
        if (not isinstance(record, dict) or set(record) != keys
                or type(record.get("render_version")) is not int or record["render_version"] < 1
                or any(not isinstance(record.get(k), str) or not record[k] for k in keys - {"render_version", "output_sha256", "ink_color_proof", "cjk_source_proof"})
                or record.get("decision") not in ("approved", "rejected")):
            return None
        digest = record.get("output_sha256")
        if record["decision"] == "approved" and (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            return None
        if record["decision"] == "rejected" and digest is not None:
            return None
        decision = decision_row.action.removeprefix("render_item_")
        job = get_job(session,user_id,item.job_id)
        from .ink_render_provenance import validate_ink_output
        ink_proof = validate_ink_output(session,job,item,output_bytes=output_bytes) if decision == 'approved' else None
        expected_schema = 'render-output-review/2' if ink_proof is not None else 'render-output-review/1'
        from .natural_layout_v1 import digest as proof_digest
        if ink_proof is not None and (not isinstance(record.get('ink_color_proof'),dict) or proof_digest(record['ink_color_proof']) != proof_digest(ink_proof)):return None
        if ink_proof is None and 'ink_color_proof' in record:return None
        cjk_proof = _validate_cjk_output(session,job,item,output_bytes=output_bytes) if decision == 'approved' else None
        if cjk_proof is not None:
            expected_schema = 'render-output-review/3'
            if proof_digest(record.get('cjk_source_proof')) != proof_digest(cjk_proof):return None
        elif 'cjk_source_proof' in record:return None
        expected = {"schema": expected_schema, "decision": decision,
                    "job_id": item.job_id, "item_id": item.item_id, "render_version": item.render_version,
                    "output_relative_path": item.output_relative_path, "reviewed_by": item.reviewed_by,
                    "reviewed_at": item.reviewed_at, "review_batch_id": item.review_batch_id}
        if not isinstance(record, dict) or any(record.get(k) != value for k, value in expected.items()):
            return None
        if decision == "rejected":
            return "rejected"
        from app.core.storage import ensure_within_root
        output = ensure_within_root(storage_root() / (item.output_relative_path or ""), storage_root())
        if not output.is_file() or hashlib.sha256(output.read_bytes() if output_bytes is None else output_bytes).hexdigest() != record.get("output_sha256"):
            return None
        return "approved"
    except (OSError, ValueError, KeyError, TypeError, ApiError):
        return None


def _linked_retry_items(session, user_id, job, originals):
    """Resolve only server-audited explicit item edges, never parent-job guesses."""
    rows=session.query(AuditLog).filter(AuditLog.action=='render_retry_binding',AuditLog.actor==str(user_id)).order_by(AuditLog.id).all()
    edges={}
    for row in rows:
        try:record=json.loads(row.result)
        except (ValueError,TypeError):continue
        if not isinstance(record,dict) or any(not isinstance(record.get(k),str) for k in ('source_job_id','source_item_id')):continue
        edges.setdefault((record.get('source_job_id'),record.get('source_item_id')),[]).append(row.object_ref)
    selected=[]; mappings={}
    for original in originals:
        stack=[original];visited=set();candidates=[]
        while stack:
            current=stack.pop()
            if current.item_id in visited:raise ApiError(409,'RETRY_BINDING_INVALID','Retry lineage cycle')
            visited.add(current.item_id)
            for target_job in edges.get((current.job_id,current.item_id),[]):
                _lock_review_job(session,user_id,target_job)
                binding,target,order=_retry_binding(session,user_id,target_job)
                if binding['source_job_id']!=current.job_id or binding['source_item_id']!=current.item_id:
                    raise ApiError(409,'RETRY_BINDING_INVALID','Retry edge differs')
                stack.append(target)
                if target.status in ('completed','completed_with_warning') and _current_output_review(session,user_id,target)=='approved':candidates.append((order,target))
        source=None
        if original.status in ('completed','completed_with_warning') and _current_output_review(session,user_id,original)=='approved':source=original
        elif candidates:source=max(candidates,key=lambda pair:pair[0])[1]
        if source is not None:
            selected.append(source)
            source_job=get_job(session,user_id,source.job_id)
            mappings[source.item_id]={'original_item_id':original.item_id,'source_job_id':source.job_id,'source_item_id':source.item_id,
                                     'frozen_identity_sha256':_retry_identity(source_job,source)}
    return selected,mappings


def export_job(session: Session, user_id: int, job_id: str, *, include_retries: bool = False) -> dict:
    """Section 1.6 export: approved items only; any unreviewed completed item
    blocks with ITEM_NOT_APPROVED; draft jobs cannot export; artifacts are
    copied under outputs/export/{export_id}/ and registered."""
    # Review and edits take the same lock, so approval remains current until
    # export commit; rejected/changed bytes cannot race the publication step.
    _lock_review_job(session, user_id, job_id)
    job = get_job(session, user_id, job_id)
    if job.draft:
        raise ApiError(400, "INVALID_REQUEST", "Invalid request", "draft jobs cannot be exported")
    items = existing_items(session, job_id)
    terminal = [i for i in items if i.status in ("completed", "completed_with_warning")]
    decisions = {i.item_id: _current_output_review(session, user_id, i) for i in terminal}
    approved = [i for i in terminal if decisions[i.item_id] == "approved"]
    unreviewed = [i for i in terminal if decisions[i.item_id] is None]
    if unreviewed:
        raise ApiError(409, "ITEM_NOT_APPROVED", "Item not approved", f"{len(unreviewed)} unreviewed items")
    linked_mapping={}
    if include_retries:approved,linked_mapping=_linked_retry_items(session,user_id,job,items)
    export_id = ('linked-' if include_retries else '') + _uuid()
    export_dir = storage_root() / str(user_id) / "outputs" / "export" / export_id
    export_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    failures = []
    ink_members = []
    cjk_members = []
    linked_members = []
    for item in approved:
        if not item.output_relative_path:
            failures.append({"item_id": item.item_id, "error": "no output"})
            continue
        src = storage_root() / item.output_relative_path
        if not src.exists() or src.stat().st_size > EXPORT_SINGLE_CAP:
            failures.append({"item_id": item.item_id, "error": "EXPORT_TOO_LARGE" if src.exists() else "missing"})
            continue
        payload = src.read_bytes()
        # Bind the bytes actually copied, not a prior filesystem read; a
        # changed/revoked output between admission and copy is never exported.
        if _current_output_review(session, user_id, item, output_bytes=payload) != "approved":
            failures.append({"item_id": item.item_id, "error": "ITEM_NOT_APPROVED"})
            continue
        from .ink_render_provenance import validate_ink_output
        source_job=get_job(session,user_id,item.job_id)
        ink_proof = validate_ink_output(session,source_job,item,output_bytes=payload)
        cjk_proof = _validate_cjk_output(session,source_job,item,output_bytes=payload)
        dst = export_dir / f"{item.item_id}.png"
        tmp = dst.with_suffix(".png.part")
        tmp.write_bytes(payload)
        os.replace(tmp, dst)
        rel = str(dst.relative_to(storage_root())).replace("\\", "/")
        # patch-C section 11.6: single-artifact cap via the T71 guard.
        try:
            from app.domains.export_guard import service as guard

            guard.check_single_export(dst.stat().st_size)
        except ApiError as exc:
            failures.append({"item_id": item.item_id, "error": exc.code})
            os.remove(dst)
            continue
        art = ExportArtifact(
            artifact_id=_uuid(),
            job_id=job_id,
            user_id=user_id,
            relative_path=rel,
            sha256=cjk_proof['output_sha256'] if cjk_proof is not None else hashlib.sha256(dst.read_bytes()).hexdigest(),
            size_bytes=len(payload) if cjk_proof is not None else dst.stat().st_size,
            created_at=_now(),
        )
        session.add(art)
        artifacts.append({"artifact_id": art.artifact_id, "relative_path": rel})
        if include_retries:
            member={**linked_mapping[item.item_id],'filename':dst.name,'output_sha256':art.sha256,'size_bytes':art.size_bytes}
            linked_members.append(member)
            audit(session,action='export_linked_artifact_binding',actor=str(user_id),object_ref=art.artifact_id,
                  result=json.dumps({'schema':'export-linked-artifact/1','artifact_id':art.artifact_id,'job_id':job_id,
                    'relative_path':rel,'sha256':art.sha256,'size_bytes':art.size_bytes,'members':[member]},sort_keys=True))
        if cjk_proof is not None:
            cjk_members.append({'filename':dst.name, 'output_sha256':cjk_proof['output_sha256'],
                                'size_bytes':len(payload)})
        if ink_proof is not None:
            member={'item_id':item.item_id,'render_version':item.render_version,
                    'filename':dst.name,'output_sha256':art.sha256,'size_bytes':art.size_bytes,'ink_color_proof':ink_proof}
            if include_retries:member['source_job_id']=item.job_id
            ink_members.append(member)
            audit(session,action='export_ink_artifact_binding',actor=str(user_id),object_ref=art.artifact_id,
                  result=json.dumps({'schema':'export-ink-artifact/2' if include_retries else 'export-ink-artifact/1','artifact_id':art.artifact_id,
                    'job_id':job_id,'relative_path':rel,'sha256':art.sha256,'size_bytes':art.size_bytes,
                    'members':[member]},sort_keys=True,allow_nan=False))
    # ZIP packaging reuses the T71 guard (ZIP cap 500MB, no half-written zip).
    zip_rel = None
    if artifacts:
        from app.domains.export_guard import service as guard

        files = [str(storage_root() / a["relative_path"]) for a in artifacts]
        dest = export_dir / f"{export_id}.zip"
        guard.pack_outputs_zip(files, str(dest))
        cjk_zip_bytes = None
        if cjk_members:
            try:
                import io, zipfile
                # The packaging boundary must preserve both copied PNGs and
                # every ZIP member. Freeze the same ZIP bytes we verify for
                # registry hashing so a later filesystem swap cannot acquire
                # a new trusted checksum.
                for member in cjk_members:
                    copied = (export_dir / member['filename']).read_bytes()
                    if len(copied) != member['size_bytes'] or hashlib.sha256(copied).hexdigest() != member['output_sha256']:
                        raise ValueError('CJK copied export differs from its approved producer output')
                cjk_zip_bytes = dest.read_bytes()
                _verify_ink_zip_members(io.BytesIO(cjk_zip_bytes),cjk_members)
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
                session.rollback()
                for rejected_path in [*map(Path,files), dest]:rejected_path.unlink(missing_ok=True)
                raise ApiError(409,'GLYPH_EXPORT_PROVENANCE_INVALID','Mixed-source export bytes changed during packaging',str(exc)) from exc
        zip_rel = str(dest.relative_to(storage_root())).replace("\\", "/")
        art = ExportArtifact(
            artifact_id=_uuid(),
            job_id=job_id,
            user_id=user_id,
            relative_path=zip_rel,
            sha256=hashlib.sha256(cjk_zip_bytes if cjk_zip_bytes is not None else dest.read_bytes()).hexdigest(),
            size_bytes=len(cjk_zip_bytes) if cjk_zip_bytes is not None else dest.stat().st_size,
            created_at=_now(),
        )
        session.add(art)
        artifacts.append({"artifact_id": art.artifact_id, "relative_path": zip_rel})
        if include_retries:
            audit(session,action='export_linked_artifact_binding',actor=str(user_id),object_ref=art.artifact_id,
                  result=json.dumps({'schema':'export-linked-artifact/1','artifact_id':art.artifact_id,'job_id':job_id,
                    'relative_path':zip_rel,'sha256':art.sha256,'size_bytes':art.size_bytes,'members':linked_members},sort_keys=True))
        if ink_members:
            with dest.open('rb') as sealed:
                _verify_ink_zip_members(sealed,ink_members)
        if ink_members:
            audit(session,action='export_ink_artifact_binding',actor=str(user_id),object_ref=art.artifact_id,
                  result=json.dumps({'schema':'export-ink-artifact/2' if include_retries else 'export-ink-artifact/1','artifact_id':art.artifact_id,
                    'job_id':job_id,'relative_path':zip_rel,'sha256':art.sha256,'size_bytes':art.size_bytes,
                    'members':ink_members},sort_keys=True,allow_nan=False))
    audit(session, action="export", actor=str(user_id), object_ref=job_id, result=f"{len(artifacts)} artifacts, {len(failures)} failures")
    session.commit()
    status = "succeeded" if not failures else ("partial" if artifacts else "failed")
    return {"export_id": export_id, "status": status, "artifacts": artifacts, "failures": failures,
            'exported_item_count':len(artifacts)-1 if artifacts else 0,'includes_linked_retries':include_retries}


def verify_export_linked_mapping(session,user_id,job_id,artifact,stream):
    """Validate immutable membership for a newly exported recovery-chain bundle."""
    rows=session.query(AuditLog).filter(AuditLog.action=='export_linked_artifact_binding',AuditLog.actor==str(user_id),AuditLog.object_ref==artifact.artifact_id).all()
    if not rows:
        if Path(artifact.relative_path).parent.name.startswith('linked-'):raise ValueError('Linked export authority missing')
        return None
    if len(rows)!=1:raise ValueError('Conflicting linked export authority')
    record=json.loads(rows[0].result)
    expected={'schema':'export-linked-artifact/1','artifact_id':artifact.artifact_id,'job_id':job_id,'relative_path':artifact.relative_path,'sha256':artifact.sha256,'size_bytes':artifact.size_bytes}
    if not isinstance(record,dict) or set(record)!=set(expected)|{'members'} or any(record.get(k)!=v for k,v in expected.items()):raise ValueError('Linked export registry differs')
    members=record['members'];seen=set()
    if not isinstance(members,list) or not members:raise ValueError('Linked export has no members')
    root=get_job(session,user_id,job_id)
    for m in members:
        if not isinstance(m,dict) or set(m)!={'original_item_id','source_job_id','source_item_id','frozen_identity_sha256','filename','output_sha256','size_bytes'} or m['original_item_id'] in seen or m['filename']!=m['source_item_id']+'.png':raise ValueError('Invalid linked export member')
        seen.add(m['original_item_id']);source=session.get(RenderItem,m['source_item_id']);original=session.get(RenderItem,m['original_item_id'])
        if source is None or original is None or original.job_id!=root.job_id or original.user_id!=user_id or source.user_id!=user_id or source.job_id!=m['source_job_id']:raise ValueError('Linked export crosses owner or source')
        cursor=source;visited=set()
        while cursor.item_id!=original.item_id:
            if cursor.item_id in visited:raise ValueError('Linked export lineage cycle')
            visited.add(cursor.item_id);binding,_,_=_retry_binding(session,user_id,cursor.job_id)
            if binding['item_id']!=cursor.item_id:raise ValueError('Linked item edge differs')
            cursor=session.get(RenderItem,binding['source_item_id'])
        if _retry_identity(get_job(session,user_id,source.job_id),source)!=m['frozen_identity_sha256']:raise ValueError('Linked frozen source changed')
    if artifact.relative_path.lower().endswith('.zip'):_verify_ink_zip_members(stream,members)
    elif len(members)!=1 or members[0]['filename']!=Path(artifact.relative_path).name or members[0]['output_sha256']!=artifact.sha256 or members[0]['size_bytes']!=artifact.size_bytes:raise ValueError('Linked PNG differs')
    return record


def _verify_ink_zip_members(stream,members):
    import zipfile
    if not isinstance(members,list) or not members:raise ValueError('Missing frozen color export members')
    expected={m['filename']:m for m in members}
    if len(expected)!=len(members):raise ValueError('Duplicate frozen export member')
    with zipfile.ZipFile(stream) as archive:
        if len(archive.infolist())!=len(expected) or set(archive.namelist())!=set(expected):raise ValueError('Color ZIP membership changed')
        for name,member in expected.items():
            size=member['size_bytes']
            if type(size) is not int or not 0<size<=EXPORT_SINGLE_CAP or archive.getinfo(name).file_size!=size:raise ValueError('Color ZIP member size changed')
            h=hashlib.sha256();count=0
            with archive.open(name) as inp:
                while chunk:=inp.read(1024*1024):
                    count+=len(chunk)
                    if count>size:raise ValueError('Color ZIP member exceeded bound')
                    h.update(chunk)
            if count!=size or h.hexdigest()!=member['output_sha256']:raise ValueError('Color ZIP member checksum changed')


def verify_export_ink_mapping(session,user_id,job_id,artifact,stream):
    """Historical export authority; deliberately independent of current palettes."""
    from .selection_contract import frozen_ink_mode
    from .natural_layout_v1 import digest
    job=get_job(session,user_id,job_id)
    rows=session.query(AuditLog).filter(AuditLog.action=='export_ink_artifact_binding',
         AuditLog.actor==str(user_id),AuditLog.object_ref==artifact.artifact_id).all()
    modern=frozen_ink_mode(json.loads(job.style_snapshot or '{}'))=='source-solid-srgb-v1'
    if not rows:
        if modern:raise ValueError('Modern color export lacks its immutable authority mapping')
        return None
    if len(rows)!=1:raise ValueError('Conflicting historical color export authority')
    record=json.loads(rows[0].result)
    linked=record.get('schema')=='export-ink-artifact/2'
    if linked:
        if verify_export_linked_mapping(session,user_id,job_id,artifact,stream) is None:raise ValueError('Linked color export authority missing')
        stream.seek(0)
    expected={'schema':'export-ink-artifact/2' if linked else 'export-ink-artifact/1','artifact_id':artifact.artifact_id,'job_id':job_id,
              'relative_path':artifact.relative_path,'sha256':artifact.sha256,'size_bytes':artifact.size_bytes}
    if not isinstance(record,dict) or set(record)!=set(expected)|{'members'} or any(digest(record.get(k))!=digest(v) for k,v in expected.items()):raise ValueError('Historical export registry/authority differs')
    members=record['members']
    if not isinstance(members,list) or not members or any(not isinstance(m,dict) for m in members):raise ValueError('Invalid historical color membership')
    for m in members:
        from app.core.storage import check_segment
        if not isinstance(m.get('item_id'),str):raise ValueError('Invalid frozen export item identity')
        check_segment(m['item_id'],'item_id')
        if m.get('filename') != m['item_id']+'.png':raise ValueError('Frozen export filename differs from item identity')
        proof=m.get('ink_color_proof')
        if (set(m)!=({'item_id','render_version','filename','output_sha256','size_bytes','ink_color_proof','source_job_id'} if linked else {'item_id','render_version','filename','output_sha256','size_bytes','ink_color_proof'})
            or type(m['render_version']) is not int or m['render_version']<1
            or type(m['size_bytes']) is not int or not 0<m['size_bytes']<=EXPORT_SINGLE_CAP
            or not isinstance(m['filename'],str) or Path(m['filename']).name!=m['filename']
            or not isinstance(proof,dict) or proof.get('item_id')!=m['item_id']
            or proof.get('job_id')!=(m.get('source_job_id') if linked else job_id) or type(proof.get('owner_user_id')) is not int or proof['owner_user_id']!=user_id
            or type(proof.get('render_version')) is not int or proof['render_version']!=m['render_version']
            or proof.get('output_sha256')!=m['output_sha256']):raise ValueError('Invalid frozen color member proof')
        anchors=session.query(AuditLog).filter(AuditLog.action=='render_item_ink_output_binding',
             AuditLog.actor==str(user_id),AuditLog.object_ref==m['item_id']).all()
        if not any(digest(json.loads(a.result))==digest(proof) for a in anchors):raise ValueError('Export member has no original producer anchor')
    if artifact.relative_path.lower().endswith('.zip'):_verify_ink_zip_members(stream,members)
    elif len(members)!=1 or members[0]['filename']!=Path(artifact.relative_path).name or members[0]['output_sha256']!=artifact.sha256 or members[0]['size_bytes']!=artifact.size_bytes:raise ValueError('PNG export differs from frozen member')
    return record


def list_outputs(session: Session, user_id: int, job_id: str) -> dict:
    job = get_job(session, user_id, job_id)
    items = [
        {
            "item_id": i.item_id,
            "status": i.status,
            "render_version": i.render_version,
            "output_relative_path": i.output_relative_path,
            "reviewed_by": i.reviewed_by,
        }
        for i in existing_items(session, job_id)
    ]
    arts = [
        {"artifact_id": a.artifact_id, "relative_path": a.relative_path}
        for a in session.query(ExportArtifact).filter(ExportArtifact.job_id == job_id).all()
    ]
    return {"job_id": job_id, "items": items, "export_artifacts": arts}


def enqueue_job(job_id: str, draft: bool) -> None:
    """Put a queued job on its PRD section 5.7 lane: user jobs FIFO first,
    draft jobs FIFO with one guaranteed slot every DRAFT_FAIRNESS user
    dispatches (bounded starvation)."""
    with _done_lock:
        _done_events.setdefault(job_id, threading.Event())
    with _queue_cv:
        (_draft_queue if draft else _user_queue).append(job_id)
        _queue_cv.notify()


def wait_job_done(job_id: str, timeout: float = RENDER_WAIT_SECONDS) -> bool:
    """Block until the worker finished job_id (or timeout). Callers re-read
    the job from the DB afterwards either way."""
    with _done_lock:
        ev = _done_events.get(job_id)
    if ev is None:
        return True  # already completed and reaped
    return ev.wait(timeout)


def _next_job_id() -> str | None:
    global _dispatch_count
    with _queue_cv:
        while True:
            if _user_queue or _draft_queue:
                _dispatch_count += 1
                prefer_draft = bool(_draft_queue) and (
                    not _user_queue or _dispatch_count % DRAFT_FAIRNESS == 0
                )
                lane = _draft_queue if prefer_draft else _user_queue
                return lane.popleft()
            _queue_cv.wait(timeout=1.0)


def _execute_job(job_id: str) -> None:
    """Worker-side run: fresh session (sqlite single writer), full failure
    containment - the queue path never surfaces a 500 (G75-11)."""
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        job = s.get(RenderJob, job_id)
        if job is None or job.status != "queued":
            return
        run_job(s, job)
    except Exception as exc:  # noqa: BLE001 - containment is the point
        try:
            s.rollback()
            job = s.get(RenderJob, job_id)
            if job is not None:
                job.status = "failed"
                job.updated_at = _now()
                s.query(RenderItem).filter(
                    RenderItem.job_id == job_id,
                    RenderItem.status.in_(("pending", "running")),
                ).update(
                    {"status": "failed", "last_error": str(exc)[:400], "retryable": True, "updated_at": _now()},
                    synchronize_session=False,
                )
                audit(s, action="render_worker_error", actor="system", object_ref=job_id, result=str(exc)[:200])
                s.commit()
        except Exception:  # noqa: BLE001 - never let the worker die
            s.rollback()
    finally:
        s.close()
        with _done_lock:
            ev = _done_events.pop(job_id, None)
        if ev is not None:
            ev.set()


def queue_status() -> dict:
    """Read-only telemetry snapshot for GET /api/v1/workers/status (B1-06):
    dual-lane queue depths (PRD section 5.7), worker liveness and the last
    loop heartbeat. Never mutates queue state."""
    with _queue_cv:
        user_depth = len(_user_queue)
        draft_depth = len(_draft_queue)
    return {
        "worker_id": "render-worker-01",
        "alive": bool(_worker_thread is not None and _worker_thread.is_alive()),
        "queue_depth": user_depth + draft_depth,
        "user_queue_depth": user_depth,
        "draft_queue_depth": draft_depth,
        "max_concurrency": 1,  # single resident worker thread per process
        "last_heartbeat": _last_heartbeat,
    }


def _worker_loop() -> None:
    global _last_heartbeat
    while True:
        _last_heartbeat = _now()
        job_id = _next_job_id()
        if job_id is not None:
            _execute_job(job_id)


def start_worker() -> threading.Thread:
    """create_app() wiring: one resident daemon worker per process. Queued
    jobs left behind by a previous process are re-dispatched (section 7)."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return _worker_thread
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        stale = [j.job_id for j in s.query(RenderJob.job_id).filter(RenderJob.status == "queued").all()]
    finally:
        s.close()
    for job_id in stale:
        job = None
        s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
        try:
            job = s.get(RenderJob, job_id)
            draft = bool(job.draft) if job is not None else False
        finally:
            s.close()
        if job is not None:
            enqueue_job(job_id, draft)
    _worker_thread = threading.Thread(target=_worker_loop, name="yy-render-worker", daemon=True)
    _worker_thread.start()
    return _worker_thread


def recover_stale_running(session: Session) -> int:
    """Reconcile interrupted items and their job before the worker starts.

    Running jobs can be interrupted between item commits, including after an
    earlier recovery failed their items. Finalize from all committed outcomes;
    pending work in an interrupted job must also remain reachable by retry.
    Queued jobs keep their pending items for normal startup dispatch.
    """
    jobs = session.query(RenderJob).filter(RenderJob.status == "running").all()
    interrupted = {(job.job_id, job.user_id): job for job in jobs}
    stuck = session.query(RenderItem).filter(RenderItem.status == "running").all()
    if jobs:
        pending = session.query(RenderItem).filter(
            RenderItem.status == "pending",
            RenderItem.job_id.in_([job.job_id for job in jobs]),
        ).all()
        stuck.extend(item for item in pending if (item.job_id, item.user_id) in interrupted)
    for item in stuck:
        job = interrupted.get((item.job_id, item.user_id))
        if job is not None and job.cancel_requested:
            item.status = "cancelled"
        else:
            item.status = "failed"
            item.last_error = "WORKER_RESTARTED"
            item.retryable = True
        item.updated_at = _now()
        audit(session, action="worker_recovered", actor="system", object_ref=item.item_id, result=item.status)
    # Both item recovery and aggregate reconciliation commit atomically, even
    # with the worker/API's autoflush-disabled sessions.
    session.flush()
    for job in jobs:
        _finalize_job(session, job)
    session.commit()
    return len(stuck)


def cleanup_expired_drafts(session: Session) -> int:
    """Section 1.6 draft constraint 4: TTL batch cleanup (default 24h)."""
    now = datetime.now(timezone.utc)
    expired = session.query(RenderJob).filter(RenderJob.draft.is_(True), RenderJob.ttl_expires_at.isnot(None)).all()
    n = 0
    for job in expired:
        if datetime.fromisoformat(job.ttl_expires_at) <= now:
            session.query(RenderItem).filter(RenderItem.job_id == job.job_id).delete()
            session.delete(job)
            n += 1
    session.commit()
    return n


def job_dict(job: RenderJob) -> dict:
    try:
        snapshot = json.loads(job.style_snapshot or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        **({key:snapshot[key] for key in ('text_style','handwriting','v3_layout_binding') if key in snapshot}),
        "style_mode": snapshot.get("style_mode"),
        "style_id": snapshot.get("style_id"),
        "glyph_source_policy": snapshot.get("glyph_source_policy"),
        "layout_profile": snapshot.get("layout_profile"),
        "layout_seed": snapshot.get("layout_seed"),
        "layout_profile_sha256": snapshot.get("layout_profile_sha256"),
        **({"non_cjk_fonts": snapshot["non_cjk_fonts"], "non_cjk_font_binding": snapshot.get("non_cjk_font_binding")} if "non_cjk_fonts" in snapshot else {}),
        "ink_color_mode": snapshot.get("ink_color_mode","auto-contrast-v1"),
        **({"ink_color_rgb":snapshot['ink_color_rgb']} if 'ink_color_rgb' in snapshot else {}),
        "ink_palette_id": snapshot.get("ink_palette_id"),
        "ink_color_binding": snapshot.get("ink_color_binding"),
        "ink_color_binding_sha256": snapshot.get("ink_color_binding_sha256"),
        "job_id": job.job_id,
        "status": job.status,
        "draft": job.draft,
        "retry_of_job_id": job.retry_of_job_id,
        "ttl_expires_at": job.ttl_expires_at,
        "sign_blogger": job.sign_blogger,
        "blogger_name": job.blogger_name,
        "created_at": job.created_at,
    }


def item_dict(item: RenderItem, review_decision: str | None = None) -> dict:
    return {
        "item_id": item.item_id,
        "job_id": item.job_id,
        # Render status remains `completed` in storage; review decisions are
        # separate audit events. The HTTP contract may project that decision
        # for clients that need to resume a review session after reload.
        "status": review_decision or item.status,
        "cn": item.cn,
        "blessing": item.blessing,
        # P13 (2026-09-27): the item's EFFECTIVE template ref (per-row override
        # or the batch value) - lets clients verify the per-row image/template
        # mapping instead of inferring it.
        "template_id": item.template_id,
        "style_id": item.style_id,
        "render_version": item.render_version,
        "output_relative_path": item.output_relative_path,
        "fallback_used": item.fallback_used,
        "fallback_map": json.loads(item.fallback_map) if item.fallback_map else {},
        "warnings": json.loads(item.warnings) if item.warnings else [],
        "last_error": item.last_error,
        "retryable": item.retryable,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at,
        "comment": item.comment,
    }
