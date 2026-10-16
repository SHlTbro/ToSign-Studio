"""V3 geometry from the accepted sidecar; fonts and M2 cells remain verified producers."""
import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageChops
from app.core.text_style import validate_text_style, validate_handwriting
from .mixed_fonts import load_font, char_missing, font_role

PROFILE = Path(__file__).with_name('v3_layout_profile.json')
PAIR = {'latin': 'preset-caveat-600', 'other': 'preset-noto-cjk-sc-face2'}
ADAPTER_SHA = '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'


def profile():
    return json.loads(PROFILE.read_text(encoding='utf-8'))


def selected(snapshot):
    return snapshot.get('layout_profile', 'baseline-v1') == 'baseline-v1' and snapshot.get('non_cjk_fonts') == PAIR


def binding():
    return {'schema': 'accepted-v3-layout-binding/1', 'profile_sha256': hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
            'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'adapter_sha256': ADAPTER_SHA}


def verify(snapshot):
    if not selected(snapshot) or snapshot.get('v3_layout_binding') != binding():
        raise ValueError('V3 frozen profile or implementation identity changed')
    if (snapshot.get('personal_binding') or {}).get('m2_adapter', {}).get('sha256') != ADAPTER_SHA:
        raise ValueError('V3 requires the exact original 0178 adapter')


def model_seed(char):
    return next((row.get('model_seed') for row in profile()['per_segment'] if row['text'] == char), None)


def _boxes(alpha):
    support = alpha.getbbox()
    core = alpha.point(lambda p: 255 if p >= 128 else 0).getbbox()
    if support is None or core is None:
        raise ValueError('V3 empty glyph alpha')
    return list(support), list(core)


def _font_alpha(text, size, angle, identity, path):
    from PIL import features
    if not features.check_feature('raqm'):
        raise ValueError('V3_SHAPER_UNAVAILABLE: uncached words require whole-span RAQM shaping')
    # Match the original complete span rasterizer at 5x; stroke/outline is absent.
    k = 5
    font = load_font(path, round(size * k), identity)
    bb = font.getbbox(text)
    pad = 8 * k
    high = Image.new('L', (bb[2] - bb[0] + 2 * pad, bb[3] - bb[1] + 2 * pad))
    ImageDraw.Draw(high).text((pad - bb[0], pad - bb[1]), text, font=font, fill=255)
    alpha = high.resize((round(high.width / k), round(high.height / k)), Image.Resampling.LANCZOS)
    return alpha.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=0)


