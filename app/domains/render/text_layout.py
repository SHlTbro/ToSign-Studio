"""Render real font/glyph ink at frozen slot scales; never scale a background."""
from PIL import Image, ImageDraw, ImageFont
from app.core.text_style import validate_text_style, validate_handwriting, text_segments

def adjust_handwriting(cells, records, settings, *, text, slot_key):
    """Transform accepted glyph pixels once; hash sampling never uses job/item IDs."""
    import hashlib
    settings = validate_handwriting(settings)
    adjusted = []
    for index, (cell, record) in enumerate(zip(cells, records)):
        identity = f"handwriting/1:{settings['seed']}:{slot_key}:{text}:{index}".encode('utf-8')
        sample = hashlib.sha256(identity).digest()
        scale = 1 + settings['size_variation_pct'] / 100 * (int.from_bytes(sample[:8], 'big') / (2**64-1) * 2 - 1)
        angle = settings['tilt_variation_deg'] * (int.from_bytes(sample[8:16], 'big') / (2**64-1) * 2 - 1)
        source_size = list(cell.size)
        if scale != 1:
            cell = cell.resize((max(1,round(cell.width*scale)), max(1,round(cell.height*scale))), Image.Resampling.LANCZOS)
        if angle != 0:
            cell = cell.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        record['handwriting_transform'] = {'schema':'handwriting-transform/1', 'seed':settings['seed'],
            'scale':scale, 'rotation_degrees':angle, 'source_canvas':source_size, 'adjusted_canvas':list(cell.size)}
        adjusted.append(cell)
    return adjusted

def render_scaled_text(text, key, style, *, w, h, font_path, font_name, fill, stroke_fill,
                       handwriting=None):
    style = validate_text_style(style)
    if handwriting is not None:
        handwriting = validate_handwriting(handwriting)
        if handwriting['size_variation_pct'] or handwriting['tilt_variation_deg']:
            return _render_adjusted_text(text, key, style, handwriting, w=w, h=h,
                font_path=font_path, fill=fill, stroke_fill=stroke_fill)
    ordinary_size = max(6, round(h * 0.6 * style["ordinary_scale"]))
    groups, records, blocker = [], [], None
    for content, prefix in text_segments(text, key):
        if not content:
            continue
        size = max(6, round(ordinary_size * (style["to_ratio"] if prefix else 1)))
        font = ImageFont.truetype(font_path, size=size)
        stroke = max(1, size // 40)
        box = font.getbbox(content, stroke_width=stroke)
        group = Image.new("RGBA", (max(1, box[2]-box[0]), max(1, box[3]-box[1])))
        ImageDraw.Draw(group).text((-box[0], -box[1]), content, font=font,
            fill=tuple(fill)+(255,), stroke_width=stroke, stroke_fill=tuple(stroke_fill)+(255,))
        ink = group.getbbox()
        if ink:
            groups.append(group.crop(ink))
    if blocker is not None or not groups:
        return None, records, blocker
    # Gap is measured between real ink boxes, in source-image pixels.
    gap = round(style["header_gap"]) if len(groups) > 1 else 0
    return _fit_ink_groups(groups, [gap] * (len(groups)-1), w=w, h=h), records, None

def _render_adjusted_text(text, key, style, handwriting, *, w, h, font_path, fill, stroke_fill):
    size = max(6, round(h * .6 * style['ordinary_scale']))
    cells, records, flags = [], [], []
    for content, prefix in text_segments(text, key):
        char_size = max(6, round(size * (style['to_ratio'] if prefix else 1)))
        font = ImageFont.truetype(font_path, size=char_size)
        stroke = max(1, char_size // 40)
        for char in content:
            box = font.getbbox(char, stroke_width=stroke)
            cell = Image.new('RGBA',(max(1,box[2]-box[0]),max(1,box[3]-box[1])))
            ImageDraw.Draw(cell).text((-box[0],-box[1]),char,font=font,fill=tuple(fill)+(255,),
                stroke_width=stroke,stroke_fill=tuple(stroke_fill)+(255,))
            cells.append(cell); flags.append(prefix)
            records.append({'char':char,'source':'M0','size_px':char_size})
    if not cells: return None, records, None
    cells = adjust_handwriting(cells,records,handwriting,text=text,slot_key=key)
    return compose_scaled_cells(cells,flags,w=w,h=h,size=size,gap=style['header_gap'],records=records),records,None



def compose_scaled_cells(cells,prefix_flags,*,w,h,size,gap,records=None):
    """Compose only cells already accepted by GlyphSession source/quality gates."""
    trimmed=[c.crop(c.getbbox()) if c.getbbox() else c for c in cells]
    gaps=[round(gap) if prefix_flags[i-1] and not prefix_flags[i] else max(0,size//40)
          for i in range(1,len(cells))]
    return _fit_ink_groups(trimmed, gaps, w=w, h=h, records=records)


def _fit_ink_groups(groups, gaps, *, w, h, records=None):
    # Fit actual ink first, then place it with the exact source-image pixel gaps.
    available = w - sum(gaps)
    if available < len(groups) or h < 1:
        raise ValueError("TEXT_STYLE_REGION_TOO_SMALL_FOR_FIXED_GAPS")
    scale = min(1.0, available / sum(g.width for g in groups), h / max(g.height for g in groups))
    if scale < 1:
        widths=[max(1,int(group.width*scale)) for group in groups]
        excess=sum(widths)-available
        while excess>0:
            index=max(range(len(widths)),key=widths.__getitem__)
            delta=min(excess,widths[index]-1)
            widths[index]-=delta
            excess-=delta
        resized=[]
        for group,target_width in zip(groups,widths):
            image=group.resize((target_width,max(1,int(group.height*scale))), Image.Resampling.LANCZOS)
            ink=image.getbbox()
            resized.append(image.crop(ink) if ink else image)
        groups=resized
    width=sum(g.width for g in groups)+sum(gaps)
    height=max(g.height for g in groups)
    strip=Image.new("RGBA",(width,height))
    x=0
    for index,group in enumerate(groups):
        if index:x+=gaps[index-1]
        strip.paste(group,(x,height-group.height))
        if records is not None:
            records[index]['typography_placement'] = {'x':x,'y':height-group.height,
                'width':group.width,'height':group.height,'global_fit':scale,'output_size':[width,height]}
        x+=group.width
    return strip
