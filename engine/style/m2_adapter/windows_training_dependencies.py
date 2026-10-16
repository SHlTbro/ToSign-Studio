"""Read-only installation diagnostics; never qualification or execution authority.

Web discovery checks local path metadata only. Explicit operator --verify-files
hashes pinned feature/font inputs. Neither path loads Torch or pretrained weights.
"""
import hashlib
import json
from pathlib import Path, PureWindowsPath

SCHEMA = 'windows-personal-style-installation/1'
KEYS = ('module', 'checkpoint', 'state_schema', 'font_sans', 'font_serif', 'source_ledger')
FONT_NAMES = ('NotoSansCJK-Regular.ttc', 'NotoSerifCJK-Regular.ttc')
FONT_SIZES = (19484784, 26411360)


def _e_path(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError('an absolute E-drive path is required')
    lexical = PureWindowsPath(value)
    if (not lexical.is_absolute() or lexical.drive.upper() != 'E:'
            or '..' in lexical.parts):
        raise ValueError('an absolute contained E-drive path is required')
    return Path(value)


def _safe_path(value):
    path = _e_path(value)
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise ValueError('linked installation paths are not accepted')
    if path.resolve().drive.upper() != 'E:':
        raise ValueError('resolved installation path must remain on E')
    return path


def _read(path):
    path = _safe_path(str(path))
    if path.stat().st_size > 32768:
        raise ValueError('installation configuration must be a bounded regular file')
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if (not isinstance(value, dict) or set(value) - {'schema', 'python', 'asset_root', 'assets'}
            or value.get('schema') != SCHEMA or not isinstance(value.get('assets'), dict)
            or set(value['assets']) - set(KEYS)):
        raise ValueError('invalid installation configuration schema or asset names')
    for field in ('python', 'asset_root'):
        if field in value:
            _e_path(value[field])
    for item in value['assets'].values():
        _e_path(item)
    return value


def _probe(key, raw_path, expected=None, size=None, verify=False):
    row = {'key': key, 'status': 'NOT_CONFIGURED', 'verified': False}
    if expected is not None:
        row['expected_sha256'] = expected
    if size is not None:
        row['expected_bytes'] = size
    if not raw_path:
        return row
    try:
        path = _safe_path(raw_path)
        info = path.stat()
        if not path.is_file():
            raise ValueError('a regular file is required')
        row.update(status='PRESENT_UNVERIFIED', bytes=info.st_size)
        if size is not None and info.st_size != size:
            row['status'] = 'SIZE_MISMATCH'
        elif verify and expected is not None:
            # Fixed SCR files only: bounded input, no pretrained model loading.
            if info.st_size > 512 * 1024 * 1024:
                raise ValueError('pinned SCR verification input exceeds 512 MiB')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            row['sha256'] = digest.hexdigest()
            row['verified'] = row['sha256'] == expected
            row['status'] = 'BYTES_VERIFIED' if row['verified'] else 'HASH_MISMATCH'
    except FileNotFoundError:
        row['status'] = 'MISSING'
    except PermissionError:
        row['status'] = 'ACCESS_DENIED'
    except (OSError, ValueError):
        row['status'] = 'INVALID_PATH'
    return row


def inspect_dependencies(app, installation_path=None, verify=False):
    """Metadata is guidance only; even matching hashes never grant may_train."""
    from .user_scr_contract import asset_contract
    from .release_runtime_policy import assert_m2_execution_allowed
    app = Path(app)
    path = installation_path or app / 'config/personal-style-installation.json'
    value, config_status = {}, 'NOT_CONFIGURED'
    try:
        value = _read(path)
        config_status = 'PRESENT'
    except FileNotFoundError:
        pass
    except PermissionError:
        config_status = 'ACCESS_DENIED'
    except (OSError, ValueError, TypeError):
        config_status = 'INVALID'
    spec = asset_contract()
    assets = value.get('assets', {})
    rows = []
    for key in KEYS:
        expected = (spec['font_sha256'][('font_sans', 'font_serif').index(key)]
                    if key.startswith('font_') else spec.get(key + '_sha256'))
        size = (FONT_SIZES[('font_sans', 'font_serif').index(key)] if key.startswith('font_')
                else spec.get('checkpoint_size_bytes') if key == 'checkpoint' else None)
        row = _probe(key, assets.get(key), expected, size, verify)
        if key.startswith('font_'):
            row['file'] = FONT_NAMES[('font_sans', 'font_serif').index(key)]
        rows.append(row)
    interpreter = _probe('python', value.get('python'))
    try:
        assert_m2_execution_allowed(app)
        policy = {'status': 'UNSUPPORTED', 'code': 'E_PERSONAL_RUNTIME_POLICY_REQUIRED'}
    except (OSError, ValueError) as exc:
        policy = {'status': 'BLOCKED', 'code': getattr(exc, 'code', 'E_RELEASE_RUNTIME_POLICY')}
    from .personal_runtime_policy import present,bootstrap
    if present(app):
        try:
            bootstrap(app)
            policy={'status':'QUALIFICATION_ONLY','code':'E_WINDOWS_GENERIC_QUALIFICATION_REQUIRED'}
        except (OSError,ValueError) as exc:
            policy={'status':'BLOCKED','code':getattr(exc,'code','BLOCKED_ENV')}
    return {'schema': 'windows-personal-style-dependencies/1', 'status': 'BLOCKED',
            'configuration_status': config_status, 'interpreter': interpreter, 'assets': rows,
            'full_model': {'status': 'NOT_PROBED', 'configured': bool(value.get('asset_root'))},
            'execution_policy': policy, 'qualification': 'NOT_RUN_BY_DIAGNOSTIC',
            'claims_quality_pass': False, 'may_prepare': False, 'may_train': False,
            'may_publish': False, 'verification_mode': 'PINNED_FILE_BYTES' if verify else 'METADATA_ONLY',
            'recovery': {'check_command': "python scripts/windows_personal_runtime_check.py --inputs '<E-drive installation JSON>' --verify-files",
                'steps': ['INSTALL_E_ONLY_INPUTS', 'VERIFY_FIXED_SCR_FONTS_AND_FEATURES',
                          'INSTALL_SIGNED_PERSONAL_RUNTIME_AND_EXISTING_LOCAL_AUTHORITY',
                          'RUN_FORMAL_CURRENT_SOURCE_WINDOWS_CPU_QUALIFICATION',
                          'REFRESH_P7_SELECT_QUALIFIED_REGISTRATION', 'PREPARE_OWNED_FROZEN_S03',
                          'CREATE_AND_START_REAL_M2'],
                'qualification_entrypoint': 'scripts/windows_scr_training.py',
                'qualification_requires': ['separate_executable_policy', 'readable_full_model_assets',
                    'verified_owned_S03_and_source_ledger', 'resource_clearance',
                    'fresh_current_source_producer_PASS'],
                'receipt_import_supported': False}}
