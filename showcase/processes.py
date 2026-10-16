"""Own a Windows venv redirector child without searching or killing other processes."""
import ctypes
import json
import os
from pathlib import Path


def _windows_api():
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.GetCurrentProcess.restype = w.HANDLE
    api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    api.CloseHandle.argtypes = [w.HANDLE]
    return api, w


def _created(api, w, handle):
    times = [w.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def record_identity():
    """Called by the actual server before importing dependencies or starting work."""
    raw = os.environ.get('YY_SHOWCASE_PROCESS_RECORD')
    if not raw:
        return
    record = {'nonce': os.environ['YY_SHOWCASE_INSTANCE'], 'pid': os.getpid()}
    if os.name == 'nt':
        api, w = _windows_api()
        record['created'] = _created(api, w, api.GetCurrentProcess())
    target = Path(raw)
    part = target.with_suffix('.part')
    part.write_text(json.dumps(record), encoding='utf-8')
    os.replace(part, target)


def terminate_owned(process, record_path, nonce):
    """Terminate only a nonce-bound child with the exact recorded creation time."""
    try:
        if os.name == 'nt' and Path(record_path).is_file():
            record = json.loads(Path(record_path).read_text(encoding='utf-8'))
            if record.get('nonce') != nonce:
                raise ValueError('Process record ownership differs; refusing child termination')
            api, w = _windows_api()
            handle = api.OpenProcess(0x1000 | 0x100000 | 0x0001, False, record['pid'])
            if handle:
                try:
                    if _created(api, w, handle) != record['created']:
                        raise ValueError('Process identity changed; refusing child termination')
                    if api.WaitForSingleObject(handle, 0) == 258:
                        if not api.TerminateProcess(handle, 1):
                            error = ctypes.get_last_error()
                            # Windows can report ACCESS_DENIED while a process
                            # is already exiting. Success requires its exact
                            # handle to become signaled; never ignore a live one.
                            if api.WaitForSingleObject(handle, 5000) != 0:
                                raise ctypes.WinError(error)
                        if api.WaitForSingleObject(handle, 5000) != 0:
                            raise TimeoutError('Owned server child did not exit')
                finally:
                    api.CloseHandle(handle)
            elif ctypes.get_last_error() != 87:  # invalid PID means it already exited
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=20)
