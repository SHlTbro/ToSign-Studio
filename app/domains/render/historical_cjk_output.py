"""Read an anchored completed image; this context grants no execution rights."""
import hashlib
import io
import json
from dataclasses import dataclass

from app.core.audit import AuditLog
from .natural_layout_v1 import digest

ACCEPTED_HISTORICAL_V3 = {
    'schema': 'accepted-v3-layout-binding/1',
    'profile_sha256': 'f2b7b809b9b7a356929922a591e65b161c90f720f14ea14e22980555eb6daaba',
    'implementation_sha256': 'b73689a45bbe55a96e6a40e4a6a7ee57f4bc192654d405fb3918cf36c6191352',
    'adapter_sha256': '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81',
}


def validate_v3_binding(snapshot):
    """Known signed geometry identity for anchored historical reads only."""
    from .v3_layout import binding, selected
    current = binding()
    frozen = snapshot.get('v3_layout_binding')
    if (not selected(snapshot)
            or (snapshot.get('personal_binding') or {}).get('m2_adapter', {}).get('sha256')
            != ACCEPTED_HISTORICAL_V3['adapter_sha256']
            or current['profile_sha256'] != ACCEPTED_HISTORICAL_V3['profile_sha256']
            or frozen not in (current, ACCEPTED_HISTORICAL_V3)):
        raise ValueError('Historical V3 geometry/model identity is unknown or changed')
    return dict(frozen)


@dataclass(frozen=True)
class EvidenceContext:
    requested: dict
    mixed_fonts: object
    v3_binding: object = None


def resolve(session, job, item, snapshot, *, output_bytes, sidecar_bytes,
            render_context, stable_binding):
    """Keep original producer identity while checking current owned source bytes.

    Only a matching server producer anchor authorizes this historical read.
    The caller still recomputes every character, layout and embedded proof.
    """
    from .glyph_provider import resolve_personal_binding
    from .mixed_fonts import resolve_frozen
    if item.status not in ('completed', 'completed_with_warning'):
        raise ValueError('Historical CJK output is not complete')
    old = snapshot.get('personal_binding')
    if (not isinstance(old, dict) or old.get('state') != 'bound'
            or old.get('engine_mode') != 'M2_ADAPTER'
            or type(old.get('style_id')) is not int
            or snapshot.get('style_id', old['style_id']) != old['style_id']):
        raise ValueError('Historical CJK source identity is invalid')
    current = resolve_personal_binding(session, job.user_id, old['style_id'])
    if current.get('state') != 'bound' or stable_binding(old) != stable_binding(current):
        raise ValueError('Historical CJK owned model/data/font identity changed')
    for binding in (old, current):
        owner = binding.get('deployment_user_id')
        if owner is not None and (type(owner) is not int or owner != job.user_id):
            raise ValueError('Historical CJK execution owner changed')
    if not isinstance(output_bytes, bytes) or not isinstance(sidecar_bytes, bytes):
        raise ValueError('Historical CJK evidence bytes are missing')
    rows = session.query(AuditLog).filter(
        AuditLog.action == 'render_item_cjk_output_binding',
        AuditLog.actor == str(job.user_id), AuditLog.object_ref == item.item_id,
    ).order_by(AuditLog.id).all()
    anchors = [json.loads(row.result) for row in rows]
    anchors = [a for a in anchors if isinstance(a, dict)
               and a.get('render_version') == item.render_version]
    if not anchors:
        raise ValueError('Historical CJK server producer anchor is missing')
    anchor = anchors[-1]
    expected = dict(schema='render-cjk-output-proof/1', owner_user_id=job.user_id,
                    job_id=job.job_id, item_id=item.item_id,
                    render_version=item.render_version,
                    glyph_source_policy='m2_cjk_required',
                    render_context_sha256=digest(render_context),
                    output_relative_path=item.output_relative_path,
                    output_sha256=hashlib.sha256(output_bytes).hexdigest(),
                    glyph_sidecar_relative_path=f'{job.user_id}/outputs/{job.job_id}/{item.item_id}_v{item.render_version}.glyphs.json',
                    glyph_sidecar_sha256=hashlib.sha256(sidecar_bytes).hexdigest())
    if any(digest(anchor.get(key)) != digest(value) for key, value in expected.items()):
        raise ValueError('Historical CJK producer bytes/context changed')
    from PIL import Image
    with Image.open(io.BytesIO(output_bytes)) as image:
        if image.format != 'PNG' or image.info.get('glyph_source_policy') != 'm2_cjk_required':
            raise ValueError('Historical CJK PNG source policy changed')
        embedded = json.loads(image.info.get('render_glyph_provenance', 'null'))
    if (not isinstance(embedded, dict)
            or digest(embedded) != anchor.get('embedded_provenance_sha256')
            or embedded.get('style_snapshot_sha256') != digest(snapshot)):
        raise ValueError('Historical CJK embedded source/context changed')
    requested = {key: old[key] for key in
                 ('style_id', 'artifact_id', 'engine_version', 'manifest_sha256')}
    from .v3_layout import selected
    v3_binding = validate_v3_binding(snapshot) if selected(snapshot) or 'v3_layout_binding' in snapshot else None
    return EvidenceContext(requested, resolve_frozen(snapshot), v3_binding)
