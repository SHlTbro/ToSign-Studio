"""Pinned independent Windows inference producer. No optimizer or SCR feature use."""
from pathlib import Path
from contextlib import contextmanager,ExitStack
import importlib.util,json,sys,time,traceback
APP=Path(__file__).resolve().parents[3];sys.path.insert(0,str(APP))
from engine.style.m2_adapter import windows_scr_execution as E
from engine.style.m2_adapter.windows_scr_deployment import ADAPTER_SHA,RECEIPT_SHA,SNAPSHOT_SHA,TRAINING_BINDING_SHA
from engine.style.m2_adapter.runtime_variants import sha,value_sha
from engine.style.m2_adapter.trainer_resume_protocol import native_path

def setup():
    import torch
    torch.set_num_threads(2);torch.set_num_interop_threads(1);torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    torch.backends.cudnn.benchmark=False;torch.backends.cudnn.deterministic=True


@contextmanager
def unet_forward_progress(unet):
    """Bounded timing-only feedback; hooks never inspect or replace tensors."""
    origin_wall,origin_cpu=time.monotonic(),time.process_time()
    forward=0;emitted=0;starts=[]
    def feedback(phase,index,wall,cpu,started=None):
        nonlocal emitted
        if emitted>=64:return
        row={'stage':'UNET_FORWARD_PROGRESS','phase':phase,'forward':index,
             'elapsed_seconds':round(wall-origin_wall,6),'process_cpu_seconds':round(cpu-origin_cpu,6)}
        if started is not None:
            row.update(forward_elapsed_seconds=round(wall-started[0],6),
                       forward_cpu_seconds=round(cpu-started[1],6))
        emitted+=1;print(json.dumps(row),flush=True)
    def begin(_module,_inputs):
        nonlocal forward
        forward+=1;wall,cpu=time.monotonic(),time.process_time()
        starts.append((forward,wall,cpu));feedback('start',forward,wall,cpu)
    def end(_module,_inputs,_output):
        index,wall,cpu=starts.pop()
        feedback('end',index,time.monotonic(),time.process_time(),(wall,cpu))
    with ExitStack() as cleanup:
        cleanup.callback(unet.register_forward_pre_hook(begin).remove)
        cleanup.callback(unet.register_forward_hook(end).remove)
        yield

