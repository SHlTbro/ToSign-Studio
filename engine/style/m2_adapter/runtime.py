"""Verified runtime-addon discovery and explicit regenerated-NPZ lineage."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

PROJ=Path(__file__).resolve().parents[3]
NAMES=('unet.npz','style_encoder.npz','content_encoder.npz')


def sha(path):
    import sys
    policy=sys.modules.get('engine.style.m2_adapter.personal_runtime_policy')
    if policy is not None:
        locked=policy.operation_digest(path)
        if locked is not None:return locked
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1<<20),b''):h.update(part)
    return h.hexdigest()


def identity(manifest):
    body={'engine_id':manifest['engine_id'],'upstream_revision':manifest['upstream_revision'],
          'base_npz_sha256':manifest['base_npz_sha256'],
          'tensor_digests':{k:manifest['base_assets'][k]['tensor_state_digest'] for k in NAMES},
          'source_pth_sha256':{k:manifest['base_assets'][k]['upstream_pth']['sha256'] for k in NAMES},
          'content_font_sha256':manifest['content_font']['sha256'],
          'code_files':{r['path']:r['sha256'] for r in manifest['code_assets']}}
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def manifest_location():
    return Path(os.environ.get('YY_M2_RUNTIME_MANIFEST') or PROJ/'runtime/m2/runtime-manifest.json')


def runtime_python(manifest_path=None):
    if os.environ.get('YY_STYLE_M2_PYTHON'):return Path(os.environ['YY_STYLE_M2_PYTHON'])
    parent=(Path(manifest_path) if manifest_path else manifest_location()).parent
    for p in (parent/'python/python.exe',parent/'.venv-cpu/bin/python',parent/'.venv-cpu/Scripts/python.exe',PROJ/'.venv-ml/Scripts/python.exe'):
        if p.is_file():return p
    return parent/'python/python.exe'


def verify_runtime(manifest_path=None, fd_repo=None):
    path=Path(manifest_path) if manifest_path else manifest_location()
    manifest=json.loads(path.read_text(encoding='utf-8'))
    catalog=json.loads((PROJ/'config/m2-runtime-catalog.json').read_text(encoding='utf-8'))
    expected=catalog['runtimes'].get(manifest.get('engine_id'))
    if not expected or identity(manifest)!=expected['identity_sha256']:
        raise ValueError('runtime manifest does not match the verified official lineage catalog')
    root=Path(fd_repo).resolve() if fd_repo else (path.parent/'FontDiffuser-Gradio').resolve()
    for entry in manifest['code_assets']:
        p=(root/entry['path']).resolve()
        if not p.is_relative_to(root) or sha(p)!=entry['sha256']:raise ValueError('runtime source/upstream asset hash mismatch: '+entry['path'])
    for name in NAMES:
        if sha(root/'ckpt'/name)!=manifest['base_npz_sha256'][name]:raise ValueError('runtime NPZ hash mismatch: '+name)
    if sha(root/'ttf/KaiXinSongA.ttf')!=manifest['content_font']['sha256']:raise ValueError('runtime font hash mismatch')
    return {'manifest':manifest,'manifest_path':str(path.resolve()),'manifest_sha256':sha(path),
            'identity_sha256':identity(manifest),'fd_repo':str(root),'engine_id':manifest['engine_id'],
            'python':str(runtime_python(path)),'license_scope':'private noncommercial research only; no redistribution clearance'}


def tensor_digest(arrays):
    h=hashlib.sha256()
    for name in sorted(arrays):
        arr=arrays[name];raw=arr.tobytes(order='C')
        header=json.dumps({'name':name,'dtype':arr.dtype.str,'shape':list(arr.shape),'nbytes':arr.nbytes},sort_keys=True,separators=(',',':')).encode()
        h.update(len(header).to_bytes(8,'big'));h.update(header);h.update(len(raw).to_bytes(8,'big'));h.update(raw)
    return h.hexdigest()
