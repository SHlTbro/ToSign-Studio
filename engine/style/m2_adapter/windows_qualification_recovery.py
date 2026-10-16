"""Archive a dead authenticated qualification lease; never retry or run ML."""
from contextlib import contextmanager
import ctypes
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from . import personal_runtime_policy as Personal
from . import windows_training_runtime as W
from .runtime_variants import require, sha
from .windows_user_scr import CPU, GPU, cache_identity, profile_for_source
from .trainer_resume_protocol import native_path


def _stage_record(path, payload):
    """Uncommitted same-directory evidence; interruptions leave this file intact."""
    with native_path(path).open('xb') as stream:
        require(stream.write(payload) == len(payload), 'E_QUALIFICATION_RECOVERY_RECORD',
                'incomplete staged record write')
        stream.flush()
        os.fsync(stream.fileno())


def _commit_record(path, value):
    """Publish only a complete, flushed record; never replace immutable evidence."""
    path = Path(path)
    W._regular(path.parent, True)
    if native_path(path).exists():
        require(W._read(path) == value, 'E_WINDOWS_SCR_IMMUTABLE', 'record changed')
        return
    payload = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')
    staged = path.with_name(path.stem[0] + '-' + uuid.uuid4().hex + '.tmp')
    _stage_record(staged, payload)
    # Windows rename is atomic and refuses an existing destination. Pending
    # files remain historical evidence and are never accepted as final records.
    try:
        native_path(staged).rename(native_path(path))
    except FileExistsError:
        require(W._read(path) == value, 'E_WINDOWS_SCR_IMMUTABLE', 'record changed')
    require(W._read(path) == value, 'E_QUALIFICATION_RECOVERY_RECORD', 'committed record differs')


def _digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value),
            'E_QUALIFICATION_RECOVERY_BINDING', 'explicit original SHA256 required')
    return value


def _kernel():
    from ctypes import wintypes as T
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = (T.DWORD, T.BOOL, T.DWORD)
    k.OpenProcess.restype = T.HANDLE
    k.GetExitCodeProcess.argtypes = (T.HANDLE, ctypes.POINTER(T.DWORD))
    k.GetExitCodeProcess.restype = T.BOOL
    k.GetProcessTimes.argtypes = (T.HANDLE, *(ctypes.POINTER(T.FILETIME),) * 4)
    k.GetProcessTimes.restype = T.BOOL
    k.CloseHandle.argtypes = (T.HANDLE,)
    k.CloseHandle.restype = T.BOOL
    return k


def process_state(identity):
    """Only an exact recorded PID. Access denial and ambiguity stay unknown."""
    if (os.name != 'nt' or not isinstance(identity, dict)
            or set(identity) != {'pid', 'birth'}
            or type(identity['pid']) is not int or not 0 < identity['pid'] <= 0xffffffff
            or type(identity['birth']) not in (int, float) or identity['birth'] <= 0
            or identity['birth'] > 0xffffffffffffffff or not math.isfinite(identity['birth'])):
        return 'unknown'
    from ctypes import wintypes as T
    k = _kernel()
    handle = k.OpenProcess(0x1000, False, identity['pid'])
    if not handle:
        return 'dead' if ctypes.get_last_error() == 87 else 'unknown'
    try:
        exit_code = T.DWORD()
        times = [T.FILETIME() for _ in range(4)]
        if (not k.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                or not k.GetProcessTimes(handle, *(ctypes.byref(x) for x in times))):
            return 'unknown'
        birth = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if type(identity['birth']) is int:
            same = birth == identity['birth']
        else:
            # Legacy START.json stores psutil's floating Unix creation time.
            observed = birth / 10000000.0 - 11644473600.0
            same = observed == identity['birth']
            if not same and abs(observed - identity['birth']) <= 0.00001:
                return 'unknown'  # Precision loss cannot establish owner death.
        return 'dead' if not same or exit_code.value != 259 else 'alive'
    except OSError:
        return 'unknown'
    finally:
        k.CloseHandle(handle)


