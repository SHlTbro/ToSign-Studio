"""Device-specific SCR qualification checks. Never creates execution authority."""
from pathlib import Path
from .runtime_variants import require,value_sha

GPU_LIMIT=3*2**30
MIN_FREE=4*2**30

def device_for_variant(variant):
    from .windows_user_scr import CPU,GPU
    require(variant in (CPU,GPU),'E_WINDOWS_SCR_VARIANT','exact Windows CPU or CUDA SCR variant required')
    return 'cuda' if variant==GPU else 'cpu'

def gpu_lock_path(app):
    # Fixed consumer-local config is covered by the RSA source inventory and
    # GPU execution profile. A candidate-relative lock cannot protect one card
    # shared by several source namespaces. Never accept an environment override.
    import json
    from .runtime_variants import safe_relative
    p=safe_relative(app,'config/windows-gpu-resources.json')
    require(p.is_file() and not any(x.is_symlink() or x.is_junction() for x in (p,*p.parents)),
        'E_WINDOWS_SCR_GPU_RESOURCE','regular signed common GPU resource config required')
    row=json.loads(p.read_text(encoding='utf-8'))
    require(set(row)=={'schema','device_index','gpu_lock_path'} and row.get('schema')=='windows-scr-shared-gpu-resource/1'
        and type(row.get('device_index')) is int and row['device_index']==0,
        'E_WINDOWS_SCR_GPU_RESOURCE','exact common GPU device/lock configuration required')
    path=Path(row['gpu_lock_path']).absolute()
    require(path.drive.upper()=='E:' and path.name=='gpu.lock'
        and not any(x.is_symlink() or x.is_junction() for x in (path,*path.parents)),
        'E_WINDOWS_SCR_GPU_RESOURCE','shared regular E-only gpu.lock required')
    return path

def acquire_gpu_lease(app,task):
    import os,json
    from .personal_runtime_resources import process_start
    p=gpu_lock_path(app)
    require(p.drive.upper()=='E:' and not any(x.is_symlink() or x.is_junction() for x in (p,*p.parents)),
            'E_WINDOWS_SCR_PATH','regular E GPU lock required')
    p.parent.mkdir(parents=True,exist_ok=True)
    token={'pid':os.getpid(),'process_start':process_start(),'nonce':os.urandom(16).hex(),'task':task}
    try:
        with p.open('x',encoding='utf-8') as f:json.dump(token,f)
    except FileExistsError as exc:
        from .runtime_variants import RuntimeUnavailable
        raise RuntimeUnavailable('E_WINDOWS_SCR_BUSY','GPU lease exists; no stale lock or existing process changed') from exc
    return p,token

def release_gpu_lease(lease):
    import json,os,uuid
    from .windows_qualification_recovery import _locked_lease
    p,token=lease
    # Atomic rename through the SAME exclusive Windows file handle. Keep the
    # released token as evidence; a successor path can never be deleted here.
    with _locked_lease(p) as (raw,rename):
        require(json.loads(raw)==token,'E_WINDOWS_SCR_LEASE','GPU lease ownership changed')
        archive=p.with_name('gpu-released-'+uuid.uuid4().hex+'.json')
        rename(archive)

def configure_cuda(torch):
    require(torch.cuda.is_available(),'BLOCKED_ENV','actual CUDA device required')
    free,total=torch.cuda.mem_get_info(0)
    require(type(free) is int and type(total) is int and free>=MIN_FREE and total>=MIN_FREE,
            'BLOCKED_ENV','at least 4 GiB currently free CUDA memory required')
    # The allocator itself enforces the bound during calls, including native DPM.
    torch.cuda.set_per_process_memory_fraction(GPU_LIMIT/total,device=0)
    torch.cuda.reset_peak_memory_stats(0)
    return {'device_index':0,'free_before_bytes':free,'total_bytes':total,
            'allocator_limit_bytes':GPU_LIMIT,'minimum_free_bytes':MIN_FREE}

def observe_cuda(torch,initial):
    torch.cuda.synchronize(0)
    row={**initial,'peak_allocated_bytes':int(torch.cuda.max_memory_allocated(0)),
         'peak_reserved_bytes':int(torch.cuda.max_memory_reserved(0)),
         'allocator_enforced':True}
    validate_cuda_observation(row)
    return row

def validate_cuda_observation(row):
    keys={'device_index','free_before_bytes','total_bytes','allocator_limit_bytes','minimum_free_bytes',
          'peak_allocated_bytes','peak_reserved_bytes','allocator_enforced'}
    require(isinstance(row,dict) and set(row)==keys,'E_WINDOWS_SCR_GPU_EVIDENCE','complete CUDA allocation observation required')
    require(all(type(row[k]) is int for k in keys-{'allocator_enforced'}),
            'E_WINDOWS_SCR_GPU_EVIDENCE','typed GPU byte counts required')
    require(row['device_index']==0 and row['minimum_free_bytes']==MIN_FREE
            and row['free_before_bytes']>=MIN_FREE and row['total_bytes']>=row['free_before_bytes']
            and row['allocator_limit_bytes']==GPU_LIMIT and row['allocator_enforced'] is True
            and 0<row['peak_allocated_bytes']<=row['peak_reserved_bytes']<=GPU_LIMIT,
            'E_WINDOWS_SCR_GPU_BUDGET','CUDA allocation/free-memory bounds differ')
    return row

