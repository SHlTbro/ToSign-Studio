"""This release ships Base only; historical execution pins grant no authority.

Policy removal or partial coverage fails closed. A later executable release
requires a separately reviewed policy/schema and source-bound qualification.
"""
import hashlib,json,re

SCHEMA='m2-base-release-runtime-policy/1'
DISABLED_CODE='E_RELEASE_BASE_ONLY'
VARIANT_IDS=frozenset((
    'cpu-cp312-v1',
    'fontdiffuser-win312-torch210-cu128-fp32-cpu-dcn-v1',
    'cpu-cp312-offset-delta-v1',
    'cpu-cp312-spatial-offset-v1',
    'cpu-cp312-scr2n-spatial-v1',
    'cloud-linux-cp312-user-scr2n-spatial-v1',
    'windows-cp312-cu128-user-scr2n-spatial-cpu-v1',
    'windows-cp312-cu128-user-scr2n-spatial-gpu-v1',
))

def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _require(ok,detail):
    if not ok:
        from .runtime_variants import RuntimeUnavailable
        raise RuntimeUnavailable('E_RELEASE_RUNTIME_POLICY',detail)

def validate_catalog(catalog):
    """Validate complete disabled metadata, not interpreter/model availability."""
    variants=catalog.get('variants');policy=catalog.get('release_policy')
    _require(isinstance(variants,dict) and set(variants)==VARIANT_IDS,'exact eight release variants required')
    _require(isinstance(policy,dict) and set(policy)=={'schema','mode','variants'}
        and policy.get('schema')==SCHEMA and policy.get('mode')=='BASE_ONLY','explicit Base-only release policy required')
    rows=policy.get('variants')
    _require(isinstance(rows,dict) and set(rows)==set(variants),'release policy must cover every variant exactly')
    for variant,spec in variants.items():
        row=rows[variant]
        _require(isinstance(spec,dict) and spec.get('execution_enabled') is False,'all release execution flags must be false')
        _require(all(spec.get(key,False) is False for key in ('may_prepare','may_train','may_infer','may_resume','may_publish')),
                 'Base release cannot grant execution or publication actions')
        _require(isinstance(row,dict) and set(row)=={'status','reason','execution_profile_sha256'}
            and row.get('status')=='DISABLED' and isinstance(row.get('reason'),str) and 1<=len(row['reason'])<=300,
            'every release variant needs an explicit disabled reason')
        pin=spec.get('execution_profile_sha256')
        _require(isinstance(pin,str) and re.fullmatch('[0-9a-f]{64}',pin) is not None
            and pin==_digest({k:v for k,v in spec.items() if k!='execution_profile_sha256'})
            and row.get('execution_profile_sha256')==pin,'disabled profile identity differs')
        files=spec.get('required_engine_files')
        _require(isinstance(files,dict) and all(isinstance(rel,str) and isinstance(h,str)
            and re.fullmatch('[0-9a-f]{64}',h) is not None for rel,h in files.items()),'historical source pins malformed')
    return policy

def reject_execution(app,variant_id):
    """Deny before assets, environment subprocesses or registration writes."""
    from .runtime_variants import catalog,RuntimeUnavailable
    c=catalog(app);validate_catalog(c)
    _require(variant_id in c['variants'],'unknown release variant')
    exc=RuntimeUnavailable(DISABLED_CODE,c['release_policy']['variants'][variant_id]['reason'])
    exc.variant_id=variant_id;exc.device=c['variants'][variant_id]['device']
    raise exc

def verify_disabled_source_closure(app,catalog):
    """Current signed inventory covers bytes; disabled pins remain historical.

Do not refresh historical hashes, delete entries, or call qualification. All
referenced paths/locks still have to be regular contained shipped files.
"""
    validate_catalog(catalog)
    from .runtime_variants import safe_relative,sha,require
    from .deployment import regular_path
    result={}
    for variant,spec in catalog['variants'].items():
        require(sha(regular_path(safe_relative(app,spec['lock'])))==spec['lock_sha256'],
                'E_DEPLOYMENT_SOURCE_CLOSURE','disabled dependency lock changed: '+variant)
        stale=[]
        for rel,expected in spec['required_engine_files'].items():
            actual=sha(regular_path(safe_relative(app,rel)))
            if actual!=expected:stale.append(rel)
        result[variant]={'status':'DISABLED','historical_pin_count':len(spec['required_engine_files']),
            'stale_historical_pins':stale,'may_execute':False,'qualification_inherited':False}
    return result

def assert_m2_execution_allowed(app, *, binding=None, purpose=None):
    """Release-wide denial; request fields and variant membership grant no authority.

    This Base release has no executable policy. Missing/malformed policy also
    fails closed. Inspect only release metadata, before caller-supplied assets.
    """
    from pathlib import Path
    from .runtime_variants import RuntimeUnavailable
    from .personal_runtime_policy import permits_guard, authorize_binding
    if permits_guard(app):return
    if binding is not None:
        authorize_binding(app,binding,purpose);return
    try:
        path=Path(app)/'config/m2-runtime-variants.json'
        _require(path.is_file() and path.stat().st_size<=1000000,'release policy missing or oversized')
        body=json.loads(path.read_text(encoding='utf-8'))
        policy=body.get('release_policy') if isinstance(body,dict) else None
        _require(isinstance(policy,dict) and set(policy)=={'schema','mode','variants'}
            and policy.get('schema')==SCHEMA and policy.get('mode')=='BASE_ONLY'
            and isinstance(policy.get('variants'),dict),'explicit Base-only release policy required')
    except (OSError,ValueError,TypeError) as exc:
        if isinstance(exc,RuntimeUnavailable):raise
        raise RuntimeUnavailable('E_RELEASE_RUNTIME_POLICY','release metadata cannot be read') from exc
    raise RuntimeUnavailable(DISABLED_CODE,'This release provides Base only; M2 execution is disabled.')
