"""Explicit raw-skip spatial offset topology; legacy topology bytes stay frozen."""
from __future__ import annotations
import hashlib
from pathlib import Path
from .adapter_topology import digest,RESIDUAL_MODULES,RESIDUAL_HOOK_KEYS,TOPOLOGY_FIELDS
SPATIAL_TOPOLOGY_ID='resnet22-skipoffset6-delta-v1'
SPATIAL_RECIPE_ID='m2-control-spatial-offset-v1'
SPATIAL_HEADS=tuple({'module':f'up_blocks.{b}.sc_interpreter_offsets.{i}','input_index':0,'in_channels':channels,'out_channels':18}
                   for b,widths in ((1,(256,256,128)),(2,(128,128,64))) for i,channels in enumerate(widths))
def spatial_key(module_name):return 's'+hashlib.sha256(module_name.encode()).hexdigest()[:10]
def topology_contract():
 body={'schema':'m2-adapter-topology/1','id':SPATIAL_TOPOLOGY_ID,'base_engine_id':'fontdiffuser-hf7bd89f3-npz-v1',
       'residual_hooks':{'module_class':'ResnetBlock2D','count':22,'module_names':list(RESIDUAL_MODULES),'hook_keys':list(RESIDUAL_HOOK_KEYS),'kernel':[1,1],'state_prefix':'blocks','zero_init':True},
       'offset_projection_heads':[],'offset_delta':{'count':0,'parameters':0,'state_prefix':'offset_blocks'},
       'spatial_offset_heads':[dict(x) for x in SPATIAL_HEADS],
       'spatial_delta':{'count':6,'kernel':[1,1],'stride':[1,1],'padding':[0,0],'operation':'original_interpreter_output + delta_conv(group_norm(raw_skip_input))','input_source':'OffsetRefStrucInter.forward inputs[0], before frozen attention','normalization':{'kind':'GroupNorm','num_groups':32,'eps':1e-6,'affine':False,'per_sample':True},'state_prefix':'spatial_blocks','zero_init':True,'initialization_preserves_rng_state':True,'parameters':17388},
       'expected_adapter_tensors':56,'expected_adapter_parameters':2288300,'base_parameter_policy':'all original base tensors frozen; separately exported adapter deltas only','precision':'float32 without autocast','implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
 return {**body,'sha256':digest(body)}
def topology_fields():
 t=topology_contract();return {'adapter_topology_id':SPATIAL_TOPOLOGY_ID,'adapter_topology':t,'adapter_topology_sha256':t['sha256']}
def validate_spatial_topology_fields(record):
 if not isinstance(record,dict) or not all(k in record for k in TOPOLOGY_FIELDS):raise ValueError('E_ADAPTER_TOPOLOGY: complete explicit spatial topology required')
 t=topology_contract();supplied=record['adapter_topology']
 if not isinstance(supplied,dict) or record['adapter_topology_id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('E_ADAPTER_TOPOLOGY: spatial topology identity')
 try:actual=digest({k:v for k,v in supplied.items() if k!='sha256'})
 except (TypeError,ValueError) as exc:raise ValueError('E_ADAPTER_TOPOLOGY: noncanonical spatial topology') from exc
 if actual!=supplied.get('sha256') or actual!=t['sha256'] or record['adapter_topology_sha256']!=actual:raise ValueError('E_ADAPTER_TOPOLOGY: spatial topology hash mismatch')
 return t
def expected_spatial_shapes():
 shapes={}
 for h in SPATIAL_HEADS:
  k='spatial_blocks.'+spatial_key(h['module']);shapes[k+'.weight']=(18,h['in_channels'],1,1);shapes[k+'.bias']=(18,)
 return shapes