@contextmanager
def _locked_lease(path):
    """Rename the same exclusively opened Windows file; never replace a path."""
    from ctypes import wintypes as T
    require(os.name == 'nt', 'E_QUALIFICATION_RECOVERY_PLATFORM', 'Windows required')
    W._regular(path)
    k = _kernel()
    k.CreateFileW.argtypes = (T.LPCWSTR, T.DWORD, T.DWORD, T.LPVOID,
                             T.DWORD, T.DWORD, T.HANDLE)
    k.CreateFileW.restype = T.HANDLE
    k.GetFileSizeEx.argtypes = (T.HANDLE, ctypes.POINTER(ctypes.c_longlong))
    k.GetFileSizeEx.restype = T.BOOL
    k.ReadFile.argtypes = (T.HANDLE, T.LPVOID, T.DWORD,
                          ctypes.POINTER(T.DWORD), T.LPVOID)
    k.ReadFile.restype = T.BOOL
    k.SetFileInformationByHandle.argtypes = (T.HANDLE, ctypes.c_int, T.LPVOID, T.DWORD)
    k.SetFileInformationByHandle.restype = T.BOOL
    handle = k.CreateFileW(str(native_path(path)), 0x80000000 | 0x10000, 0, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = ctypes.c_longlong()
        if not k.GetFileSizeEx(handle, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        require(0 < size.value <= 8192, 'E_QUALIFICATION_RECOVERY_LEASE', 'lease size invalid')
        buffer = ctypes.create_string_buffer(size.value)
        read = T.DWORD()
        if not k.ReadFile(handle, buffer, size.value, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        require(read.value == size.value, 'E_QUALIFICATION_RECOVERY_LEASE', 'incomplete lease read')

        def rename(target):
            class Rename(ctypes.Structure):
                _fields_ = [('Flags', T.DWORD), ('RootDirectory', T.HANDLE),
                            ('FileNameLength', T.DWORD), ('FileName', T.WCHAR * 1)]
            encoded = str(native_path(target)).encode('utf-16-le')
            data = ctypes.create_string_buffer(Rename.FileName.offset + len(encoded) + 2)
            info = Rename.from_buffer(data)
            info.Flags = 0  # ReplaceIfExists is false.
            info.RootDirectory = None
            info.FileNameLength = len(encoded)
            ctypes.memmove(ctypes.addressof(data) + Rename.FileName.offset, encoded, len(encoded))
            if not k.SetFileInformationByHandle(handle, 3, data, len(data)):
                raise ctypes.WinError(ctypes.get_last_error())

        yield buffer.raw, rename
    finally:
        k.CloseHandle(handle)


def recover(app, request_path, request_sha256, receipt_path, receipt_sha256, lease_sha256, *, gpu_lease_sha256=None):
    """Authenticate original signed source/request and archive only dead owners.

    An expired request may be archived, but is never relaunched or qualified.
    Explicit public-receipt pinning covers the legacy unsigned worker START record.
    """
    app = W._regular(app, True)
    current_policy = Personal.bootstrap(app)
    request_path, receipt_path = W._regular(request_path), W._regular(receipt_path)
    for path, pin in ((request_path, request_sha256), (receipt_path, receipt_sha256)):
        require(sha(path) == _digest(pin), 'E_QUALIFICATION_RECOVERY_BINDING', 'original bytes differ')
    _digest(lease_sha256)
    request, public = W._read(request_path), W._read(receipt_path)
    require(isinstance(public, dict), 'E_QUALIFICATION_RECOVERY_BINDING', 'public interruption receipt required')
    require(isinstance(request, dict) and request.get('schema') == 'windows-scr-server-launch/1',
            'E_QUALIFICATION_RECOVERY_REQUEST', 'original server launch required')
    original = W._regular(request['app'], True)
    original_policy = Personal.bootstrap(original)
    variant=original_policy['body']['variant_id']
    require(variant in (CPU,GPU), 'E_QUALIFICATION_RECOVERY_SOURCE','exact Windows device required')
    body = {k: v for k, v in request.items() if k != 'hmac_sha256'}
    require(hmac.compare_digest(str(request.get('hmac_sha256')), W._mac(original, body)),
            'E_WINDOWS_SCR_AUTH', 'original server request authentication failed')
    require(request.get('personal_policy_sha256') == original_policy['sha256']
            and request.get('profile') == profile_for_source(original, variant),
            'E_QUALIFICATION_RECOVERY_SOURCE', 'original signed source/profile differs')
    issued, expires = request.get('issued_at'), request.get('expires_at')
    require(type(issued) is int and type(expires) is int
            and 0 < issued <= time.time() and 0 < expires - issued <= 900,
            'E_QUALIFICATION_RECOVERY_REQUEST', 'original bounded launch times required')
    require(isinstance(request.get('nonce'), str) and re.fullmatch('[0-9a-f]{64}', request['nonce'])
            and isinstance(request.get('lease_nonce'), str)
            and re.fullmatch('[0-9a-f]{32}', request['lease_nonce'])
            and type(request.get('issuer_pid')) is int and 0 < request['issuer_pid'] <= 0xffffffff
            and type(request.get('issuer_process_start')) is int
            and 0 < request['issuer_process_start'] <= 0xffffffffffffffff,
            'E_QUALIFICATION_RECOVERY_REQUEST', 'exact launcher identity and nonces required')
    require(request_path.name == 'request.json'
            and W._regular(request['out'], True) == request_path.parent / 'result',
            'E_QUALIFICATION_RECOVERY_REQUEST', 'original trial layout differs')
    lock = W._root(original) / 'cpu-execution.lock'
    evidence = public.get('public_evidence_sha256', {}) if isinstance(public, dict) else {}
    require(public.get('status') == 'INTERRUPTED_EXECUTION_SERVICE_DISCONNECT'
            and public.get('qualification_status') == 'NOT_QUALIFIED'
            and public.get('registration_status') == 'NOT_CREATED'
            and public.get('registration_id') is None
            and Path(public.get('source_app', '')).resolve() == original
            and Path(public.get('CPU_lease_path', '')).resolve() == lock
            and public.get('CPU_lease_issuer_pid') == request['issuer_pid']
            and evidence.get('trial/request.json') == request_sha256
            and public.get('execution_profile_sha256') == request['profile']['execution_profile_sha256']
            and public.get('input_binding') == request.get('input_binding')
            and public.get('observed_environment') == request.get('environment')
            and public.get('windows_cache_identity') == request.get('windows_cache_identity'),
            'E_QUALIFICATION_RECOVERY_BINDING', 'explicit interrupted-attempt receipt differs')
    start_path = W._regular(request_path.parent / 'START.json')
    require(sha(start_path) == _digest(evidence.get('trial/START.json')),
            'E_QUALIFICATION_RECOVERY_BINDING', 'original worker identity bytes differ')
    start = W._read(start_path)
    require(set(start) == {'pid', 'create_time', 'max_seconds', 'max_rss_bytes', 'GPU_used'}
            and type(start['pid']) is int and 0 < start['pid'] <= 0xffffffff
            and start['pid'] != request['issuer_pid']
            and type(start['create_time']) is float and math.isfinite(start['create_time'])
            and start['create_time'] > 0 and start['max_seconds'] == 900
            and start['max_rss_bytes'] == int(4.5 * 2**30) and start['GPU_used'] is (variant==GPU),
            'E_QUALIFICATION_RECOVERY_REQUEST', 'exact original worker identity required')
    environment = request['environment']
    require(isinstance(environment, dict)
            and environment.get('schema') == 'windows-scr-training-environment/1'
            and environment.get('identity_sha256') == environment.get('numerical_environment', {}).get('identity_sha256')
            and request.get('windows_cache_identity') == cache_identity(
                original, variant, environment['identity_sha256'], request['snapshot_sha256']),
            'E_QUALIFICATION_RECOVERY_BINDING', 'original environment/cache binding differs')
    checked = W.preflight(original, request['python'], request['base_asset_root'],
                          request['snapshot'], request['snapshot_sha256'], request['assets'],variant=variant)
    require(not checked['errors'] and checked['input_binding'] == request.get('input_binding'),
            'E_QUALIFICATION_RECOVERY_BINDING', 'original input/interpreter/asset binding differs')
    token = {'pid': request['issuer_pid'], 'process_start': request['issuer_process_start'],
             'nonce': request['lease_nonce'], 'task': 'two-update-qualification'}
    owners = {'issuer': {'pid': token['pid'], 'birth': token['process_start']},
              'worker': {'pid': start['pid'], 'birth': start['create_time']}}
    folder = W._root(original) / 'qualification-recoveries' / request_sha256
    # Validate every existing component before creating any recovery evidence.
    require(not any(native_path(p).is_symlink() or native_path(p).is_junction() or native_path(p / 'FROZEN').exists()
                    for p in (folder, *folder.parents)),
            'E_WINDOWS_SCR_PATH', 'regular recovery path outside frozen data required')
    archive, prepared, completed = (folder / 'cpu-execution.lock', folder / 'PREPARED.json',
                                     folder / 'RECOVERED.json')
    binding = {'schema': 'windows-scr-qualification-lease-recovery/1',
               'original_app': str(original), 'recovery_app': str(app),
               'original_policy_sha256': original_policy['sha256'],
               'original_source_catalog_sha256': original_policy['source_catalog_sha256'],
               'recovery_source_catalog_sha256': current_policy['source_catalog_sha256'],
               'request_path': str(request_path), 'public_receipt_path': str(receipt_path),
               'worker_start_path': str(start_path),
               'request_sha256': request_sha256, 'public_receipt_sha256': receipt_sha256,
               'worker_start_sha256': sha(start_path), 'lease_sha256': lease_sha256,
               'token': token, 'owners': owners, 'archive': str(archive),
               'qualification_status': 'NOT_QUALIFIED', 'retry_started': False,
               'cache_reused': False, 'fresh_compute_required_on_retry': True,
               'ML_started': False, 'GPU_used': False, 'existing_processes_stopped': False}
    if variant==GPU:
        from .windows_gpu_support import gpu_lock_path
        gpu_lock=gpu_lock_path(original);gpu_archive=folder/'gpu.lock'
        _digest(gpu_lease_sha256)
        require(public.get('GPU_lease_path')==str(gpu_lock) and public.get('GPU_lease_sha256')==gpu_lease_sha256
                and isinstance(request.get('gpu_lease_nonce'),str)
                and re.fullmatch('[0-9a-f]{32}',request['gpu_lease_nonce']),
                'E_QUALIFICATION_RECOVERY_BINDING','explicit original GPU lease receipt/digest required')
        gpu_token={**token,'nonce':request['gpu_lease_nonce']}
        binding.update(schema='windows-scr-qualification-lease-recovery/2',variant_id=variant,
            gpu_lease_sha256=gpu_lease_sha256,gpu_token=gpu_token,gpu_archive=str(gpu_archive),
            GPU_requested_in_original_trial=True)
        return _recover_gpu_pair(lock,gpu_lock,archive,gpu_archive,prepared,completed,binding,owners)
    require(gpu_lease_sha256 is None,'E_QUALIFICATION_RECOVERY_BINDING','CPU recovery cannot consume a GPU lease')

    def finish():
        require(W._read(prepared) == binding and sha(W._regular(archive)) == lease_sha256
                and W._read(archive) == token, 'E_QUALIFICATION_RECOVERY_BINDING',
                'immutable prepared/archive evidence differs')
        value = {**binding, 'status': 'DEAD_QUALIFICATION_LEASE_ARCHIVED'}
        _commit_record(completed, value)
        return {**value, 'receipt_path': str(completed), 'receipt_sha256': sha(completed)}

    if native_path(archive).exists():
        return finish()  # Idempotent; never inspect or change a successor lease.
    require(not native_path(completed).exists(), 'E_QUALIFICATION_RECOVERY_BINDING', 'receipt has no archive')
    with _locked_lease(lock) as (raw, rename):
        supplied = json.loads(raw)
        require(hashlib.sha256(raw).hexdigest() == lease_sha256 and supplied == token
                and type(supplied.get('pid')) is int and type(supplied.get('process_start')) is int,
                'E_QUALIFICATION_RECOVERY_LEASE', 'exact original lease bytes/task/owner differ')
        try:
            states = {name: process_state(identity) for name, identity in owners.items()}
        except OSError:
            states = {'issuer': 'unknown', 'worker': 'unknown'}
        require(all(state == 'dead' for state in states.values()),
                'E_QUALIFICATION_RECOVERY_OWNER', 'issuer and worker must both be confirmed dead')
        native_path(folder).mkdir(parents=True, exist_ok=True)
        _commit_record(prepared, binding)
        rename(archive)
    return finish()


def _recover_gpu_pair(cpu_lock,gpu_lock,cpu_archive,gpu_archive,prepared,completed,binding,owners):
    """Two exclusive Windows handles, durable intent, then GPU and CPU archives.

    If interrupted between renames, the GPU archive + PREPARED authenticate the
    partial state. Reentry only opens the still-original CPU file. A successor
    GPU lock is never opened, removed or assumed stale.
    """
    from contextlib import ExitStack
    def match(raw,token,digest):
        require(hashlib.sha256(raw).hexdigest()==digest and json.loads(raw)==token,
            'E_QUALIFICATION_RECOVERY_LEASE','original exact GPU/CPU lease bytes differ')
    def archived(path,token,digest):
        require(sha(W._regular(path))==digest and W._read(path)==token,
            'E_QUALIFICATION_RECOVERY_BINDING','immutable lease archive differs')
    def finish():
        require(W._read(prepared)==binding,'E_QUALIFICATION_RECOVERY_BINDING','prepared GPU recovery changed')
        archived(cpu_archive,binding['token'],binding['lease_sha256'])
        archived(gpu_archive,binding['gpu_token'],binding['gpu_lease_sha256'])
        value={**binding,'status':'DEAD_QUALIFICATION_LEASES_ARCHIVED'}
        _commit_record(completed,value)
        return {**value,'receipt_path':str(completed),'receipt_sha256':sha(completed)}
    if native_path(cpu_archive).exists():return finish()
    require(not native_path(completed).exists(),'E_QUALIFICATION_RECOVERY_BINDING','GPU completion has no CPU archive')
    with ExitStack() as stack:
        cpu_raw,cpu_rename=stack.enter_context(_locked_lease(cpu_lock))
        match(cpu_raw,binding['token'],binding['lease_sha256'])
        gpu_rename=None
        if native_path(gpu_archive).exists():
            require(W._read(prepared)==binding,'E_QUALIFICATION_RECOVERY_BINDING','partial GPU recovery differs')
            archived(gpu_archive,binding['gpu_token'],binding['gpu_lease_sha256'])
        else:
            gpu_raw,gpu_rename=stack.enter_context(_locked_lease(gpu_lock))
            match(gpu_raw,binding['gpu_token'],binding['gpu_lease_sha256'])
        require(all(process_state(identity)=='dead' for identity in owners.values()),
            'E_QUALIFICATION_RECOVERY_OWNER','both recorded issuer and worker must be dead')
        native_path(prepared.parent).mkdir(parents=True,exist_ok=True)
        _commit_record(prepared,binding)
        if gpu_rename is not None:gpu_rename(gpu_archive)
        cpu_rename(cpu_archive)
    return finish()
