"""Read the local Windows qualification key without opening a write/delete race."""

import ctypes
import os
import sys
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path

from .trainer_resume_protocol import native_path


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_SE_DACL_PROTECTED = 0x1000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


def _win_error():
    return ctypes.WinError(ctypes.get_last_error())


def _bind(dll, name, argtypes, restype):
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


class _WindowsApi:
    """Small private Win32 seam; tests replace it with synthetic metadata."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows qualification-key access is required")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    @contextmanager
    def open_deny_write_delete(self, path, *, directory):
        access = (0x80 | 0x20000) if directory else (0x80000000 | 0x20000)
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        create = _bind(
            self.kernel,
            "CreateFileW",
            (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE),
            wintypes.HANDLE,
        )
        handle = create(str(native_path(path)), access, 0x1, None, 3, flags, None)
        if handle is None or handle == -1 or handle == ctypes.c_void_p(-1).value:
            raise _win_error()
        try:
            yield handle
        finally:
            close = _bind(self.kernel, "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL)
            if not close(handle):
                raise _win_error()

    def file_information(self, handle):
        info = _BY_HANDLE_FILE_INFORMATION()
        query = _bind(
            self.kernel,
            "GetFileInformationByHandle",
            (wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)),
            wintypes.BOOL,
        )
        if not query(handle, ctypes.byref(info)):
            raise _win_error()
        attributes = int(info.dwFileAttributes)
        return {
            "is_directory": bool(attributes & _FILE_ATTRIBUTE_DIRECTORY),
            "is_reparse": bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            "link_count": int(info.nNumberOfLinks),
            "size": (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
        }

    def _sid_text(self, sid):
        if not sid:
            raise ValueError("E_QUALIFICATION_KEY_OWNER")
        valid = _bind(self.advapi, "IsValidSid", (wintypes.LPVOID,), wintypes.BOOL)
        if not valid(sid):
            raise ValueError("E_QUALIFICATION_KEY_SID")
        convert = _bind(
            self.advapi, "ConvertSidToStringSidW",
            (wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID)), wintypes.BOOL,
        )
        text_pointer = wintypes.LPVOID()
        if not convert(sid, ctypes.byref(text_pointer)):
            raise _win_error()
        try:
            return ctypes.wstring_at(text_pointer.value)
        finally:
            local_free = _bind(self.kernel, "LocalFree", (wintypes.HLOCAL,), wintypes.HLOCAL)
            local_free(text_pointer)

    def current_user_sid(self):
        get_process = _bind(self.kernel, "GetCurrentProcess", (), wintypes.HANDLE)
        open_token = _bind(
            self.advapi, "OpenProcessToken",
            (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)), wintypes.BOOL,
        )
        token = wintypes.HANDLE()
        if not open_token(get_process(), 0x0008, ctypes.byref(token)):
            raise _win_error()
        try:
            get_info = _bind(
                self.advapi, "GetTokenInformation",
                (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                 ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL,
            )
            needed = wintypes.DWORD()
            get_info(token, 1, None, 0, ctypes.byref(needed))  # TokenUser sizing query
            if needed.value < ctypes.sizeof(_TOKEN_USER) or needed.value > 65536:
                raise ValueError("E_QUALIFICATION_KEY_TOKEN")
            buffer = ctypes.create_string_buffer(needed.value)
            if not get_info(token, 1, ctypes.byref(buffer), needed.value, ctypes.byref(needed)):
                raise _win_error()
            user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
            return self._sid_text(user.User.Sid)
        finally:
            close = _bind(self.kernel, "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL)
            if not close(token):
                raise _win_error()

    def security_information(self, handle):
        get_security = _bind(
            self.advapi, "GetSecurityInfo",
            (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
             ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
             ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
             ctypes.POINTER(wintypes.LPVOID)),
            wintypes.DWORD,
        )
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        status = get_security(
            handle, 1, 0x1 | 0x4, ctypes.byref(owner), None, ctypes.byref(dacl), None,
            ctypes.byref(descriptor),
        )
        if status:
            raise OSError(status, "GetSecurityInfo failed")
        try:
            get_control = _bind(
                self.advapi, "GetSecurityDescriptorControl",
                (wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)),
                wintypes.BOOL,
            )
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise _win_error()
            get_dacl = _bind(
                self.advapi, "GetSecurityDescriptorDacl",
                (wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(wintypes.LPVOID),
                 ctypes.POINTER(wintypes.BOOL)), wintypes.BOOL,
            )
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            queried_dacl = wintypes.LPVOID()
            if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(queried_dacl),
                            ctypes.byref(defaulted)):
                raise _win_error()
            result = {
                "owner_sid": self._sid_text(owner),
                "dacl_present": bool(present.value and queried_dacl.value),
                "dacl_protected": bool(control.value & _SE_DACL_PROTECTED),
                "aces": [],
            }
            if not result["dacl_present"]:
                return result
            acl_info = _ACL_SIZE_INFORMATION()
            get_acl_info = _bind(
                self.advapi, "GetAclInformation",
                (wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD),
                wintypes.BOOL,
            )
            if not get_acl_info(queried_dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2):
                raise _win_error()
            get_ace = _bind(
                self.advapi, "GetAce",
                (wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)),
                wintypes.BOOL,
            )
            for index in range(int(acl_info.AceCount)):
                ace = wintypes.LPVOID()
                if not get_ace(queried_dacl, index, ctypes.byref(ace)):
                    raise _win_error()
                if not ace.value:
                    raise ValueError("E_QUALIFICATION_KEY_ACE")
                header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
                if header.AceSize < ctypes.sizeof(_ACE_HEADER):
                    raise ValueError("E_QUALIFICATION_KEY_ACE")
                if header.AceType not in (0, 1):  # ACCESS_ALLOWED / ACCESS_DENIED only
                    result["aces"].append({"type": "unknown"})
                    continue
                if header.AceSize < 8:
                    raise ValueError("E_QUALIFICATION_KEY_ACE")
                address = ctypes.cast(ace, ctypes.c_void_p).value
                mask = ctypes.c_uint32.from_address(address + 4).value
                sid_pointer = wintypes.LPVOID(address + 8)
                sid_length = _bind(self.advapi, "GetLengthSid", (wintypes.LPVOID,), wintypes.DWORD)(sid_pointer)
                if sid_length < 8 or sid_length > header.AceSize - 8:
                    raise ValueError("E_QUALIFICATION_KEY_ACE")
                result["aces"].append({
                    "type": "allow" if header.AceType == 0 else "deny",
                    "sid": self._sid_text(sid_pointer),
                    "mask": int(mask),
                })
            return result
        finally:
            if descriptor:
                local_free = _bind(self.kernel, "LocalFree", (wintypes.HLOCAL,), wintypes.HLOCAL)
                local_free(descriptor)

    def read(self, handle, size):
        buffer = (ctypes.c_ubyte * size)()
        received = wintypes.DWORD()
        read_file = _bind(
            self.kernel, "ReadFile",
            (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
             wintypes.LPVOID),
            wintypes.BOOL,
        )
        if not read_file(handle, buffer, size, ctypes.byref(received), None):
            raise _win_error()
        return bytes(buffer[:received.value])


def _windows_api():
    return _WindowsApi()


def current_user_sid():
    """Return the current Windows token's user SID for enrollment binding."""
    _reject(os.name == "nt" and sys.platform == "win32", "E_QUALIFICATION_KEY_WINDOWS_ONLY")
    return _windows_api().current_user_sid()


