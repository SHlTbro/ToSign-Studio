"""Explicit CPU deployment of immutable trained artifacts to a new installation.

A descriptor is a checked reference, never authority to rewrite training history.
Its trust anchor is the original artifact manifest digest from the registry/package.
Only path relocation is supported: assets, dependencies, numerical identity and
all measured training source must remain exact. GPU/cross-platform equivalence is
not inferred. No models are loaded and no historical file is written here.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import tempfile

from .deployment_trust import SOURCE_CATALOG_SHA256, SOURCE_CATALOG_PUBLIC_RSA_N, SOURCE_CATALOG_PUBLIC_RSA_E
from .runtime_variants import (
    CPU, RuntimeUnavailable, make_binding, require, resolve_binding, safe_relative,
    validate_binding_shape, value_sha, verify_execution_receipt,
)
from .runtime import sha
from engine.style.dataset.binding import verify_snapshot_binding
from engine.style.dataset.schema import DatasetError

SCHEMA = 'm2-cpu-model-deployment/1'
BINDING_SCHEMA = 'm2-model-deployment-binding/1'
SOURCE_CATALOG = 'config/m2-cpu-deployment-source.json'
TRUST_ANCHOR = 'engine/style/m2_adapter/deployment_trust.py'
TRAINING_SOURCE = (
    'engine/style/m2_adapter/runner.py', 'engine/style/m2_adapter/adapter.py',
    'engine/style/m2_adapter/preprocessing.py', 'engine/style/m2_adapter/control_recipe.py',
    'engine/style/m2_adapter/runtime_variants.py', 'engine/style/m2_adapter/environment.py',
    'engine/style/m2_adapter/deterministic_ops.py', 'engine/style/trainer/trainer.py',
    'engine/style/trainer/config.py', 'engine/style/trainer/receipt.py',
)
SNAPSHOT_KEYS = ('authority', 'snapshot_id', 'style_id', 'snapshot_sha256', 'index_sha256', 'provenance_sha256')
EQUIVALENCE_KEYS = ('variant_id', 'device', 'asset_identity_sha256', 'dependency_lock_sha256',
                    'execution_profile_sha256', 'environment_identity_sha256')
DESCRIPTOR_KEYS = {'schema', 'manifest_sha256', 'run_dir', 'adapter_sha256',
                   'training_runtime_binding', 'training_snapshot_binding',
                   'engine_receipt_sha256', 'training_receipt_sha256',
                   'runtime_binding', 'snapshot_binding', 'source_catalog_sha256',
                   'source_sha256', 'equivalence_policy'}


def _digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None,
            'E_DEPLOYMENT_DIGEST', 'full lowercase SHA256 required')
    return value


def regular_path(path, *, directory=False):
    """Reject symlinks in every component before resolving or opening a path."""
    original = Path(path).absolute()
    for part in (original, *original.parents):
        require(not part.is_symlink(), 'E_DEPLOYMENT_PATH', 'symlink in deployment path')
    require(original.is_dir() if directory else original.is_file(),
            'E_DEPLOYMENT_PATH', 'missing deployment ' + ('directory' if directory else 'file'))
    return original.resolve()


def _json(path):
    path = regular_path(path)
    require(path.stat().st_size <= 4000000, 'E_DEPLOYMENT_PROOF', 'oversized JSON proof')
    body = json.loads(path.read_text(encoding='utf-8'))
    require(isinstance(body, dict), 'E_DEPLOYMENT_PROOF', 'JSON object required')
    return body


def source_inventory(app):
    """Exact runtime source set; no descriptor-supplied subset is accepted."""
    root = regular_path(app, directory=True)
    paths = {p.relative_to(root).as_posix() for p in (root / 'engine').rglob('*.py')
             if p.relative_to(root).as_posix() != TRUST_ANCHOR}
    paths.update(('app/domains/render/mixed_fonts.py', 'app/domains/font_presets/service.py', 'app/domains/render/natural_layout_v1.py', 'app/domains/render/natural_authorization.py',
                  'app/domains/render/natural_layout_catalog.json', 'app/domains/render/selection_contract.py', 'app/core/text_style.py', 'app/domains/render/text_layout.py',
                  'app/domains/render/service.py', 'app/domains/templates/service.py',
                  'app/domains/render/custom_ink.py', 'app/domains/render/ink_render_provenance.py',
                  'app/domains/render/ink_color_v1.py',
                  'app/domains/render/glyph_provider.py', 'installer/model_deployment.py',
                  'installer/probe_runtime_variant.py', 'installer/runtime_addon.py', 'installer/runtime_variant.py', 'installer/prepare_runtime_validation.py',
                  'app/domains/styles/model_restore.py', 'installer/restore_personal_model.py',
                  'app/domains/styles/m2_runtime_api.py', 'app/domains/styles/owner_visual_api.py',
                  'config/bundled-model-restore-catalog.json',
                  'config/m2-runtime-catalog.json', 'config/m2-runtime-variants.json',
                  'requirements-personal-style-runtime.lock'))
    if (root/'config/personal-style-runtime-policy.json').is_file():
        paths.update(('config/personal-style-runtime-policy.json','scripts/windows_scr_training.py',
                      'config/windows-gpu-resources.json',
                      'scripts/assemble_personal_runtime_policy.py','app/domains/styles/windows_scr_training_api.py',
                      'app/domains/styles/user_scr_jobs.py','app/domains/styles/windows_scr_product.py',
                      'app/domains/styles/worker.py','app/domains/styles/models.py',
                      'app/domains/styles/service.py','app/domains/styles/router.py',
                      'app/domains/styles/run_recovery.py','app/domains/styles/runstore.py'))
        if (root/'scripts/windows_scr_resume.py').is_file():
            paths.add('scripts/windows_scr_resume.py')
        if (root/'scripts/old0178_windows_inference.py').is_file():
            paths.add('scripts/old0178_windows_inference.py')
    # Historical output and optional ROI recovery are a shipped authority pair.
    # Earlier source trees without this module retain their original inventory.
    if (root/'app/domains/render/historical_cjk_output.py').is_file():
        paths.update(('app/domains/render/historical_cjk_output.py',
                      'app/domains/render/router.py'))
    # Optional stage extensions join the exact signed inventory when shipped.
    for relative in ('app/core/font_shaping.py', 'app/domains/render/v3_layout.py',
                     'app/domains/render/v3_layout_profile.json',
                     'app/domains/styles/imported_inference.py',
                     'installer/restore_unpublished_inference.py',
                     'app/domains/styles/unpublished_models.py',
                     'app/domains/training_sets/candidate_review_jobs.py',
                     'app/domains/training_sets/candidate_review_ingress.py',
                     'app/domains/styles/user_scr_preparation.py',
                     'config/candidate-review-import-policy.json',
                     'app/main.py', 'installer/yy_product_serve.py'):
        if (root/relative).is_file():
            paths.add(relative)
    if (root/'app/domains/render/v3_layout.py').is_file():
        paths.update(p.relative_to(root).as_posix() for p in (root/'fonts/presets').rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts)
    return {rel: sha(regular_path(safe_relative(root, rel))) for rel in sorted(paths)}


def _verify_signed_source_catalog(app, catalog_path):
    """Authenticate raw catalog bytes with the release's immutable code-pinned key."""
    try:
        proof = _json(regular_path(safe_relative(app, SOURCE_CATALOG + '.signature.json')))
    except (OSError, ValueError) as exc:
        raise RuntimeUnavailable('E_DEPLOYMENT_SOURCE_AUTHORITY', 'missing or invalid source release signature') from exc
    require(set(proof) == {'schema', 'algorithm', 'catalog_sha256', 'signature_base64'}
            and proof['schema'] == 'yy-source-catalog-signature/1'
            and proof['algorithm'] == 'RSA3072-PKCS1-SHA256',
            'E_DEPLOYMENT_SOURCE_AUTHORITY', 'unsupported source signature')
    require(proof['catalog_sha256'] == sha(catalog_path),
            'E_DEPLOYMENT_SOURCE_AUTHORITY', 'signed catalog digest mismatch')
    try:
        require(isinstance(proof['signature_base64'], str) and len(proof['signature_base64']) == 512,
                'E_DEPLOYMENT_SOURCE_AUTHORITY', 'invalid signature length')
        signature = base64.b64decode(proof['signature_base64'], validate=True)
        size = (SOURCE_CATALOG_PUBLIC_RSA_N.bit_length() + 7) // 8
        value = int.from_bytes(signature, 'big')
        require(size == 384 and len(signature) == size and 0 < value < SOURCE_CATALOG_PUBLIC_RSA_N,
                'E_DEPLOYMENT_SOURCE_AUTHORITY', 'invalid signature encoding')
        message = b'YY_DEPLOYMENT_SOURCE_CATALOG_V1\x00' + bytes.fromhex(proof['catalog_sha256'])
        encoded = pow(value, SOURCE_CATALOG_PUBLIC_RSA_E, SOURCE_CATALOG_PUBLIC_RSA_N).to_bytes(size, 'big')
        digest_info = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(message).digest()
        expected = b'\x00\x01' + b'\xff' * (size - len(digest_info) - 3) + b'\x00' + digest_info
        require(hmac.compare_digest(encoded, expected),
                'E_DEPLOYMENT_SOURCE_AUTHORITY', 'source release signature mismatch')
    except (ValueError, TypeError, OverflowError) as exc:
        raise RuntimeUnavailable('E_DEPLOYMENT_SOURCE_AUTHORITY', 'invalid source signature') from exc


