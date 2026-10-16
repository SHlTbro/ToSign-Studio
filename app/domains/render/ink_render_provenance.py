"""Mandatory, per-version source-solid evidence; no inference or model mutation.

The service must store each returned execution in its slot record, composite the
returned RGBA once, write the PNG and glyph sidecar, then call finalization in the
same database transaction that completes the item. Review/export call validation
under the service's job lock. An audit anchor prevents re-authoring filesystem
receipts before a new review. Existing clean automatic-color output is untouched.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from functools import wraps
from pathlib import Path, PurePosixPath

from PIL import Image

from app.core import storage
from app.core.audit import AuditLog, audit
from app.core.errors import ApiError
from sqlalchemy.exc import SQLAlchemyError
from app.domains.assets_upload.models import Asset
from . import glyph_provider, ink_authorization, ink_color_v1
from .natural_layout_v1 import digest

COLOR_KEYS = {'ink_color_mode', 'ink_palette_id', 'ink_color_binding', 'ink_color_binding_sha256'}
ANCHOR_ACTION = 'render_item_ink_output_binding'
RECEIPT_SCHEMA = 'render-ink-output/1'
PROOF_SCHEMA = 'render-ink-output-proof/1'


def _invalid_as_value_error(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, TypeError, KeyError, IndexError, AttributeError, RuntimeError, ApiError, SQLAlchemyError) as exc:
            raise ValueError('malformed or unavailable ink provenance: ' + str(exc)) from exc
    return checked


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf8')


def _same(left, right):
    # Python's == aliases True/1 and 1/1.0; immutable JSON identities must not.
    return _json_bytes(left) == _json_bytes(right)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate provenance JSON field')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite provenance JSON')))


def _mode(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('frozen render selection is not an object')
    from .selection_contract import frozen_ink_mode
    if frozen_ink_mode(snapshot) == 'custom-solid-srgb-v1':return None
    present = COLOR_KEYS.intersection(snapshot)
    if not present:
        return None
    mode = snapshot.get('ink_color_mode')
    if mode == ink_color_v1.AUTO_MODE:
        if any(snapshot.get(k) is not None for k in COLOR_KEYS - {'ink_color_mode'}):
            raise ValueError('automatic ink cannot carry modern palette authority')
        return None
    if mode != ink_color_v1.SOURCE_MODE or present != COLOR_KEYS:
        raise ValueError('missing, partial or unsupported frozen ink selection')
    if (not isinstance(snapshot['ink_palette_id'], str) or not snapshot['ink_palette_id']
            or not isinstance(snapshot['ink_color_binding'], dict)
            or snapshot['ink_color_binding_sha256'] != digest(snapshot['ink_color_binding'])):
        raise ValueError('frozen ink identity or digest differs')
    return mode


def _identity(job, item):
    if (type(job.user_id) is not int or job.user_id < 1 or type(item.user_id) is not int or item.user_id != job.user_id
            or item.job_id != job.job_id or type(item.render_version) is not int or item.render_version < 1):
        raise ValueError('foreign or malformed render item identity')
    for label, value in (('job', job.job_id), ('item', item.item_id)):
        storage.check_segment(value, label)
    return {'owner_user_id': job.user_id, 'job_id': job.job_id, 'item_id': item.item_id,
            'render_version': item.render_version}


def _relative(job, item, kind, index=None):
    _identity(job, item)
    base = PurePosixPath(str(job.user_id)) / 'outputs' / job.job_id
    stem = f'{item.item_id}_v{item.render_version}'
    if kind in ('layout', 'colored'):
        if type(index) is not int or index < 0:
            raise ValueError('invalid slot evidence index')
        return (base / (stem + '.ink') / f'slot-{index:04d}.{kind}.png').as_posix()
    suffix = {'output': '.png', 'glyph': '.glyphs.json', 'receipt': '.ink.json'}[kind]
    return (base / (stem + suffix)).as_posix()


def _path(rel):
    # Reject even contained symbolic indirection. No external path is an API input.
    if (not isinstance(rel, str) or not rel or '\\' in rel or ':' in rel
            or any(p in ('', '.', '..') for p in rel.split('/')) or rel.startswith('/')):
        raise ValueError('invalid relative ink evidence path')
    # Use the same server-owned root as the compositor (also its test seam).
    from .service import storage_root
    root = Path(storage_root()).absolute()
    candidate = root
    for part in rel.split('/'):
        storage.check_segment(part, 'evidence segment')
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('indirect ink evidence path')
    return storage.ensure_within_root(candidate, root)


def _read(rel):
    path = _path(rel)
    if not path.is_file():
        raise ValueError('mandatory ink evidence is missing')
    return path.read_bytes()


def _write_once(rel, raw, user_id):
    path = _path(rel)
    with storage.QUOTA_LOCK:
        if path.exists():
            if not path.is_file() or path.read_bytes() != raw:
                raise ValueError('immutable version already has different ink evidence')
            return
        storage.enforce_quota(user_id, len(raw))
        storage.atomic_write_bytes(path, raw)


def _png(image):
    target = io.BytesIO()
    image.save(target, format='PNG')
    return target.getvalue()


def _image(raw, mode, *, png=False):
    with Image.open(io.BytesIO(raw)) as opened:
        if png and opened.format != 'PNG':
            raise ValueError('ink evidence must be PNG')
        opened.load()
        if png and opened.mode != mode:
            raise ValueError('ink evidence pixel mode changed')
        return opened.convert(mode)


def _qualified(session, job, item, glyph_session=None):
    _identity(job, item)
    snapshot = _json(job.style_snapshot)
    if _mode(snapshot) is None:
        return snapshot, None
    if (snapshot.get('layout_profile') != 'natural-layout-v1'
            or snapshot.get('glyph_source_policy') != 'm2_required'
            or snapshot.get('source') != 'M2_ADAPTER'
            or type(snapshot.get('layout_seed')) is not int or not 0 <= snapshot['layout_seed'] <= 0xffffffff
            or not isinstance(snapshot.get('layout_binding'), dict)):
        raise ValueError('source-solid ink requires frozen natural layout and M2')
    personal = snapshot.get('personal_binding')
    if (not isinstance(personal, dict) or type(personal.get('style_id')) is not int
            or snapshot.get('style_id') != personal['style_id']):
        raise ValueError('missing exact owned personal style identity')
    resolved = glyph_provider.resolve_personal_binding(session, job.user_id, personal['style_id'])
    keys = ('state', 'style_id', 'artifact_id', 'engine_mode', 'engine_version', 'manifest_sha256', 'runtime_binding')
    if (resolved.get('state') != 'bound' or resolved.get('engine_mode') != 'M2_ADAPTER'
            or any(not _same(personal.get(k), resolved.get(k)) for k in keys)):
        raise ValueError('current personal artifact differs from frozen color render')
    if glyph_session is not None:
        if (glyph_session.user_id != job.user_id or glyph_session.job_id != job.job_id
                or glyph_session.output_blocked or glyph_session.source_policy != 'm2_required'
                or any(not _same(glyph_session.resolved.get(k), resolved.get(k)) for k in keys)):
            raise ValueError('composing glyph session differs from current owned artifact')
    qualified = ink_authorization.resolve_author_palette(
        job.user_id, resolved, snapshot['ink_palette_id'], layout_profile=snapshot['layout_profile'],
        render_source_policy=snapshot['glyph_source_policy'], frozen=snapshot['ink_color_binding'], session=session)
    binding = qualified['binding']
    if (not _same(binding, snapshot['ink_color_binding'])
            or binding.get('natural_layout_binding_sha256') != digest(snapshot['layout_binding'])
            or snapshot.get('layout_profile_sha256') != snapshot['layout_binding'].get('profile_sha256')
            or ink_color_v1.validate_rgb(qualified['rgb']) != ink_color_v1.validate_rgb(binding.get('rgb'))):
        raise ValueError('current palette and frozen layout authority differ')
    return snapshot, qualified


def _plan(job, item):
    root = _json(job.template_snapshot)
    if not isinstance(root, dict):
        raise ValueError('invalid frozen template')
    overrides = root.get('item_overrides', {})
    if not isinstance(overrides, dict):
        raise ValueError('invalid frozen item overrides')
    items, templates = overrides.get('items', {}), overrides.get('templates', {})
    if not isinstance(items, dict) or not isinstance(templates, dict):
        raise ValueError('invalid frozen item/template override maps')
    override = items.get(item.item_id, {})
    if not isinstance(override, dict):
        raise ValueError('invalid frozen item override')
    if override.get('template_id') is not None and override['template_id'] not in templates:
        raise ValueError('missing frozen item template override')
    template = templates[override['template_id']] if override.get('template_id') else root
    if not isinstance(template, dict):
        raise ValueError('invalid frozen effective template')
    signed = override.get('sign_blogger', job.sign_blogger)
    if type(signed) is not bool:
        raise ValueError('invalid frozen signature selection')
    blogger = job.blogger_name if signed else None
    regions = _json(item.region_override) if item.region_override else {}
    if not isinstance(regions, dict) or not isinstance(template.get('slots'), list):
        raise ValueError('invalid frozen slot plan')
    # Use the service's exact whitespace/signature expansion, without inference.
    from .service import _expand_slot_text
    slots = []
    for index, slot in enumerate(template['slots']):
        if not isinstance(slot, dict) or not isinstance(slot.get('key'), str):
            raise ValueError('invalid frozen slot identity')
        text = _expand_slot_text(slot, item.cn, item.blessing, blogger)
        region = regions.get(slot['key'], slot.get('region', {}))
        if not isinstance(text, str) or not isinstance(region, dict):
            raise ValueError('invalid slot text or region')
        slots.append({'slot_index': index, 'slot_key': slot['key'], 'text': text, 'region': region,
                      'skip_reason': ('signature_disabled' if slot['key'] == 'signature' and not blogger
                                      else 'empty_text' if not text else 'blank_only') if not text.strip() else None})
    return template, slots


def _context(job, item, snapshot):
    return {**_identity(job, item), 'style_snapshot_sha256': digest(snapshot),
            'template_snapshot_sha256': digest(_json(job.template_snapshot)),
            'item_input_sha256': digest({'cn': item.cn, 'blessing': item.blessing,
                'region_override': _json(item.region_override) if item.region_override else None,
                'sign_blogger': job.sign_blogger, 'blogger_name': job.blogger_name,
                'base_asset_id': job.base_asset_id, 'base_checksum': job.base_checksum})}


def _background(session, job, item, asset=None):
    template, _ = _plan(job, item)
    asset_id = template.get('base_asset_id') or job.base_asset_id
    current = session.query(Asset).filter(Asset.asset_id == asset_id).populate_existing().one_or_none()
    if (current is None or str(current.user_id) != str(job.user_id) or current.enabled != 1
            or current.status != 'approved' or (asset is not None and asset.asset_id != current.asset_id)):
        raise ValueError('current background is unavailable or foreign')
    rel = current.relative_path
    if not isinstance(rel, str) or rel.split('/')[:2] != [str(job.user_id), 'assets']:
        raise ValueError('background path is not in the owner asset pool')
    raw = _read(rel)
    background = _image(raw, 'RGB')
    if (_sha(raw) != current.sha256 or len(raw) != current.bytes
            or list(background.size) != [current.width, current.height]
            or (asset_id == job.base_asset_id and current.sha256 != job.base_checksum)):
        raise ValueError('background bytes or dimensions differ from owned asset identity')
    identity = {'asset_id': current.asset_id, 'owner_user_id': job.user_id, 'relative_path': rel,
                'file_sha256': _sha(raw), 'file_bytes': len(raw), 'decoded_mode': 'RGB',
                'decoded_dimensions': list(background.size), 'decoded_rgb_sha256': _sha(background.tobytes())}
    return background, identity


def _placement(plan, background):
    region = plan['region']
    values = [region.get('x_norm', 0), region.get('y_norm', 0), region.get('w_norm', .5), region.get('h_norm', .2)]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('nonfinite slot placement')
    return [int(values[0] * background.width), int(values[1] * background.height)]


def _file_evidence(job, item, index, kind, raw, image):
    return {'relative_path': _relative(job, item, kind, index), 'file_sha256': _sha(raw),
            'file_bytes': len(raw), 'mode': image.mode, 'dimensions': list(image.size),
            'pixels_sha256': _sha(image.tobytes()), 'alpha_sha256': _sha(image.getchannel('A').tobytes())}


def _execution(job, item, snapshot, qualified, background_identity, canvas, original, colored, xy, plan, layouts):
    again, primitive = ink_color_v1.recolor_strip(original, qualified['rgb'])
    if again.tobytes() != colored.tobytes() or again.size != colored.size:
        raise ValueError('colored strip is not the exact alpha-preserving solid assignment')
    ink_color_v1.validate_placement(canvas, colored, xy)
    after = canvas.copy()
    after.paste(colored, tuple(xy), colored)
    return {**primitive, **_identity(job, item),
            'slot_index': plan['slot_index'], 'slot_key': plan['slot_key'], 'text_sha256': _sha(plan['text'].encode('utf8')),
            'render_context_sha256': digest(_context(job, item, snapshot)),
            'ink_palette_id': snapshot['ink_palette_id'], 'ink_color_binding': qualified['binding'],
            'ink_color_binding_sha256': digest(qualified['binding']),
            'background': background_identity, 'placement_xy': list(xy),
            'surface_before_rgb_sha256': _sha(canvas.tobytes()), 'surface_after_rgb_sha256': _sha(after.tobytes()),
            'surface_dimensions': list(canvas.size), 'layout_execution_sha256': digest(layouts),
            'local_background_diagnostics': ink_color_v1.background_diagnostics(canvas, colored, xy)}, after


@_invalid_as_value_error
def apply_source_ink(session, job, item, glyph_session, background_asset, canvas_RGB, strip_RGBA, xy, slot_index, slot_key):
    """Persist original/colored strips; return color operation and strict evidence.

    Caller performs exactly one paste. This function never modifies the canvas.
    """
    snapshot, qualified = _qualified(session, job, item, glyph_session)
    if qualified is None:
        raise ValueError('source-solid operation requested for automatic ink')
    background, background_identity = _background(session, job, item, background_asset)
    _, plans = _plan(job, item)
    if type(slot_index) is not int or not 0 <= slot_index < len(plans):
        raise ValueError('unknown frozen slot')
    plan = plans[slot_index]
    if plan['slot_key'] != slot_key or plan['skip_reason'] is not None or list(xy) != _placement(plan, background):
        raise ValueError('slot identity, text or placement differs from frozen plan')
    if not isinstance(canvas_RGB, Image.Image) or canvas_RGB.mode != 'RGB' or canvas_RGB.size != background.size:
        raise ValueError('actual RGB surface differs from background dimensions')
    layouts = [entry for entry in glyph_session.layout_executions if entry.get('slot_index') == slot_index]
    if len(layouts) != 1 or layouts[0].get('slot_key') != slot_key:
        raise ValueError('source-solid slot requires its existing natural-layout execution')
    colored, _ = ink_color_v1.recolor_strip(strip_RGBA, qualified['rgb'])
    execution, _ = _execution(job, item, snapshot, qualified, background_identity, canvas_RGB,
                              strip_RGBA, colored, xy, plan, layouts)
    for kind, image in (('layout', strip_RGBA), ('colored', colored)):
        raw = _png(image)
        evidence = _file_evidence(job, item, slot_index, kind, raw, image)
        _write_once(evidence['relative_path'], raw, job.user_id)
        execution[kind + '_strip'] = evidence
    return colored, execution


def _range(value, length, start):
    if (not isinstance(value, list) or len(value) != 2 or any(type(v) is not int for v in value)
            or value[0] != start or not start <= value[1] <= length):
        raise ValueError('missing, overlapping or incomplete slot provenance range')
    return value[1]


def _source_meta(meta, job, item, snapshot):
    personal = snapshot['personal_binding']
    expected = {'schema': 'render-glyph-meta/1', 'job_id': job.job_id, 'item_id': item.item_id,
                'render_version': item.render_version, 'mode': 'personal', 'style_id': snapshot['style_id'],
                'glyph_source_policy': 'm2_required', 'layout_profile': 'natural-layout-v1',
                'layout_seed': snapshot['layout_seed'], 'layout_binding': snapshot['layout_binding'],
                'outcome': 'rendered', 'blocked_reason': None, 'fallback_count': 0, 'attempted_fallback_count': 0}
    if not isinstance(meta, dict) or any(not _same(meta.get(k), v) for k, v in expected.items()):
        raise ValueError('glyph sidecar does not describe the exact current completed render')
    if type(meta.get('render_version')) is not int:
        raise ValueError('invalid sidecar render version')
    requested = {k: personal.get(k) for k in ('style_id', 'artifact_id', 'engine_version', 'manifest_sha256')}
    resolved = {k: personal.get(k) for k in ('artifact_id', 'engine_mode', 'engine_version', 'manifest_sha256', 'runtime_binding')}
    if (not _same(meta.get('requested_binding'), requested) or not isinstance(meta.get('resolved'), dict)
            or any(not _same(meta['resolved'].get(k), v) for k, v in resolved.items()) or meta['resolved'].get('problems')):
        raise ValueError('glyph sidecar source is foreign or stale')
    if any(not isinstance(meta.get(k), list) for k in ('chars', 'layout_executions', 'slot_executions')):
        raise ValueError('missing complete slot/source/layout provenance')


def _reconstruct(session, job, item, snapshot, qualified, output_bytes=None):
    glyph_rel, output_rel = _relative(job, item, 'glyph'), _relative(job, item, 'output')
    if item.output_relative_path != output_rel:
        raise ValueError('output pointer differs from exact owned item version')
    glyph_raw = _read(glyph_rel)
    meta = _json(glyph_raw)
    _source_meta(meta, job, item, snapshot)
    background, background_identity = _background(session, job, item)
    canvas = background.copy()
    _, plans = _plan(job, item)
    if len(meta['slot_executions']) != len(plans):
        raise ValueError('partial or extra visible-slot provenance')
    chars, layouts = meta['chars'], meta['layout_executions']
    if any(not isinstance(entry, dict) for entry in chars + layouts):
        raise ValueError('malformed character or layout provenance entry')
    char_end = layout_end = 0
    executions = []
    for plan, slot in zip(plans, meta['slot_executions']):
        if not isinstance(slot, dict) or any(not _same(slot.get(k), plan[k]) for k in ('slot_index', 'slot_key', 'text')):
            raise ValueError('slot provenance differs from the frozen item')
        next_char = _range(slot.get('char_record_range'), len(chars), char_end)
        next_layout = _range(slot.get('layout_execution_range'), len(layouts), layout_end)
        slot_chars, slot_layouts = chars[char_end:next_char], layouts[layout_end:next_layout]
        char_end, layout_end = next_char, next_layout
        if plan['skip_reason'] is not None:
            if (slot.get('outcome') != 'skipped' or slot.get('reason') != plan['skip_reason']
                    or 'ink_color_execution' in slot or slot_chars or slot_layouts):
                raise ValueError('skipped slot has incomplete or unexpected rendering evidence')
            continue
        if slot.get('outcome') != 'composed' or not isinstance(slot.get('ink_color_execution'), dict):
            raise ValueError('visible slot lacks mandatory color execution')
        if [r.get('char') for r in slot_chars] != [ch for ch in plan['text'] if not ch.isspace()]:
            raise ValueError('visible slot character source records changed')
        for record in slot_chars:
            if (record.get('source') != 'M2' or record.get('reason') or record.get('artifact_id') != snapshot['personal_binding']['artifact_id']
                    or record.get('version') != snapshot['personal_binding']['engine_version']
                    or any(not _same(record.get(k), plan[k]) for k in ('slot_index', 'slot_key'))):
                raise ValueError('visible slot contains foreign or downgraded glyph source')
        if len(slot_layouts) != 1:
            raise ValueError('visible slot lacks its complete natural layout')
        layout = slot_layouts[0]
        if (layout.get('schema') != 'natural-layout-paragraph/1' or layout.get('profile') != 'natural-layout-v1'
                or layout.get('seed') != snapshot['layout_seed'] or layout.get('original_text') != plan['text']
                or layout.get('profile_sha256') != snapshot['layout_profile_sha256']
                or any(not _same(layout.get(k), plan[k]) for k in ('slot_index', 'slot_key'))):
            raise ValueError('visible slot natural-layout identity differs')
        stored = slot['ink_color_execution']
        images, files = {}, {}
        for kind in ('layout', 'colored'):
            rel = _relative(job, item, kind, plan['slot_index'])
            raw = _read(rel)
            images[kind] = _image(raw, 'RGBA', png=True)
            files[kind + '_strip'] = _file_evidence(job, item, plan['slot_index'], kind, raw, images[kind])
        if layout.get('output_size') != list(images['layout'].size):
            raise ValueError('original post-layout strip dimensions differ')
        xy = _placement(plan, canvas)
        expected, canvas = _execution(job, item, snapshot, qualified, background_identity, canvas,
                                       images['layout'], images['colored'], xy, plan, slot_layouts)
        expected.update(files)
        if not _same(stored, expected):
            raise ValueError('slot color, alpha, background, placement or surface evidence changed')
        executions.append(expected)
    if char_end != len(chars) or layout_end != len(layouts) or not executions:
        raise ValueError('missing or orphan source/layout/color evidence')
    raw = _read(output_rel)
    if output_bytes is not None and (not isinstance(output_bytes, bytes) or output_bytes != raw):
        raise ValueError('export bytes differ from current stored output')
    output = _image(raw, 'RGB', png=True)
    if output.size != canvas.size or output.tobytes() != canvas.tobytes():
        raise ValueError('output is not the exact one-coverage RGB reconstruction')
    return {'schema': RECEIPT_SCHEMA, **_context(job, item, snapshot),
            'provenance_implementation_sha256': _sha(Path(__file__).read_bytes()),
            'ink_color_mode': ink_color_v1.SOURCE_MODE, 'ink_palette_id': snapshot['ink_palette_id'],
            'ink_color_binding': qualified['binding'], 'binding_sha256': digest(qualified['binding']),
            'background': background_identity, 'glyph_sidecar_relative_path': glyph_rel,
            'glyph_sidecar_sha256': _sha(glyph_raw), 'output_relative_path': output_rel,
            'output_sha256': _sha(raw), 'output_rgb_sha256': _sha(output.tobytes()),
            'output_dimensions': list(output.size), 'slot_executions': executions,
            'one_coverage_reconstruction_verified': True, 'readability_pass': None}


def _proof(job, item, receipt, sidecar_raw):
    return {'schema': PROOF_SCHEMA, **_identity(job, item), 'binding_sha256': receipt['binding_sha256'],
            'sidecar_relative_path': _relative(job, item, 'receipt'), 'sidecar_sha256': _sha(sidecar_raw),
            'glyph_sidecar_sha256': receipt['glyph_sidecar_sha256'], 'output_sha256': receipt['output_sha256'],
            'output_rgb_sha256': receipt['output_rgb_sha256'], 'render_context_sha256': digest(_context(job, item, _json(job.style_snapshot)))}


def _anchors(session, job, item):
    rows = session.query(AuditLog).filter(AuditLog.action == ANCHOR_ACTION, AuditLog.actor == str(job.user_id),
                                         AuditLog.object_ref == item.item_id).order_by(AuditLog.id).all()
    result = []
    required = {'schema', 'owner_user_id', 'job_id', 'item_id', 'render_version', 'binding_sha256',
                'sidecar_relative_path', 'sidecar_sha256', 'glyph_sidecar_sha256', 'output_sha256',
                'output_rgb_sha256', 'render_context_sha256'}
    for row in rows:
        value = _json(row.result)
        if (not isinstance(value, dict) or set(value) != required or value.get('schema') != PROOF_SCHEMA
                or type(value.get('render_version')) is not int or value['render_version'] < 1
                or type(value.get('owner_user_id')) is not int
                or any(not _same(value.get(k), v) for k, v in {
                    'owner_user_id': job.user_id, 'job_id': job.job_id, 'item_id': item.item_id}.items())
                or not isinstance(value.get('sidecar_relative_path'), str)):
            raise ValueError('invalid server ink anchor')
        for key in required - {'schema', 'owner_user_id', 'job_id', 'item_id', 'render_version', 'sidecar_relative_path'}:
            field = value[key]
            if not isinstance(field, str) or len(field) != 64 or any(c not in '0123456789abcdef' for c in field):
                raise ValueError('invalid server ink anchor digest')
        if value['render_version'] == item.render_version:
            result.append(value)
    return result


def _has_modern(value, custom_rgb=None):
    if isinstance(value, dict):
        execution=value.get('ink_color_execution')
        valid_custom=(custom_rgb is not None and isinstance(execution,dict)
            and execution.get('ink_color_mode')=='custom-solid-srgb-v1'
            and execution.get('rgb')==custom_rgb and execution.get('alpha_preserved') is True)
        if ('ink_color_execution' in value and not valid_custom or 'ink_color_binding' in value and value['ink_color_binding'] is not None
                or value.get('ink_color_mode') not in (None, ink_color_v1.AUTO_MODE, *(['custom-solid-srgb-v1'] if custom_rgb is not None else []))
                or value.get('ink_palette_id') is not None or value.get('ink_color_binding_sha256') is not None):
            return True
        if value.get('ink_color_mode')=='custom-solid-srgb-v1' and value.get('rgb')!=custom_rgb:return True
        return any(_has_modern(child,custom_rgb) for child in value.values())
    return isinstance(value, list) and any(_has_modern(child,custom_rgb) for child in value)


def _legacy_guard(session, job, item, custom_rgb=None):
    if (_path(_relative(job, item, 'receipt')).exists()
            or _path(_relative(job, item, 'layout', 0)).parent.exists() or _anchors(session, job, item)):
        raise ValueError('modern ink evidence cannot downgrade to automatic ink')
    glyph = _path(_relative(job, item, 'glyph'))
    if glyph.exists() and _has_modern(_json(glyph.read_bytes()),custom_rgb):
        raise ValueError('partial modern color evidence cannot use legacy approval')


@_invalid_as_value_error
def finalize_ink_output(session, job, item):
    """Verify and atomically persist mandatory receipt plus a DB audit anchor.

    Does not commit. A failure must fail the item and clear its output pointer.
    """
    snapshot, qualified = _qualified(session, job, item)
    if qualified is None:
        _legacy_guard(session, job, item, snapshot.get('ink_color_rgb') if snapshot.get('ink_color_mode')=='custom-solid-srgb-v1' else None)
        return None
    receipt = _reconstruct(session, job, item, snapshot, qualified)
    raw = _json_bytes(receipt)
    proof = _proof(job, item, receipt, raw)
    anchors = _anchors(session, job, item)
    if anchors and not _same(anchors, [proof]):
        raise ValueError('immutable render version has a different server ink anchor')
    _write_once(_relative(job, item, 'receipt'), raw, job.user_id)
    if not anchors:
        audit(session, action=ANCHOR_ACTION, actor=str(job.user_id), object_ref=item.item_id,
              result=_json_bytes(proof).decode('utf8'))
        session.flush()
    return proof


@_invalid_as_value_error
def validate_ink_output(session, job, item, output_bytes=None):
    """Return canonical current proof, None only for clean legacy/default ink.

    Raises on missing, partial, foreign, stale or tampered modern evidence.
    File ownership/hashes, exact source/layout identity, every original alpha,
    constant color, placement and every composited pixel are independently read.
    """
    snapshot, qualified = _qualified(session, job, item)
    if snapshot.get('ink_color_mode') == 'custom-solid-srgb-v1':
        from .custom_ink import validate_custom_output
        validate_custom_output(snapshot,output_bytes if output_bytes is not None else _read(_relative(job,item,'output')))
    if qualified is None:
        _legacy_guard(session, job, item, snapshot.get('ink_color_rgb') if snapshot.get('ink_color_mode')=='custom-solid-srgb-v1' else None)
        return None
    if item.status not in ('completed', 'completed_with_warning'):
        raise ValueError('source-solid output is not a completed current item')
    receipt = _reconstruct(session, job, item, snapshot, qualified, output_bytes)
    raw = _read(_relative(job, item, 'receipt'))
    if _json(raw) != receipt or raw != _json_bytes(receipt):
        raise ValueError('mandatory ink sidecar differs from current rendered evidence')
    proof = _proof(job, item, receipt, raw)
    if not _same(_anchors(session, job, item), [proof]):
        raise ValueError('missing or changed server-authoritative ink output anchor')
    return proof
