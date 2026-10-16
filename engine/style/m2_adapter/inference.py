"""Real adapter inference. No font/sample fallback may claim source=M2.

Dependencies and authorized, full-hash-pinned runtime assets are required.
No automatic installation, download, training, or unsafe pickle fallback.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import time
from pathlib import Path
from .trainer_resume_protocol import native_path
from .personal_runtime_policy import bound_method
from .inference_diagnostics import stage, traced_stage
os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'

from engine.style.dataset.binding import verify_snapshot_binding

BASE_NAMES = ('unet.npz', 'style_encoder.npz', 'content_encoder.npz')
_MISSING = object()
ML_MODULES = ('torch', 'torchvision', 'diffusers', 'transformers', 'accelerate', 'fontTools')


class InferenceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256_file(path):
    h = hashlib.sha256()
    with native_path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _write_png(output, png):
    """Exclusive producer commit after generate() verifies output/frozen guards."""
    native_path(output.parent).mkdir(parents=True, exist_ok=True)
    with native_path(output).open('xb') as stream:
        stream.write(png)


def preflight(fd_repo, content_ttf=None, base_npz_sha256=None):
    root = Path(fd_repo)
    font = Path(content_ttf) if content_ttf else root / 'ttf/KaiXinSongA.ttf'
    missing_modules = [m for m in ML_MODULES if importlib.util.find_spec(m) is None]
    needed = [root / 'configs/fontdiffuser.py', root / 'src/__init__.py', font]
    needed += [root / 'ckpt' / n for n in BASE_NAMES]
    missing_files = [str(p) for p in needed if not native_path(p).is_file()]
    hashes = base_npz_sha256 or {}
    missing_hashes = [n for n in BASE_NAMES if not re.fullmatch(r'[0-9a-f]{64}', str(hashes.get(n, '')))]
    return {'status': 'PASS' if not (missing_modules or missing_files or missing_hashes) else 'BLOCKED',
            'missing_modules': missing_modules, 'missing_files': missing_files,
            'missing_full_base_sha256': missing_hashes, 'downloads_attempted': False}


def application_code_hashes(*, user_scr=False):
    root=Path(__file__).resolve().parent
    names=('inference.py','adapter.py','preprocessing.py','runtime.py','inference_runner.py','environment.py','deterministic_ops.py','runtime_variants.py','control_recipe.py','adapter_topology.py','offset_recipe.py','spatial_topology.py','spatial_recipe.py','topology_dispatch.py','recipe_dispatch.py','scr_recipe.py','scr_contract.py')
    names += ('deployment.py','deployment_trust.py','windows_scr_deployment.py','windows_scr_execution.py','windows_scr_probe.py','inference_diagnostics.py')
    if user_scr:names += ('user_scr_contract.py','user_scr_recipe.py','user_scr_runtime.py','windows_scr_deployment.py','windows_scr_execution.py','windows_scr_probe.py','preserved_training_deployment.py')
    return {name:sha256_file(root/name) for name in names}

def configure_cuda_inference_cap(torch_module, device):
    """Every new inference process needs its own allocator cap before model load."""
    if device != 'cuda':
        return None
    if not torch_module.cuda.is_available():
        raise InferenceError('E_CUDA_UNAVAILABLE', 'CUDA requested but unavailable')
    cap = 5 * 2**30
    torch_module.cuda.set_device(0)
    total = torch_module.cuda.get_device_properties(0).total_memory
    torch_module.cuda.set_per_process_memory_fraction(min(1., cap / total), 0)
    torch_module.cuda.reset_peak_memory_stats()
    return cap


def guided_ddpm_prediction(model, model_input, timestep_batch, style, content, scale):
    """Same null condition and CFG algebra as the official DPM wrapper."""
    import torch
    if scale == 1.0:
        return model({'x_t':model_input,'t':timestep_batch,'style':style,'content':content})
    paired = model({'x_t':torch.cat([model_input,model_input]),
                    't':torch.cat([timestep_batch,timestep_batch]),
                    'style':torch.cat([torch.ones_like(style),style]),
                    'content':torch.cat([torch.ones_like(content),content])})
    unconditional, conditional = paired.chunk(2)
    return unconditional + scale * (conditional - unconditional)


def recipe_validation_required(meta):
    identities=('m2-control-corrected-v1','m2-control-offset-delta-v1',
                'm2-control-spatial-offset-v1','m2-scr-two-negative-spatial-v1','m2-scr2n-user-snapshot-v1')
    objective=meta.get('training_objective')
    return (meta.get('training_recipe') is not None or meta.get('training_budget') is not None
            or meta.get('recipe_id') in identities
            or isinstance(objective,dict) and objective.get('recipe_id') in identities)


class M2Inference:
    @traced_stage('inference.ctor')
    def __init__(self, *, adapter_path, adapter_sha256, snapshot_dir, snapshot_sha256,
                 fd_repo, base_npz_sha256, content_ttf=None, device='cpu', steps=20,
                 use_adapter=True, runtime_manifest_path=None, allow_legacy_compatibility=False,
                 sampler='dpm', guidance_scale=7.5, cpu_threads=2, preprocessing_profile=None, runtime_binding=None, recipe_id=None, objective_profile=None, checkpoint_selection=None,
                 adapter_topology_id=_MISSING, adapter_topology=_MISSING, adapter_topology_sha256=_MISSING,
                 scr_objective=_MISSING,scr_assets=_MISSING,scr_data_binding=_MISSING,
                 scr_cache_binding=_MISSING,scr_preparation_binding=_MISSING, windows_cache_identity=None, deployment_binding=None, deployment_user_id=None):
        from .release_runtime_policy import assert_m2_execution_allowed
        from .runtime_variants import RuntimeUnavailable
        try:assert_m2_execution_allowed(Path(__file__).resolve().parents[3],binding=runtime_binding,purpose='inference')
        except RuntimeUnavailable as exc:raise InferenceError(exc.code,str(exc)) from exc
        from .topology_dispatch import TOPOLOGY_FIELDS, validate_topology_fields, OFFSET_TOPOLOGY_ID
        from .spatial_topology import SPATIAL_TOPOLOGY_ID
        from .spatial_recipe import RECIPE_ID as SPATIAL_RECIPE_ID, validate_spatial_binding
        from .offset_recipe import RECIPE_ID as OFFSET_RECIPE_ID, validate_offset_binding
        from .scr_recipe import RECIPE_ID as SCR_RECIPE_ID,validate_scr_binding
        from .scr_contract import validate_scr_provenance
        from .user_scr_recipe import RECIPE_ID as USER_SCR_RECIPE_ID,validate_user_scr_binding
        from .user_scr_contract import user_scr_identity
        from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID
        self.windows_user_scr_enabled=recipe_id==WINDOWS_SCR_RECIPE_ID
        self.user_scr_enabled=recipe_id in (USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID)
        from . import preserved_training_deployment as preserved
        self.preserved_deployment=None
        if isinstance(deployment_binding,dict) and deployment_binding.get('schema')==preserved.BINDING_SCHEMA:
            try:
                if type(deployment_user_id) is not int or deployment_user_id!=1:
                    raise ValueError('Preserved training deployment requires its owned account')
                if recipe_id!=WINDOWS_SCR_RECIPE_ID or device!='cuda' or steps!=20 or sampler!='dpm' or cpu_threads!=2 or guidance_scale!=7.5 or allow_legacy_compatibility:
                    raise ValueError('Preserved training deployment requires exact independently qualified GPU inference')
                with stage('ctor.preserved_deployment'):
                    self.preserved_deployment=preserved.resolve(Path(__file__).resolve().parents[3],deployment_binding,user_id=deployment_user_id)
                preserved.verify_request(self.preserved_deployment,adapter_path=adapter_path,adapter_sha256=adapter_sha256,
                    snapshot_dir=snapshot_dir,snapshot_sha256=snapshot_sha256,runtime_binding=runtime_binding)
            except (OSError,ValueError,KeyError,TypeError) as exc:raise InferenceError('E_DEPLOYMENT',str(exc)) from exc
        from .windows_scr_deployment import is_windows,resolve_deployment as resolve_windows
        self.windows_deployment=None
        if is_windows(deployment_binding):
            try:
                if recipe_id not in (USER_SCR_RECIPE_ID,SCR_RECIPE_ID) or not use_adapter or device!='cpu' or cpu_threads!=2 or steps!=20 or sampler!='dpm' or guidance_scale!=7.5 or allow_legacy_compatibility:
                    raise ValueError('Windows SCR deployment permits its exact CPU M2 inference policy only')
                with stage('ctor.windows_deployment'):
                    self.windows_deployment=resolve_windows(Path(__file__).resolve().parents[3],deployment_binding)
            except (OSError,ValueError,KeyError,TypeError) as exc:raise InferenceError('E_DEPLOYMENT',str(exc)) from exc
        self.requested_scr={k:v for k,v in (('scr_objective',scr_objective),('scr_assets',scr_assets),
            ('scr_data_binding',scr_data_binding),('scr_cache_binding',scr_cache_binding),
            ('scr_preparation_binding',scr_preparation_binding)) if v is not _MISSING}
        self.requested_recipe_id = recipe_id
        self.requested_recipe_identity={k:v for k,v in (("recipe_id",recipe_id),("objective_profile",objective_profile),("checkpoint_selection",checkpoint_selection)) if v is not None}
        supplied = dict(zip(TOPOLOGY_FIELDS,(adapter_topology_id,adapter_topology,adapter_topology_sha256)))
        self.requested_topology = {k:v for k,v in supplied.items() if v is not _MISSING}
        try:
            requested = validate_topology_fields(self.requested_topology,required=recipe_id in (OFFSET_RECIPE_ID,SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID))
            if self.user_scr_enabled:
                if self.windows_user_scr_enabled:
                    from .runtime_variants import validate_binding_shape
                    from .windows_user_scr import VARIANTS as WINDOWS_VARIANTS
                    from .windows_gpu_support import device_for_variant
                    validate_binding_shape(runtime_binding)
                    if runtime_binding['variant_id'] not in WINDOWS_VARIANTS or runtime_binding['device']!=device_for_variant(runtime_binding['variant_id']) or device!=runtime_binding['device']:raise ValueError('Windows inference requires its own qualified CPU binding')
                    from .windows_user_scr import validate_cache_identity
                    if self.preserved_deployment:
                        # This is immutable training provenance, not a new SCR cache.
                        original_cache=self.preserved_deployment['source']['immutable_training_identity']['windows_cache_identity']
                        if windows_cache_identity!=original_cache:raise ValueError('Preserved training cache provenance differs')
                        self.windows_cache_identity=original_cache
                    else:
                        self.windows_cache_identity=validate_cache_identity(Path(__file__).resolve().parents[3],runtime_binding['variant_id'],windows_cache_identity,runtime_binding['environment_identity_sha256'],snapshot_sha256)
                else:validate_user_scr_binding(self.windows_deployment['descriptor']['training_runtime_binding'] if self.windows_deployment else runtime_binding)
                user_scr_identity({**self.requested_scr,'snapshot_sha256':snapshot_sha256})
                if requested['id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('user SCR request topology mismatch')
            elif recipe_id==SCR_RECIPE_ID:
                validate_scr_provenance(self.requested_scr);validate_scr_binding(self.windows_deployment['descriptor']['training_runtime_binding'] if self.windows_deployment else runtime_binding)
                if requested['id']!=SPATIAL_TOPOLOGY_ID:raise ValueError('SCR request topology mismatch')
            elif self.requested_scr:raise ValueError('SCR provenance requires explicit SCR recipe')
            if requested['id']==OFFSET_TOPOLOGY_ID or recipe_id==OFFSET_RECIPE_ID:
                if recipe_id!=OFFSET_RECIPE_ID or requested['id']!=OFFSET_TOPOLOGY_ID:
                    raise ValueError('geometry request recipe/topology mismatch')
                validate_offset_binding(runtime_binding)
            if recipe_id not in (SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID) and (requested['id']==SPATIAL_TOPOLOGY_ID or recipe_id==SPATIAL_RECIPE_ID):
                if recipe_id!=SPATIAL_RECIPE_ID or requested['id']!=SPATIAL_TOPOLOGY_ID:
                    raise ValueError('spatial request recipe/topology mismatch')
                validate_spatial_binding(runtime_binding)
        except (ValueError,TypeError,KeyError) as exc:
            raise InferenceError('E_ADAPTER_TOPOLOGY',str(exc)) from exc
        self.runtime_binding=runtime_binding
        self.runtime_environment=None
        self.deployment = None
        if deployment_binding is not None and not self.preserved_deployment:
            from .deployment import resolve_deployment, verify_deployment_request
            try:
                with stage('ctor.deployment'):
                    self.deployment = self.windows_deployment or resolve_deployment(Path(__file__).resolve().parents[3], deployment_binding)
                verify_deployment_request(self.deployment, adapter_path=adapter_path,
                    adapter_sha256=adapter_sha256, snapshot_dir=snapshot_dir,
                    snapshot_sha256=snapshot_sha256, runtime_binding=runtime_binding)
                if device != 'cpu' or allow_legacy_compatibility:
                    raise ValueError('deployment permits exact CPU inference only')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise InferenceError('E_DEPLOYMENT', str(exc)) from exc
        if runtime_binding is not None and (not isinstance(runtime_binding, dict) or not runtime_binding):
            raise InferenceError('E_RUNTIME_BINDING', 'runtime binding must be a nonempty verified descriptor')
        if runtime_binding is None and device!='cpu':
            raise InferenceError('E_GPU_BINDING_REQUIRED','non-CPU inference requires an explicit verified runtime binding')
        if runtime_binding is not None:
            import sys
            from .runtime_variants import binding_for_config
            try:
                if self.windows_deployment or self.preserved_deployment:
                    selected=(self.preserved_deployment or self.windows_deployment)['execution']
                    if self.preserved_deployment:
                        from .runtime_variants import make_binding
                        selected_binding=make_binding(selected)
                    else:selected_binding=selected['runtime_binding']
                    if (runtime_binding!=selected_binding or Path(fd_repo).resolve()!=Path(selected['fd_repo'])
                        or Path(runtime_manifest_path).resolve()!=Path(selected['manifest_path'])
                        or Path(content_ttf).resolve()!=Path(selected['fd_repo'])/'ttf/KaiXinSongA.ttf'
                        or base_npz_sha256!=selected['manifest']['base_npz_sha256']):raise ValueError('Windows deployment execution assets differ')
                else:
                    selected=binding_for_config(Path(__file__).resolve().parents[3],{'runtime_binding':runtime_binding,**self.requested_recipe_identity,**self.requested_topology,**self.requested_scr,'snapshot_sha256':snapshot_sha256,'train':{'device':device},'runtime_manifest_path':runtime_manifest_path,'fd_repo':fd_repo,'content_font_path':content_ttf,'base_npz_sha256':base_npz_sha256,'runtime_identity_sha256':runtime_binding.get('asset_identity_sha256')},**({'user_scr_purpose':'inference'} if self.user_scr_enabled else {}))
                if Path(sys.executable).absolute()!=Path(selected['python']).absolute():raise ValueError('actual inference interpreter differs from the selected runtime')
                fd_repo=selected['fd_repo'];runtime_manifest_path=selected['manifest_path'];content_ttf=str(Path(fd_repo)/'ttf/KaiXinSongA.ttf');base_npz_sha256=selected['manifest']['base_npz_sha256']
            except (OSError,ValueError,KeyError) as exc:raise InferenceError('E_RUNTIME_BINDING',str(exc)) from exc
        self.application_code_sha256 = application_code_hashes(user_scr=True) if self.user_scr_enabled else application_code_hashes()
        self.expected_preprocessing_profile = preprocessing_profile
        self.adapter_path = Path(adapter_path).resolve()
        self.adapter_sha256 = adapter_sha256
        if not re.fullmatch(r'[0-9a-f]{64}', str(adapter_sha256)):
            raise InferenceError('E_ADAPTER_HASH', 'full adapter SHA256 is required')
        if not native_path(self.adapter_path).is_file() or sha256_file(self.adapter_path) != adapter_sha256:
            raise InferenceError('E_ADAPTER_HASH', 'adapter missing or SHA256 mismatch')
        self.binding = verify_snapshot_binding(snapshot_dir, snapshot_sha256)
        self.snapshot_dir = Path(self.binding['snapshot_dir'])
        self.fd_repo = Path(fd_repo).resolve()
        self.content_ttf = (Path(content_ttf) if content_ttf else self.fd_repo / 'ttf/KaiXinSongA.ttf').resolve()
        self.base_hashes = dict(base_npz_sha256 or {})
        self.device = str(device)
        self.runtime_manifest_path = runtime_manifest_path
        self.allow_legacy_compatibility = bool(allow_legacy_compatibility)
        self.sampler = sampler
        self.guidance_scale = float(guidance_scale)
        self.cpu_threads = int(cpu_threads)
        if sampler not in ('dpm','ddpm') or not 0 <= self.guidance_scale <= 20 or not 1 <= self.cpu_threads <= 8:
            raise InferenceError('E_INPUT_SAMPLER', 'invalid declared sampler/thread budget')
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 1000:
            raise InferenceError('E_INPUT_STEPS', 'steps must be an integer in 1..1000')
        if not isinstance(use_adapter, bool):
            raise InferenceError('E_INPUT_MODE', 'use_adapter must be a boolean')
        self.steps = steps
        self.use_adapter = use_adapter
        self._model = None
        self._scheduler = None
        splits = json.loads((self.snapshot_dir / 'split.json').read_text(encoding='utf-8'))['assignments']
        rows = [json.loads(line) for line in (self.snapshot_dir / 'samples.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        self.train_rows = sorted([r for r in rows if splits.get(r['sample_id']) == 'train'], key=lambda r: r['sample_id'])
        if not self.train_rows:
            raise InferenceError('E_INPUT_SAMPLES', 'snapshot has no training-side reference glyph')

    @bound_method
    @traced_stage('inference.load')
    def _load(self):
        if self._model is not None:
            return
        ready = preflight(self.fd_repo, self.content_ttf, self.base_hashes)
        if ready['status'] != 'PASS':
            raise InferenceError('E_RUNTIME_BLOCKED', json.dumps(ready, ensure_ascii=False))
        import torch
        version = tuple(int(x) for x in torch.__version__.split('+')[0].split('.')[:2])
        if version < (2,10):
            raise InferenceError('E_UNSAFE_TORCH_VERSION', 'restricted checkpoint loading requires torch >=2.10')
        torch.set_num_threads(self.cpu_threads)
        if torch.get_num_interop_threads() != 1:
            torch.set_num_interop_threads(1)
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
        torch.backends.cudnn.benchmark=False
        torch.backends.cudnn.deterministic=True
        self.cuda_memory_cap_bytes = configure_cuda_inference_cap(torch, self.device)
        if self.windows_user_scr_enabled and self.device=='cuda':
            from .windows_gpu_support import configure_cuda,GPU_LIMIT
            configure_cuda(torch);self.cuda_memory_cap_bytes=GPU_LIMIT
        if self.runtime_binding is not None:
            if getattr(self,'windows_deployment',None):
                from .windows_scr_execution import measure
                self.runtime_environment=measure(Path(__file__).resolve().parents[3])
                if self.runtime_environment!=self.windows_deployment['execution']['environment']:raise InferenceError('E_DEPLOYMENT_NUMERICS','actual Windows execution environment differs')
            else:
                from .runtime_variants import measure_in_process
                self.runtime_environment = measure_in_process(Path(__file__).resolve().parents[3], self.runtime_binding)
        from engine.style.m2_adapter.adapter import M2FontDiffuser, load_base_model
        if self.device.startswith('cuda') and not torch.cuda.is_available():
            raise InferenceError('E_CUDA_UNAVAILABLE', 'CUDA requested but unavailable')
        try:
            payload = torch.load(io.BytesIO(native_path(self.adapter_path).read_bytes()), map_location='cpu', weights_only=True)
        except Exception as exc:
            raise InferenceError('E_ADAPTER_SAFE_LOAD', f'restricted adapter load failed: {exc}') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('meta'), dict) or not isinstance(payload.get('adapter_state'), dict):
            raise InferenceError('E_ADAPTER_FORMAT', 'adapter_state/meta are required')
        meta = payload['meta']
        training_binding = self.runtime_binding
        if getattr(self,'preserved_deployment',None) is not None:
            from .preserved_training_deployment import verify_metadata
            try:verify_metadata(self.preserved_deployment['source'],meta)
            except (ValueError,KeyError,TypeError) as exc:raise InferenceError('E_DEPLOYMENT_METADATA',str(exc)) from exc
            training_binding=self.preserved_deployment['training_runtime_binding']
        if getattr(self, 'deployment', None) is not None:
            from .deployment import verify_deployment_metadata
            try:
                verify_deployment_metadata(self.deployment, meta)
            except (ValueError, KeyError, TypeError) as exc:
                raise InferenceError('E_DEPLOYMENT_METADATA', str(exc)) from exc
            training_binding = self.deployment['descriptor']['training_runtime_binding']
        if (training_binding is not None or meta.get('runtime_binding') is not None) and meta.get('runtime_binding') != training_binding:
            raise InferenceError('E_ADAPTER_RUNTIME_BINDING', 'adapter and request must carry the same runtime binding; no silent import or downgrade')
        if 'training_objective' in meta and meta.get('preprocessing') is None:
            raise InferenceError('E_PREPROCESSING_IDENTITY', 'modern adapter is missing its preprocessing identity')
        from .preprocessing import verify_preprocessing_contract
        try:
            self.preprocessing = verify_preprocessing_contract(meta.get('preprocessing'))
        except ValueError as exc:
            raise InferenceError('E_PREPROCESSING_IDENTITY', str(exc)) from exc
        if self.expected_preprocessing_profile is not None and self.expected_preprocessing_profile != self.preprocessing['profile']:
            raise InferenceError('E_PREPROCESSING_IDENTITY', 'requested preprocessing differs from adapter training')
        from .topology_dispatch import validate_topology_fields, TOPOLOGY_FIELDS, OFFSET_TOPOLOGY_ID
        from .spatial_topology import SPATIAL_TOPOLOGY_ID
        from .spatial_recipe import RECIPE_ID as SPATIAL_RECIPE_ID, validate_spatial_adapter_metadata
        from .offset_recipe import RECIPE_ID as OFFSET_RECIPE_ID, validate_offset_adapter_metadata
        from .scr_recipe import RECIPE_ID as SCR_RECIPE_ID,validate_scr_adapter_metadata
        from .user_scr_recipe import RECIPE_ID as USER_SCR_RECIPE_ID,validate_user_scr_adapter_metadata
        from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID,validate_adapter_metadata as validate_windows_adapter_metadata
        try:
            topology = validate_topology_fields(meta,required=meta.get('recipe_id') in (OFFSET_RECIPE_ID,SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID))
            if self.requested_topology and any(meta.get(k)!=v for k,v in self.requested_topology.items()):
                raise ValueError('request and adapter topology differ')
            if any(meta.get(k)!=v for k,v in self.requested_recipe_identity.items()):
                raise ValueError('request and adapter recipe differ')
            if any(meta.get(k)!=v for k,v in self.requested_scr.items()):raise ValueError('request and adapter SCR provenance differ')
            if (topology['id']==OFFSET_TOPOLOGY_ID)!=(meta.get('recipe_id')==OFFSET_RECIPE_ID):
                raise ValueError('adapter geometry recipe/topology mismatch')
            if (topology['id']==SPATIAL_TOPOLOGY_ID)!=(meta.get('recipe_id') in (SPATIAL_RECIPE_ID,SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID)):
                raise ValueError('adapter spatial recipe/topology mismatch')
            self.topology_identity = {k:meta[k] for k in TOPOLOGY_FIELDS if k in meta}
            self.adapter_topology_id = topology['id']
        except (ValueError,TypeError,KeyError) as exc:
            raise InferenceError('E_ADAPTER_TOPOLOGY',str(exc)) from exc
        if (self.adapter_topology_id==OFFSET_TOPOLOGY_ID or 'n_offset_projection_heads' in meta) and (type(meta.get('n_offset_projection_heads')) is not int or meta['n_offset_projection_heads'] != (6 if self.adapter_topology_id==OFFSET_TOPOLOGY_ID else 0)):
            raise InferenceError('E_ADAPTER_TOPOLOGY','declared offset projection count differs from topology')
        if (self.adapter_topology_id==SPATIAL_TOPOLOGY_ID or 'n_spatial_offset_heads' in meta) and (type(meta.get('n_spatial_offset_heads')) is not int or meta['n_spatial_offset_heads'] != (6 if self.adapter_topology_id==SPATIAL_TOPOLOGY_ID else 0)):
            raise InferenceError('E_ADAPTER_TOPOLOGY','declared spatial offset count differs from topology')
        self.training_objective = meta.get('training_objective')
        self.training_recipe = meta.get('training_recipe')
        self.training_budget = meta.get('training_budget')
        self.recipe_identity = {key: meta.get(key) for key in ('recipe_id', 'objective_profile', 'checkpoint_selection')}
        if meta.get('recipe_id') in (USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID):
            from .user_scr_contract import user_scr_identity
            self.recipe_identity.update(user_scr_identity(meta))
            if self.windows_user_scr_enabled:
                if meta.get('windows_cache_identity')!=self.windows_cache_identity:
                    raise InferenceError('E_ADAPTER_BINDING','Windows adapter cache differs from the requested exact source/environment/S03')
                self.recipe_identity['windows_cache_identity']=self.windows_cache_identity
        elif meta.get('recipe_id')==SCR_RECIPE_ID:
            from .scr_contract import validate_scr_provenance
            self.recipe_identity.update(validate_scr_provenance(meta))
        if recipe_validation_required(meta):
            from .control_recipe import validate_adapter_recipe_metadata
            try:
                if meta.get('recipe_id')==WINDOWS_SCR_RECIPE_ID:
                    validate_windows_adapter_metadata(meta,len(self.train_rows))
                elif meta.get('recipe_id')==USER_SCR_RECIPE_ID:
                    validate_user_scr_adapter_metadata(meta,len(self.train_rows))
                elif meta.get('recipe_id')==SCR_RECIPE_ID:
                    if getattr(self,'windows_deployment',None):
                        # Original manifest/receipt and metadata were checked above.
                        # Re-deriving a recipe hashes today's implementation into old
                        # training history, which this deployment must preserve.
                        from .windows_scr_deployment import OLD0178_MANIFEST_SHA
                        if self.windows_deployment['source']['artifact_manifest_sha256']!=OLD0178_MANIFEST_SHA or len(self.train_rows)!=51:
                            raise ValueError('old0178 deployment requires its exact original snapshot/recipe')
                    else:validate_scr_adapter_metadata(meta,len(self.train_rows))
                elif meta.get('recipe_id')==SPATIAL_RECIPE_ID:
                    validate_spatial_adapter_metadata(meta,len(self.train_rows))
                elif meta.get('recipe_id')==OFFSET_RECIPE_ID:
                    validate_offset_adapter_metadata(meta,len(self.train_rows))
                else:
                    validate_adapter_recipe_metadata(meta, len(self.train_rows))
            except (TypeError, ValueError) as exc:
                raise InferenceError('E_CONTROL_RECIPE_BINDING', str(exc)) from exc
        self.training_environment = meta.get('training_environment')
        from .environment import execution_environment
        self.inference_environment = execution_environment(self.device)
        if self.deployment is not None and not getattr(self,'windows_deployment',None) and self.inference_environment != self.training_environment:
            raise InferenceError('E_DEPLOYMENT_NUMERICS', 'actual inference numerical environment differs from original training')
        if meta.get('snapshot_sha256') != self.binding['snapshot_sha256']:
            raise InferenceError('E_ADAPTER_BINDING', 'adapter metadata does not match S03 identity')
        runtime = None
        if self.runtime_manifest_path is not None:
            from .runtime import verify_runtime
            runtime = verify_runtime(self.runtime_manifest_path,self.fd_repo)
            self.engine_id = runtime['engine_id']
            if self.base_hashes != runtime['manifest']['base_npz_sha256']:
                raise InferenceError('E_BASE_IDENTITY','requested hashes differ from verified runtime')
        else:
            self.engine_id = 'fontdiffuser-frozen-v1'
            from engine.style.m2_adapter.adapter import BASE_NPZ_SHA256
            if any(not str(self.base_hashes.get(n,'')).startswith(prefix) for n,prefix in BASE_NPZ_SHA256.items()):
                raise InferenceError('E_BASE_IDENTITY','historical mode requires historical NPZ anchors')
        exact = (meta.get('base_ckpt') == self.engine_id and meta.get('base_npz_sha256') == self.base_hashes
                 and (runtime is None or meta.get('runtime_identity_sha256') == runtime['identity_sha256']))
        if not exact and not self.allow_legacy_compatibility:
            raise InferenceError('E_BASE_IDENTITY','adapter lacks exact linkage to this runtime; compatibility-only flag required')
        self.runtime_identity_sha256 = runtime["identity_sha256"] if runtime else None
        self._quality_eligible = exact
        self._base_binding_strength = 'exact_training_runtime_lineage' if exact else 'UNVERIFIED_HISTORICAL_COMPATIBILITY_SMOKE_ONLY'
        actual_font_sha = sha256_file(self.content_ttf)
        if meta.get('content_font_sha256') and meta['content_font_sha256'] != actual_font_sha:
            raise InferenceError('E_CONTENT_FONT_IDENTITY','content font differs from training metadata')
        if runtime and actual_font_sha != runtime['manifest']['content_font']['sha256']:
            raise InferenceError('E_CONTENT_FONT_IDENTITY','content font differs from verified runtime')
        base,args = load_base_model(self.fd_repo,device=self.device,expected_hashes=self.base_hashes,
                                   runtime_manifest_path=self.runtime_manifest_path)
        model = M2FontDiffuser(base, self.content_ttf,adapter_topology_id=self.adapter_topology_id).to(self.device)
        state = payload['adapter_state']
        from .adapter import load_verified_adapter_state
        try:
            hook_channels=load_verified_adapter_state(model,state,enabled=self.use_adapter)
        except (ValueError,TypeError,KeyError) as exc:
            code='E_ADAPTER_NONFINITE' if 'E_ADAPTER_NONFINITE' in str(exc) else 'E_ADAPTER_SHAPE'
            raise InferenceError(code,str(exc)) from exc
        expected=set(hook_channels)
        if meta.get('n_hook_points')!=len(expected):
            raise InferenceError('E_ADAPTER_SHAPE','hook count differs from base architecture')
        model.requires_grad_(False)
        model.eval()
        self._hook_calls = {key:0 for key in expected}
        def counter(key):
            def count(_module,_inputs,_output):self._hook_calls[key] += 1
            return count
        for key,module in model.adapter.blocks.items():module.register_forward_hook(counter(key))
        from src import build_ddpm_scheduler,FontDiffuserModelDPM,FontDiffuserDPMPipeline
        self._scheduler = build_ddpm_scheduler(args)
        dpm_model = FontDiffuserModelDPM(unet=base.unet,style_encoder=base.style_encoder,content_encoder=base.content_encoder).eval().requires_grad_(False)
        self._dpm = FontDiffuserDPMPipeline(dpm_model,self._scheduler,guidance_scale=self.guidance_scale)
        self._model = model

    @bound_method
    def generate_baseline(self, style_ref_image, target_char, seed, out_png=None):
        """M1 comparison on this exact verified base, without a second base load."""
        self._load()
        previous_mode,previous_enabled=self.use_adapter,self._model.adapter.enabled
        self.use_adapter=False;self._model.adapter.enabled=False
        try:
            image,receipt=self.generate(style_ref_image,target_char,seed,out_png)
        finally:
            self.use_adapter=previous_mode;self._model.adapter.enabled=previous_enabled
        receipt['comparison_mode']='SAME_VERIFIED_BASE_ADAPTER_DISABLED'
        receipt['separate_base_model_loaded']=False
        return image,receipt

    @bound_method
    @traced_stage('inference.generate')
    def generate(self, style_ref_image, target_char, seed, out_png=None):
        if not isinstance(target_char, str) or len(target_char) != 1:
            raise InferenceError('E_INPUT_CHAR', 'target_char must be one character')
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise InferenceError('E_INPUT_SEED', 'seed must be a non-negative integer')
        current_code = application_code_hashes(user_scr=True) if getattr(self,'user_scr_enabled',False) else application_code_hashes()
        if current_code!=self.application_code_sha256:raise InferenceError('E_CODE_CHANGED','inference implementation changed after initialization')
        if getattr(self,'preserved_deployment',None) is not None:
            from .preserved_training_deployment import resolve
            with stage('generate.preserved_deployment'):
                self.preserved_deployment=resolve(Path(__file__).resolve().parents[3],self.preserved_deployment['deployment_binding'],
                    user_id=self.preserved_deployment['source']['user_id'])
        if getattr(self, 'deployment', None) is not None:
            from .deployment import resolve_deployment
            with stage('generate.deployment'):
                self.deployment = resolve_deployment(Path(__file__).resolve().parents[3],
                                                  self.deployment['deployment_binding'])
        self.binding = verify_snapshot_binding(self.snapshot_dir, self.binding['snapshot_sha256'])
        if sha256_file(self.adapter_path) != self.adapter_sha256:
            raise InferenceError('E_ADAPTER_HASH', 'adapter changed after initialization')
        refs = [(self.snapshot_dir / r['snapshot_file']).resolve() for r in self.train_rows]
        ref = Path(style_ref_image).resolve() if style_ref_image is not None else refs[0]
        if ref not in refs:
            raise InferenceError('E_REF_NOT_TRAIN', 'reference must be a verified training-side snapshot glyph')
        output = Path(out_png).resolve() if out_png is not None else None
        if output is not None and (output.is_relative_to(self.snapshot_dir) or any(native_path(parent/'FROZEN').exists() for parent in output.parents)):
            raise InferenceError('E_FROZEN_OUTPUT', 'inference output cannot modify a frozen snapshot')
        if output is not None and native_path(output).exists():
            raise InferenceError('E_OUTPUT_EXISTS', 'refusing to overwrite inference evidence')
        with stage('generate.load'):
            self._load()
        import torch
        from PIL import Image
        from fontTools.ttLib import TTFont
        from engine.style.m2_adapter.adapter import _glyph_to_tensor, render_content_char, RENDER_SIZE
        with TTFont(str(native_path(self.content_ttf))) as font:
            if ord(target_char) not in (font.getBestCmap() or {}):
                raise InferenceError('E_CHAR_NOT_IN_FONT', 'target missing from content font cmap')
        # Pinned upstream DPM allocates initial noise on CPU, then moves it.
        # Its generator must therefore be CPU even when the model is CUDA.
        generator_device = "cpu" if self.sampler == "dpm" else self.device
        generator = torch.Generator(device=generator_device).manual_seed(seed)
        style = _glyph_to_tensor(native_path(ref), preprocessing_profile=self.preprocessing["profile"]).unsqueeze(0).to(self.device)
        content = render_content_char(target_char, native_path(self.content_ttf), preprocessing_profile=self.preprocessing["profile"]).unsqueeze(0).to(self.device)
        from .deterministic_ops import execution_counts
        before_operators = execution_counts(self._model.base)
        before = dict(self._hook_calls)
        before_offsets = dict(self._model.adapter.offset_calls)
        before_spatial = dict(self._model.adapter.spatial_calls)
        start = time.monotonic()
        with stage('generate.native'), torch.inference_mode():
            if self.sampler == 'dpm':
                rgb = self._dpm.generate(content,style,batch_size=1,order=2,num_inference_step=self.steps,
                                         content_encoder_downsample_size=3,generator=generator)[0].convert('RGB')
            else:
                scheduler = self._scheduler
                scheduler.set_timesteps(self.steps,device=self.device)
                noisy=torch.randn((1,3,RENDER_SIZE,RENDER_SIZE),generator=generator,device=self.device)
                noisy *= getattr(scheduler,'init_noise_sigma',1.0)
                for timestep in scheduler.timesteps:
                    t=torch.full((1,),int(timestep),device=self.device,dtype=torch.long)
                    model_input = scheduler.scale_model_input(noisy, timestep)
                    predicted = guided_ddpm_prediction(self._model, model_input, t, style, content,
                                                       self.guidance_scale)
                    noisy=scheduler.step(predicted,timestep,noisy,generator=generator).prev_sample
                pixels=((((noisy[0].detach().cpu().clamp(-1,1)+1)/2).permute(1,2,0).numpy()*255).round().astype('uint8'))
                rgb=Image.fromarray(pixels,'RGB')
        operator_counts = execution_counts(self._model.base)
        operator_deltas = {name:{key:value-before_operators[name][key] for key,value in values.items()}
                           for name,values in operator_counts.items()}
        if len(operator_deltas) != 6 or not all(v["forward_calls"]>0 for v in operator_deltas.values()):
            raise InferenceError("E_OPERATOR_POLICY_NOT_APPLIED", "all six deterministic deform operators must participate")
        if self.device.startswith("cuda") and not all(v["cross_device_calls"]==v["forward_calls"] for v in operator_deltas.values()):
            raise InferenceError("E_OPERATOR_POLICY_NOT_APPLIED", "CUDA inference must preserve the CPU deform route")
        inference_memory = {'device': self.device, 'cap_bytes': self.cuda_memory_cap_bytes,
                            'scope': 'PyTorch allocator peaks since model load; CUDA context/driver/other processes excluded'}
        if self.device == 'cuda':
            inference_memory.update(peak_allocated=torch.cuda.max_memory_allocated(), peak_reserved=torch.cuda.max_memory_reserved())
            if max(inference_memory['peak_allocated'], inference_memory['peak_reserved']) > self.cuda_memory_cap_bytes:
                raise InferenceError('E_INFERENCE_MEMORY_CAP', 'inference exceeded its declared allocator cap')
        hook_calls={key:value-before[key] for key,value in self._hook_calls.items()}
        if self.use_adapter and (len(hook_calls)!=22 or not all(value>0 for value in hook_calls.values())):
            raise InferenceError('E_ADAPTER_NOT_APPLIED','not every trained residual hook participated in generation')
        offset_calls={key:value-before_offsets[key] for key,value in self._model.adapter.offset_calls.items()}
        from .adapter_topology import OFFSET_TOPOLOGY_ID, OFFSET_HEADS
        expected_offset_names={h['module'] for h in OFFSET_HEADS} if self.adapter_topology_id==OFFSET_TOPOLOGY_ID else set()
        if set(offset_calls)!=expected_offset_names or (self.use_adapter and not all(v>0 for v in offset_calls.values())):
            raise InferenceError('E_ADAPTER_NOT_APPLIED','offset projection delta execution differs from topology')
        spatial_calls={key:value-before_spatial[key] for key,value in self._model.adapter.spatial_calls.items()}
        from .spatial_topology import SPATIAL_TOPOLOGY_ID,SPATIAL_HEADS
        wanted_spatial={h['module'] for h in SPATIAL_HEADS} if self.adapter_topology_id==SPATIAL_TOPOLOGY_ID else set()
        if set(spatial_calls)!=wanted_spatial or (self.use_adapter and not all(v>0 for v in spatial_calls.values())):
            raise InferenceError('E_ADAPTER_NOT_APPLIED','spatial delta execution differs from topology')
        if not self.use_adapter and (any(hook_calls.values()) or any(offset_calls.values()) or any(spatial_calls.values())):
            raise InferenceError('E_ADAPTER_NOT_DISABLED','M1 must execute no trained adapter delta')
        paper, ink_level = self.preprocessing["paper"], self.preprocessing["ink"]
        alpha = rgb.convert('L').point(lambda value: max(0, min(255, round((paper - value) * 255 / (paper - ink_level)))))
        ink = sum(alpha.histogram()[128:])
        if ink < 16 or ink / (rgb.width * rgb.height) > 0.60:
            raise InferenceError('E_OUTPUT_INVALID', f'blank/block glyph: {ink} opaque pixels')
        rgba = Image.new('RGBA', rgb.size, (0, 0, 0, 0))
        rgba.putalpha(alpha)
        buf = io.BytesIO()
        rgba.save(buf, format='PNG')
        png = buf.getvalue()
        if output is not None:
            _write_png(output, png)
        source = 'M2' if self.use_adapter else 'M1'
        receipt = {'ok': True, 'source': source, 'engine_mode': 'M2_ADAPTER' if self.use_adapter else 'M1_FROZEN',
                   'engine_id': self.engine_id, 'model_version': f'adapter@{self.adapter_sha256[:16]}' if self.use_adapter else 'frozen-npz-base',
                   'adapter_applied': self.use_adapter, 'quality_eligible': self._quality_eligible,
                   'n_offset_projection_heads': self._model.n_offset_projection_heads, 'n_spatial_offset_heads': self._model.n_spatial_offset_heads,
                   'preprocessing': self.preprocessing, 'training_objective': self.training_objective,
                   'training_recipe':self.training_recipe,'training_budget':self.training_budget,
                   'training_environment':self.training_environment,'inference_environment':self.inference_environment,
                   'operator_execution_policy':self._model.base._r9_operator_policy,'operator_execution_counts':operator_deltas,
                   'inference_memory':inference_memory,
                   'runtime_identity_sha256': self.runtime_identity_sha256, 'application_code_sha256': self.application_code_sha256,
                   'adapter_hook_calls': hook_calls, 'offset_projection_hook_calls':offset_calls, 'spatial_offset_hook_calls':spatial_calls, 'adapter_sha256': self.adapter_sha256 if self.use_adapter else None,
                   'base_ckpt_sha256': self.base_hashes, 'base_binding_strength': self._base_binding_strength, 'dataset_snapshot_sha256': self.binding['snapshot_sha256'],
                   'snapshot_binding': self.binding,
                   'inputs': {'target_char': target_char, 'seed': seed, 'steps': self.steps, 'device': self.device,
                              'sampler': self.sampler, 'guidance_scale': self.guidance_scale, 'cpu_threads': self.cpu_threads,
                              'noise_generator_device': generator_device,
                              'style_ref_path': str(ref), 'style_ref_sha256': sha256_file(ref),
                              'content_ttf_sha256': sha256_file(self.content_ttf),
                              'unseen_in_training': target_char not in {r['label'] for r in self.train_rows}},
                   'output': {'png': str(output) if output else None, 'png_sha256': hashlib.sha256(png).hexdigest(),
                              'bytes': len(png), 'size': list(rgba.size), 'mode': 'RGBA', 'ink_px': ink},
                   'timings': {'sample_seconds': time.monotonic() - start},
                   'quality_gate': 'AI_VISUAL_REVIEW_PENDING', 'owner_blind_review': 'PENDING'}
        if getattr(self,'windows_user_scr_enabled',False):
            # This inference implementation never constructs a SCR feature
            # encoder; the full SCR producer separately measures this invariant.
            receipt.update(inference_scr_feature_forwards=0,scr_feature_encoder_loaded_for_inference=False)
        receipt.update(self.recipe_identity)
        receipt.update(self.topology_identity)
        from .topology_dispatch import validate_inference_topology_execution
        try:validate_inference_topology_execution(receipt)
        except ValueError as exc:raise InferenceError('E_ADAPTER_NOT_APPLIED',str(exc)) from exc
        if self.runtime_binding is not None:
            if getattr(self,'windows_deployment',None):
                from .windows_scr_execution import measure
                measured=measure(Path(__file__).resolve().parents[3])
                if measured!=self.windows_deployment['execution']['environment']:raise InferenceError('E_DEPLOYMENT_NUMERICS','actual Windows environment drifted')
            else:
                from .runtime_variants import measure_in_process
                measured = measure_in_process(Path(__file__).resolve().parents[3], self.runtime_binding)
            receipt.update(runtime_binding=self.runtime_binding, runtime_environment=measured)
        if getattr(self, 'deployment', None) is not None:
            from .deployment import deployment_receipt_fields
            receipt.update(deployment_receipt_fields(self.deployment))
        if getattr(self,'preserved_deployment',None) is not None:
            from .preserved_training_deployment import inference_identity,verify_inference_receipt,resolve
            # Recheck the independently qualified consumer and preserved origin
            # before committing a usable receipt, even if PNG bytes already exist.
            self.preserved_deployment=resolve(Path(__file__).resolve().parents[3],self.preserved_deployment['deployment_binding'],
                user_id=self.preserved_deployment['source']['user_id'])
            receipt.update(inference_identity(self.preserved_deployment))
            if self.use_adapter:verify_inference_receipt(self.preserved_deployment,receipt)
        return rgba, receipt
