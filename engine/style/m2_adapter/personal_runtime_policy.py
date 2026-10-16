"""Independent signed Personal Runtime bootstrap; Base metadata stays disabled.

No environment flag or request PASS creates a scope. Only a verified release
policy plus the existing server-owned Windows qualification can authorize work.
No keys, credentials, ML models or runtime registrations are created here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import time
import sys
import os
import threading
from .runtime_variants import require, sha, value_sha, RuntimeUnavailable

POLICY = 'config/personal-style-runtime-policy.json'
SCHEMA = 'yy-personal-style-runtime-policy/1'
TTL = 30 * 24 * 3600
_scope = ContextVar('windows_personal_runtime_scope', default=None)
_operation = ContextVar('old0178_inference_operation', default=None)


@contextmanager
def inference_operation():
    """One call's observations and write-denying read handles; no execution authority."""
    parent = _operation.get()
    if (parent is not None and not parent['closed']
            and parent['pid'] == os.getpid()
            and parent['thread'] == threading.get_ident()):
        # Nested character consumers belong to this item. Only the outer owner
        # releases handles; all callers still perform their semantic checks.
        yield
        return
    state = {'pid': os.getpid(), 'thread': threading.get_ident(), 'closed': False,
             'observed': set(), 'locked_files': {}}
    token = _operation.set(state)
    try:
        yield
    finally:
        state['closed'] = True
        state['observed'].clear()
        for stream, identity, digest in state['locked_files'].values():
            stream.close()
        state['locked_files'].clear()
        _operation.reset(token)


