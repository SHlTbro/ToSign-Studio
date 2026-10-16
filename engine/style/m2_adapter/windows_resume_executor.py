"""Actual shared S05 Trainer trial. Deferred ML; never called by metadata tests."""
from pathlib import Path
import copy, hashlib, json, os
from .runtime_variants import require, sha
from .windows_resume_recipe import plan

def _write(path, value):
    with Path(path).open('x',encoding='utf-8') as f:
        json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False)

def execute(request):
    from .windows_resume_runtime import validate_launch
    from .personal_runtime_policy import _verified_scope
    app,policy,selected = validate_launch(request)
    with _verified_scope(app,policy,'qualification',expires_at=request['expires_at']):
        return _execute(request,app,selected)

def _execute(request, app, selected):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .windows_training_runtime import measure
    # Every check above precedes the actual Torch import and model loading.
    import torch, numpy as np
    torch.set_num_threads(2); torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    torch.backends.cudnn.benchmark=False;torch.backends.cudnn.deterministic=True
    require(measure(app,request['python']) == request['environment'], 'E_RESUME_ENVIRONMENT','actual numerical environment changed')
    out = Path(request['out']); out.mkdir(exist_ok=False)
    req,cfg,resolved,preprocessing,config_sha = plan(request['config'],request['resume_binding'],request['snapshot'],out)
    require(config_sha == request['context']['trainer_config_hash'], 'E_RESUME_CONFIG','actual S05 config differs')
    from .adapter import load_base_model, load_snapshot_pairs
    from .topology_dispatch import validate_topology_fields
    from .native_training_session import build_training_session
    from .trainer_resume_protocol import verify_manifest, digest
    from engine.style.dataset.binding import verify_snapshot_binding
    from engine.style.trainer.trainer import TrainerError
    from .windows_resume_capability import arrays_sha
    snapshot_binding = verify_snapshot_binding(request['snapshot'],request['resume_binding']['snapshot_sha256'])
    base,args = load_base_model(Path(selected['fd_repo']), device='cpu', expected_hashes=req['base_npz_sha256'],
                                runtime_manifest_path=selected['manifest_path'])
    from src import build_ddpm_scheduler
    scheduler = build_ddpm_scheduler(args)
    font = Path(req['content_font_path'])
    train_pairs = load_snapshot_pairs(Path(request['snapshot']),'train')
    val_pairs = load_snapshot_pairs(Path(request['snapshot']),'val')
    record = None
    if request['branch'] == 'resumed':
        saved = request['checkpoint']
        directory = Path(saved['directory']).absolute()
        require(directory.is_relative_to(Path(request['trial_out'])/'cancelled/train_run/resume_generations'),
                'E_RESUME_ANCHOR','owned cancelled branch checkpoint required')
        record = verify_manifest(directory,request['resume_binding'],saved['manifest_sha256'])
    trace = []
    # The shared builder installs qualification trace callbacks before the same
    # Trainer constructor executes its actual restricted resume_from path.
    session = build_training_session(req,cfg=cfg,base=base,scheduler=scheduler,font_path=font,device='cpu',
        train_pairs=train_pairs,val_pairs=val_pairs,preprocessing=preprocessing,
        conditioning_dropout=resolved['train']['conditioning_dropout'],adapter_topology=validate_topology_fields(resolved),
        resume_binding=request['resume_binding'],resume_record=record,scr_enabled=True,user_scr_enabled=True,
        snapshot_binding=snapshot_binding,out_dir=out,trial_trace=trace)
    trainer = session['trainer']; wrapper = session['wrapper']

    def arrays():
        result = {f'model::{name}':wrapper.state_dict()[name].detach().cpu().contiguous().numpy()
                  for name in trainer.trainable_names}
        named = dict(wrapper.named_parameters())
        for name in trainer.trainable_names:
            state = trainer.optimizer.state.get(named[name],{})
            for field,prefix in (('exp_avg','adam_exp_avg'),('exp_avg_sq','adam_exp_avg_sq'),('step','adam_step')):
                if field in state:
                    value=state[field]
                    result[f'{prefix}::{name}'] = value.detach().cpu().contiguous().numpy() if torch.is_tensor(value) else np.asarray(value)
        return result

    def frozen(path):
        values = {name:value.detach().cpu().contiguous().numpy() for name,value in wrapper.state_dict().items()
                  if name not in trainer.trainable_names}
        with path.open('xb') as f:np.savez(f,**values)
        return {'path':path.relative_to(Path(request['trial_out'])).as_posix(),'sha256':sha(path),'bytes':path.stat().st_size}
    before = frozen(out/'base-before.npz') if request['branch'] in ('continuous','resumed') else None
    points = {}; latest = {}
    metadata_fields = ('checkpoint_schema','trainer_version','code_version','config_hash','run_id','data_snapshot_sha256',
        'resume_binding','frozen_model_sha256','model_structure','epoch','global_step','batch_ptr')
    def export(point, frozen_record):
        folder=out/point;folder.mkdir(exist_ok=False)
        payload = trainer._ckpt_payload(); values = arrays()
        values['rng::torch_cpu']=payload['rng']['torch'].detach().cpu().numpy()
        values['rng::shuffle']=payload['loader_state']['shuffle_rng'].detach().cpu().numpy()
        values['rng::iterator']=payload['loader_state']['iterator_rng'].detach().cpu().numpy()
        path=folder/'state.npz'
        with path.open('xb') as f:np.savez(f,**values)
        state={'schema':'windows-full-trainer-resume-state/1','trial_nonce':request['nonce'],
            'binding_sha256':digest(request['resume_binding']),'point':point,
            'checkpoint_metadata':{k:payload[k] for k in metadata_fields},
            'rng':{k:v for k,v in payload['rng'].items() if k != 'torch'},
            'loader_state':{k:v for k,v in payload['loader_state'].items() if k not in ('shuffle_rng','iterator_rng')},
            'step_trace':copy.deepcopy(trace),
            'training_state':{k:payload[k] for k in ('epoch_losses','best_val','best_epoch','gradient_diagnostics','epochs_log','scaler_state')}}
        _write(folder/'state.json',state)
        def file(p):return {'path':p.relative_to(Path(request['trial_out'])).as_posix(),'sha256':sha(p),'bytes':p.stat().st_size}
        points[point]={'state_json':file(folder/'state.json'),'state_npz':file(path),'frozen_npz':frozen_record}
    if record:export('restored2',before)
    commit = trainer.on_step_complete
    def committed(step,pointer):
        commit(step,pointer)
        values=arrays(); inputs=wrapper.last_inputs
        # The target noise is available in the dataset collate result only
        # before _step. Capture its digest with the wrapper below.
        trace.append({'step':step,'sample_ids':list(inputs['sample_id']),
            'noise_sha256':noise_digest[0],'timesteps':inputs['t'].detach().cpu().tolist(),
            'loss':trainer._epoch_losses[-1],
            'adapter_sha256':arrays_sha({k:v for k,v in values.items() if k.startswith('model::')}),
            'optimizer_sha256':arrays_sha({k:v for k,v in values.items() if not k.startswith('model::')})})
    trainer.on_step_complete=committed
    original_step=trainer._step;noise_digest=[None]
    def step(batch):
        _, target=trainer._unpack(batch)
        noise_digest[0]=[hashlib.sha256(item.detach().cpu().contiguous().numpy().tobytes()).hexdigest() for item in target]
        return original_step(batch)
    trainer._step=step
    def checkpoint(saved):
        latest.clear();latest.update(saved)
        if request['branch']=='continuous' and trainer.global_step==0 and 'initial0' not in points:
            export('initial0',before)
        if request['branch']=='continuous' and trainer.global_step==2 and 'continuous2' not in points:
            export('continuous2',None)
        if request['branch']=='cancelled' and trainer.global_step==2 and not (out/'control.json').exists():
            _write(out/'control.json',{'run_id':req['run_id'],'cancel_requested':True})
    trainer.on_checkpoint_committed=checkpoint
    status='FAILED'
    try:
        trainer.train();status='COMPLETED'
    except TrainerError as exc:
        if request['branch']!='cancelled' or exc.code!='CANCELLED':raise
        status='CANCELLED'
    require((status,trainer.global_step) == (('CANCELLED',2) if request['branch']=='cancelled' else ('COMPLETED',4)),
            'E_RESUME_TRIAL','actual cancellation/update count differs')
    # Five unique frozen exports for the three processes fit the separate 3GiB
    # trial budget. The verifier checks the actual final tensors against the
    # original three base NPZ and each checkpoint's frozen tensor fingerprint.
    after=frozen(out/'frozen-base.npz')
    if 'continuous2' in points:points['continuous2']['frozen_npz']=after
    terminal={'continuous':'continuous4','cancelled':'cancelled2','resumed':'resumed4'}[request['branch']]
    export(terminal,after)
    checkpoint_record={'directory':str(Path(latest['checkpoint_path']).parent),
        'manifest_sha256':latest['manifest_sha256']}
    result={'schema':'windows-full-trainer-resume-branch/1','branch':request['branch'],
        'status':status,'global_step':trainer.global_step,'points':points,'checkpoint':checkpoint_record,
        'pid':os.getpid(),'environment':measure(app,request['python'])}
    require(result['environment']==request['environment'],'E_RESUME_ENVIRONMENT','final worker environment changed')
    _write(out/'BRANCH.json',result)
    return result