def verify_source_catalog(app):
    path = regular_path(safe_relative(app, SOURCE_CATALOG))
    policy = _json(path)
    signed_release = sha(path) != SOURCE_CATALOG_SHA256
    if signed_release:
        _verify_signed_source_catalog(app, path)
    require(set(policy) == {'schema', 'files'} and policy['schema'] == 'm2-cpu-deployment-source/1',
            'E_DEPLOYMENT_SOURCE', 'unsupported product source catalog')
    actual = source_inventory(app)
    require(policy['files'] == actual, 'E_DEPLOYMENT_SOURCE',
            'product source differs from released deployment catalog')
    if signed_release:
        from .runtime_variants import catalog
        from .release_runtime_policy import verify_disabled_source_closure
        # Authenticated current source bytes and disabled historical execution
        # pins serve different purposes. This release grants no M2 execution.
        verify_disabled_source_closure(app,catalog(app))
    return sha(path), actual


def verify_training_proof(app, run_dir, manifest_sha256):
    """Follow the registry-anchored manifest -> wrapper -> training receipt chain."""
    run = regular_path(run_dir, directory=True)
    manifest_path = regular_path(safe_relative(run, 'artifact/artifact.json'))
    require(sha(manifest_path) == _digest(manifest_sha256), 'E_DEPLOYMENT_MANIFEST',
            'original artifact manifest differs from trusted registry/package digest')
    manifest = _json(manifest_path)
    require(manifest.get('engine_mode') == 'M2_ADAPTER', 'E_DEPLOYMENT_PROOF', 'trained M2 artifact required')
    original = validate_binding_shape(manifest.get('runtime_binding'))
    require(original['variant_id'] == CPU and original['device'] == 'cpu',
            'E_DEPLOYMENT_CPU_ONLY', 'only the exact CPU training environment is portable')
    files = manifest.get('files')
    require(isinstance(files, list) and files, 'E_DEPLOYMENT_PROOF', 'bound artifact inventory missing')
    indexed = {}
    for entry in files:
        require(isinstance(entry, dict), 'E_DEPLOYMENT_PROOF', 'invalid artifact entry')
        rel = entry.get('relative_path')
        path = regular_path(safe_relative(run, rel))
        require(rel not in indexed and sha(path) == _digest(entry.get('sha256')),
                'E_DEPLOYMENT_PROOF', 'artifact inventory duplicate or hash mismatch')
        indexed[rel] = entry['sha256']
    adapter_sha = indexed.get('artifact/m2_adapter.pt')
    require(adapter_sha is not None, 'E_DEPLOYMENT_PROOF', 'trained adapter absent')
    receipt_path = regular_path(safe_relative(run, manifest['metrics_receipt_relative_path']))
    require(sha(receipt_path) == _digest(manifest['metrics_receipt_sha256']),
            'E_DEPLOYMENT_PROOF', 'engine receipt hash mismatch')
    wrapper = _json(receipt_path)
    anchor = wrapper.get('training_receipt')
    require(isinstance(anchor, dict), 'E_DEPLOYMENT_PROOF', 'anchored training receipt required')
    training_path = regular_path(safe_relative(run, anchor.get('relative_path')))
    require(sha(training_path) == _digest(anchor.get('sha256')),
            'E_DEPLOYMENT_PROOF', 'training receipt hash mismatch')
    training = _json(training_path)
    require(isinstance(training.get('process'), dict), 'E_DEPLOYMENT_PROOF', 'measured training process required')
    verify_execution_receipt(original, training, 'training')
    numerical = training.get('training_environment')
    require(isinstance(numerical, dict) and numerical.get('schema') == 'r9-numerical-environment/2'
            and numerical.get('device') == 'cpu'
            and numerical.get('identity_sha256') == value_sha({k: v for k, v in numerical.items() if k != 'identity_sha256'}),
            'E_DEPLOYMENT_PROOF', 'original full CPU numerical proof is missing or malformed')
    for record in (wrapper, training):
        require(record.get('ok') is True and record.get('engine_mode') == 'M2_ADAPTER',
                'E_DEPLOYMENT_PROOF', 'successful actual M2 training proof required')
        for key in ('runtime_binding', 'snapshot_binding', 'dataset_snapshot_sha256',
                    'runtime_identity_sha256', 'engine_id', 'preprocessing',
                    'training_objective', 'training_recipe', 'training_budget'):
            require(record.get(key) == manifest.get(key) and record.get(key) is not None,
                    'E_DEPLOYMENT_PROOF', 'training lineage differs: ' + key)
    require(training.get('base_ckpt_sha256') == manifest.get('base_ckpt_sha256')
            and training.get('model_version') == manifest.get('engine_version'),
            'E_DEPLOYMENT_PROOF', 'base or adapter version differs from training')
    output_record = training.get('output')
    require(isinstance(output_record, dict) and isinstance(output_record.get('files'), list)
            and all(isinstance(r, dict) for r in output_record['files']),
            'E_DEPLOYMENT_PROOF', 'actual training output inventory required')
    output = {r.get('relative_path'): r.get('sha256') for r in output_record['files']}
    require(output.get('artifact/m2_adapter.pt') == adapter_sha, 'E_DEPLOYMENT_PROOF',
            'adapter was not produced by the anchored training receipt')
    measured = (training.get('process') or {}).get('initial_training_code_sha256')
    require(isinstance(measured, dict) and set(measured) == set(TRAINING_SOURCE),
            'E_DEPLOYMENT_SOURCE', 'complete measured training source inventory required')
    for rel, expected in measured.items():
        require(sha(regular_path(safe_relative(app, rel))) == _digest(expected),
                'E_DEPLOYMENT_SOURCE', 'measured training source changed: ' + rel)
    return {'manifest': manifest, 'training': training, 'adapter_sha256': adapter_sha,
            'engine_receipt_sha256': sha(receipt_path), 'training_receipt_sha256': sha(training_path),
            'run_dir': str(run)}