def _locked_reader(path):
    """Windows denies existing/new writers and deletion, including hard-link writers."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    from .trainer_resume_protocol import native_path
    path=Path(path).absolute()
    if path.drive.upper()!='E:' or any(p.is_symlink() or p.is_junction() for p in (path,*path.parents)):
        raise ValueError('E_OPERATION_FILE_PATH')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                     wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    create.restype=wintypes.HANDLE
    # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL.
    handle=create(str(native_path(path)),0x80000000,1,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close=kernel.CloseHandle;close.argtypes=(wintypes.HANDLE,);close(handle)
        raise
    return os.fdopen(fd,'rb')


def _locked_content_digest(stream):
    import hashlib
    digest=hashlib.sha256()
    for block in iter(lambda:stream.read(1<<20),b''):
        digest.update(block)
    return digest.hexdigest()


def operation_digest(path):
    """Reuse bytes only while Windows forbids mutation; never cache a PASS result.

    Reopening the current path and comparing OS file identity on EVERY call rejects
    path replacement. No stat/mtime shortcut, no cross-call or cross-thread cache.
    Source signatures, capability MACs, expiry and all semantic validators still run.
    """
    state=_operation.get()
    if (sys.platform!='win32' or state is None or state['closed']
            or state['pid']!=os.getpid() or state['thread']!=threading.get_ident()):
        return None
    key=str(Path(path).absolute())
    current=_locked_reader(path)
    try:
        stat=os.fstat(current.fileno())
        identity=(stat.st_dev,stat.st_ino)
        previous=state['locked_files'].get(key)
        if previous is not None:
            if identity!=previous[1]:raise ValueError('E_OPERATION_FILE_CHANGED')
            return previous[2]
        digest=_locked_content_digest(current)
        state['locked_files'][key]=(current,identity,digest)
        current=None
        return digest
    finally:
        if current is not None:current.close()


def operation_entry(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with inference_operation():
            return function(*args, **kwargs)
    return wrapped


def _environment_observation(app, record, environment, policy):
    """Used only after the current MAC/source/assets/snapshot/expiry checks."""
    from .windows_scr_execution import OLD0178
    state, scope = _operation.get(), _scope.get()
    app = str(Path(app).resolve())
    if (state is None or state['closed'] or state['pid'] != os.getpid()
            or state['thread'] != threading.get_ident() or not scope
            or scope['app'] != app or scope['purpose'] not in ('verification', 'inference')
            or record.get('variant_id') != OLD0178 or policy is None
            or scope['policy_sha256'] != policy['sha256']):
        return None
    # Digest only; never print environment values or make them an authority.
    key = (app, 'inference', policy['sha256'], value_sha(record), value_sha(environment),
           value_sha(dict(os.environ)))
    return state, key


def present(app):
    return (Path(app) / POLICY).is_file()


def candidate(app, variant=None):
    """Unsigned release input; this object has no execution authority."""
    from .windows_user_scr import CPU, GPU, profile_for_source
    from .runtime_variants import catalog
    variant = CPU if variant is None else variant
    profile = profile_for_source(app, variant)
    from .windows_scr_execution import profile as inference_profile, OLD0178
    from .windows_scr_deployment import identity, OLD0178_MANIFEST_SHA
    old_profile=inference_profile(app,OLD0178);old=identity(OLD0178_MANIFEST_SHA)
    return {'schema': SCHEMA, 'mode': 'QUALIFICATION_ONLY_BOOTSTRAP',
            'variant_id': variant, 'device': profile['device'], 'platform': 'win32',
            'recipe_id': profile['recipe_id'],
            'execution_profile_sha256': profile['execution_profile_sha256'],
            'source_binding_sha256': value_sha(profile['required_source_files']),
            'dependency_lock_sha256': profile['lock_sha256'],
            'asset_identity_sha256': catalog(app)['asset_identity_sha256'],
            'scr_assets_sha256': profile['scr_assets']['sha256'],
            'producer_sha256': profile['training_producer_sha256'],
            'verifier_sha256': profile['evidence_verifier_sha256'],
            'bootstrap_entrypoint': 'scripts/windows_scr_training.py',
            'qualification_ttl_seconds': TTL,
            'qualified_actions': ['prepare', 'train', 'inference'] if variant == GPU else ['prepare', 'train', 'inference', 'resume'],
            'resume_requires': 'independent-full-trainer-checkpoint-qualification',
            'GPU_enabled': variant == GPU, 'may_publish': False, 'claims_quality_pass': False,
            'old0178_inference': {'variant_id':OLD0178,'device':'cpu','platform':'win32',
                'artifact_manifest_sha256':old['artifact_manifest_sha256'],'adapter_sha256':old['adapter_sha256'],
                'execution_profile_sha256':old_profile['execution_profile_sha256'],
                'dependency_lock_sha256':old_profile['lock_sha256'],'qualified_actions':['inference'],
                'may_train':False,'may_prepare':False,'may_resume':False,'may_publish':False,'claims_quality_pass':False}}


def validate_shape(value):
    from .windows_user_scr import CPU, GPU
    require(isinstance(value, dict) and value.get('schema') == SCHEMA
            and value.get('variant_id') in (CPU, GPU)
            and value.get('device') == ('cuda' if value.get('variant_id') == GPU else 'cpu')
            and value.get('platform') == 'win32'
            and value.get('mode') == 'QUALIFICATION_ONLY_BOOTSTRAP'
            and value.get('qualification_ttl_seconds') == TTL
            and value.get('GPU_enabled') is (value.get('variant_id') == GPU) and value.get('may_publish') is False
            and value.get('claims_quality_pass') is False
            and value.get('qualified_actions') == (['prepare', 'train', 'inference'] if value.get('variant_id') == GPU else ['prepare', 'train', 'inference', 'resume']),
            'E_PERSONAL_RUNTIME_POLICY', 'independent device-specific bootstrap policy required')
    return value


def verify(app):
    from .deployment import _json, regular_path, safe_relative, SOURCE_CATALOG
    from .deployment import _verify_signed_source_catalog, verify_source_catalog
    require(sys.platform == 'win32', 'E_PERSONAL_RUNTIME_DEVICE', 'actual Windows platform required')
    app = regular_path(app, directory=True)
    require(app.drive.upper() == 'E:', 'E_PERSONAL_RUNTIME_PATH', 'E-only Personal Runtime required')
    path = regular_path(safe_relative(app, POLICY))
    value = validate_shape(_json(path))
    # Always require the current RSA signature, including legacy pinned catalogs.
    # The existing code-pinned public key and signature format are unchanged.
    catalog_path = regular_path(safe_relative(app, SOURCE_CATALOG))
    _verify_signed_source_catalog(app, catalog_path)
    source_sha, inventory = verify_source_catalog(app)
    require(POLICY in inventory and inventory[POLICY] == sha(path),
            'E_PERSONAL_RUNTIME_SOURCE', 'signed source inventory must cover this policy')
    require(value == candidate(app, value['variant_id']), 'E_PERSONAL_RUNTIME_SOURCE', 'source, assets, lock or selected device profile changed')
    return {'body': value, 'sha256': sha(path), 'source_catalog_sha256': source_sha}


def assert_fresh_record(record, policy, now=None):
    """Checked before any environment observer or model input probe."""
    from .windows_user_scr import CPU, GPU
    now = time.time() if now is None else now
    body = policy['body']
    require(record.get('variant_id') == body['variant_id'] and record.get('device') == body['device'],
            'E_PERSONAL_RUNTIME_DEVICE', 'Windows capability cannot authorize another signed device or Linux')
    require(record.get('personal_policy_sha256') == policy['sha256']
            and record.get('execution_profile_sha256') == body['execution_profile_sha256']
            and record.get('dependency_lock_sha256') == body['dependency_lock_sha256']
            and record.get('asset_identity_sha256') == body['asset_identity_sha256'],
            'E_PERSONAL_RUNTIME_BINDING', 'qualification belongs to another signed source/assets/lock')
    issued, expires = record.get('issued_at'), record.get('expires_at')
    require(type(issued) is int and type(expires) is int and issued <= now < expires
            and expires - issued == body['qualification_ttl_seconds'],
            'E_PERSONAL_RUNTIME_EXPIRED', 'qualification is absent, expired or future-dated')
    require(record.get('synthetic_test_only') is False,
            'E_PERSONAL_RUNTIME_QUALIFICATION', 'synthetic or historical registrations cannot unlock execution')


@contextmanager
def _verified_scope(app, policy, purpose, registration_id=None, expires_at=None):
    token = _scope.set({'app': str(Path(app).resolve()), 'policy_sha256': policy['sha256'],
                        'purpose': purpose, 'registration_id': registration_id, 'expires_at': expires_at})
    try:
        yield
    finally:
        _scope.reset(token)


def permits_guard(app):
    current = _scope.get()
    if not current or current['app'] != str(Path(app).resolve()):
        return False
    require(current['purpose'] in ('qualification', 'environment', 'verification', 'prepare', 'train', 'inference', 'resume'),
            'E_PERSONAL_RUNTIME_SCOPE', 'unsupported internal operation')
    require(current['expires_at'] is None or time.time() < current['expires_at'],
            'E_PERSONAL_RUNTIME_EXPIRED', 'operation authority expired before model boundary')
    # Re-authenticate current source on every deferred ML boundary.
    policy = verify(app)
    require(policy['sha256'] == current['policy_sha256'],
            'E_PERSONAL_RUNTIME_SOURCE', 'signed release changed during execution')
    return True


def bootstrap(app):
    require(present(app), 'E_RELEASE_BASE_ONLY', 'Base cannot launch qualification')
    return verify(app)


def authorize_binding(app, binding, purpose):
    from .windows_scr_execution import OLD0178,inspect as inspect_old
    if isinstance(binding,dict) and binding.get('variant_id')==OLD0178:
        if purpose!='inference':raise ValueError('E_WINDOWS_EXECUTION_INFERENCE_ONLY')
        from .runtime_variants import validate_binding_shape
        validate_binding_shape(binding)
        policy=verify(app)
        selected=inspect_old(app,binding['registration_id'],purpose=purpose,variant=OLD0178)
        require(selected.get('ready') is True and selected['runtime_binding']==binding,
                'E_PERSONAL_RUNTIME_BINDING','exact independent old0178 registration required')
        return policy,selected
    from .windows_user_scr import CPU, GPU
    from .runtime_variants import validate_binding_shape, make_binding
    from .windows_training_runtime import inspect
    validate_binding_shape(binding)
    policy = verify(app)
    require(binding['variant_id'] == policy['body']['variant_id'] and binding['device'] == policy['body']['device'],
            'E_PERSONAL_RUNTIME_DEVICE', 'exact signed Windows device binding required')
    require(purpose in policy['body']['qualified_actions'],
            'E_PERSONAL_RUNTIME_ACTION', 'operation is not qualified; full trainer resume needs separate evidence')
    selected = inspect(app, binding['registration_id'], purpose=purpose)
    require(selected.get('ready') is True and make_binding(selected) == binding,
            'E_PERSONAL_RUNTIME_BINDING', 'exact freshly verified registration required')
    return policy, selected


@contextmanager
def execution_scope(app, binding, purpose):
    policy, selected = authorize_binding(app, binding, purpose)
    expires = selected['registration']['expires_at']
    if purpose == 'resume':
        expires = min(expires, *(item['expires_at'] for item in selected['resume_capability']))
    with _verified_scope(app, policy, purpose, binding['registration_id'], expires):
        yield selected


def bound_action(purpose):
    """Fixed native entrypoint wrapper; caller flags never grant a scope."""
    def decorate(function):
        @wraps(function)
        def wrapped(request, *args, **kwargs):
            from .windows_training_recipe import RECIPE_ID
            from .windows_scr_execution import OLD0178
            if isinstance(request,dict) and isinstance(request.get('runtime_binding'),dict) and request['runtime_binding'].get('variant_id')==OLD0178:
                with execution_scope(Path(__file__).resolve().parents[3],request['runtime_binding'],purpose):
                    return function(request,*args,**kwargs)
            if not isinstance(request, dict) or request.get('recipe_id') != RECIPE_ID:
                return function(request, *args, **kwargs)
            app = Path(__file__).resolve().parents[3]
            action = 'resume' if purpose == 'train' and request.get('resume_checkpoint') is not None else purpose
            with execution_scope(app, request.get('runtime_binding'), action):
                return function(request, *args, **kwargs)
        return wrapped
    return decorate


def bound_method(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        from .windows_user_scr import CPU, GPU
        from .windows_scr_execution import OLD0178
        binding = getattr(self, 'runtime_binding', None)
        if not isinstance(binding, dict) or binding.get('variant_id') not in (CPU, GPU, OLD0178):
            return function(self, *args, **kwargs)
        with execution_scope(Path(__file__).resolve().parents[3], binding, 'inference'):
            return function(self, *args, **kwargs)
    return wrapped


def assert_launch_fresh(request, policy, now=None):
    now = time.time() if now is None else now
    issued, expires = request.get('issued_at'), request.get('expires_at')
    require(type(issued) is int and type(expires) is int and issued <= now < expires
            and 0 < expires - issued <= 900
            and request.get('personal_policy_sha256') == policy['sha256'],
            'E_PERSONAL_RUNTIME_LAUNCH_EXPIRED', 'server launch is expired or belongs to another release')
