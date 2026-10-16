"""Exact owned-author layout qualification; catalog is server-owned, never HTTP input."""
from pathlib import Path
import hashlib,json
from .natural_layout_v1 import digest,validate_profile

ROOT=Path(__file__).resolve().parent
CATALOG=ROOT/'natural_layout_catalog.json'
ASSETS=ROOT/'natural_layout_assets'

def _read(path):
 if path.is_symlink() or not path.is_file():raise ValueError('layout authority file is missing or indirect')
 raw=path.read_bytes()
 return json.loads(raw),hashlib.sha256(raw).hexdigest()

def _train_trace_binding(snapshot,profile,ordered):
 from engine.style.dataset.snapshot import _content_hash
 root=Path(snapshot['snapshot_dir']);raw={}
 for name in ('index.json','samples.jsonl','split.json','provenance.json'):
  p=root/name
  if p.is_symlink() or not p.is_file():raise ValueError('indirect or missing author snapshot authority')
  raw[name]=p.read_bytes()
 index=json.loads(raw['index.json'])
 if hashlib.sha256(raw['index.json']).hexdigest()!=snapshot.get('index_sha256'):raise ValueError('author snapshot index changed during qualification')
 if _content_hash(raw['samples.jsonl'],raw['split.json'],{sid:x['sha256'] for sid,x in index['files'].items()},raw['provenance.json'])!=snapshot['snapshot_sha256']:raise ValueError('author snapshot metadata changed during qualification')
 rows=[json.loads(line) for line in raw['samples.jsonl'].decode('utf-8').splitlines() if line.strip()];byid={row['sample_id']:row for row in rows}
 if len(byid)!=len(rows):raise ValueError('duplicate author snapshot sample identity')
 assignments=json.loads(raw['split.json'])['assignments'];traces=ordered.get('traces',[]);trace_map={t['trace_id']:t for t in traces}
 if len(trace_map)!=len(traces):raise ValueError('duplicate source trace identity')
 seen=set();ordered_positions={}
 for trace in traces:
  if trace.get('split')!='train':raise ValueError('held-out source trace cannot qualify author layout')
  ordinals=set()
  for pos in trace['positions']:
   ordinal=pos['source_occurrence_ordinal']
   if ordinal in ordinals:raise ValueError('duplicate source occurrence ordinal')
   ordinals.add(ordinal);ordered_positions[(trace['trace_id'],ordinal)]=pos
   if pos.get('accepted') is not True:
    if pos.get('sample_id') is not None:raise ValueError('unaccepted trace claims a canonical sample')
    continue
   sid=pos.get('sample_id');row=byid.get(sid)
   if sid in seen or row is None or assignments.get(sid)!='train':raise ValueError('trace sample is absent, duplicated or held out')
   seen.add(sid)
   actual_candidate=((row.get('review_provenance') or {}).get('offline_review_context') or {}).get('candidate_id')
   if row.get('label')!=pos.get('label') or actual_candidate!=pos.get('candidate_id') or row.get('parent_source_hash')!=pos.get('source_sha256') or trace.get('source_sha256')!=pos.get('source_sha256'):raise ValueError('trace label, candidate or original source does not match canonical training sample')
 segment_ids=set();eligible=set()
 for segment in profile['segments']:
  if segment['segment_id'] in segment_ids:raise ValueError('duplicate replay segment identity')
  segment_ids.add(segment['segment_id']);trace=trace_map.get(segment.get('trace_id'))
  if trace is None or segment.get('source_sha256')!=trace['source_sha256'] or segment.get('source_slope_y_down')!=trace['normalization']['ink_bottom_ols_slope_y_down']:raise ValueError('eligible fragment does not match its ordered source trace')
  for pos in segment['positions']:
   sid=pos['sample_id']
   if sid in eligible or sid not in seen or pos!=ordered_positions.get((segment['trace_id'],pos['source_occurrence_ordinal'])):raise ValueError('eligible fragment position is duplicated, absent or differs from its exact ordered authority')
   eligible.add(sid)
 return {'ordered_train_samples':len(seen),'eligible_train_samples':len(eligible),'samples_jsonl_sha256':hashlib.sha256(raw['samples.jsonl']).hexdigest(),'split_json_sha256':hashlib.sha256(raw['split.json']).hexdigest(),'all_positions_exact_canonical_train':True}

