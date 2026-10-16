"""Real bounded Windows CPU SCR kernel, shared only by trusted qualification.

The SCR mathematics and batching come from the frozen generic implementation.
This kernel creates a fresh cache and fresh zero-delta adapter; it never edits an
old model, snapshot or qualification receipt. No eight-epoch run is invoked here.
"""
from pathlib import Path
import gc,hashlib,importlib.util,json,random,sys,time,weakref
from .runtime_variants import sha,value_sha
from .windows_training_recipe import RECIPE_ID
from .windows_user_scr import CPU,GPU
from .windows_gpu_support import device_for_variant,configure_cuda,observe_cuda

def write(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
def need(ok,detail):
    if not ok:raise ValueError(detail)
def execute(request):
    from .windows_training_runtime import validate_launch
    from .personal_runtime_policy import bootstrap,_verified_scope
    app,verified,base_assets=validate_launch(request)
    policy=bootstrap(app)
    with _verified_scope(app,policy,'qualification',expires_at=request['expires_at']):
        return _execute_verified(request,app,verified,base_assets)

def _execute_verified(request,app,verified,base_assets):
    """Callable actual two-update executor. Launch authentication precedes ML."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
    from .windows_training_runtime import measure, validate_launch
    out=Path(request['out']);out.mkdir(exist_ok=False);started=time.monotonic()
    import torch,numpy as np
    torch.set_num_threads(2);torch.set_num_interop_threads(1);torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    torch.backends.cudnn.benchmark=False;torch.backends.cudnn.deterministic=True
    seed=20261001
    def reset():random.seed(seed);np.random.seed(seed);torch.manual_seed(seed)
    reset();variant=request['profile']['variant_id'];device=device_for_variant(variant)
    cuda_initial=configure_cuda(torch) if variant==GPU else None
    environment=measure(app,request['python'],variant)
    def move(value):
        if isinstance(value,torch.Tensor):return value.to(device)
        if isinstance(value,dict):return {k:move(v) for k,v in value.items()}
        if isinstance(value,(tuple,list)):return type(value)(move(v) for v in value)
        return value
    component={'schema':'windows-scr-qualification-core-trial/1','status':'RUNNING','variant_id':variant,
        'recipe_id':RECIPE_ID,'platform':'win32','device':device,'threads':2,'interop_threads':1,
        'optimizer_steps':0,'generation_calls':0,'synthetic_test_only':False,'may_publish':False,
        'claims_quality_pass':False,'preparation_authority_created':False,'registered_runtime_created':False,
        'full_trainer_resume_claimed':False,'source_hashes':request['profile']['required_source_files'],
        'driver_sha256':sha(app/'engine/style/m2_adapter/windows_scr_training_probe.py'),
        'executable':sys.executable,'numerical_environment':environment['numerical_environment'],
        'snapshot_binding':verified['snapshot_binding'],'scr_data_binding':verified['data_binding']}
    def progress(stage,**fields):print(json.dumps({'stage':stage,'seconds':time.monotonic()-started,**fields}),flush=True)
    if variant==GPU:
        from .windows_gpu_support import measure_bridge_parity
        component['cuda_cpu_deform_parity']=measure_bridge_parity(torch)
        progress('ACTUAL_MASKED_AND_UNMASKED_CUDA_CPU_DEFORM_PARITY_COMPLETE')
    paths={**request['assets'],'reference_cache_manifest':str(out/'cache'/request['windows_cache_identity']['sha256']/'manifest.json'),
        'reference_cache_vectors':str(out/'cache'/request['windows_cache_identity']['sha256']/'reference-vectors.npz')}
    from .user_scr_cache import prepare_user_reference_cache
    from .user_scr_objective import make_user_scr_reference_producer,UserSCRTwoNegativeObjective
    from .user_scr_contract import asset_contract,objective_contract
    producer=make_user_scr_reference_producer(paths);ref=weakref.ref(producer._encoder);forward={'reference':0,'query':0}
    def count(kind):
        def hook(_m,_i,_o):forward[kind]+=1
        return hook
    producer._encoder.register_forward_hook(count('reference'))
    prepared=prepare_user_reference_cache(Path(paths['reference_cache_manifest']).parent,verified['snapshot_binding'],paths,feature_producer=producer)
    execution=prepared['manifest']['feature_execution'];labels=len({r['label'] for r in verified['data_binding']['rows']})
    need(forward['reference']==verified['data_binding']['counts']['total']+2*labels,'reference hook forwards differ')
    component.update(scr_cache_binding=prepared['cache_binding'],reference_feature_execution={**execution,'created_fresh_in_trial':True},
        scr_assets=asset_contract(),scr_objective=objective_contract())
    del producer;gc.collect();need(ref() is None,'reference SCR encoder was not released')
    progress('FRESH_WINDOWS_SCR_CACHE_COMPLETE',reference_forwards=forward['reference'])
    spec=importlib.util.spec_from_file_location('_windows_scr_math_helpers',app/'scripts/probe_m2_runtime_scr_two_negative.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper);digest=helper.tensor_digest
    from .adapter import load_base_model,M2FontDiffuser,load_snapshot_pairs,GlyphPairDataset,load_verified_adapter_state,_glyph_to_tensor,render_content_char
    from .preprocessing import UPSTREAM_PROFILE
    from .spatial_topology import SPATIAL_TOPOLOGY_ID
    from .runner import user_scr_batch_identity
    from .x0_objectives import analytic_x0
    from .deterministic_ops import execution_counts
    repo=Path(base_assets['fd_repo']);font=repo/'ttf/KaiXinSongA.ttf'
    base,args=load_base_model(repo,device=device,expected_hashes=base_assets['manifest']['base_npz_sha256'],runtime_manifest_path=base_assets['manifest_path'])
    base.requires_grad_(False).eval();before=digest(base.state_dict())
    from src import build_ddpm_scheduler,FontDiffuserModelDPM,FontDiffuserDPMPipeline
    scheduler=build_ddpm_scheduler(args);pairs=load_snapshot_pairs(Path(request['snapshot']),'train');indices={p['sample_id']:i for i,p in enumerate(pairs)}
    chosen=[indices[r['sample_id']] for r in request['fixed_rows']]
    from torch.utils.data import default_collate
    warm=GlyphPairDataset(pairs,font,seed,scheduler,preprocessing_profile=UPSTREAM_PROFILE,conditioning_dropout=.1)
    warm_batch=move(default_collate([warm[i] for i in chosen]));wrapper=M2FontDiffuser(base,font,adapter_topology_id=SPATIAL_TOPOLOGY_ID).to(device)
    with torch.no_grad():wrapper(warm_batch[0])
    for p in wrapper.adapter.parameters():p.data.zero_()
    del warm,warm_batch;gc.collect();reset()
    from engine.style.trainer.config import FreezePolicy
    trainable,_=FreezePolicy(trainable_prefixes=('adapter.',)).apply(wrapper)
    need(all(p.device.type==device for p in base.parameters()) and all(p.device.type==device for p in wrapper.adapter.parameters()),'base/adapter must execute on the selected device')
    need(len(trainable)==56 and sum(p.numel() for p in wrapper.adapter.parameters())==2288300,'fresh full56 adapter mismatch')
    from .adapter_topology import RESIDUAL_HOOK_KEYS
    residual={k:0 for k in RESIDUAL_HOOK_KEYS}
    for k,block in wrapper.adapter.blocks.items():
        def hook(_m,_i,_o,key=k):residual[key]+=1
        block.register_forward_hook(hook)
    ds=GlyphPairDataset(pairs,font,seed,scheduler,preprocessing_profile=UPSTREAM_PROFILE,conditioning_dropout=.1)
    batch=move(default_collate([ds[i] for i in chosen]));inputs=user_scr_batch_identity(batch[0],verified['data_binding'],'train')
    component['fixed_batch']={'rows':request['fixed_rows'],'access_trace':ds.access_trace,'input_tensor_digest':digest(inputs),'target_noise_digest':digest(batch[1]),'reused_identical_batch':True}
    optimizer=torch.optim.AdamW(wrapper.adapter.parameters(),lr=1e-4,weight_decay=.01)
    scr=UserSCRTwoNegativeObjective(paths,verified['snapshot_binding'],expected_data_binding=verified['data_binding'],expected_cache_binding=prepared['cache_binding']).to(device)
    query=weakref.ref(scr._encoder);scr._encoder.register_forward_hook(count('query'));component['steps']=[]
    for step in (1,2):
        start=time.perf_counter();wrapper.train();optimizer.zero_grad(set_to_none=True)
        prediction=wrapper(inputs);epsilon=torch.nn.functional.mse_loss(prediction,batch[1]);offset=.1*wrapper.last_offset.square().mean()
        x0=analytic_x0(prediction,inputs['x_t'],inputs['t'],scheduler.alphas_cumprod.to(device=prediction.device,dtype=torch.float32),clip=True);aux=scr.loss(x0,inputs,'train').mean
        query_gradient,=torch.autograd.grad(aux,x0,retain_graph=True)
        need(bool(torch.isfinite(query_gradient).all()) and bool(query_gradient.abs().sum()>0),'SCR live query gradient absent')
        total=epsilon+offset+.01*aux;total.backward();gradients={}
        for name,p in wrapper.adapter.named_parameters():
            g=p.grad;need(g is not None and bool(torch.isfinite(g).all()) and bool(g.abs().sum()>0),'adapter gradient missing/nonfinite/zero: '+name)
            gradients[name]={'shape':list(g.shape),'dtype':'float32','finite':True,'abs_sum':float(g.abs().sum()),'device':g.device.type}
        need(len(gradients)==56 and all(p.grad is None for p in base.parameters()),'named gradients/base freeze differs')
        norm=torch.nn.utils.clip_grad_norm_(wrapper.adapter.parameters(),1.,error_if_nonfinite=True);optimizer.step()
        row={'step':step,'seconds':time.perf_counter()-start,'epsilon_mse':float(epsilon.detach()),'offset_term':float(offset.detach()),
            'scr_raw':float(aux.detach()),'scr_term':float((.01*aux).detach()),'total':float(total.detach()),
            'unclipped_gradient_norm':float(norm),'scr_query_gradient_abs_sum':float(query_gradient.abs().sum()),'scr_query_gradient_device':query_gradient.device.type,
            'nonzero_adapter_gradient_tensors':56,'base_gradients_absent':True,'adapter_gradients':gradients}
        if variant==GPU:row['cuda_allocation_observation']=observe_cuda(torch,cuda_initial)
        component['steps'].append(row);component['optimizer_steps']+=1;write(out/f'optimizer-step-{step}.json',row)
        progress('ACTUAL_SCR_OPTIMIZER_UPDATE',step=step,loss=row['total'],named_gradients=56)
        del prediction,epsilon,offset,x0,aux,total,query_gradient,gradients
    component['scr_execution_evidence']=scr.evidence();need(forward['query']==2,'two actual query forwards required')
    write(out/'actual-scr-execution-evidence.json',component['scr_execution_evidence'])
    del scr;optimizer.zero_grad(set_to_none=True);gc.collect();need(query() is None,'query SCR encoder was not released')
    state=wrapper.adapter.state_dict();component['adapter_state_observation']={k:{'shape':list(v.shape),'dtype':'float32','finite':bool(torch.isfinite(v).all()),'device':v.device.type,'sha256':hashlib.sha256(v.detach().cpu().contiguous().numpy().tobytes()).hexdigest()} for k,v in state.items()}
    component['adapter_load']={'weights_only':True,'strict_load':True,'map_location':'cpu','partial_load':False,'prefix_stripping':False}
    adapter_sha=digest(state);optimizer_sha=digest(optimizer.state_dict());state_path=out/'adapter-optimizer-state.pt'
    metadata={'schema':'windows-scr-qualification-delta/1','recipe_id':RECIPE_ID,'qualification_only':True,'optimizer_steps':2,
        'snapshot_sha256':request['snapshot_sha256'],'windows_cache_identity':request['windows_cache_identity'],'may_publish':False}
    with state_path.open('xb') as f:torch.save({'adapter_state':state,'optimizer_state':optimizer.state_dict(),'meta':metadata},f)
    restored=torch.load(state_path,map_location='cpu',weights_only=True)
    need(set(restored)=={'adapter_state','optimizer_state','meta'} and restored['meta']==metadata,'small roundtrip metadata changed')
    with torch.no_grad():
        for p in wrapper.adapter.parameters():p.zero_()
    optimizer.state.clear();load_verified_adapter_state(wrapper,restored['adapter_state']);optimizer.load_state_dict(restored['optimizer_state'])
    need(digest(wrapper.adapter.state_dict())==adapter_sha and digest(optimizer.state_dict())==optimizer_sha,'strict state/optimizer roundtrip changed')
    component['delta_roundtrip']={'weights_only':True,'strict_load':True,'equal':True,'full_trainer_resume_claimed':False,
        'adapter_sha256':adapter_sha,'optimizer_sha256':optimizer_sha,'state_file_sha256':sha(state_path),'bytes':state_path.stat().st_size}
    del restored,optimizer,batch,inputs,ds,state;gc.collect();wrapper.eval().requires_grad_(False)
    need(digest(base.state_dict())==before,'base state changed after training')
    reference=request['reference'];pair=next(p for p in pairs if p['sample_id']==reference['sample_id'])
    style=_glyph_to_tensor(Path(pair['glyph_path']),preprocessing_profile=UPSTREAM_PROFILE).unsqueeze(0).to(device)
    content=render_content_char('\u5fc3',font,preprocessing_profile=UPSTREAM_PROFILE).unsqueeze(0).to(device)
    model=FontDiffuserModelDPM(unet=base.unet,style_encoder=base.style_encoder,content_encoder=base.content_encoder).eval().requires_grad_(False)
    pipe=FontDiffuserDPMPipeline(model,scheduler,guidance_scale=7.5);s_before=dict(wrapper.adapter.spatial_calls);r_before=dict(residual);f_before=dict(forward)
    with torch.inference_mode():rgb=pipe.generate(content,style,batch_size=1,order=2,num_inference_step=20,content_encoder_downsample_size=3,generator=torch.Generator(device='cpu').manual_seed(402907350))[0].convert('RGB')
    gen={'target':'\u5fc3','seed':402907350,'steps':20,'cfg':7.5,'reference':reference,'not_blind_quality_evidence':True,
        'outputs':[],'sha256':[],'raw_rgb_sha256':[],'output_count':0,'pixels_saved_before_guard':False}
    helper.save_repeat_output(rgb,out,0,gen)
    component.update(generation_calls=1,generation=gen,inference_scr_feature_forwards=sum(forward.values())-sum(f_before.values()),
        spatial_generation_hook_calls={k:v-s_before[k] for k,v in wrapper.adapter.spatial_calls.items()},
        residual_generation_hook_calls={k:v-r_before[k] for k,v in residual.items()},operator_execution_counts=execution_counts(base),
        base_state_before_sha256=before,base_state_after_sha256=digest(base.state_dict()),status='MEASURED_FULL_SCR_GPU_TRIAL' if variant==GPU else 'MEASURED_FULL_SCR_CPU_TRIAL')
    need(component['inference_scr_feature_forwards']==0 and component['base_state_before_sha256']==component['base_state_after_sha256'],'generation freeze/feature counters differ')
    if variant==GPU:component['cuda_allocation_observation']=observe_cuda(torch,cuda_initial)
    validate_launch(request);need(measure(app,request['python'],request['profile']['variant_id'])==environment,'final environment changed')
    result={'component':component,'environment':environment,'asset_paths':paths,'seconds':time.monotonic()-started}
    write(out/'EXECUTOR-RESULT.json',result);progress(component['status']+'_COMPLETE');return result
