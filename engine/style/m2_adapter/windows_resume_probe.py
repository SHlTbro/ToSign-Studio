"""Fixed child entrypoint; persisted requests alone cannot replay a trial."""
from pathlib import Path
import json, sys
APP=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(APP))

def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if len(args)!=1:raise ValueError('Exactly one server launch file required')
    from engine.style.m2_adapter.windows_training_runtime import _read
    from engine.style.m2_adapter.windows_resume_executor import execute
    result=execute(_read(args[0]))
    print(json.dumps({'status':result['status'],'branch':result['branch'],'global_step':result['global_step']}),flush=True)
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'BLOCKED','code':getattr(exc,'code','E_RESUME_TRIAL'),'detail':str(exc),
            'error_type':type(exc).__name__,'errno':getattr(exc,'errno',None),'winerror':getattr(exc,'winerror',None),
            'cleanup_errors':getattr(exc,'cache_cleanup_errors',[])}),flush=True)
        sys.exit(3)
