"""Load the pinned app-local FriBidi before Pillow initializes its RAQM bridge."""
import ctypes
import hashlib
import os
from pathlib import Path

DLL_SHA256 = 'ae9e30c61e1c62886d36e23a91d2943970ae990f85155cbd222b9ba44de9c51b'
LICENSE_SHA256 = '20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95'
_handles = []
_library = None


def bootstrap():
    global _library
    if os.name != 'nt' or _handles:
        return
    root = Path(os.environ.get('YY_FONT_PRESETS_ROOT') or Path(__file__).resolve().parents[2] / 'fonts/presets')
    library = root / 'native/fribidi/libfribidi-0.dll'
    if not library.is_file():
        return
    if (hashlib.sha256(library.read_bytes()).hexdigest() != DLL_SHA256
            or hashlib.sha256(library.with_name('COPYING').read_bytes()).hexdigest() != LICENSE_SHA256):
        raise ValueError('Pinned V3 font shaping library identity/license unavailable')
    _handles.append(os.add_dll_directory(str(library.parent.resolve())))
    _handles.append(ctypes.CDLL(str(library.resolve())))
    _library = library.resolve()


def identity():
    bootstrap()
    if _library is not None and (hashlib.sha256(_library.read_bytes()).hexdigest() != DLL_SHA256
            or hashlib.sha256(_library.with_name('COPYING').read_bytes()).hexdigest() != LICENSE_SHA256):
        raise ValueError('V3 font shaping resource changed after startup')
    from PIL import features
    return {'schema': 'v3-font-shaping/1', 'fribidi_library_sha256': DLL_SHA256 if _handles else None,
            'raqm': features.version_feature('raqm'), 'harfbuzz': features.version_feature('harfbuzz'),
            'fribidi': features.version_feature('fribidi'), 'license_sha256': LICENSE_SHA256,
            'ready': features.check_feature('raqm')}