def verify_snapshot_relocation(snapshot_dir, original):
    root = regular_path(snapshot_dir, directory=True)
    require(not any(p.is_symlink() for p in root.rglob('*')), 'E_DEPLOYMENT_PATH',
            'symlink in relocated snapshot')
    try:
        binding = verify_snapshot_binding(root, original['snapshot_sha256'])
    except DatasetError as exc:
        raise RuntimeUnavailable('E_DEPLOYMENT_SNAPSHOT', str(exc)) from exc
    require(all(binding.get(k) == original.get(k) and binding.get(k) is not None for k in SNAPSHOT_KEYS),
            'E_DEPLOYMENT_SNAPSHOT', 'relocated snapshot is not the exact original training snapshot')
    return binding


def descriptor_path(app, manifest_sha256):
    regular_path(app, directory=True)
    return safe_relative(app, 'runtime/model-deployments/' + _digest(manifest_sha256) + '/deployment.json')


def deployment_reference(descriptor):
    return {'schema': BINDING_SCHEMA, 'manifest_sha256': descriptor['manifest_sha256'],
            'deployment_sha256': value_sha(descriptor)}


def _verify_descriptor(app, descriptor, *, expected_run_dir=None):
    require(set(descriptor) == DESCRIPTOR_KEYS and descriptor.get('schema') == SCHEMA,
            'E_DEPLOYMENT_DESCRIPTOR', 'unsupported or incomplete deployment descriptor')
    require(descriptor['equivalence_policy'] == 'exact-cpu-numerics-assets-training-source-v1',
            'E_DEPLOYMENT_DESCRIPTOR', 'unsupported equivalence policy')
    run = regular_path(descriptor['run_dir'], directory=True)
    if expected_run_dir is not None:
        require(run == regular_path(expected_run_dir, directory=True), 'E_DEPLOYMENT_PATH',
                'deployment refers to a different registry run')
    proof = verify_training_proof(app, run, descriptor['manifest_sha256'])
    original = proof['manifest']['runtime_binding']
    require(descriptor['training_runtime_binding'] == original
            and descriptor['training_snapshot_binding'] == proof['manifest']['snapshot_binding']
            and all(descriptor[k] == proof[k] for k in ('adapter_sha256', 'engine_receipt_sha256', 'training_receipt_sha256')),
            'E_DEPLOYMENT_PROOF', 'descriptor changed original training identity')
    current = validate_binding_shape(descriptor['runtime_binding'])
    require(current['variant_id'] == CPU and current['device'] == 'cpu',
            'E_DEPLOYMENT_CPU_ONLY', 'destination must be exact CPU runtime')
    require(all(current[k] == original[k] for k in EQUIVALENCE_KEYS),
            'E_DEPLOYMENT_EQUIVALENCE', 'destination assets, lock, execution or numerical identity differs')
    selected = resolve_binding(app, current)  # fresh interpreter, asset and numerical probe
    require(selected['manifest']['base_npz_sha256'] == proof['manifest']['base_ckpt_sha256'],
            'E_DEPLOYMENT_EQUIVALENCE', 'base asset hashes differ')
    snapshot = verify_snapshot_relocation(descriptor['snapshot_binding']['snapshot_dir'],
                                         proof['manifest']['snapshot_binding'])
    require(snapshot == descriptor['snapshot_binding'], 'E_DEPLOYMENT_SNAPSHOT', 'destination snapshot descriptor changed')
    source_sha, inventory = verify_source_catalog(app)
    require(source_sha == descriptor['source_catalog_sha256'] and inventory == descriptor['source_sha256'],
            'E_DEPLOYMENT_SOURCE', 'deployment source is stale')
    return {**proof, 'descriptor': descriptor, 'deployment_binding': deployment_reference(descriptor),
            'runtime': selected, 'runtime_binding': current, 'snapshot_binding': snapshot}


