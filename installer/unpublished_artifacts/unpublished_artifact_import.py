"""Candidate offline registry for the exact old 0178 artifact; never publishes.

This module does not import ML libraries, execute archive code, load weights,
create style runs/profiles, register a runtime, or modify historical receipts.
The registry is separate from style_run_artifacts, whose rows mean published.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import uuid
import zipfile

from engine.style.m2_adapter.trainer_resume_protocol import native_path


ADAPTER_SHA256 = '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
ADAPTER_MEMBER = 'training-run/artifact/m2_adapter.pt'
MANIFEST_MEMBER = 'training-run/artifact/artifact.json'
PACKAGES = {
    'core': {
        'library_file_id': 'libfile_b60e37d4d1c881919e005509530cee9c',
        'filename': 'YY-NIKA-Reviewed72-SCR2N-Spatial-Run4-M2-Actual-Adapter-Core-20261004.zip',
        'bytes': 63360336,
        'sha256': '165de8f1e7872531b40644379b850f84bde05ec63a84331928e8cb8a5ea3f01d',
    },
    'profile': {
        'library_file_id': 'libfile_9a4957e5a00c8191bb51e2cb596cbf66',
        'filename': 'YY-Accepted-V3-Profile-Import-20261005.zip',
        'bytes': 7228185,
        'sha256': 'de1169745f33f43ace2db2bc534ddc9f83a9b84860e1238670311fb127f3224f',
    },
}
TABLE = 'personal_unpublished_artifact_imports'


class ImportBlocked(ValueError):
    pass


def require(condition, detail):
    if not condition:
        raise ImportBlocked(detail)


def sha_stream(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        h.update(block)
    return h.hexdigest()


def sha_file(path):
    with native_path(path).open('rb') as stream:
        return sha_stream(stream)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def regular(path, *, directory=False, e_only=False):
    path = Path(path).absolute()
    if e_only:
        require(path.drive.upper() == 'E:', 'installation input/output must be on E:')
    for node in (path, *path.parents):
        native = native_path(node)
        if native.exists():
            info = native.lstat()
            require(not stat.S_ISLNK(info.st_mode)
                    and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'symlink/reparse input or destination is not allowed')
    if directory:
        require(native_path(path).is_dir(), 'explicit existing import root required')
    else:
        require(native_path(path).is_file(), 'local regular file required: ' + path.name)
    return path


def safe_member(name):
    parts = name.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                *('LPT' + str(i) for i in range(1, 10))}
    require(name and '\\' not in name and ':' not in name and not PurePosixPath(name).is_absolute()
            and all(p not in ('', '.', '..') and p == p.strip() and not p.endswith('.')
                    and not any(ord(c) < 32 for c in p)
                    and p.split('.')[0].upper() not in reserved for p in parts),
            'unsafe archive member')
    return name


def archive_inventory(archive):
    infos = archive.infolist()
    require(len(infos) <= 10000 and sum(i.file_size for i in infos) <= 512 * 1024 * 1024,
            'archive inventory exceeds bounded import budget')
    seen, inventory = {}, {}
    for info in infos:
        # ZIP directory records contain no evidence and are never extracted.
        name = info.filename[:-1] if info.is_dir() else info.filename
        safe_member(name)
        key = name.casefold()
        mode = info.external_attr >> 16
        require(key not in seen and not info.flag_bits & 1
                and stat.S_IFMT(mode) in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG),
                'duplicate/case-colliding, encrypted, or special archive member')
        seen[key] = info.is_dir()
        if not info.is_dir():
            with archive.open(info) as stream:
                inventory[info.filename] = {'bytes': info.file_size, 'sha256': sha_stream(stream)}
    for name in inventory:
        require(not any('/'.join(name.split('/')[:i]).casefold() in seen
                        and not seen['/'.join(name.split('/')[:i]).casefold()]
                        for i in range(1, len(name.split('/')))), 'archive file/directory collision')
    return inventory


def json_object(archive, name, inventory):
    require(inventory[name]['bytes'] <= 4 * 1024 * 1024, 'JSON evidence exceeds budget')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate JSON evidence key')
            value[key] = item
        return value
    def bad_number(value):
        raise ImportBlocked('non-finite JSON evidence number')
    return json.loads(archive.read(name), object_pairs_hook=unique, parse_constant=bad_number)


def hash_claims(value, pointer=''):
    """Return only hash declarations; never copy account/profile text into reports."""
    claims = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
            if isinstance(item, str) and ('sha256' in key or key == 'sha'):
                if len(item) == 64 and all(c in '0123456789abcdef' for c in item.lower()):
                    claims.append({'pointer': child, 'sha256': item.lower()})
            elif isinstance(item, (dict, list)):
                claims.extend(hash_claims(item, child))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            claims.extend(hash_claims(item, pointer + '/' + str(i)))
    return claims


def declared_link(relative_path, expected_sha256, inventory):
    """Distinguish a producer declaration from bytes actually present in core."""
    require(isinstance(relative_path, str), 'manifest evidence path must be a string')
    require(isinstance(expected_sha256, str) and len(expected_sha256) == 64
            and all(c in '0123456789abcdef' for c in expected_sha256), 'invalid declared SHA256')
    safe_member(relative_path)
    member = 'training-run/' + relative_path
    actual = inventory.get(member)
    return {'relative_path': relative_path, 'member': member,
            'expected_sha256': expected_sha256,
            'actual_sha256': actual['sha256'] if actual else None,
            'present': actual is not None,
            'hash_verified': actual is not None and actual['sha256'] == expected_sha256}


def inspect_packages(core_zip, profile_zip):
    """Hash pinned bytes and discover real manifest/evidence; no model execution."""
    paths = {'core': core_zip, 'profile': profile_zip}
    report = {'schema': 'personal-unpublished-artifact-input/1', 'adapter_sha256': ADAPTER_SHA256,
              'publication_status': 'UNPUBLISHED', 'runtime_status': 'NOT_REGISTERED',
              'model_quality_approval': False, 'training_resume_state_required': False,
              'packages': {}, 'manifest': None, 'evidence_links': []}
    with ExitStack() as stack:
        archives, inventories = {}, {}
        for role, source in paths.items():
            handle = stack.enter_context(native_path(regular(source)).open('rb'))
            spec = PACKAGES[role]
            require(os.fstat(handle.fileno()).st_size == spec['bytes']
                    and sha_stream(handle) == spec['sha256'], role + ' package SHA/size mismatch')
            handle.seek(0)
            archive = stack.enter_context(zipfile.ZipFile(handle))
            inventories[role] = inventory = archive_inventory(archive)
            archives[role] = archive
            report['packages'][role] = {**spec, 'members': inventory, 'locally_verified': True}
        indexed = inventories['core']
        require(ADAPTER_MEMBER in indexed and indexed[ADAPTER_MEMBER]['sha256'] == ADAPTER_SHA256,
                'core must contain exact old 0178 adapter; generic/other adapter refused')
        report['adapter'] = {'member': ADAPTER_MEMBER, **indexed[ADAPTER_MEMBER]}
        for role, inventory in inventories.items():
            for name in sorted(inventory):
                if name.endswith('.json') and inventory[name]['bytes'] <= 4 * 1024 * 1024:
                    try:
                        value = json_object(archives[role], name, inventory)
                    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                        raise ImportBlocked('invalid bounded JSON evidence: ' + name) from exc
                    claims = hash_claims(value)
                    if claims:
                        report['evidence_links'].append({'package': role, 'member': name,
                                                        'sha256': inventory[name]['sha256'], 'hash_claims': claims})
                    if role == 'core' and name == MANIFEST_MEMBER:
                        require(isinstance(value, dict), 'artifact manifest must be an object')
                        files = value.get('files')
                        matches = [f for f in files if isinstance(f, dict)
                                   and f.get('relative_path') == 'artifact/m2_adapter.pt'] if isinstance(files, list) else []
                        require(len(matches) == 1 and matches[0].get('sha256') == ADAPTER_SHA256,
                                'manifest does not bind the exact 0178 adapter')
                        links = []
                        paths_seen = set()
                        for entry in files:
                            require(isinstance(entry, dict), 'invalid artifact inventory declaration')
                            require(entry.get('relative_path') not in paths_seen, 'duplicate artifact inventory declaration')
                            paths_seen.add(entry.get('relative_path'))
                            links.append(declared_link(entry.get('relative_path'), entry.get('sha256'), indexed))
                        metrics = value.get('metrics_receipt_relative_path')
                        if metrics is not None:
                            engine = declared_link(metrics, value.get('metrics_receipt_sha256'), indexed)
                            links.append(engine)
                            if engine['hash_verified']:
                                wrapper = json_object(archives['core'], engine['member'], indexed)
                                anchor = wrapper.get('training_receipt') if isinstance(wrapper, dict) else None
                                if isinstance(anchor, dict):
                                    links.append(declared_link(anchor.get('relative_path'), anchor.get('sha256'), indexed))
                        report['manifest'] = {'member': name, 'sha256': inventory[name]['sha256'],
                                              'adapter_binding_verified': True,
                                              'declared_file_links': links,
                                              'snapshot_declarations': hash_claims(value.get('snapshot_binding')),
                                              'lineage_status': 'RECORDED_NOT_RUNTIME_VERIFIED'}
        report['profile_target_declaration_found'] = any(
            e['package'] == 'profile' and any(c['sha256'] == ADAPTER_SHA256 for c in e['hash_claims'])
            for e in report['evidence_links'])
        report['profile_manifest_declaration_found'] = report['manifest'] is not None and any(
            e['package'] == 'profile' and any(c['sha256'] == report['manifest']['sha256'] for c in e['hash_claims'])
            for e in report['evidence_links'])
        report['importable_unpublished'] = report['manifest'] is not None
        report['inference_ready'] = False
        report['publish_ready'] = False
    report['input_fingerprint_sha256'] = hashlib.sha256(canonical(report)).hexdigest()
    return report


CREATE_TABLE = f'''CREATE TABLE IF NOT EXISTS {TABLE} (
 import_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
 adapter_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 input_fingerprint_sha256 TEXT NOT NULL, relative_directory TEXT NOT NULL,
 receipt_sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status='unpublished'),
 created_at TEXT NOT NULL, UNIQUE(user_id, adapter_sha256)
)'''


def import_unpublished(*, database, import_root, user_id, core_zip, profile_zip, e_only=True):
    """Explicit offline commit. Caller must authorize it; inspect does not call it.

    No rows in existing published/run/profile/auth/review tables are written.
    A cancelled/failed import retains any newly staged files for inspection.
    """
    require(isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0,
            'positive existing account ID required')
    database = regular(database, e_only=e_only)
    root = regular(import_root, directory=True, e_only=e_only)
    core_zip = regular(core_zip, e_only=e_only)
    profile_zip = regular(profile_zip, e_only=e_only)
    report = inspect_packages(core_zip, profile_zip)
    require(report['importable_unpublished'], 'original bound artifact manifest is missing')
    name = ADAPTER_SHA256 + '-u' + str(user_id)
    destination = root / name
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True)) as db:
        db.execute('BEGIN IMMEDIATE')
        account = db.execute('SELECT enabled FROM users WHERE id=?', (user_id,)).fetchone()
        require(account is not None and account[0] == 1, 'existing enabled application account required')
        db.execute(CREATE_TABLE)
        existing = db.execute(f'SELECT input_fingerprint_sha256,relative_directory,receipt_sha256 '
                              f'FROM {TABLE} WHERE user_id=? AND adapter_sha256=?',
                              (user_id, ADAPTER_SHA256)).fetchone()
        if existing:
            require(existing[0] == report['input_fingerprint_sha256'] and existing[1] == name,
                    'existing association differs; no overwrite/reassignment')
            regular(destination, directory=True)
            receipt = regular(destination / 'import-receipt.json')
            require(sha_file(receipt) == existing[2], 'existing receipt changed')
            previous = json.loads(receipt.read_bytes())
            require(previous['publication_status'] == 'UNPUBLISHED' and previous['user_id'] == user_id
                    and previous['input_fingerprint_sha256'] == existing[0], 'existing receipt identity changed')
            for role, spec in PACKAGES.items():
                require(sha_file(regular(destination / 'packages' / spec['filename'])) == spec['sha256'],
                        'existing package changed: ' + role)
            require(sha_file(regular(destination / 'artifact' / 'm2_adapter.pt')) == ADAPTER_SHA256,
                    'existing adapter changed')
            require(sha_file(regular(destination / 'artifact' / 'artifact.json')) == report['manifest']['sha256'],
                    'existing manifest changed')
            return {**previous, 'idempotent_replay': True}
        require(not destination.exists(), 'destination already exists; preserve and inspect prior import')
        stage = root / ('.unpublished-stage-' + uuid.uuid4().hex)
        stage.mkdir()
        (stage / 'packages').mkdir()
        (stage / 'artifact').mkdir()
        for role, source in [('core', core_zip), ('profile', profile_zip)]:
            target = stage / 'packages' / PACKAGES[role]['filename']
            with source.open('rb') as incoming, target.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1 << 20)
            require(sha_file(target) == PACKAGES[role]['sha256'], 'source package changed during copy')
        with zipfile.ZipFile(stage / 'packages' / PACKAGES['core']['filename']) as archive:
            for member, filename in [(ADAPTER_MEMBER, 'm2_adapter.pt'), (MANIFEST_MEMBER, 'artifact.json')]:
                with archive.open(member) as incoming, (stage / 'artifact' / filename).open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1 << 20)
        require(sha_file(stage / 'artifact' / 'm2_adapter.pt') == ADAPTER_SHA256
                and sha_file(stage / 'artifact' / 'artifact.json') == report['manifest']['sha256'],
                'materialized identity differs')
        now = datetime.now(timezone.utc).isoformat()
        receipt = {**report, 'schema': 'personal-unpublished-artifact-import/1', 'user_id': user_id,
                   'import_id': uuid.uuid4().hex, 'created_at': now,
                   'historical_evidence_modified': False, 'new_training_performed': False,
                   'sample_approval_scope': 'SAMPLE_ONLY_NOT_MODEL_PUBLICATION',
                   'published_registry_rows_written': 0, 'profiles_created': 0}
        raw = canonical(receipt) + b'\n'
        with (stage / 'import-receipt.json').open('xb') as target:
            target.write(raw)
        # mkdir is the atomic replay/crash barrier. Never replace an existing path.
        destination.mkdir()
        for child in stage.iterdir():
            child.rename(destination / child.name)
        # Empty staging directories are retained; no historical path is deleted.
        db.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?)',
                   (receipt['import_id'], user_id, ADAPTER_SHA256, report['manifest']['sha256'],
                    report['input_fingerprint_sha256'], name, hashlib.sha256(raw).hexdigest(), 'unpublished', now))
        db.commit()
        return {**receipt, 'idempotent_replay': False}
