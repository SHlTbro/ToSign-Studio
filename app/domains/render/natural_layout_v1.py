"""Opt-in measured source-trace layout; no invented/erased strokes or jitter.

Input cells retain their native transparent canvas. Ordered fragments never
wrap across unknown gaps. Overflow scales the completed line uniformly.
"""
from __future__ import annotations
import hashlib,json,math
from pathlib import Path

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _whitespace(text):
 # Engineered text semantics, not measurements of the author's pen movement.
 if len(text)>1000:raise ValueError('explicit inline whitespace exceeds supported bound')
 if any(not c.isspace() or c in '\r\n' for c in text):raise ValueError('invalid inline whitespace')
 counts={'ascii_spaces':text.count(' '),'fullwidth_spaces':text.count('\u3000'),'tabs':text.count('\t'),'other_spaces':sum(c not in ' \u3000\t' for c in text)}
 return {**counts,'advance_h':counts['ascii_spaces']*.4+counts['fullwidth_spaces']+counts['tabs']*1.6+counts['other_spaces']*.4}

def _bounded(value,low,high):
 if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('finite layout measurement required')
 return min(high,max(low,float(value)))

def validate_profile(profile):
 if not isinstance(profile,dict) or profile.get('schema')!='natural-layout-profile/1' or profile.get('id')!='natural-layout-v1':raise ValueError('unknown natural layout profile')
 advisory=profile.get('advisory');segments=profile.get('segments')
 if not isinstance(advisory,dict) or advisory.get('fit_split')!='train' or not isinstance(segments,list) or not segments:raise ValueError('train-only measured fragments required')
 for name in ('measurement_sha256','trace_sha256'):
  h=profile.get(name)
  if not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h):raise ValueError('exact measurement provenance required')
 for segment in segments:
  rows=segment.get('positions')
  if not isinstance(rows,list) or len(rows)<2:raise ValueError('adjacent source fragment required')
  for i,row in enumerate(rows):
   if not row.get('accepted') or not row.get('normal_body_calibration') or row.get('missing_reason') is not None:raise ValueError('ineligible source position')
   _bounded(row.get('observed_height_over_H'),.5,1.5);_bounded(row.get('observed_detrended_bottom_residual_H'),-.5,.5)
   if i and (row.get('verified_adjacency_to_previous_ordinal') is not True or row['source_occurrence_ordinal']!=rows[i-1]['source_occurrence_ordinal']+1):raise ValueError('unverified source adjacency')
   if i:_bounded(row.get('edge_gap_from_previous_H'),0,1)
 return profile

def _pick(profile,style_id,seed,line_index,counter,purpose):
 raw=json.dumps([profile['advisory']['profile_id'],style_id,seed,line_index,counter,purpose],ensure_ascii=False,separators=(',',':')).encode()
 return int.from_bytes(hashlib.sha256(raw).digest()[:8],'big')