def resolve_deployment(app, binding, *, expected_run_dir=None, expected_manifest_sha256=None):
    from .windows_scr_deployment import OLD0178_MANIFEST_SHA,is_windows,resolve_deployment as resolve_windows
    if is_windows(binding) and binding.get('manifest_sha256')==OLD0178_MANIFEST_SHA:
        return resolve_windows(app,binding,expected_run_dir=expected_run_dir,expected_manifest_sha256=expected_manifest_sha256)
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .windows_scr_deployment import is_windows, resolve_deployment as resolve_windows
    if is_windows(binding):
        return resolve_windows(app,binding,expected_run_dir=expected_run_dir,expected_manifest_sha256=expected_manifest_sha256)
    require(isinstance(binding, dict) and set(binding) == {'schema', 'manifest_sha256', 'deployment_sha256'}
            and binding.get('schema') == BINDING_SCHEMA, 'E_DEPLOYMENT_BINDING', 'trusted deployment reference required')
    _digest(binding['deployment_sha256'])
    if expected_manifest_sha256 is not None:
        require(binding['manifest_sha256'] == expected_manifest_sha256, 'E_DEPLOYMENT_MANIFEST',
                'deployment differs from registry artifact')
    descriptor = _json(descriptor_path(app, binding['manifest_sha256']))
    require(deployment_reference(descriptor) == binding, 'E_DEPLOYMENT_BINDING', 'deployment descriptor changed')
    return _verify_descriptor(app, descriptor, expected_run_dir=expected_run_dir)