def validate_mixed_counts(counts):
    modules={f'unet.up_blocks.{b}.dcn_deforms.{i}' for b in (1,2) for i in range(3)}
    require(isinstance(counts,dict) and set(counts)==modules and all(
        type(r.get('forward_calls')) is int and r['forward_calls']>=22
        and type(r.get('cross_device_calls')) is int and r['cross_device_calls']==r['forward_calls']
        for r in counts.values()),'E_WINDOWS_SCR_GPU_EVIDENCE','every frozen CPU deform call must cross CUDA/CPU differentiably')

def measure_bridge_parity(torch):
    """Tiny bounded parity inside the owned GPU trial; never run on import."""
    import copy,hashlib,gc
    from torchvision.ops import DeformConv2d
    from .deterministic_ops import install_cpu_deform_policy,execution_counts,policy_identity
    def digest(t):return hashlib.sha256(t.detach().cpu().contiguous().numpy().tobytes()).hexdigest()
    cases=[]
    with torch.random.fork_rng(devices=[0]):
        torch.manual_seed(618)
        for masked in (False,True):
            native=DeformConv2d(2,3,3,padding=1).float();wrapped=copy.deepcopy(native).to('cuda')
            install_cpu_deform_policy(wrapped,expected_count=1)
            x=torch.randn(1,2,5,6,requires_grad=True)
            offset=(torch.randn(1,18,5,6)*.7).requires_grad_(True)
            mask=torch.sigmoid(torch.randn(1,9,5,6)).requires_grad_(True) if masked else None
            x2=x.detach().to('cuda').clone().requires_grad_(True)
            o2=offset.detach().to('cuda').clone().requires_grad_(True)
            m2=mask.detach().to('cuda').clone().requires_grad_(True) if masked else None
            names=['input','offset','weight','bias']+(['mask'] if masked else [])
            cpu_inputs=[x,offset,native.weight,native.bias]+([mask] if masked else [])
            gpu_inputs=[x2,o2,wrapped.weight,wrapped.bias]+([m2] if masked else [])
            a=native(x,offset,mask);probe=torch.randn_like(a)
            ga=torch.autograd.grad((a*probe).sum(),cpu_inputs)
            reference={'output_sha256':digest(a),'gradient_sha256':{k:digest(g) for k,g in zip(names,ga)}}
            repeats=[]
            for _ in range(2):
                b=wrapped(x2,o2,m2);gb=torch.autograd.grad((b*probe.to('cuda')).sum(),gpu_inputs)
                require(b.device.type=='cuda' and torch.equal(a,b.cpu())
                    and all(g.device.type=='cuda' and bool(torch.isfinite(g).all()) and bool(g.abs().sum()>0)
                        and torch.equal(ref,g.cpu()) for ref,g in zip(ga,gb)),
                    'E_WINDOWS_SCR_GPU_PARITY','actual CPU-deform CUDA outputs and all gradients must repeat bit-exactly')
                repeats.append({'output_sha256':digest(b),'gradient_sha256':{k:digest(g) for k,g in zip(names,gb)}})
            cases.append({'masked':masked,'shape':[1,2,5,6],'native_device':'cpu','caller_device':'cuda',
                'all_gradients_finite_nonzero':True,'reference':reference,'repeats':repeats,
                'operator_execution_counts':execution_counts(wrapped)['']})
    del native,wrapped,x,offset,mask,x2,o2,m2,a,b,ga,gb,cpu_inputs,gpu_inputs;gc.collect()
    return {'schema':'windows-scr-cuda-cpu-deform-parity/1','policy':policy_identity(),'cases':cases}

def validate_bridge_parity(report):
    import re
    from .deterministic_ops import policy_identity
    require(isinstance(report,dict) and report.get('schema')=='windows-scr-cuda-cpu-deform-parity/1'
        and report.get('policy')==policy_identity() and isinstance(report.get('cases'),list)
        and len(report['cases'])==2,'E_WINDOWS_SCR_GPU_PARITY','current-source masked and unmasked CPU-deform parity required')
    for masked,row in zip((False,True),report['cases']):
        require(row.get('masked') is masked and row.get('shape')==[1,2,5,6]
            and row.get('native_device')=='cpu' and row.get('caller_device')=='cuda'
            and row.get('all_gradients_finite_nonzero') is True
            and row.get('operator_execution_counts')=={'forward_calls':2,'cross_device_calls':2},
            'E_WINDOWS_SCR_GPU_PARITY','actual differentiable mixed-device parity differs')
        ref=row.get('reference',{});keys={'input','offset','weight','bias'}|({'mask'} if masked else set())
        require(isinstance(ref,dict) and set(ref)=={'output_sha256','gradient_sha256'}
            and isinstance(ref.get('gradient_sha256'),dict) and set(ref['gradient_sha256'])==keys
            and all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) for v in (ref['output_sha256'],*ref['gradient_sha256'].values()))
            and isinstance(row.get('repeats'),list) and row['repeats']==[ref,ref],
            'E_WINDOWS_SCR_GPU_PARITY','both CUDA repeat outputs and each gradient must match native CPU hashes')
    return report