def compose(cells,records,*,width,height,seed,profile,style_id=0,line_index=0,text_style=None,prefix_flags=None):
 from PIL import Image
 validate_profile(profile)
 if type(seed) is not int or not 0<=seed<=0xffffffff:raise ValueError('explicit uint32 layout seed required')
 if not cells or len(cells)!=len(records):raise ValueError('one native cell per rendered glyph required')
 if type(width) is not int or type(height) is not int or min(width,height)<1:raise ValueError('positive region dimensions required')
 rec=profile['advisory']['recommendations'];segments=profile['segments'];chosen=[];counter=0
 while len(chosen)<len(cells):
  segment=segments[_pick(profile,style_id,seed,line_index,counter,'segment')%len(segments)];rows=segment['positions'];phase=_pick(profile,style_id,seed,line_index,counter,'phase')%(len(rows)-1)
  for j in range(phase,len(rows)):
   if len(chosen)==len(cells):break
   chosen.append({'segment':segment,'row':rows[j],'phase':phase,'counter':counter,'gap_observed':rows[j]['edge_gap_from_previous_H'] if j>phase else None,'boundary':j==phase and bool(chosen)})
  counter+=1
 scale_cfg=rec['isotropic_scale'];res_cfg=rec['bottom_residual_H'];gap_cfg=rec['edge_gap_H'];slope_cfg=rec['whole_line_slope_y_down']
 residuals=[_bounded(res_cfg['shrink_gain']*c['row']['observed_detrended_bottom_residual_H'],res_cfg['min'],res_cfg['max']) for c in chosen];mean=sum(residuals)/len(residuals);residuals=[x-mean for x in residuals];maxabs=max(abs(x) for x in residuals);rescale=min(1,res_cfg['max']/maxabs) if maxabs else 1;residuals=[x*rescale for x in residuals]
 styled=text_style is not None
 if styled:
  from app.core.text_style import validate_text_style
  text_style=validate_text_style(text_style)
  if not isinstance(prefix_flags,list) or len(prefix_flags)!=len(cells) or any(type(x) is not bool for x in prefix_flags):raise ValueError('one prefix flag per glyph required')
 threshold=0 if styled else 128
 def ink_box(cell):return cell.getchannel('A').getbbox() if styled else cell.getchannel('A').point(lambda a:255 if a>=128 else 0).getbbox()
 nominal_boxes=[ink_box(c) for c in cells]
 if any(b is None for b in nominal_boxes):raise ValueError('no opaque ink for measured layout')
 normal_heights=sorted(b[3]-b[1] for i,(b,c) in enumerate(zip(nominal_boxes,cells)) if b[3]-b[1]>=.5*c.height and (not styled or not prefix_flags[i]))
 nominal=normal_heights[len(normal_heights)//2] if normal_heights else sum(c.height for c in cells)/len(cells)
 nominal_origin='median_normal_body_ink' if normal_heights else 'explicit_native_canvas_fallback_for_sparse_only_line'
 if not styled:
  prepared=[];placements=[];ink_x=0.0
  slope=_bounded(slope_cfg['shrink_gain']*chosen[0]['segment']['source_slope_y_down'],slope_cfg['min'],slope_cfg['max'])
  for i,(cell,c) in enumerate(zip(cells,chosen)):
   row=c['row'];scale=_bounded(1+scale_cfg['shrink_gain']*(row['observed_height_over_H']-1),scale_cfg['min'],scale_cfg['max'])
   resized=cell.resize((max(1,round(cell.width*scale)),max(1,round(cell.height*scale))),Image.Resampling.LANCZOS);box=resized.getchannel('A').point(lambda a:255 if a>=128 else 0).getbbox()
   if box is None:raise ValueError('empty glyph cannot be laid out')
   observed=c['gap_observed'];gap_h=None if i==0 else (rec['segment_boundary']['applied_neutral_gap_H'] if c['boundary'] else _bounded(gap_cfg['runtime_center']+gap_cfg['shrink_gain']*(observed-gap_cfg['source_center']),gap_cfg['min'],gap_cfg['max']))
   if gap_h is not None:ink_x+=gap_h*nominal
   whitespace_units=records[i].get('space_before_units',0)
   if type(whitespace_units) is not int or not 0<=whitespace_units<=1000:raise ValueError('invalid explicit whitespace')
   whitespace_h=records[i].get('space_before_h',whitespace_units*.4)
   _bounded(whitespace_h,0,1600)
   if i:ink_x+=whitespace_h*nominal
   placements.append({'x_float':ink_x-box[0],'ink_x':ink_x,'scale':scale,'baseline_residual_h':residuals[i],'gap_before_observed_h':observed if i else None,'gap_before_applied_h':gap_h,'explicit_whitespace_units':whitespace_units,'explicit_whitespace_advance_h':whitespace_h,'explicit_whitespace':records[i].get('inline_whitespace'),'segment_boundary_gap_unobserved':c['boundary'],'source_segment_id':c['segment']['segment_id'],'source_candidate_id':row['candidate_id'],'source_sample_id':row['sample_id'],'phase':c['phase'],'segment_counter':c['counter'],'native_canvas':[cell.width,cell.height],'transformed_ink_bbox':list(box)})
   prepared.append(resized);ink_x+=box[2]-box[0]
  if ink_x:slope=_bounded(slope,-slope_cfg['max_end_to_end_change_H']*nominal/ink_x,slope_cfg['max_end_to_end_change_H']*nominal/ink_x)
  for p,cell in zip(placements,prepared):p.update(x=round(p['x_float']),y=round(nominal-cell.height+slope*p['ink_x']+p['baseline_residual_h']*nominal))
  min_x=min(p['x'] for p in placements);min_y=min(p['y'] for p in placements);max_x=max(p['x']+c.width for p,c in zip(placements,prepared));max_y=max(p['y']+c.height for p,c in zip(placements,prepared));canvas=Image.new('RGBA',(max_x-min_x,max_y-min_y),(0,0,0,0))
  for p,cell in zip(placements,prepared):canvas.alpha_composite(cell,(p['x']-min_x,p['y']-min_y))
  crop=canvas.getchannel('A').getbbox()
  if crop is None:raise ValueError('empty strip')
  canvas=canvas.crop(crop)
  leading=round(records[0].get('space_before_h',records[0].get('space_before_units',0)*.4)*nominal)
  trailing=round(records[-1].get('space_after_h',0)*nominal)
  if leading or trailing:
   padded=Image.new('RGBA',(canvas.width+leading+trailing,canvas.height),(0,0,0,0));padded.alpha_composite(canvas,(leading,0));canvas=padded
  fit=min(1.0,width/canvas.width,height/canvas.height)
  if fit<1:canvas=canvas.resize((max(1,math.floor(canvas.width*fit)),max(1,math.floor(canvas.height*fit))),Image.Resampling.LANCZOS)
 else:
  def at(glyph_fit):
   prepared=[];placements=[];ink_x=0.0
   slope=_bounded(slope_cfg['shrink_gain']*chosen[0]['segment']['source_slope_y_down'],slope_cfg['min'],slope_cfg['max'])
   for i,(cell,c) in enumerate(zip(cells,chosen)):
    row=c['row'];scale=_bounded(1+scale_cfg['shrink_gain']*(row['observed_height_over_H']-1),scale_cfg['min'],scale_cfg['max'])
    resized=cell.resize((max(1,round(cell.width*scale*glyph_fit)),max(1,round(cell.height*scale*glyph_fit))),Image.Resampling.LANCZOS);box=ink_box(resized)
    if box is None:raise ValueError('empty glyph cannot be laid out')
    observed=c['gap_observed'];gap_h=None if i==0 else (rec['segment_boundary']['applied_neutral_gap_H'] if c['boundary'] else _bounded(gap_cfg['runtime_center']+gap_cfg['shrink_gain']*(observed-gap_cfg['source_center']),gap_cfg['min'],gap_cfg['max']))
    header_boundary=i>0 and prefix_flags[i-1] and not prefix_flags[i]
    if header_boundary:ink_x+=text_style['header_gap']
    elif gap_h is not None:ink_x+=gap_h*nominal*glyph_fit
    whitespace_units=records[i].get('space_before_units',0)
    if type(whitespace_units) is not int or not 0<=whitespace_units<=1000:raise ValueError('invalid explicit whitespace')
    whitespace_h=records[i].get('space_before_h',whitespace_units*.4)
    _bounded(whitespace_h,0,1600)
    if i and not header_boundary:ink_x+=whitespace_h*nominal*glyph_fit
    placements.append({'x_float':ink_x-box[0],'ink_x':ink_x,'scale':scale,'baseline_residual_h':residuals[i],'gap_before_observed_h':observed if i else None,'gap_before_applied_h':gap_h,'fixed_header_gap_px':text_style['header_gap'] if header_boundary else None,'glyph_fit':glyph_fit,'explicit_whitespace_units':whitespace_units,'explicit_whitespace_advance_h':whitespace_h,'explicit_whitespace':records[i].get('inline_whitespace'),'segment_boundary_gap_unobserved':c['boundary'],'source_segment_id':c['segment']['segment_id'],'source_candidate_id':row['candidate_id'],'source_sample_id':row['sample_id'],'phase':c['phase'],'segment_counter':c['counter'],'native_canvas':[cell.width,cell.height],'transformed_ink_bbox':list(box)})
    prepared.append(resized);ink_x+=box[2]-box[0]
   if ink_x:slope=_bounded(slope,-slope_cfg['max_end_to_end_change_H']*nominal*glyph_fit/ink_x,slope_cfg['max_end_to_end_change_H']*nominal*glyph_fit/ink_x)
   for p,cell in zip(placements,prepared):p.update(x=round(p['x_float']),y=round(nominal*glyph_fit-cell.height+slope*p['ink_x']+p['baseline_residual_h']*nominal*glyph_fit))
   min_x=min(p['x'] for p in placements);min_y=min(p['y'] for p in placements);max_x=max(p['x']+c.width for p,c in zip(placements,prepared));max_y=max(p['y']+c.height for p,c in zip(placements,prepared));canvas=Image.new('RGBA',(max_x-min_x,max_y-min_y),(0,0,0,0))
   for p,cell in zip(placements,prepared):canvas.alpha_composite(cell,(p['x']-min_x,p['y']-min_y))
   crop=canvas.getchannel('A').getbbox()
   if crop is None:raise ValueError('empty strip')
   canvas=canvas.crop(crop)
   leading=round(records[0].get('space_before_h',records[0].get('space_before_units',0)*.4)*nominal*glyph_fit)
   trailing=round(records[-1].get('space_after_h',0)*nominal*glyph_fit)
   if leading or trailing:
    padded=Image.new('RGBA',(canvas.width+leading+trailing,canvas.height),(0,0,0,0));padded.alpha_composite(canvas,(leading,0));canvas=padded
   return canvas,placements,prepared,min_x,min_y,crop,leading,trailing,slope
  current=at(1.0);fit=1.0
  if current[0].width>width or current[0].height>height:
   low,high=min(1.0,12/max(c.height for c in cells)),1.0
   minimum=at(low)
   if minimum[0].width>width or minimum[0].height>height:raise ValueError('fixed header gap cannot fit region')
   current=minimum
   for _ in range(24):
    mid=(low+high)/2;candidate=at(mid)
    if candidate[0].width<=width and candidate[0].height<=height:low=mid;current=candidate
    else:high=mid
   fit=low
  canvas,placements,prepared,min_x,min_y,crop,leading,trailing,slope=current
 for record,p in zip(records,placements):record['layout_transform']={**p,'canvas_position':[p['x']-min_x-crop[0]+leading,p['y']-min_y-crop[1]],'global_fit':fit,'rotation_degrees':0,'shear':0,'width_factor':1}
 metadata={'schema':'natural-layout-execution/1','profile':'natural-layout-v1','profile_sha256':digest(profile),'measurement_sha256':profile['measurement_sha256'],'trace_sha256':profile['trace_sha256'],'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'seed':seed,'style_id':style_id,'line_index':line_index,'line_slope':slope,'residual_centering_mean_h':mean,'residual_centering_rescale':rescale,'global_fit':fit,'output_size':list(canvas.size),'added_outline':False,'independent_random_jitter':False,'wrap_across_fragment_gaps':False,'nominal_body_height':nominal,'nominal_body_origin':nominal_origin,'layout_measurement_alpha_threshold':threshold,'threshold_changes_glyph_pixels':False,'leading_inline_padding':leading,'trailing_inline_padding':trailing,'whitespace_semantics':'ascii-0.4H-fullwidth-1.0H-tab-1.6H-v1'}
 if styled:metadata.update(frozen_text_style=text_style,fit_strategy='glyphs-before-fixed-ink-gap',header_gap_source_pixels=text_style['header_gap'])
 return canvas,metadata

def compose_wrapped(cells,records,*,text,width,height,seed,profile,style_id=0,text_style=None,prefix_flags=None):
 """Preserve explicit line breaks; otherwise wrap before shrinking the block.

    Line fitting uses one uniform scale for native canvases, never equal-height
    tight crops. Sparse glyphs/punctuation retain their native geometry.
    """
 import unicodedata
 from PIL import Image
 validate_profile(profile)
 original_text=text;text=text.replace('\r\n','\n').replace('\r','\n')
 positions=[i for i,c in enumerate(text) if not c.isspace()]
 if not positions:raise ValueError('natural layout requires at least one visible glyph')
 if len(positions)!=len(records) or ''.join(text[i] for i in positions)!=''.join(r['char'] for r in records):raise ValueError('layout text/records differ')
 for i,(record,pos) in enumerate(zip(records,positions)):
  separator=text[(positions[i-1]+1 if i else 0):pos];record['text_index']=pos
  record['explicit_line_breaks_before']=separator.count('\n')
  if i and '\n' in separator:records[i-1]['space_after_h']=_whitespace(separator.split('\n',1)[0])['advance_h']
  inline=separator.rsplit('\n',1)[-1];record['inline_whitespace']=_whitespace(inline)
  record['space_before_units']=sum(4 if c=='\t' else 1 for c in inline)
  record['space_before_h']=record['inline_whitespace']['advance_h']
  record['space_after_h']=0
 tail=text[positions[-1]+1:];trailing_breaks=tail.count('\n')
 records[-1]['space_after_h']=_whitespace(tail.split('\n',1)[0])['advance_h']
 styled=text_style is not None
 if styled:
  from app.core.text_style import validate_text_style
  text_style=validate_text_style(text_style)
  if not isinstance(prefix_flags,list) or len(prefix_flags)!=len(cells) or any(type(x) is not bool for x in prefix_flags):raise ValueError('one prefix flag per glyph required')
 bounds=[c.getchannel('A').getbbox() if styled else c.getchannel('A').point(lambda a:255 if a>=128 else 0).getbbox() for c in cells]
 if any(b is None for b in bounds):raise ValueError('empty measured glyph')
 full_heights=[b[3]-b[1] for b,c in zip(bounds,cells) if b[3]-b[1]>=.5*c.height]
 body_h=sorted(full_heights)[len(full_heights)//2] if full_heights else sum(c.height for c in cells)/len(cells)
 canvas_h=max(c.height for c in cells);gap_max=profile['advisory']['recommendations']['edge_gap_H']['max']
 def lines_at(scale):
  lines=[];line=[];used=0.0
  for i,(record,box) in enumerate(zip(records,bounds)):
   forced=record['explicit_line_breaks_before']
   if forced:
    if line:lines.append(line);line=[];used=0.0;blank_count=forced-1
    else:blank_count=forced
    lines.extend([[] for _ in range(blank_count)])
   advance=((box[2]-box[0])*1.08+(gap_max*body_h if line else 0)+(record['space_before_h']+record.get('space_after_h',0))*body_h)*scale
   header_boundary=styled and i>0 and prefix_flags[i-1] and not prefix_flags[i]
   if header_boundary:advance=(box[2]-box[0])*1.08*scale+text_style['header_gap']
   closing=unicodedata.category(record['char']) in ('Pe','Pf','Po') or (styled and (prefix_flags[i] or header_boundary))
   if line and used+advance>width and not closing:lines.append(line);line=[];used=0.0;advance=((box[2]-box[0])*1.08+(record['space_before_h']+record.get('space_after_h',0))*body_h)*scale
   line.append(i);used+=advance
  if line:lines.append(line)
  lines.extend([[] for _ in range(trailing_breaks)])
  return lines
 # Largest deterministic native-canvas scale that keeps all line slots in
 # the region. A very narrow single glyph is fitted by compose as one strip.
 low,high=.01,1.0
 for _ in range(24):
  mid=(low+high)/2
  if len(lines_at(mid))*canvas_h*1.35*mid<=height:low=mid
  else:high=mid
 scale=1.0 if len(lines_at(1.0))*canvas_h*1.35<=height else low
 groups=lines_at(scale)
 if scale*canvas_h<12 or len(groups)>height:raise ValueError('natural layout cannot fit at minimum canvas size without clipping')
 pitch=max(1,height//max(1,len(groups)));strips=[];line_receipts=[]
 for index,indices in enumerate(groups):
  if not indices:strips.append(None);line_receipts.append({'line_index':index,'explicit_blank_line':True});continue
  scaled=[cells[i].resize((max(1,round(cells[i].width*scale)),max(1,round(cells[i].height*scale))),Image.Resampling.LANCZOS) for i in indices]
  row_records=[records[i] for i in indices]
  strip,meta=compose(scaled,row_records,width=width,height=pitch,seed=seed,profile=profile,style_id=style_id,line_index=index,**({'text_style':text_style,'prefix_flags':[prefix_flags[i] for i in indices]} if styled else {}))
  if meta['global_fit']*scale*canvas_h<12:raise ValueError('natural layout would be unreadably small')
  for record in row_records:record['layout_transform'].update(line_index=index,line_top=index*pitch,paragraph_native_scale=scale)
  strips.append(strip);line_receipts.append(meta)
 out_h=max((i*pitch+(s.height if s is not None else pitch) for i,s in enumerate(strips)),default=1)
 result=Image.new('RGBA',(max((s.width for s in strips if s is not None),default=1),min(height,out_h)),(0,0,0,0))
 for i,strip in enumerate(strips):
  if strip is not None:result.alpha_composite(strip,(0,i*pitch))
 return result,{'schema':'natural-layout-paragraph/1','profile':'natural-layout-v1','profile_sha256':digest(profile),'seed':seed,'line_count':len(groups),'line_pitch':pitch,'native_canvas_scale':scale,'source_dimensions_preserved':True,'explicit_line_breaks_preserved':True,'explicit_blank_lines_preserved':True,'original_text':original_text,'normalized_text':text,'line_break_normalization':'CRLF-and-CR-to-LF','whitespace_semantics':{'ascii_space_h':.4,'fullwidth_space_h':1.0,'tab_h':1.6,'source':'engineered-semantic-advances-not-author-measurement'},'trailing_line_breaks':trailing_breaks,'lines':line_receipts,'output_size':list(result.size)}
