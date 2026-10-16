"""Dependency-free, fail-closed adapter topology identities."""
from __future__ import annotations
import hashlib,json
from pathlib import Path

BASELINE_TOPOLOGY_ID='resnet22-pointwise-v1'
OFFSET_TOPOLOGY_ID='resnet22-offsetproj6-delta-v1'
TOPOLOGY_FIELDS=('adapter_topology_id','adapter_topology','adapter_topology_sha256')
OFFSET_HEADS=tuple({'module':f'up_blocks.{block}.sc_interpreter_offsets.{index}.proj_out','in_channels':channels,'out_channels':18}
                   for block,channels in ((1,128),(2,64)) for index in range(3))

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

RESIDUAL_MODULES=tuple([f'down_blocks.{b}.resnets.{i}' for b in range(4) for i in range(2)]+[f'mid_block.resnets.{i}' for i in range(2)]+[f'up_blocks.{b}.resnets.{i}' for b in range(4) for i in range(3)])
def residual_key(module_name):return 'h'+hashlib.sha256(module_name.encode()).hexdigest()[:10]
RESIDUAL_HOOK_KEYS=tuple(residual_key(name) for name in RESIDUAL_MODULES)

def offset_key(module_name):return 'o'+hashlib.sha256(module_name.encode()).hexdigest()[:10]

def topology_contract(topology_id=BASELINE_TOPOLOGY_ID):
 if topology_id not in (BASELINE_TOPOLOGY_ID,OFFSET_TOPOLOGY_ID):raise ValueError('E_ADAPTER_TOPOLOGY: unrecognized topology')
 geometry=topology_id==OFFSET_TOPOLOGY_ID
 body={'schema':'m2-adapter-topology/1','id':topology_id,'base_engine_id':'fontdiffuser-hf7bd89f3-npz-v1',
       'residual_hooks':{'module_class':'ResnetBlock2D','count':22,'module_names':list(RESIDUAL_MODULES),'hook_keys':list(RESIDUAL_HOOK_KEYS),'kernel':[1,1],'state_prefix':'blocks','zero_init':True},
       'offset_projection_heads':[dict(x) for x in OFFSET_HEADS] if geometry else [],
       'offset_delta':{'count':6 if geometry else 0,'kernel':[1,1],'stride':[1,1],'padding':[0,0],'operation':'original_proj_out(input) + delta_weight(input) + delta_bias','state_prefix':'offset_blocks','zero_init':True,'initialization_preserves_rng_state':True,'parameters':10476 if geometry else 0},
       'expected_adapter_tensors':56 if geometry else 44,'expected_adapter_parameters':2281388 if geometry else 2270912,
       'base_parameter_policy':'all original base tensors frozen; separately exported adapter deltas only',
       'precision':'float32 without autocast','implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
 return {**body,'sha256':digest(body)}

def topology_fields(topology_id=BASELINE_TOPOLOGY_ID):
 record=topology_contract(topology_id)
 return {'adapter_topology_id':topology_id,'adapter_topology':record,'adapter_topology_sha256':record['sha256']}

def validate_topology_fields(record, *, required=False):
 if not isinstance(record,dict):raise ValueError('E_ADAPTER_TOPOLOGY: metadata must be an object')
 present=[name in record for name in TOPOLOGY_FIELDS]
 if not any(present):
  if required:raise ValueError('E_ADAPTER_TOPOLOGY: explicit topology metadata required')
  return topology_contract(BASELINE_TOPOLOGY_ID)
 if not all(present):raise ValueError('E_ADAPTER_TOPOLOGY: partial topology metadata')
 expected=topology_contract(record['adapter_topology_id'])
 supplied=record['adapter_topology']
 if not isinstance(supplied,dict):raise ValueError('E_ADAPTER_TOPOLOGY: topology object required')
 try:actual=digest({k:v for k,v in supplied.items() if k!='sha256'})
 except (TypeError,ValueError) as exc:raise ValueError('E_ADAPTER_TOPOLOGY: noncanonical topology data') from exc
 if actual!=supplied.get('sha256') or actual!=expected['sha256'] or record['adapter_topology_sha256']!=actual:
  raise ValueError('E_ADAPTER_TOPOLOGY: topology identity/hash mismatch')
 return expected

def expected_offset_shapes(topology_id):
 topology_contract(topology_id)
 if topology_id==BASELINE_TOPOLOGY_ID:return {}
 shapes={}
 for h in OFFSET_HEADS:
  prefix='offset_blocks.'+offset_key(h['module'])
  shapes[prefix+'.weight']=(18,h['in_channels'],1,1);shapes[prefix+'.bias']=(18,)
 return shapes

def validate_inference_topology_execution(receipt):
 """Shared cache/runtime/quality proof: exact keys, typed counts and active mode."""
 if not isinstance(receipt,dict):raise ValueError('E_ADAPTER_EXECUTION: receipt object required')
 topology=validate_topology_fields(receipt,required=receipt.get('recipe_id')=='m2-control-offset-delta-v1')
 geometry=topology['id']==OFFSET_TOPOLOGY_ID
 if geometry != (receipt.get('recipe_id')=='m2-control-offset-delta-v1'):
  raise ValueError('E_ADAPTER_EXECUTION: recipe/topology mismatch')
 source=receipt.get('source')
 if source not in ('M1','M2') or receipt.get('adapter_applied') is not (source=='M2'):
  raise ValueError('E_ADAPTER_EXECUTION: source/adapter mode mismatch')
 residual=receipt.get('adapter_hook_calls')
 if not isinstance(residual,dict) or set(residual)!=set(RESIDUAL_HOOK_KEYS):
  raise ValueError('E_ADAPTER_EXECUTION: residual hook key set differs')
 offsets=receipt.get('offset_projection_hook_calls',{})
 wanted={h['module'] for h in OFFSET_HEADS} if geometry else set()
 if not isinstance(offsets,dict) or set(offsets)!=wanted:
  raise ValueError('E_ADAPTER_EXECUTION: offset hook key set differs')
 active=source=='M2'
 for value in [*residual.values(),*offsets.values()]:
  if type(value) is not int or (value<=0 if active else value!=0):
   raise ValueError('E_ADAPTER_EXECUTION: counts must be positive M2 integers or exact M1 zero')
 return topology