def render_line(text, key, *, binding, paths, chinese_cell, width, height,
                text_style, handwriting, fill):
    """Return transparent ROI pixels plus char evidence; never quietly fit down."""
    cfg = profile()
    style = validate_text_style(text_style)
    writing = validate_handwriting(handwriting)
    roi = cfg['reference_roi']
    rows = cfg['per_segment']
    header = key == 'to_cn' and re.match(r'^To：', text) is not None
    tokens = ['To', '：', *re.findall(r'[A-Za-z]+|[^A-Za-z]', text[3:])] if header else re.findall(r'[A-Za-z]+|[^A-Za-z]', text)
    tokens = [token for token in tokens if token and not token.isspace()]
    reference_tokens = [row['text'] for row in rows if row['line_index'] == (0 if header else 1)]
    reference_sequence = tokens == reference_tokens
    canvas = Image.new('RGBA', (width, height))
    records, spans = [], []
    previous = None
    for index, token in enumerate(tokens):
        role = font_role(token[0], binding)
        row = next((row for row in rows if row['text'] == token and row['line_index'] == (0 if header else 1)), None)
        source_records = []
        from .selection_contract import is_cjk_personal_character
        if len(token) == 1 and is_cjk_personal_character(token):
            if chinese_cell is None:
                raise ValueError('V3 M2 glyph producer required')
            cell, record = chinese_cell(token)
            if cell is None or record.get('source') != 'M2' or record.get('generation_kind') != 'adapter_inference':
                raise ValueError('V3 M2 glyph unavailable: ' + token)
            source_records = [dict(record)]
            alpha = cell.getchannel('A')
            native = row['native_size'] if row else 108
            angle = row['angle_ccw'] if row else 0
            alpha = alpha.resize((native, native), Image.Resampling.LANCZOS)
            padded = Image.new('L', (native + 16, native + 16)); padded.paste(alpha, (8, 8))
            alpha = padded.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=0)
            scale = style['ordinary_scale']
            alpha = alpha.transform((math.ceil(alpha.width * scale), math.ceil(alpha.height * scale)),
                Image.Transform.AFFINE, (1 / scale, 0, 0, 0, 1 / scale, 0), resample=Image.Resampling.BICUBIC, fillcolor=0)
            size = native * scale
        else:
            identity = binding['fonts'][role]
            if any(char_missing(paths[role], char, identity) for char in token):
                raise ValueError('V3 font glyph unavailable')
            base = 112 * style['to_ratio'] if header and index == 0 else 78 if token == '：' else 118 if header else 112
            size = round(base * style['ordinary_scale'], 8)
            angle = -3.8 if header and index == 0 else .8 if header and index == 1 else 2.1 if header else row['angle_ccw'] if row else -3.4
            alpha = _font_alpha(token, size, angle, identity, paths[role])
            source_records = [{'char': char, 'source': 'M0', 'version': identity['id'], 'generation_kind': 'system_font',
                'required_source': 'M0', 'selection_reason': 'non_cjk_system_font', 'attempted': [],
                'font_role': role, 'font_id': identity['id'], 'font_sha256': identity['sha256'],
                'font_face_index': identity.get('face_index', 0), 'font_variation_axes': identity.get('variation_axes', {})}
                for char in token]
        # User controls apply to real alpha, after the preserved V3 transforms.
        if writing['size_variation_pct'] or writing['tilt_variation_deg']:
            from .text_layout import adjust_handwriting
            rgba = Image.new('RGBA', alpha.size); rgba.putalpha(alpha)
            rgba = adjust_handwriting([rgba], [source_records[0]], writing, text=text, slot_key=f'{key}:{index}')[0]
            alpha = rgba.getchannel('A')
        support, core = _boxes(alpha)
        if header:
            left = 70 - roi[0] if previous is None else previous[2] + round(style['header_gap'])
            bottom = (1712 if index == 0 else 1705 if index == 1 else 1718) - roi[1]
            xy = [left - support[0], bottom - core[3]]
        elif reference_sequence and row and style['ordinary_scale'] == 1.3 and not (writing['size_variation_pct'] or writing['tilt_variation_deg']):
            xy = [row['photo_canvas_xy'][0] - roi[0], row['photo_canvas_xy'][1] - roi[1]]
        else:
            left = 100 - roi[0] if previous is None else previous[2] + 10
            xy = [left - support[0], 1944 - roi[1] - core[3]]
        placed = [support[0] + xy[0], support[1] + xy[1], support[2] + xy[0], support[3] + xy[1]]
        if min(placed[:2]) < 0 or placed[2] > width or placed[3] > height:
            raise ValueError('V3 REGION too small for requested ink; enlarge the region or reduce size')
        occupied = canvas.getchannel('A').crop((xy[0], xy[1], xy[0] + alpha.width, xy[1] + alpha.height))
        if ImageChops.multiply(occupied.point(lambda p: 255 if p else 0), alpha.point(lambda p: 255 if p else 0)).getbbox():
            raise ValueError('V3 actual ink collision; enlarge the region or reduce size')
        color = Image.new('RGBA', alpha.size, tuple(fill) + (0,)); color.putalpha(alpha)
        canvas.alpha_composite(color, tuple(xy))
        span = {'text': token, 'size_px': size, 'rotation_degrees_ccw': angle, 'canvas_xy': xy,
                'support_bbox': placed, 'alpha_sha256': hashlib.sha256(alpha.tobytes()).hexdigest()}
        if source_records[0]['source'] == 'M0':
            span['font_rasterizer'] = {'schema':'v3-font-rasterizer/1', 'supersampling':5,
                'supersampled_font_size_px':round(size * 5), 'rerasterized':True,
                'shaping_identity':binding['fonts'][role]['shaping_identity']}
        spans.append(span)
        for record in source_records:
            record.update(size_px=size, v3_span_index=index)
            records.append(record)
        previous = placed
    execution = {'schema': 'accepted-v3-layout-execution/1', 'binding': globals()['binding'](),
        'text': text, 'slot_key': key, 'text_style': style, 'handwriting': writing,
        'reference_roi': roi, 'canvas_size': [width, height], 'spans': spans, 'hidden_fit_down': False}
    return canvas, records, execution