def _reject(condition, code):
    if not condition:
        raise ValueError(code)


def _private_key_path(path):
    path = Path(path).absolute()
    _reject(
        path.drive.upper() == "E:"
        and path.name.casefold() == "qualification-key"
        and path.parent.name.casefold() == "private"
        and path.parent.parent.name.casefold() == "windows-scr-training",
        "E_QUALIFICATION_KEY_PATH",
    )
    try:
        chain = (path, *path.parents)
        for component in chain:
            native = native_path(component)
            _reject(not native.is_symlink() and not native.is_junction(), "E_QUALIFICATION_KEY_REPARSE")
    except OSError as exc:
        raise ValueError("E_QUALIFICATION_KEY_PATH") from exc
    return path


def _validate_security(api, handle, user_sid):
    security = api.security_information(handle)
    _reject(isinstance(security, dict), "E_QUALIFICATION_KEY_ACL")
    _reject(security.get("dacl_present") is True, "E_QUALIFICATION_KEY_NULL_DACL")
    _reject(security.get("dacl_protected") is True, "E_QUALIFICATION_KEY_DACL_INHERITED")
    trusted = {str(user_sid).upper(), _SYSTEM_SID, _ADMINISTRATORS_SID}
    owner = security.get("owner_sid")
    _reject(isinstance(owner, str) and owner.upper() in trusted, "E_QUALIFICATION_KEY_OWNER")
    aces = security.get("aces")
    _reject(isinstance(aces, list) and bool(aces), "E_QUALIFICATION_KEY_ACL")
    allowed_count = 0
    for ace in aces:
        _reject(isinstance(ace, dict), "E_QUALIFICATION_KEY_ACE")
        kind = ace.get("type")
        # The private runtime directories must remain writable by their trusted
        # owner so leases and registrations work. Denies and all non-allow ACEs
        # are rejected conservatively; trust is decided by SID, not access mask.
        _reject(kind == "allow", "E_QUALIFICATION_KEY_ACE")
        sid = ace.get("sid")
        mask = ace.get("mask")
        _reject(isinstance(sid, str) and type(mask) is int and 0 <= mask <= 0xFFFFFFFF,
                "E_QUALIFICATION_KEY_ACE")
        _reject(sid.upper() in trusted, "E_QUALIFICATION_KEY_BROAD_ACE")
        allowed_count += 1
    _reject(allowed_count > 0, "E_QUALIFICATION_KEY_NO_READ_ACE")