def _resolve_author_profile(user_id,resolved,*,frozen=None):
 catalog,catalog_sha=_read(CATALOG)
 if not isinstance(catalog,dict) or catalog.get('schema')!='author-layout-catalog/1':raise ValueError('unknown author layout catalog')
 entries=catalog.get('entries')
 if not isinstance(entries,list) or any(not isinstance(e,dict) for e in entries):raise ValueError('malformed author layout catalog entries')
 if not isinstance(resolved,dict) or resolved.get('state')!='bound' or resolved.get('engine_mode')!='M2_ADAPTER':raise ValueError('bound trained adapter required')
 matches=[e for e in catalog.get('entries',[]) if e.get('owner_user_id')==user_id and e.get('profile_id')==resolved.get('style_id')]
 if len(matches)!=1:raise ValueError('this owned author profile has no qualified layout')
 entry=matches[0]
 if entry.get('enabled') is not True:raise ValueError('author layout acceptance remains incomplete')
 adapter=resolved.get('m2_adapter') or {};snapshot=resolved.get('snapshot_binding') or {}
 for actual,expected in [(resolved.get('artifact_id'),entry.get('artifact_id')),(resolved.get('manifest_sha256'),entry.get('artifact_manifest_sha256')),(adapter.get('sha256'),entry.get('adapter_sha256')),(snapshot.get('snapshot_sha256'),entry.get('snapshot_sha256')),(resolved.get('runtime_binding'),entry.get('runtime_binding'))]:
  if actual is None or actual!=expected:raise ValueError('current artifact, author snapshot or numerical runtime does not match layout qualification')
 from engine.style.dataset.binding import verify_snapshot_binding
 from engine.style.dataset.schema import DatasetError
 try:canonical=verify_snapshot_binding(snapshot.get('snapshot_dir'),entry['snapshot_sha256'])
 except DatasetError as exc:raise ValueError('canonical author snapshot is invalid') from exc
 if canonical!=snapshot:raise ValueError('canonical author snapshot changed')
 adapter_path=Path(adapter['path'])
 if adapter_path.is_symlink():raise ValueError('indirect adapter path is not qualified')
 if hashlib.sha256(adapter_path.read_bytes()).hexdigest()!=entry['adapter_sha256']:raise ValueError('layout-bound adapter bytes changed')
 implementation=ROOT/'natural_layout_v1.py'
 if hashlib.sha256(implementation.read_bytes()).hexdigest()!=entry.get('implementation_sha256'):raise ValueError('layout implementation is not the reviewed revision')
 files=entry.get('files');loaded={}
 if not isinstance(files,dict) or not files:raise ValueError('layout authority files missing')
 for name,expected in files.items():
  if not isinstance(name,str) or Path(name).name!=name:raise ValueError('layout authority path escapes catalog')
  value,actual=_read(ASSETS/name)
  if actual!=expected:raise ValueError('layout authority file hash mismatch: '+name)
  loaded[name]=value
 profile=loaded.get(entry.get('profile_filename'))
 validate_profile(profile)
 if digest(profile)!=entry.get('profile_sha256'):raise ValueError('layout profile content changed')
 if profile['advisory']!=loaded.get('measurements.json') or profile['segments']!=(loaded.get('eligible-segments.json') or {}).get('segments'):raise ValueError('embedded measured profile differs from source authority')
 for key,name in [('measurement_sha256','measurements.json'),('trace_sha256','eligible-segments.json'),('ordered_trace_sha256','ordered-traces.json')]:
  if profile.get(key)!=files.get(name):raise ValueError('measured source-trace provenance changed')
 train_trace=_train_trace_binding(snapshot,profile,loaded['ordered-traces.json'])
 binding={'train_trace_binding':train_trace,'schema':'author-layout-binding/1','id':entry['id'],'catalog_sha256':catalog_sha,'entry_sha256':digest(entry),'profile_sha256':entry['profile_sha256'],'implementation_sha256':entry['implementation_sha256'],'owner_user_id':user_id,'profile_id':resolved['style_id'],'artifact_id':resolved['artifact_id'],'artifact_manifest_sha256':resolved['manifest_sha256'],'adapter_sha256':entry['adapter_sha256'],'snapshot_sha256':entry['snapshot_sha256'],'runtime_binding_sha256':resolved['runtime_binding']['binding_sha256'],'authority_file_hashes':files,'qualification':entry['qualification']}
 if frozen is not None and frozen!=binding:raise ValueError('frozen layout qualification changed; create a new render job')
 return {'profile':profile,'binding':binding}

def resolve_author_profile(user_id,resolved,*,frozen=None):
 try:return _resolve_author_profile(user_id,resolved,frozen=frozen)
 except (AttributeError,KeyError,TypeError,IndexError) as exc:
  raise ValueError('malformed author layout authority') from exc
