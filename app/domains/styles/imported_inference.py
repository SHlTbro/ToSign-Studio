"""Account-owned inference selection for the exact unpublished 0178 import.

This independent registry never creates a training run, published artifact,
model quality approval, or runtime qualification. Each consumption rechecks
the original import bytes and the final application's actual deployment gate.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import Integer, Text, inspect, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.audit import audit
from app.core.db import Base
from app.core.errors import ApiError
from app.domains.auth.models import User
from app.domains.styles.models import StyleProfile
from engine.style.m2_adapter import windows_scr_deployment as D
from engine.style.m2_adapter.trainer_resume_protocol import native_path
from installer.unpublished_artifacts import unpublished_artifact_import as I

APP = Path(__file__).resolve().parents[3]
PREFIX = 'imported_inference/'
QUALITY = 'V3_LIMITED_USER_ACCEPTED'
SCHEMA = 'account-imported-inference-selection/1'
ACCEPTANCE_SCOPE = {
    'id': 'user-accepted-0178-v3-20261007-r2',
    'text_lines': ['To：shitT', '拉shit永远通畅'],
    'latin_font_sha256': '0bdb6b660482d31531b3945849fba5916b3ef8695da7024a9e6b9ee3c4157988',
    'latin_weight': 600,
    'punctuation_font_sha256': 'b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a',
    'punctuation_face_index': 2,
    'color': '#8C3044', 'layout_seed': 2026100401,
    'model_quality_approval': False, 'publication_approval': False,
}


class ImportedInferenceSelection(Base):
    __tablename__ = 'imported_inference_selections'
    import_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    binding_json: Mapped[str] = mapped_column(Text, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


def _require(ok, detail, *, missing=False):
    if not ok:
        raise ApiError(404 if missing else 409,
                       'ARTIFACT_NOT_BOUND' if missing else 'IMPORTED_INFERENCE_BLOCKED', detail)


def _digest(value):
    return hashlib.sha256(I.canonical(value)).hexdigest()


def _import(session, user_id, import_id, import_root):
    _require(type(user_id) is int and user_id > 0 and isinstance(import_id, str)
             and re.fullmatch('[0-9a-f]{32}', import_id), 'Invalid import selection identity')
    account = session.get(User, user_id)
    _require(account is not None and account.enabled == 1, 'Enabled account required', missing=True)
    _require(inspect(session.connection()).has_table(I.TABLE), 'Import registry is not installed', missing=True)
    row = session.execute(text(f'SELECT * FROM {I.TABLE} WHERE import_id=:iid AND user_id=:uid'),
                          {'iid': import_id, 'uid': user_id}).mappings().first()
    _require(row is not None, 'Owned unpublished import does not resolve', missing=True)
    _require(row['status'] == 'unpublished' and row['adapter_sha256'] == D.OLD0178_ADAPTER_SHA
             and row['manifest_sha256'] == D.OLD0178_MANIFEST_SHA, 'Only the exact original 0178 import is supported')
    root = I.regular(import_root, directory=True, e_only=True)
    expected = D.OLD0178_ADAPTER_SHA + '-u' + str(user_id)
    _require(row['relative_directory'] == expected, 'Import directory identity differs')
    destination = I.regular(root / expected, directory=True, e_only=True)
    receipt_path = I.regular(destination / 'import-receipt.json', e_only=True)
    _require(I.sha_file(receipt_path) == row['receipt_sha256'], 'Original import receipt bytes changed')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    _require(receipt.get('schema') == 'personal-unpublished-artifact-import/1'
             and receipt.get('import_id') == import_id and receipt.get('user_id') == user_id
             and receipt.get('adapter_sha256') == row['adapter_sha256']
             and receipt.get('manifest', {}).get('sha256') == row['manifest_sha256']
             and receipt.get('input_fingerprint_sha256') == row['input_fingerprint_sha256']
             and receipt.get('publication_status') == 'UNPUBLISHED'
             and receipt.get('model_quality_approval') is False
             and receipt.get('new_training_performed') is False
             and receipt.get('published_registry_rows_written') == 0,
             'Original import receipt identity or scope changed')
    for role, specification in I.PACKAGES.items():
        recorded = receipt.get('packages', {}).get(role, {})
        _require(all(recorded.get(k) == specification[k] for k in ('filename', 'bytes', 'sha256')),
                 'Original package declaration changed: ' + role)
        package = I.regular(destination / 'packages' / specification['filename'], e_only=True)
        _require(native_path(package).stat().st_size == specification['bytes'] and I.sha_file(package) == specification['sha256'],
                 'Original package bytes changed: ' + role)
    for name, digest in (('m2_adapter.pt', row['adapter_sha256']), ('artifact.json', row['manifest_sha256'])):
        _require(I.sha_file(I.regular(destination / 'artifact' / name, e_only=True)) == digest,
                 'Original model bytes changed: ' + name)
    return dict(row), root


def _deployment(row, deployment_binding, *, observe_environment=True):
    resolved = D.resolve_deployment(APP, deployment_binding,
                                    expected_manifest_sha256=row['manifest_sha256'], purpose='inference',
                                    observe=observe_environment)
    source = resolved['source']
    _require(resolved['deployment_binding'] == deployment_binding
             and source['artifact_manifest_sha256'] == row['manifest_sha256']
             and source['adapter_sha256'] == row['adapter_sha256']
             and source['sha256'] == deployment_binding.get('source_sha256')
             and resolved.get('may_train') is False and resolved.get('may_publish') is False,
             'Qualified deployment is not bound to this original inference-only model')
    for field, expected in (('artifact_manifest_path', row['manifest_sha256']), ('adapter_path', row['adapter_sha256'])):
        _require(I.sha_file(I.regular(source[field], e_only=True)) == expected,
                 'Qualified deployment source bytes changed')
    manifest = json.loads(Path(source['artifact_manifest_path']).read_text(encoding='utf-8'))
    _require(manifest.get('engine_mode') == 'M2_ADAPTER'
             and manifest.get('engine_version') == 'adapter@' + row['adapter_sha256'][:16],
             'Original model manifest identity differs')
    return resolved, manifest


def _checked(session, user_id, import_id, import_root, deployment_binding, *, observe_environment=True):
    try:
        row, root = _import(session, user_id, import_id, import_root)
        deployment, manifest = _deployment(row, deployment_binding, observe_environment=observe_environment)
        return row, root, deployment, manifest
    except ApiError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ApiError(409, 'IMPORTED_INFERENCE_BLOCKED', 'Imported inference evidence or qualification is unavailable') from exc


def associate(session, user_id, import_id, import_root, deployment_binding, *, name='0178 · 已认可 V3'):
    """Trusted maintenance transaction, after backup/stop; no runtime approval."""
    row, root, deployment, _ = _checked(session, user_id, import_id, import_root, deployment_binding)
    _require(inspect(session.connection()).has_table(ImportedInferenceSelection.__tablename__),
             'Inference selection registry must be installed by the maintenance CLI')
    existing = session.get(ImportedInferenceSelection, import_id)
    if existing is not None:
        _require(existing.user_id == user_id, 'Import association belongs to another account', missing=True)
        profile = session.get(StyleProfile, existing.profile_id)
        result = resolve(session, user_id, profile)
        _require(result['deployment_binding'] == deployment_binding
                 and json.loads(existing.binding_json)['import_root'] == str(root),
                 'Existing inference selection differs; preserve it')
        return profile
    now = datetime.now(timezone.utc).isoformat()
    profile = StyleProfile(user_id=user_id, name=name, source_type='M2_ADAPTER', status='approved',
                           quality_status=QUALITY, model_version='adapter@' + row['adapter_sha256'][:16],
                           receipt_id=PREFIX + import_id, enabled=1, created_at=now)
    session.add(profile); session.flush()
    record = {'schema': SCHEMA, 'user_id': user_id, 'profile_id': profile.style_id, 'import_id': import_id,
              'application_namespace': str(APP.resolve()),
              'import_root': str(root), 'import_receipt_sha256': row['receipt_sha256'],
              'input_fingerprint_sha256': row['input_fingerprint_sha256'],
              'package_sha256': {role:spec['sha256'] for role,spec in I.PACKAGES.items()},
              'adapter_sha256': row['adapter_sha256'], 'manifest_sha256': row['manifest_sha256'],
              'source_sha256': deployment['source']['sha256'], 'deployment_binding': copy.deepcopy(deployment_binding),
              'acceptance_scope': copy.deepcopy(ACCEPTANCE_SCOPE), 'quality_status': QUALITY,
              'inference_only': True, 'model_quality_approval': False, 'publish_ready': False,
              'training_performed': False, 'created_at': now}
    session.add(ImportedInferenceSelection(import_id=import_id, user_id=user_id, profile_id=profile.style_id,
                binding_json=I.canonical(record).decode('utf-8'), binding_sha256=_digest(record), created_at=now))
    audit(session, action='associate_unpublished_inference', actor='maintenance:account:' + str(user_id),
          object_ref=PREFIX + import_id, result=json.dumps({'profile_id':profile.style_id,
          'binding_sha256':_digest(record), 'publication_status':'UNPUBLISHED', 'quality_status':QUALITY}))
    session.flush()
    return profile


def rebind(session, user_id, import_id, import_root, deployment_binding, *, expected_binding_sha256):
    """Stopped maintenance upgrade to a newly qualified final namespace.

    The original import and profile remain intact. The operator must name the
    exact existing record backed up by the CLI; a current deployment is still
    independently resolved before any registry write.
    """
    _require(isinstance(expected_binding_sha256,str)
             and re.fullmatch('[0-9a-f]{64}',expected_binding_sha256), 'Exact previous selection digest required')
    row,root,deployment,_=_checked(session,user_id,import_id,import_root,deployment_binding)
    _require(inspect(session.connection()).has_table(ImportedInferenceSelection.__tablename__),
             'Existing inference selection required',missing=True)
    selected=session.get(ImportedInferenceSelection,import_id)
    _require(selected is not None and selected.user_id==user_id
             and selected.binding_sha256==expected_binding_sha256,'Previous selection changed; preserve it')
    profile=session.get(StyleProfile,selected.profile_id)
    _require(profile is not None and profile.user_id==user_id and profile.status=='approved'
             and profile.enabled==1 and profile.source_type=='M2_ADAPTER'
             and profile.quality_status==QUALITY and profile.receipt_id==PREFIX+import_id
             and profile.model_version=='adapter@'+row['adapter_sha256'][:16],
             'Existing owned profile or acceptance scope changed')
    try:
        previous=json.loads(selected.binding_json)
        _require(_digest(previous)==expected_binding_sha256 and previous['schema']==SCHEMA
                 and previous['import_id']==import_id and previous['user_id']==user_id
                 and previous['profile_id']==profile.style_id and previous['import_root']==str(root)
                 and previous['package_sha256']=={role:spec['sha256'] for role,spec in I.PACKAGES.items()}
                 and previous['acceptance_scope']==ACCEPTANCE_SCOPE and previous['quality_status']==QUALITY
                 and previous['inference_only'] is True and previous['model_quality_approval'] is False
                 and previous['publish_ready'] is False and previous['training_performed'] is False
                 and all(previous[k]==row[v] for k,v in (('adapter_sha256','adapter_sha256'),
                     ('manifest_sha256','manifest_sha256'),('import_receipt_sha256','receipt_sha256'),
                     ('input_fingerprint_sha256','input_fingerprint_sha256'))),
                 'Previous selection evidence changed; preserve it')
    except (ValueError,KeyError,TypeError) as exc:
        raise ApiError(409,'IMPORTED_INFERENCE_BLOCKED','Previous selection evidence unavailable') from exc
    current=copy.deepcopy(previous)
    current.update(application_namespace=str(APP.resolve()),source_sha256=deployment['source']['sha256'],
                   deployment_binding=copy.deepcopy(deployment_binding))
    selected.binding_json=I.canonical(current).decode('utf-8');selected.binding_sha256=_digest(current)
    audit(session,action='rebind_unpublished_inference',actor='maintenance:account:'+str(user_id),
          object_ref=PREFIX+import_id,result=json.dumps({'profile_id':profile.style_id,
          'previous_binding_sha256':expected_binding_sha256,'binding_sha256':selected.binding_sha256,
          'previous_application_namespace':previous['application_namespace'],
          'application_namespace':current['application_namespace'],
          'previous_deployment_binding':previous['deployment_binding'],
          'publication_status':'UNPUBLISHED','quality_status':QUALITY}))
    session.flush()
    return profile


def resolve(session, user_id, profile, *, observe_environment=True):
    """Read-only, raising gate consumed by render; never trust a profile label."""
    _require(profile is not None and profile.user_id == user_id and profile.style_id is not None,
             'Owned inference profile does not resolve', missing=True)
    owned = session.query(StyleProfile).filter_by(style_id=profile.style_id, user_id=user_id).first()
    _require(owned is profile and profile.status == 'approved' and profile.enabled == 1
             and profile.source_type == 'M2_ADAPTER' and profile.quality_status == QUALITY
             and isinstance(profile.receipt_id, str) and profile.receipt_id.startswith(PREFIX),
             'Imported profile is disabled, unapproved or has changed scope')
    import_id = profile.receipt_id[len(PREFIX):]
    _require(re.fullmatch('[0-9a-f]{32}', import_id) is not None, 'Malformed inference receipt identity')
    _require(inspect(session.connection()).has_table(ImportedInferenceSelection.__tablename__),
             'Inference selection is not installed', missing=True)
    association = session.get(ImportedInferenceSelection, import_id)
    _require(association is not None and association.user_id == user_id
             and association.profile_id == profile.style_id, 'Inference association does not resolve', missing=True)
    try:
        record = json.loads(association.binding_json)
        _require(_digest(record) == association.binding_sha256
                 and record['schema'] == SCHEMA and record['import_id'] == import_id
                 and record['user_id'] == user_id and record['profile_id'] == profile.style_id
                 and record['application_namespace'] == str(APP.resolve())
                 and record['package_sha256'] == {role:spec['sha256'] for role,spec in I.PACKAGES.items()}
                 and record['acceptance_scope'] == ACCEPTANCE_SCOPE and record['quality_status'] == QUALITY
                 and record['inference_only'] is True and record['model_quality_approval'] is False
                 and record['publish_ready'] is False and record['training_performed'] is False,
                 'Inference association evidence or scope changed')
        row, _, deployment, manifest = _checked(
            session, user_id, import_id, record['import_root'], record['deployment_binding'],
            observe_environment=observe_environment)
        _require(all(record[k] == row[v] for k,v in (('adapter_sha256','adapter_sha256'),
                 ('manifest_sha256','manifest_sha256'),('import_receipt_sha256','receipt_sha256'),
                 ('input_fingerprint_sha256','input_fingerprint_sha256')))
                 and profile.model_version == 'adapter@' + row['adapter_sha256'][:16]
                 and record['source_sha256'] == deployment['source']['sha256'], 'Original import association changed')
    except ApiError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ApiError(409, 'IMPORTED_INFERENCE_BLOCKED', 'Inference selection evidence changed') from exc
    return {'state':'bound', 'style_id':profile.style_id,
            # Compatibility cache/seed key, not a StyleRunArtifact identity.
            'artifact_id':-profile.style_id, 'artifact_registry_kind':'imported_inference',
            'inference_selection_id':profile.style_id, 'run_id':None, 'import_id':import_id,
            'engine_mode':'M2_ADAPTER', 'engine_id':manifest['engine_id'], 'engine_version':manifest['engine_version'],
            'manifest_sha256':row['manifest_sha256'], 'base_dir':deployment['run_dir'], 'chars':{}, 'style_refs':[], 'problems':[],
            'm2_adapter':{'path':deployment['source']['adapter_path'], 'sha256':row['adapter_sha256']},
            'snapshot_binding':deployment['snapshot_binding'], 'runtime_binding':deployment['runtime_binding'],
            'deployment_binding':deployment['deployment_binding'], 'deployment_version':deployment['deployment_version'],
            # Server-produced after the complete current deployment gate. This
            # is request-local evidence for capability/font reads, never an
            # execution permission or a caller-supplied font override.
            'registered_content_font':{
                'path':str(Path(deployment['execution']['fd_repo'])/'ttf/KaiXinSongA.ttf'),
                'sha256':deployment['descriptor']['compatibility_receipt']['content_font_sha256'],
                'asset_identity_sha256':deployment['execution']['asset_identity_sha256'],
                'runtime_binding_sha256':deployment['runtime_binding']['binding_sha256'],
                'deployment_sha256':deployment['deployment_binding']['deployment_sha256']},
            'training_runtime_binding':manifest.get('runtime_binding'), 'training_snapshot_binding':manifest.get('snapshot_binding'),
            **{k:manifest.get(k) for k in ('base_ckpt_sha256','runtime_manifest_path','runtime_identity_sha256','preprocessing',
               'training_objective','training_recipe','training_budget')},
            **{k:manifest[k] for k in D.IDENTITY_FIELDS if k in manifest},
            'inference_only':True, 'publication_status':'UNPUBLISHED', 'quality_status':QUALITY,
            'model_quality_approval':False, 'publish_ready':False, 'acceptance_scope':copy.deepcopy(ACCEPTANCE_SCOPE)}


def public_state(session, user_id, import_id):
    """Account list augmentation; absent registry never creates one."""
    if not inspect(session.connection()).has_table(ImportedInferenceSelection.__tablename__):
        return {}
    association = session.get(ImportedInferenceSelection, import_id)
    if association is None or association.user_id != user_id:
        return {}
    profile = session.get(StyleProfile, association.profile_id)
    try:
        binding = resolve(session, user_id, profile)
    except (ApiError, OSError, ValueError, KeyError, TypeError):
        return {'runtime_status':'SELECTED_BLOCKED', 'inference_ready':False,
                'inference_selection_id':association.profile_id,
                'verification_scope':'CURRENT_SELECTION_RECHECK_BLOCKED'}
    return {'runtime_status':'INFERENCE_READY', 'inference_ready':True,
            'inference_selection_id':profile.style_id, 'quality_status':QUALITY,
            'verification_scope':'CURRENT_OWNED_QUALIFIED_V3_SELECTION',
            'inference_only':True, 'acceptance_scope':binding['acceptance_scope']}