def read_key(path):
    """Return exactly 32 key bytes after handle-bound Windows ACL checks."""
    _reject(os.name == "nt" and sys.platform == "win32", "E_QUALIFICATION_KEY_WINDOWS_ONLY")
    path = _private_key_path(path)
    api = _windows_api()
    user_sid = api.current_user_sid()
    training_root, private = path.parent.parent, path.parent
    with ExitStack() as stack:
        for directory_path in (training_root, private):
            handle = stack.enter_context(api.open_deny_write_delete(directory_path, directory=True))
            info = api.file_information(handle)
            _reject(info.get("is_directory") is True, "E_QUALIFICATION_KEY_PARENT_TYPE")
            _reject(info.get("is_reparse") is False, "E_QUALIFICATION_KEY_REPARSE")
            _validate_security(api, handle, user_sid)
        handle = stack.enter_context(api.open_deny_write_delete(path, directory=False))
        info = api.file_information(handle)
        _reject(info.get("is_directory") is False, "E_QUALIFICATION_KEY_FILE_TYPE")
        _reject(info.get("is_reparse") is False, "E_QUALIFICATION_KEY_REPARSE")
        _reject(type(info.get("link_count")) is int and info["link_count"] == 1,
                "E_QUALIFICATION_KEY_LINKS")
        _reject(type(info.get("size")) is int and info["size"] == 32,
                "E_QUALIFICATION_KEY_LENGTH")
        _validate_security(api, handle, user_sid)
        value = api.read(handle, 33)
        _reject(isinstance(value, bytes) and len(value) == 32, "E_QUALIFICATION_KEY_LENGTH")
        return value
