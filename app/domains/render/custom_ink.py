"""Explicit user RGB using the existing post-layout alpha-preserving compositor."""
import io
import json
from PIL import Image
from .ink_color_v1 import validate_rgb, recolor_strip, ALPHA_POLICY

CUSTOM_MODE='custom-solid-srgb-v1'

def apply_custom_ink(strip,snapshot):
    from .selection_contract import frozen_ink_mode
    if frozen_ink_mode(snapshot)!=CUSTOM_MODE:raise ValueError('explicit custom ink required')
    colored,execution=recolor_strip(strip,snapshot['ink_color_rgb'])
    return colored,{**execution,'ink_color_mode':CUSTOM_MODE,'color_origin':'user-selection'}

def color_metadata(snapshot,slots):
    from .natural_layout_v1 import digest
    return {'schema':'render-custom-ink/1','ink_color_mode':CUSTOM_MODE,
        'rgb':list(validate_rgb(snapshot['ink_color_rgb'])),
        'style_snapshot_sha256':digest(snapshot),'slots':slots}

def validate_custom_output(snapshot,raw):
    """Review/export must retain this job's exact color metadata and PNG bytes."""
    from .natural_layout_v1 import digest
    with Image.open(io.BytesIO(raw)) as image:
        if image.format!='PNG':raise ValueError('custom ink output must be PNG')
        meta=json.loads(image.info.get('render_ink_color','null'))
    if (not isinstance(meta,dict) or meta.get('schema')!='render-custom-ink/1'
        or meta.get('ink_color_mode')!=CUSTOM_MODE or validate_rgb(meta.get('rgb'))!=validate_rgb(snapshot['ink_color_rgb'])
        or meta.get('style_snapshot_sha256')!=digest(snapshot) or not isinstance(meta.get('slots'),list)
        or not meta['slots']):raise ValueError('custom ink metadata differs from frozen job')
    for slot in meta['slots']:
        execution=slot.get('execution',{}) if isinstance(slot,dict) else {}
        if (execution.get('ink_color_mode')!=CUSTOM_MODE or validate_rgb(execution.get('rgb'))!=validate_rgb(meta['rgb'])
            or execution.get('alpha_preserved') is not True or execution.get('alpha_policy')!=ALPHA_POLICY
            or not isinstance(execution.get('post_layout_alpha_sha256'),str)
            or len(execution['post_layout_alpha_sha256'])!=64):raise ValueError('custom ink execution missing')