def lookup_deployment(app, run_dir, manifest_sha256):
    """The caller must supply the already checked DB manifest digest, never an env override."""
    from .windows_scr_deployment import lookup_deployment as lookup_windows
    windows=lookup_windows(app,run_dir,manifest_sha256)
    path = descriptor_path(app, manifest_sha256)
    if windows is not None:
        require(not path.exists(),'E_DEPLOYMENT_AMBIGUOUS','both legacy and Windows deployments selected')
        return windows
    if not path.exists():
        return None
    descriptor = _json(path)
    return resolve_deployment(app, deployment_reference(descriptor), expected_run_dir=run_dir,
                              expected_manifest_sha256=manifest_sha256)


def _publish_descriptor(path, descriptor):
    """Publish complete bytes atomically without replacing an existing descriptor."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.deployment-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(descriptor, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard-link publication is atomic and refuses an existing
        # destination on NTFS and POSIX. rename/replace could clobber evidence.
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            require(_json(path) == descriptor, 'E_DEPLOYMENT_EXISTS', 'concurrent deployment differs')
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A locked disposable temp must neither mask the original
                # failure nor turn successful publication into a false failure.
                pass


def install_deployment(app, run_dir, manifest_sha256, snapshot_dir, registration_id):
    """Explicit installer action; creates one immutable descriptor, no promotion."""
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(app)
    from .runtime_variants import inspect_registration
    proof = verify_training_proof(app, run_dir, manifest_sha256)
    selected = inspect_registration(app, registration_id)
    current = make_binding(selected)
    source_sha, inventory = verify_source_catalog(app)
    descriptor = {'schema': SCHEMA, 'manifest_sha256': manifest_sha256, 'run_dir': proof['run_dir'],
                  'adapter_sha256': proof['adapter_sha256'],
                  'training_runtime_binding': proof['manifest']['runtime_binding'],
                  'training_snapshot_binding': proof['manifest']['snapshot_binding'],
                  'engine_receipt_sha256': proof['engine_receipt_sha256'],
                  'training_receipt_sha256': proof['training_receipt_sha256'],
                  'runtime_binding': current,
                  'snapshot_binding': verify_snapshot_relocation(snapshot_dir, proof['manifest']['snapshot_binding']),
                  'source_catalog_sha256': source_sha, 'source_sha256': inventory,
                  'equivalence_policy': 'exact-cpu-numerics-assets-training-source-v1'}
    result = _verify_descriptor(app, descriptor, expected_run_dir=run_dir)
    path = descriptor_path(app, manifest_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(_json(path) == descriptor, 'E_DEPLOYMENT_EXISTS', 'preserve existing deployment; use a fresh installation')
    else:
        _publish_descriptor(path, descriptor)
    return {'status': 'READY', 'deployment_binding': result['deployment_binding'],
            'descriptor_path': str(path), 'runtime_binding': current,
            'training_runtime_binding': descriptor['training_runtime_binding'],
            'original_evidence_modified': False, 'model_promoted': False,
            'scope': 'exact existing CPU numerical environment only; no Windows/GPU equivalence'}


def verify_deployment_request(resolved, *, adapter_path, adapter_sha256, snapshot_dir,
                              snapshot_sha256, runtime_binding):
    from .windows_scr_deployment import is_windows,verify_request
    if is_windows(resolved.get('deployment_binding')):
        return verify_request(resolved,adapter_path=adapter_path,adapter_sha256=adapter_sha256,
            snapshot_dir=snapshot_dir,snapshot_sha256=snapshot_sha256,runtime_binding=runtime_binding)
    descriptor = resolved['descriptor']
    require(regular_path(adapter_path) == regular_path(Path(resolved['run_dir']) / 'artifact/m2_adapter.pt')
            and adapter_sha256 == descriptor['adapter_sha256']
            and regular_path(snapshot_dir, directory=True) == Path(resolved['snapshot_binding']['snapshot_dir'])
            and snapshot_sha256 == resolved['snapshot_binding']['snapshot_sha256']
            and runtime_binding == resolved['runtime_binding'],
            'E_DEPLOYMENT_REQUEST', 'inference request differs from verified deployment')


def verify_deployment_metadata(resolved, meta):
    from .windows_scr_deployment import is_windows,verify_metadata
    if is_windows(resolved.get('deployment_binding')):
        verify_metadata(resolved['source'],meta)
        require(value_sha(meta)==resolved['descriptor']['compatibility_receipt']['original_metadata_sha256'],
                'E_DEPLOYMENT_METADATA','original adapter metadata digest differs')
        return
    training = resolved['training']
    require(meta.get('runtime_binding') == resolved['descriptor']['training_runtime_binding']
            and meta.get('runtime_environment') == training['runtime_environment']
            and meta.get('training_environment') == training['training_environment'],
            'E_DEPLOYMENT_METADATA', 'adapter training identity must remain original')


def deployment_receipt_fields(resolved):
    from .windows_scr_deployment import is_windows,receipt_fields
    if is_windows(resolved.get('deployment_binding')):return receipt_fields(resolved)
    return {'deployment_binding': resolved['deployment_binding'],
            'training_runtime_binding': resolved['descriptor']['training_runtime_binding'],
            'training_snapshot_binding': resolved['descriptor']['training_snapshot_binding'],
            'deployment_source_catalog_sha256': resolved['descriptor']['source_catalog_sha256']}


def verify_deployment_receipt(resolved, receipt):
    from .windows_scr_deployment import is_windows,verify_receipt
    if is_windows(resolved.get('deployment_binding')):return verify_receipt(resolved,receipt)
    require(all(receipt.get(k) == v for k, v in deployment_receipt_fields(resolved).items())
            and receipt.get('snapshot_binding') == resolved['snapshot_binding']
            and receipt.get('training_environment') == resolved['training']['training_environment']
            and receipt.get('inference_environment') == resolved['training']['training_environment'],
            'E_DEPLOYMENT_RECEIPT', 'inference receipt missing or changed deployment provenance')
    verify_execution_receipt(resolved['runtime_binding'], receipt, 'inference')
    return True
