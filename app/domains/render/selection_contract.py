"""Explicit request choices; omission alone retains historical inheritance."""
from app.core.errors import ApiError

SOURCE_POLICIES = ('fallback_allowed', 'm2_required', 'm2_cjk_required')
REQUIRED_M2_POLICIES = ('m2_required', 'm2_cjk_required')


def is_cjk_personal_character(char):
 """Fixed Han-ideograph scope; punctuation, Latin, kana and Hangul are M0.

 Includes ideographic zero, unified/compatibility ideographs and extensions
 through I. Unassigned codepoints inside these ranges also fail closed as M2.
 """
 if not isinstance(char,str) or len(char)!=1:return False
 code=ord(char)
 return code==0x3007 or any(start<=code<=end for start,end in (
  (0x3400,0x4DBF),(0x4E00,0x9FFF),(0xF900,0xFAFF),
  (0x20000,0x2A6DF),(0x2A700,0x2B73F),(0x2B740,0x2B81F),
  (0x2B820,0x2CEAF),(0x2CEB0,0x2EBEF),(0x2EBF0,0x2EE5F),
  (0x2F800,0x2FA1F),(0x30000,0x3134F),(0x31350,0x323AF)))


def character_requires_m2(policy,char):
 return policy=='m2_required' or (policy=='m2_cjk_required' and is_cjk_personal_character(char))

def choices(payload,template_style_id):
 mode=payload.get('style_mode')
 if mode is None:
  style_id=payload.get('style_id') or template_style_id;mode='legacy_inherited'
 elif mode=='system':
  if payload.get('style_id') is not None:raise ApiError(400,'INVALID_REQUEST','System mode cannot select a profile')
  style_id=None
 elif mode=='profile':
  style_id=payload.get('style_id')
  if type(style_id) is not int or style_id<1:raise ApiError(400,'INVALID_REQUEST','Profile mode requires a positive style_id')
 else:raise ApiError(400,'INVALID_REQUEST','Unknown style_mode')
 if 'non_cjk_font_binding' in payload or 'v3_layout_binding' in payload:raise ApiError(400,'INVALID_REQUEST','Font/layout authority is server-resolved')
 policy=payload.get('glyph_source_policy','fallback_allowed')
 if policy not in SOURCE_POLICIES:raise ApiError(400,'INVALID_REQUEST','Unknown glyph_source_policy')
 layout=payload.get('layout_profile','baseline-v1');seed=payload.get('layout_seed')
 if layout not in ('baseline-v1','natural-layout-v1'):raise ApiError(400,'INVALID_REQUEST','Unknown layout_profile')
 if layout=='natural-layout-v1':
  if type(seed) is not int or not 0<=seed<=0xffffffff:raise ApiError(400,'INVALID_REQUEST','Natural layout requires an explicit uint32 layout_seed')
  if policy!='m2_required' or style_id is None:raise ApiError(400,'INVALID_REQUEST','Natural layout requires an explicit trained-adapter policy and profile')
 elif seed is not None:raise ApiError(400,'INVALID_REQUEST','Baseline layout does not consume a layout seed')
 if policy in REQUIRED_M2_POLICIES and style_id is None:raise ApiError(400,'INVALID_REQUEST','M2-required policy needs a profile')
 if 'ink_color_binding' in payload or 'ink_color_binding_sha256' in payload:raise ApiError(400,'INVALID_REQUEST','Ink color authority is server-resolved and cannot be submitted')
 ink=payload.get('ink_color_mode','auto-contrast-v1');palette=payload.get('ink_palette_id')
 if ink not in ('auto-contrast-v1','source-solid-srgb-v1','custom-solid-srgb-v1'):raise ApiError(400,'INK_COLOR_UNSUPPORTED','Unknown ink color mode')
 if ink!='custom-solid-srgb-v1' and 'ink_color_rgb' in payload:raise ApiError(400,'INK_COLOR_UNSUPPORTED','Explicit RGB requires custom color mode')
 if ink=='auto-contrast-v1':
  if palette is not None:raise ApiError(400,'INK_COLOR_UNSUPPORTED','Automatic contrast does not select a source palette')
 elif ink=='custom-solid-srgb-v1':
  if palette is not None:raise ApiError(400,'INK_COLOR_UNSUPPORTED','Custom color cannot select a source palette')
  from .ink_color_v1 import validate_rgb
  try:rgb=list(validate_rgb(payload.get('ink_color_rgb')))
  except ValueError as exc:raise ApiError(400,'INK_COLOR_UNSUPPORTED','Custom color requires three uint8 RGB channels',str(exc)) from exc
 elif layout!='natural-layout-v1' or policy!='m2_required' or style_id is None or not isinstance(palette,str) or not palette:
  raise ApiError(400,'INK_COLOR_UNSUPPORTED','Source ink requires an explicit palette, natural layout and required-M2 profile')
 selected={'style_id':style_id,'style_mode':mode,'glyph_source_policy':policy,'layout_profile':layout,'layout_seed':seed,'ink_color_mode':ink,'ink_palette_id':palette}
 if ink=='custom-solid-srgb-v1':selected['ink_color_rgb']=rgb
 if 'text_style' in payload:
  from app.core.text_style import validate_text_style
  selected['text_style']=validate_text_style(payload['text_style'])
 if 'handwriting' in payload:
  from app.core.text_style import validate_handwriting
  selected['handwriting']=validate_handwriting(payload['handwriting'])
 if 'non_cjk_fonts' in payload:
  if policy!='m2_cjk_required':raise ApiError(400,'INVALID_REQUEST','Explicit mixed fonts require the Han-only M2 policy')
  from .mixed_fonts import freeze_fonts
  binding,_=freeze_fonts(payload['non_cjk_fonts'])
  selected.update(non_cjk_fonts=dict(payload['non_cjk_fonts']),non_cjk_font_binding=binding)
  from .v3_layout import selected as is_v3
  if is_v3(selected) and 'ink_color_mode' not in payload:
   selected.update(ink_color_mode='custom-solid-srgb-v1',ink_color_rgb=[140,48,68])
 return selected

