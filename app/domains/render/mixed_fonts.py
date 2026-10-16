"""Opt-in font identities for the existing Han-only M2 policy. No model access."""
import hashlib
import os
import unicodedata
from pathlib import Path

from app.core.errors import ApiError


LEGACY_ROUTING = 'latin-letters/1'
HANDWRITTEN_DIGIT_ROUTING = 'latin-letters-ascii-digits/2'


def font_role(char, binding=None):
    # ASCII digits share the selected handwriting face; punctuation keeps its own face.
    policy = HANDWRITTEN_DIGIT_ROUTING if binding is None else binding.get('routing_policy', LEGACY_ROUTING)
    if policy not in (LEGACY_ROUTING, HANDWRITTEN_DIGIT_ROUTING):
        raise ValueError('Unknown frozen font routing policy')
    digit = policy == HANDWRITTEN_DIGIT_ROUTING and char in '0123456789'
    return 'latin' if digit or ('LATIN' in unicodedata.name(char, '') and unicodedata.category(char).startswith('L')) else 'other'


def _catalog():
    from app.domains.font_presets.service import _read_catalog, _font_path
    root = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    rows = {
        'system-segoe-print': {'name': 'Segoe Print', 'path': root / 'segoepr.ttf',
                              'roles': ['latin'], 'license': 'Installed Windows font; no redistribution'},
        'system-simkai': {'name': 'Windows KaiTi', 'path': root / 'simkai.ttf',
                         'roles': ['other'], 'license': 'Installed Windows font; no redistribution'},
    }
    manifest, _ = _read_catalog()
    for font_id, entry in manifest.items():
        # Require the existing license document and declared exact font bytes.
        try:
            path = _font_path(entry['file_name'])
            if not _font_path(entry['license_file']).is_file():
                continue
            declared = entry['sha256']
            if hashlib.sha256(path.read_bytes()).hexdigest() != declared:
                continue
            rows[font_id] = {'name': entry.get('name') or font_id, 'path': path,
                             'roles': entry.get('font_roles', ['latin', 'other']), 'license': entry['license'],
                             **{key: entry[key] for key in ('face_index', 'variation_axes', 'required_cmap') if key in entry}}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def capabilities():
    return {'schema': 'non-cjk-font-catalog/1', 'fonts': [
        {'id': key, 'name': row['name'], 'roles': row['roles'], 'available': row['path'].is_file()}
        for key, row in _catalog().items()
    ], 'latin_scope': 'Latin letters and ASCII digits 0-9; other non-Han characters use the other font',
       'routing_policy': HANDWRITTEN_DIGIT_ROUTING,
       'missing_requested_fonts': [] if 'preset-caveat-600' in _catalog() else ['Caveat'], 'model_acceptance': False}


def freeze_fonts(selection, *, routing_policy=HANDWRITTEN_DIGIT_ROUTING):
    if routing_policy not in (LEGACY_ROUTING, HANDWRITTEN_DIGIT_ROUTING):
        raise ValueError('Unknown frozen font routing policy')
    if (not isinstance(selection, dict) or set(selection) != {'latin', 'other'}
            or any(not isinstance(v, str) or not v for v in selection.values())):
        raise ApiError(400, 'INVALID_REQUEST', 'Choose explicit Latin and other-character fonts')
    rows = _catalog()
    fonts = {}
    paths = {}
    for role, font_id in selection.items():
        row = rows.get(font_id)
        if row is None or role not in row['roles'] or not row['path'].is_file():
            raise ApiError(409, 'NON_CJK_FONT_UNAVAILABLE', 'Selected non-personal font unavailable', font_id, False)
        try:
            from fontTools.ttLib import TTFont, TTLibError
            face = row.get('face_index', 0)
            if type(face) is not int or face < 0:
                raise ValueError('Invalid font collection face')
            with TTFont(str(row['path']), fontNumber=face, lazy=True) as font:
                if not font.getBestCmap():
                    raise ValueError('Font coverage unavailable')
                for codepoint, glyph in row.get('required_cmap', {}).items():
                    if font.getBestCmap().get(int(codepoint.removeprefix('U+'), 16)) != glyph:
                        raise ValueError('Required font glyph identity differs')
                axes = row.get('variation_axes', {})
                if axes:
                    available = {axis.axisTag: axis for axis in font['fvar'].axes}
                    for tag, value in axes.items():
                        if tag not in available or not available[tag].minValue <= value <= available[tag].maxValue:
                            raise ValueError('Invalid font variation')
            font_sha = hashlib.sha256(row['path'].read_bytes()).hexdigest()
        except (OSError, ValueError, TypeError, TTLibError) as exc:
            raise ApiError(409, 'NON_CJK_FONT_UNAVAILABLE', 'Selected font cannot be verified', font_id, False) from exc
        fonts[role] = {'id': font_id, 'name': row['name'], 'sha256': font_sha, 'license': row['license'],
                       **{key: row[key] for key in ('face_index', 'variation_axes', 'required_cmap') if key in row}}
        if font_id in ('preset-caveat-600', 'preset-noto-cjk-sc-face2'):
            from app.core.font_shaping import identity
            fonts[role]['shaping_identity'] = identity()
            if not fonts[role]['shaping_identity']['ready']:
                raise ApiError(409, 'NON_CJK_FONT_UNAVAILABLE', 'V3 whole-span font shaping unavailable', font_id, False)
        paths[role] = str(row['path'])
    binding = {'schema': 'non-cjk-font-binding/1', 'fonts': fonts}
    if routing_policy != LEGACY_ROUTING:
        binding['routing_policy'] = routing_policy
    return binding, paths


def resolve_frozen(snapshot):
    selection = snapshot.get('non_cjk_fonts')
    if selection is None:
        if 'non_cjk_font_binding' in snapshot:
            raise ValueError('Partial frozen font selection')
        return None
    if snapshot.get('glyph_source_policy') != 'm2_cjk_required':
        raise ValueError('Explicit mixed fonts require the Han-only policy')
    frozen = snapshot.get('non_cjk_font_binding')
    if not isinstance(frozen, dict):
        raise ValueError('Missing frozen font binding')
    binding, paths = freeze_fonts(selection, routing_policy=frozen.get('routing_policy', LEGACY_ROUTING))
    if snapshot.get('non_cjk_font_binding') != binding:
        raise ValueError('Selected non-personal font bytes or identity changed')
    return binding, paths


def char_missing(path, char, identity=None):
    from app.domains.render.service import _cmap_for
    cmap = _cmap_for(path, (identity or {}).get('face_index', 0))
    return cmap is None or ord(char) not in cmap


def load_font(path, size, identity):
    """The renderer consumes the frozen face and axes, never OS family fallback."""
    from PIL import ImageFont
    font = ImageFont.truetype(path, size=size, index=identity.get('face_index', 0))
    axes = identity.get('variation_axes')
    if axes:
        from fontTools.ttLib import TTFont
        with TTFont(path, fontNumber=identity.get('face_index', 0), lazy=True) as source:
            font.set_variation_by_axes([axes.get(axis.axisTag, axis.defaultValue) for axis in source['fvar'].axes])
    return font
