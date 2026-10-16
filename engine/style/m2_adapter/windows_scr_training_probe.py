"""Fixed native Windows qualification producer; all paths come from signed launch."""
from pathlib import Path
import json,sys,traceback
APP=Path(__file__).resolve().parents[3];sys.path.insert(0,str(APP))
def main():
    from engine.style.m2_adapter.personal_runtime_policy import bootstrap,_verified_scope
    from engine.style.m2_adapter.runtime_variants import RuntimeUnavailable
    try:policy=bootstrap(APP)
    except (OSError,ValueError) as exc:
        print(json.dumps({"status":"BLOCKED","failure":{"code":getattr(exc,'code','BLOCKED_ENV'),"detail":str(exc)},"may_train":False,"may_publish":False}));return 2
    from engine.style.m2_adapter.windows_training_runtime import measure,setup_cpu,validate_environment_launch
    if len(sys.argv)==3 and sys.argv[1]=='--environment':
        request=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
        app,policy=validate_environment_launch(request)
        with _verified_scope(app,policy,'environment',expires_at=request['expires_at']):
            setup_cpu();print(json.dumps(measure(APP,sys.executable,request['variant_id'])));return 0
    if len(sys.argv)!=2:return 3
    from engine.style.m2_adapter.windows_training_executor import execute,write
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:execute(request);return 0
    except Exception as exc:
        # Preserve failed raw outputs; do not overwrite a result or old receipt.
        failure={'status':'BLOCKED','code':getattr(exc,'code','E_WINDOWS_SCR_EXECUTOR'),'detail':str(exc),
            'error_type':type(exc).__name__,'errno':getattr(exc,'errno',None),'winerror':getattr(exc,'winerror',None),
            'cleanup_errors':getattr(exc,'cache_cleanup_errors',[]),'traceback':traceback.format_exc()[-6000:],
            'may_train':False,'may_publish':False}
        p=Path(request.get('out',''))/'EXECUTOR-FAILURE.json'
        if p.parent.is_dir() and not p.exists():write(p,failure)
        print(json.dumps(failure),flush=True);return 3
if __name__=='__main__':sys.exit(main())