def work(req):
    from engine.style.m2_adapter.release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(APP)
    out=Path(req['out']);out.mkdir();start=time.monotonic()
    variant=req.get('variant_id',E.CPU)
    report={'schema':E.SCHEMA,'status':'RUNNING','variant_id':variant,'device':'cpu','purpose':'inference',
      'optimizer_updates':0,'generation_calls':0,'may_train':False,'may_prepare':False,'may_resume':False,
      'may_publish':False,'claims_quality_pass':False}
    try:
        assert req['profile']==E.profile(APP,variant) and Path(req['app']).resolve()==APP
        setup();import torch
        report.update(profile=E.profile(APP,variant),python=str(Path(sys.executable).resolve()),python_sha256=sha(sys.executable),environment=E.measure(APP,req['python']))
        root=Path(req['core_run']);adapter=root/'artifact/m2_adapter.pt';receipt_path=root/'m2-receipt.json'
        adapter_sha,receipt_sha,binding_sha=ADAPTER_SHA,RECEIPT_SHA,TRAINING_BINDING_SHA
        source=None
        if variant==E.OLD0178:
            from engine.style.m2_adapter.windows_scr_deployment import admit_source,verify_metadata,OLD0178_MANIFEST_SHA
            source=admit_source(root,root/'artifact/artifact.json')
            assert source==req['source'] and source['artifact_manifest_sha256']==OLD0178_MANIFEST_SHA
            adapter_sha,receipt_sha,binding_sha=source['adapter_sha256'],source['training_receipt_sha256'],source['training_runtime_binding']['binding_sha256']
            report['source']=source
        assert sha(adapter)==adapter_sha and sha(receipt_path)==receipt_sha
        receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        payload=torch.load(adapter,map_location='cpu',weights_only=True);meta=payload['meta'];state=payload['adapter_state']
        assert meta['runtime_binding']['binding_sha256']==binding_sha and meta['runtime_binding']==receipt['runtime_binding']
        assert len(state)==56 and all(v.dtype==torch.float32 and torch.isfinite(v).all() for v in state.values())
        if source is not None:verify_metadata(source,meta)
        else:
            from engine.style.m2_adapter.user_scr_recipe import validate_user_scr_adapter_metadata
            validate_user_scr_adapter_metadata(meta,51)
        from engine.style.dataset.binding import verify_snapshot_binding
        snapshot=Path(req['snapshot']);binding=verify_snapshot_binding(snapshot,SNAPSHOT_SHA)
        assert meta['snapshot_sha256']==SNAPSHOT_SHA
        from engine.style.m2_adapter.adapter import load_base_model,M2FontDiffuser,load_verified_adapter_state,load_snapshot_pairs,_glyph_to_tensor,render_content_char
        from engine.style.m2_adapter.preprocessing import UPSTREAM_PROFILE
        from engine.style.m2_adapter.spatial_topology import SPATIAL_TOPOLOGY_ID,topology_fields
        from engine.style.m2_adapter.deterministic_ops import execution_counts
        spec=importlib.util.spec_from_file_location('_helpers',APP/'scripts/probe_m2_runtime_scr_two_negative.py')
        helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
        assets=E.verify_asset_root(APP,req['asset_root']);repo=Path(assets['fd_repo']);font=repo/'ttf/KaiXinSongA.ttf'
        assert sha(font)==meta['content_font_sha256'] and meta['base_npz_sha256']==assets['manifest']['base_npz_sha256']
        base,args=load_base_model(repo,device='cpu',expected_hashes=meta['base_npz_sha256'],runtime_manifest_path=assets['manifest_path'])
        base.eval().requires_grad_(False);before=helpers.tensor_digest(base.state_dict())
        model=M2FontDiffuser(base,font,adapter_topology_id=SPATIAL_TOPOLOGY_ID)
        channels=load_verified_adapter_state(model,state);assert len(channels)==22
        model.eval().requires_grad_(False);calls={k:0 for k in channels}
        def hook(k):
            def count(_m,_i,_o):calls[k]+=1
            return count
        for k,m in model.adapter.blocks.items():m.register_forward_hook(hook(k))
        pairs=load_snapshot_pairs(snapshot,'train');ref=next(p for p in pairs if p['sample_id']=='ss_ai_03f07f322c563240bf61185ef8980d1d')
        style=_glyph_to_tensor(Path(ref['glyph_path']),preprocessing_profile=UPSTREAM_PROFILE).unsqueeze(0)
        content=render_content_char('\u5fc3',font,preprocessing_profile=UPSTREAM_PROFILE).unsqueeze(0)
        from src import build_ddpm_scheduler,FontDiffuserModelDPM,FontDiffuserDPMPipeline
        scheduler=build_ddpm_scheduler(args)
        dpm=FontDiffuserModelDPM(unet=base.unet,style_encoder=base.style_encoder,content_encoder=base.content_encoder).eval().requires_grad_(False)
        pipeline=FontDiffuserDPMPipeline(dpm,scheduler,guidance_scale=7.5)
        print(json.dumps({'stage':'ORIGINAL_TERMINAL56_FULL_BASE_LOADED'}),flush=True)
        with unet_forward_progress(base.unet),torch.inference_mode():
            rgb=pipeline.generate(content,style,batch_size=1,order=2,num_inference_step=20,content_encoder_downsample_size=3,
              generator=torch.Generator(device='cpu').manual_seed(402907350))[0].convert('RGB')
        report.update(terminal_adapter_sha256=adapter_sha,training_receipt_sha256=receipt_sha,
          training_runtime_binding_sha256=binding_sha,snapshot_sha256=SNAPSHOT_SHA,snapshot_binding=binding,
          base_npz_sha256=meta['base_npz_sha256'],asset_identity_sha256=assets['asset_identity_sha256'],content_font_sha256=sha(font),
          weights_only=True,strict_load=True,FP32_finite_tensor_count=56,original_metadata_sha256=value_sha(meta),
          generation_calls=1,adapter_hook_calls=calls,spatial_offset_hook_calls=dict(model.adapter.spatial_calls),
          operator_execution_counts=execution_counts(base),base_state_before_sha256=before,
          base_state_after_sha256=helpers.tensor_digest(base.state_dict()),**topology_fields())
        generation={'outputs':[],'sha256':[],'raw_rgb_sha256':[]}
        helpers.save_repeat_output(rgb,out,0,generation);output=generation['outputs'][0]
        report['output']={**output,'raw_rgb_path':str((out/output['raw_rgb_file']).resolve()),'alpha_path':str((out/output['alpha_file']).resolve())}
        assert sha(adapter)==adapter_sha and E.profile(APP,variant)==report['profile'] and E.measure(APP,req['python'])==report['environment']
        verify_snapshot_binding(snapshot,SNAPSHOT_SHA);report['status']='PASS'
    except BaseException as exc:
        report.update(status='BLOCKED',failure={'type':type(exc).__name__,'detail':str(exc),'traceback':traceback.format_exc()[-6000:]});traceback.print_exc()
    report['seconds']=time.monotonic()-start
    with (out/'RECEIPT.json').open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'status':report['status'],'seconds':report['seconds']}),flush=True)
    return 0 if report['status']=='PASS' else 2

def main(argv=None):
    from engine.style.m2_adapter.release_runtime_policy import assert_m2_execution_allowed
    from engine.style.m2_adapter.runtime_variants import RuntimeUnavailable
    from engine.style.m2_adapter import personal_runtime_policy as Personal
    try:
        if Personal.present(APP):Personal.bootstrap(APP)
        else:assert_m2_execution_allowed(APP)
    except RuntimeUnavailable as exc:
        print(json.dumps({'status':'BLOCKED','failure':{'code':exc.code,'detail':str(exc)}}));return 2
    argv=sys.argv[1:] if argv is None else argv
    if not argv:return 3
    if argv[0]=='--environment':
        if len(argv)!=2:return 3
        if native_path(argv[1]).is_file():
            request=E._read(argv[1])
            app,policy=E.validate_launch(request,purpose='environment')
            with Personal._verified_scope(app,policy,'environment',expires_at=request['expires_at']):
                setup();print(json.dumps(E.measure(app,request['python']),ensure_ascii=False));return 0
        assert_m2_execution_allowed(APP)
        setup();print(json.dumps(E.measure(Path(argv[1])),ensure_ascii=False));return 0
    request=E._read(argv[0])
    if request.get('variant_id')==E.OLD0178:
        app,policy=E.validate_launch(request,purpose='qualification')
        with Personal._verified_scope(app,policy,'qualification',expires_at=request['expires_at']):
            return work(request)
    return work(request)

if __name__=='__main__':raise SystemExit(main())