def verify_selected(snapshot,user_id=None,session=None):
 if 'non_cjk_fonts' in snapshot or 'non_cjk_font_binding' in snapshot:
  from .mixed_fonts import resolve_frozen
  try:resolve_frozen(snapshot)
  except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'NON_CJK_FONT_UNAVAILABLE','Frozen font authority unavailable',str(exc),False) from exc
 if snapshot.get('glyph_source_policy') in REQUIRED_M2_POLICIES:
  binding=snapshot.get('personal_binding') or {}
  if snapshot.get('source')!='M2_ADAPTER' or binding.get('state')!='bound' or binding.get('engine_mode')!='M2_ADAPTER' or not binding.get('m2_adapter'):
   raise ApiError(409,'M2_REQUIRED_UNAVAILABLE','Selected profile cannot provide the required trained adapter')
 from .v3_layout import selected as is_v3, binding as v3_binding, ADAPTER_SHA
 if is_v3(snapshot):
  if (snapshot.get('personal_binding') or {}).get('m2_adapter',{}).get('sha256') != ADAPTER_SHA:
   raise ApiError(409,'V3_MODEL_UNAVAILABLE','V3 requires the original 0178 adapter')
  snapshot['v3_layout_binding']=v3_binding()
 elif 'v3_layout_binding' in snapshot:
  raise ApiError(409,'V3_MODEL_UNAVAILABLE','V3 authority requires its original font pair')
 if snapshot.get('layout_profile')=='natural-layout-v1':
  from .natural_authorization import resolve_author_profile
  try:qualified=resolve_author_profile(user_id,snapshot.get('personal_binding'))
  except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'NATURAL_LAYOUT_UNAVAILABLE','Natural layout is unavailable for this exact author/profile',str(exc)) from exc
  snapshot['layout_binding']=qualified['binding']
  snapshot['layout_profile_sha256']=qualified['binding']['profile_sha256']
 if snapshot.get('ink_color_mode','auto-contrast-v1')=='source-solid-srgb-v1':
  from .ink_authorization import resolve_author_palette
  try:
   ink=resolve_author_palette(user_id,snapshot.get('personal_binding'),snapshot.get('ink_palette_id'),layout_profile=snapshot.get('layout_profile'),render_source_policy=snapshot.get('glyph_source_policy'),session=session)
  except (OSError,ValueError,KeyError,TypeError) as exc:raise ApiError(409,'INK_COLOR_UNAVAILABLE','Source ink is unavailable for this exact author/model',str(exc)) from exc
  from .natural_layout_v1 import digest
  snapshot['ink_color_binding']=ink['binding']
  snapshot['ink_color_binding_sha256']=digest(ink['binding'])


def is_personal_punctuation_unsupported(char):
 import unicodedata
 return unicodedata.category(char).startswith('P')

def verify_personal_text(snapshot,texts):
 if snapshot.get('glyph_source_policy')!='m2_required':return
 unsupported=sorted({c for text in texts if isinstance(text,str) for c in text if is_personal_punctuation_unsupported(c)})
 if unsupported:
  raise ApiError(422,'PERSONAL_PUNCTUATION_UNSUPPORTED','Personal punctuation is not qualified; use an explicit fallback policy or remove punctuation','Unsupported personal punctuation: '+''.join(unsupported))


def frozen_ink_mode(snapshot):
 """Historical all-absent color means auto; partial modern authority fails closed."""
 if not isinstance(snapshot,dict):raise ValueError('frozen render selection is not an object')
 keys={'ink_color_mode','ink_palette_id','ink_color_binding','ink_color_binding_sha256','ink_color_rgb'}
 if not any(k in snapshot for k in keys):return 'auto-contrast-v1'
 if 'ink_color_mode' not in snapshot:raise ValueError('partial frozen ink authority')
 mode=snapshot['ink_color_mode']
 if mode!='custom-solid-srgb-v1' and 'ink_color_rgb' in snapshot:raise ValueError('RGB is only supported by custom color')
 if mode=='auto-contrast-v1':
  if snapshot.get('ink_palette_id') is not None or snapshot.get('ink_color_binding') is not None or snapshot.get('ink_color_binding_sha256') is not None:raise ValueError('automatic color cannot carry palette authority')
 elif mode=='custom-solid-srgb-v1':
  if any(snapshot.get(k) is not None for k in ('ink_palette_id','ink_color_binding','ink_color_binding_sha256')):raise ValueError('custom color cannot carry source palette authority')
  from .ink_color_v1 import validate_rgb
  validate_rgb(snapshot.get('ink_color_rgb'))
 elif mode=='source-solid-srgb-v1':
  if (not isinstance(snapshot.get('ink_palette_id'),str) or not snapshot['ink_palette_id']
      or not isinstance(snapshot.get('ink_color_binding'),dict) or not snapshot['ink_color_binding']
      or snapshot.get('layout_profile')!='natural-layout-v1' or snapshot.get('glyph_source_policy')!='m2_required'):
   raise ValueError('source ink requires complete frozen palette/natural/M2 authority')
  from .natural_layout_v1 import digest
  if snapshot.get('ink_color_binding_sha256')!=digest(snapshot['ink_color_binding']):raise ValueError('frozen ink binding digest differs')
 else:raise ValueError('unknown frozen ink mode')
 return mode
