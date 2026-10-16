"""Explicit restore of one shipped, previously published model into a local account.

Package hashes and the catalog's code pin are the authority. Imported JSON cannot
add models, waive gates or create users. Historical producer bytes remain intact;
only a new ownership association and separately verified deployment are created.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid
import zipfile

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.core.storage import storage_root
from app.domains.auth.models import User
from app.domains.styles.models import StyleProfile, StyleRun, StyleRunArtifact
from engine.style.m2_adapter.deployment import descriptor_path, install_deployment, regular_path

CATALOG_PATH = 'config/bundled-model-restore-catalog.json'
CATALOG_SHA256 = '2df68ca12e7d636f3571c1ecf421b51368c71710f5132fc89c371ee9775fda9b'
QUALITY = 'AI_ACCEPTED_USER_AUTHORIZED'


class RestoreError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def _require(condition, code, detail):
    if not condition:
        raise RestoreError(code, detail)


def _sha_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def _sha(path):
    with regular_path(path).open('rb') as stream:
        return _sha_stream(stream)


def _json_bytes(raw):
    def unique(items):
        obj = {}
        for key, value in items:
            _require(key not in obj, 'E_RESTORE_JSON', 'duplicate JSON key')
            obj[key] = value
        return obj
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def load_catalog(app):
    raw = regular_path(Path(app) / CATALOG_PATH).read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == CATALOG_SHA256,
             'E_RESTORE_CATALOG', 'shipped restore catalog differs from the code-pinned release')
    return _json_bytes(raw)


def _safe_name(name):
    _require(isinstance(name, str) and name and '\\' not in name and ':' not in name,
             'E_RESTORE_ARCHIVE', 'archive member path is not portable')
    parts = name.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                *('LPT' + str(i) for i in range(1, 10))}
    _require(not PurePosixPath(name).is_absolute()
             and all(p not in ('', '.', '..') and p == p.strip() and not p.endswith('.')
                     and not any(ord(c) < 32 for c in p) and p.split('.')[0].upper() not in reserved
                     for p in parts), 'E_RESTORE_ARCHIVE', 'unsafe archive member path')
    return name


def validate_archive_members(archive):
    """Reject extraction ambiguity even before consulting a trusted inventory."""
    infos = archive.infolist()
    _require(len(infos) <= 10000, 'E_RESTORE_ARCHIVE', 'archive has too many members')
    found = {}
    for info in infos:
        name = _safe_name(info.filename)
        key = name.casefold()
        mode = info.external_attr >> 16
        _require(not info.is_dir() and (stat.S_IFMT(mode) in (0, stat.S_IFREG))
                 and not info.flag_bits & 1, 'E_RESTORE_ARCHIVE', 'archive must contain only unencrypted regular files')
        _require(key not in found, 'E_RESTORE_ARCHIVE', 'duplicate or case-colliding archive member')
        found[key] = name
    for name in found:
        _require(not any('/'.join(name.split('/')[:i]) in found for i in range(1, len(name.split('/')))),
                 'E_RESTORE_ARCHIVE', 'archive file/directory collision')
    return {info.filename: info for info in infos}


def _open_archive(stack, path, specification, *, full_inventory=True):
    # Hash and read the same open handle; replacing the pathname cannot substitute
    # another archive after verification.
    handle = stack.enter_context(regular_path(path).open('rb'))
    _require(os.fstat(handle.fileno()).st_size == specification['bytes']
             and _sha_stream(handle) == specification['sha256'],
             'E_RESTORE_PACKAGE', 'archive does not match the shipped package SHA256/size')
    handle.seek(0)
    archive = stack.enter_context(zipfile.ZipFile(handle))
    actual = validate_archive_members(archive)
    if full_inventory:
        _require(set(actual) == set(specification['files']), 'E_RESTORE_PACKAGE', 'archive inventory differs')
        for name, expected in specification['files'].items():
            with archive.open(actual[name]) as member:
                _require(actual[name].file_size == expected['bytes'] and _sha_stream(member) == expected['sha256'],
                         'E_RESTORE_PACKAGE', 'archive member hash differs: ' + name)
    return archive


def _historical_proof(core, state, catalog):
    """Check identity and real review/source chains, without re-scoring any image."""
    read = lambda archive, name: _json_bytes(archive.read(name))
    manifest = read(state, 'run/artifact/artifact.json')
    saved = read(state, 'evidence/NONSECRET-PUBLISHED-MODEL-STATE.json')
    publication = read(state, 'evidence/INTERNAL-MODEL-PUBLICATION.json')
    run = saved['tables']['style_runs'][0]
    artifact = saved['tables']['style_run_artifacts'][0]
    profile = next(p for p in saved['tables']['style_profiles'] if p['style_id'] == artifact['profile_id'])
    _require(set(saved['tables']) == {'style_runs', 'style_profiles', 'style_run_artifacts'}
             and len(saved['tables']['style_runs']) == len(saved['tables']['style_run_artifacts']) == 1,
             'E_RESTORE_PROOF', 'unexpected published state tables')
    _require(profile['status'] == publication['status'] == 'approved'
             and profile['quality_status'] == artifact['quality_status'] == publication['quality_status'] == QUALITY
             and artifact['sha256'] == catalog['manifest_sha256']
             and run['status'] == 'succeeded' and run['sample_ids'] == '[]',
             'E_RESTORE_PROOF', 'exact historical publication qualification missing')
    config = _json_bytes(run['config'].encode('utf-8'))
    canonical = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    _require(hashlib.sha256(canonical).hexdigest() == manifest['config_sha256'] == artifact['config_sha256'],
             'E_RESTORE_PROOF', 'original run configuration does not match manifest')
    for key in ('engine_mode', 'engine_id', 'engine_version', 'dataset_snapshot_sha256', 'metrics_receipt_sha256'):
        _require(manifest[key] == artifact[key], 'E_RESTORE_PROOF', 'published artifact lineage mismatch: ' + key)
    source = read(core, 'evidence/RESTORATION-DATA-RECEIPT.json')
    _require(source['snapshot_sha256'] == manifest['snapshot_binding']['snapshot_sha256']
             and source['current_review_sha256'] == catalog['source_qualification']['source_review_sha256']
             and source['ledger_sha256'] == catalog['source_qualification']['source_ledger_sha256']
             and source['synthetic_training_sample_rows'] == 0,
             'E_RESTORE_PROOF', 'source qualification chain differs')
    qualification = catalog['review_qualification']
    readiness = read(state, 'evidence/POST-REVIEW-READINESS.json')
    package = read(state, 'run/review-package.json')
    bundle = read(state, 'evidence/TRUSTED-CURRENT-BLIND-REVIEW-BUNDLE.json')
    review_raw = state.read('evidence/rebuilt-model-blinded-pixel-review-20261003.json')
    review = _json_bytes(review_raw)
    _require(readiness['status'] == 'PUBLISH_READY_AI_AUTHORIZED'
             and set(readiness['engineering_gates'].values()) == {'PASS'}
             and readiness['HUMAN_BLIND_REVIEW'] == 'WAIVED_BY_USER'
             and review['human_owner_approval'] is False
             and bundle['actual_pixels_reviewed'] is True
             and bundle['authoritative_review_sha256'] == hashlib.sha256(review_raw).hexdigest()
             and bundle['authoritative_review_original_utf8'].encode() == review_raw
             and bundle['package_sha256'] == qualification['review_package_sha256']
             and package['adapter_sha256'] == catalog['adapter_sha256']
             and package['nonce'] == bundle['nonce'] == qualification['review_nonce']
             and sorted(package['panels']) == qualification['targets'],
             'E_RESTORE_PROOF', 'actual frozen AI review chain differs')
    aggregate = read(state, 'run/reviews/' + qualification['review_id'] + '/aggregate.json')
    _require(sorted(item['target_char'] for item in aggregate['review_set']) == qualification['targets'],
             'E_RESTORE_PROOF', 'review target set differs')
    for item in aggregate['review_set']:
        codepoint = 'U+' + format(ord(item['target_char']), '04X')
        prefix = 'run/evaluations/' + qualification['evaluation_id'] + '/' + codepoint + '/'
        visual_path = 'run/reviews/' + qualification['review_id'] + '/' + codepoint + '.m2_visual_review.json'
        visual = read(state, visual_path)
        _require(hashlib.sha256(state.read(visual_path)).hexdigest() == item['visual_sha256']
                 and hashlib.sha256(state.read(prefix + 'm2_inference.json')).hexdigest() == item['inference_sha256']
                 and hashlib.sha256(state.read(prefix + 'blind/blind_panel.png')).hexdigest() == visual['panel_sha256']
                 and visual['AI_VISUAL_REVIEW'] == 'PASS'
                 and visual['thresholds'] == qualification['thresholds']
                 and visual['review_response']['pixels_inspected'] is True,
                 'E_RESTORE_PROOF', 'review panel/inference chain differs: ' + codepoint)
    return {'manifest': manifest, 'run': run, 'artifact': artifact, 'profile': profile}


def _no_symlinks(path):
    path = Path(path).absolute()
    _require(not any(p.is_symlink() for p in (path, *path.parents)),
             'E_RESTORE_PATH', 'symlink in restore destination')
    return path


def _write_member(archive, name, path, expected):
    path = _no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.is_file() and _sha(path) == expected['sha256'],
                 'E_RESTORE_CONFLICT', 'conflicting historical archive member')
        return
    with archive.open(name) as source, path.open('xb') as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    _require(path.stat().st_size == expected['bytes'] and _sha(path) == expected['sha256'],
             'E_RESTORE_PACKAGE', 'copied archive member differs')


def _extract_history(core, state, catalog, destination):
    for role, archive in (('core', core), ('state', state)):
        for name, expected in catalog['archives'][role]['files'].items():
            if name.startswith('run/'):
                rel = name[len('run/'):]
            elif role == 'core' and name.startswith('snapshot/'):
                rel = 'restored-snapshot/' + name[len('snapshot/'):]
            else:
                rel = 'restore-source/' + role + '/' + name
            _write_member(archive, name, destination / rel, expected)


def _supplement(destination, catalog, *, archive=None, directory=None):
    for rel, expected in catalog['supplemental_training_state']['required_files'].items():
        if archive is not None:
            _write_member(archive, 'training-run/' + rel, destination / rel, expected)
        else:
            source = regular_path(Path(directory) / rel)
            _require(source.stat().st_size == expected['bytes'] and _sha(source) == expected['sha256'],
                     'E_RESTORE_TRAINING_STATE', 'supplemental checkpoint is not the original: ' + rel)
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            _require(_sha(target) == expected['sha256'], 'E_RESTORE_TRAINING_STATE', 'checkpoint changed during copy')


def restore_personal_model(*, app, user_id, core_zip, state_zip, registration_id,
                           training_state_zip=None, training_state_dir=None):
    """Administrator CLI operation. Returns only after DB commit and deployment.

    The server should be stopped during this explicit install operation. No
    authentication rows, sample rows, or human-review rows are imported.
    """
    _require(bool(os.environ.get('YY_DB_URL')) and bool(os.environ.get('YY_STORAGE_ROOT')),
             'E_RESTORE_TARGET', 'explicit YY_DB_URL and YY_STORAGE_ROOT are required')
    _require(isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0,
             'E_RESTORE_ACCOUNT', 'positive existing local user ID required')
    _require(bool(training_state_zip) != bool(training_state_dir), 'E_RESTORE_TRAINING_STATE',
             'supply exactly one full training-state ZIP or verified training run directory')
    app = regular_path(app, directory=True)
    catalog = load_catalog(app)
    engine = get_engine()
    required_tables = {'users', 'style_runs', 'style_profiles', 'style_run_artifacts'}
    _require(required_tables.issubset(set(inspect(engine).get_table_names())),
             'E_RESTORE_ACCOUNT', 'initialize the installation and create an account through the normal application first')
    now = datetime.now(timezone.utc).isoformat()
    stage = final = claim = deployment_file = None
    claim_owned = deployment_owned = final_owned = committed = False
    try:
        with ExitStack() as stack:
            core = _open_archive(stack, core_zip, catalog['archives']['core'])
            state = _open_archive(stack, state_zip, catalog['archives']['state'])
            proof = _historical_proof(core, state, catalog)
            supplemental = (_open_archive(stack, training_state_zip, catalog['supplemental_training_state'],
                                           full_inventory=False) if training_state_zip else None)
            if training_state_dir:
                regular_path(training_state_dir, directory=True)
            with Session(engine, expire_on_commit=False) as session:
                account = session.get(User, user_id)
                _require(account is not None and account.enabled, 'E_RESTORE_ACCOUNT',
                         'restore target must be an existing enabled account created through the normal application')
                existing = session.query(StyleRunArtifact).filter(StyleRunArtifact.sha256 == catalog['manifest_sha256']).first()
                _require(existing is None, 'E_RESTORE_REPLAY', 'this model already has an account association; it cannot be overwritten or reassigned')
                deployment_file = _no_symlinks(descriptor_path(app, catalog['manifest_sha256']))
                _require(not deployment_file.exists(), 'E_RESTORE_CONFLICT', 'a deployment for this model already exists')
                claim = _no_symlinks(app / 'runtime/model-restore-claims' / (catalog['model_id'] + '.json'))
                claim.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with claim.open('x', encoding='utf-8') as target:
                        json.dump({'status': 'RESTORING', 'user_id': user_id, 'created_at': now}, target)
                    claim_owned = True
                except FileExistsError as exc:
                    raise RestoreError('E_RESTORE_REPLAY', 'restore claim already exists; preserve it and inspect prior restore') from exc
                parent = _no_symlinks(storage_root() / 'users' / str(user_id) / 'styles/runs')
                parent.mkdir(parents=True, exist_ok=True)
                stage = parent / ('.model-restore-' + uuid.uuid4().hex)
                stage.mkdir()
                _extract_history(core, state, catalog, stage)
                _supplement(stage, catalog, archive=supplemental, directory=training_state_dir)
                original_config = proof['run']['config']
                (stage / 'restore-source/original-run-config.json').write_bytes(original_config.encode('utf-8'))
                row = StyleRun(user_id=user_id, status='succeeded', sample_ids='[]', config=original_config,
                               created_at=now, updated_at=now)
                session.add(row)
                session.flush()
                final = _no_symlinks(parent / str(row.run_id))
                _require(not final.exists(), 'E_RESTORE_CONFLICT', 'fresh run directory already exists')
                stage.rename(final)
                final_owned = True
                stage = None
                deployment = install_deployment(app, final, catalog['manifest_sha256'],
                                                final / 'restored-snapshot', registration_id)
                deployment_owned = True
                _require(deployment.get('status') == 'READY' and deployment.get('model_promoted') is False,
                         'E_RESTORE_DEPLOYMENT', 'destination deployment did not verify')
                historical = proof['artifact']
                artifact = StyleRunArtifact(run_id=row.run_id, user_id=user_id, profile_id=None,
                    **{k: historical[k] for k in ('engine_mode', 'engine_id', 'engine_version', 'base_ckpt_sha256',
                       'dataset_snapshot_sha256', 'config_sha256', 'metrics_receipt_sha256', 'relative_path', 'sha256')},
                    quality_status=QUALITY, active=1, created_at=now)
                session.add(artifact)
                session.flush()
                profile = StyleProfile(user_id=user_id, name='Restored personal model · 2026-10-03 AI qualification',
                    source_type='M2_ADAPTER', status='approved', quality_status=QUALITY,
                    coverage=None, model_version=artifact.engine_version,
                    receipt_id='style_run_artifacts/' + str(artifact.artifact_id), enabled=1, created_at=now)
                session.add(profile)
                session.flush()
                artifact.profile_id = profile.style_id
                receipt = {'schema': 'bundled-personal-model-restoration/1', 'status': 'RESTORED',
                    'model_id': catalog['model_id'], 'catalog_sha256': CATALOG_SHA256,
                    'source_archives': {k: v['sha256'] for k, v in catalog['archives'].items()},
                    'manifest_sha256': catalog['manifest_sha256'], 'adapter_sha256': catalog['adapter_sha256'],
                    'original_identity': catalog['original_identity'],
                    'association': {'user_id': user_id, 'run_id': row.run_id, 'profile_id': profile.style_id,
                                    'artifact_id': artifact.artifact_id, 'created_at': now},
                    'historical_quality_status': QUALITY, 'source_qualification': catalog['source_qualification'],
                    'review_qualification': catalog['review_qualification'],
                    'deployment': deployment, 'original_evidence_modified': False,
                    'configuration_bytes_preserved': True, 'new_training_performed': False,
                    'new_model_quality_assessment': False, 'new_human_review': False,
                    'auth_rows_imported': 0, 'sample_rows_imported': 0, 'blind_review_rows_imported': 0}
                raw = (json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
                with (final / 'restore-association.json').open('xb') as target:
                    target.write(raw)
                # The claim is a crash/replay barrier; publication rows commit last.
                claim.write_bytes(raw)
                session.commit()
                committed = True
                return {**receipt, 'run_directory': str(final), 'receipt_path': str(final / 'restore-association.json')}
    finally:
        if not committed:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
            if final is not None and final.exists():
                # Only remove a directory this call actually renamed into place.
                if final_owned:
                    shutil.rmtree(final)
            if deployment_owned and deployment_file is not None:
                deployment_file.unlink(missing_ok=True)
            if claim_owned and claim is not None:
                claim.unlink(missing_ok=True)
