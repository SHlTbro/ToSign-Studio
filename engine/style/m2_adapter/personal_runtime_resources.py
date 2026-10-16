"""CPU-only read-only resource preflight. No process enumeration or GPU query."""
from pathlib import Path
import ctypes
import shutil
from .runtime_variants import require, RuntimeUnavailable


def memory_available():
    class Status(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                    ('physical', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                    ('page', ctypes.c_ulonglong), ('available_page', ctypes.c_ulonglong),
                    ('virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong),
                    ('extended', ctypes.c_ulonglong)]
    status = Status(); status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('Windows physical-memory observation failed')
    return status.available


def process_start(pid=None):
    """Read only this process or the exact authenticated launcher PID."""
    from ctypes import wintypes
    kernel=ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=(wintypes.HANDLE,*(ctypes.POINTER(wintypes.FILETIME),)*4)
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    handle=kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1000,False,pid)
    if not handle:raise RuntimeUnavailable('BLOCKED_ENV','authenticated launcher identity unavailable')
    created,ended,cpu,user=(wintypes.FILETIME() for _ in range(4))
    try:
        if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(ended),ctypes.byref(cpu),ctypes.byref(user)):
            raise RuntimeUnavailable('BLOCKED_ENV','authenticated launcher start time unavailable')
        return (created.dwHighDateTime<<32)|created.dwLowDateTime
    finally:
        if pid is not None:kernel.CloseHandle(handle)


def preflight(app, *, memory_probe=memory_available, disk_probe=shutil.disk_usage):
    app = Path(app).resolve()
    require(app.drive.upper() == 'E:', 'E_PERSONAL_RUNTIME_PATH', 'CPU resources must belong to E installation')
    from .windows_training_runtime import _root
    require(not (_root(app) / 'cpu-execution.lock').exists(),
            'E_WINDOWS_SCR_BUSY', 'CPU lease exists; existing processes and leases remain unchanged')
    try:
        available, free = memory_probe(), disk_probe(app).free
    except (OSError, AttributeError) as exc:
        raise RuntimeUnavailable('BLOCKED_ENV', 'CPU RAM/disk observation is unavailable') from exc
    require(type(available) is int and available >= 5 * 2**30,
            'BLOCKED_ENV', 'at least 5 GiB available CPU RAM required for bounded SCR work')
    require(type(free) is int and free >= 2 * 2**30,
            'BLOCKED_ENV', 'at least 2 GiB free E-drive disk required')
    return {'schema': 'personal-runtime-cpu-preflight/1', 'status': 'AVAILABLE_NOT_QUALIFIED',
            'available_cpu_ram_bytes': available, 'free_E_bytes': free,
            'GPU_requested': False, 'GPU_queried': False, 'existing_processes_stopped': False}
