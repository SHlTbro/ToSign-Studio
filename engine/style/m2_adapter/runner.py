# -*- coding: utf-8 -*-
"""M2 runner — worker 子进程协议（NDJSON progress + receipt 文件）。

请求（REQ.json）：
{
  "run_id": int, "out_dir": str, "style_id": int,
  "snapshot_dir": str, "snapshot_sha256": str,
  "content_font_path": str,
  "recipe_id": "m2-control-corrected-v1",
  "train": {"max_epochs": 8, "max_steps": 80, "max_seconds": 900,
             "batch": 2, "lr": 1e-4, "patience": 9, "seed": 20261001}
}
max_steps is derived from the verified training count; 20 training rows gives80.
输出：
- NDJSON progress 行：{"type":"progress","epoch":n,"total_epochs":T,"loss":l,"samples_seen":s}
- receipt 文件（out_dir/m2-receipt.json）：
  {"ok": true, "engine_mode": "M2_ADAPTER", "engine_id": …, "model_version": …,
   "output": {"files": [{"relative_path", "sha256", "bytes"}…]}, "stats": …}
退出码：0 ok；2 engine failure；3 input failure。
"""
from __future__ import annotations

import hashlib
import os
os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

ENGINE_MODE = "M2_ADAPTER"
RUNNER_VERSION = "engine/style/m2_adapter/runner/0.7.0+EXPLICIT_SCR2N"
EXIT_OK, EXIT_ENGINE, EXIT_INPUT = 0, 2, 3


def measured_topology_counts(wrapper):
    """Bind terminal producer counts to the actually materialized adapter modules."""
    observed={'n_hook_points':len(wrapper.adapter.blocks),'n_offset_projection_heads':len(wrapper.adapter.offset_blocks),'n_spatial_offset_heads':len(wrapper.adapter.spatial_blocks)}
    if any(type(getattr(wrapper,key,None)) is not int or getattr(wrapper,key)!=value for key,value in observed.items()):
        raise ValueError('E_ADAPTER_TOPOLOGY: declared and materialized module counts differ')
    return observed

def _fail_payload(code: str, detail: str) -> dict:
    return {"engine_mode": ENGINE_MODE, "runner_version": RUNNER_VERSION,
            "ok": False, "failure": {"code": code, "detail": detail}}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def validate_user_scr_run_request(req, snapshot_binding):
    """Fresh per-run authority/cache check before importing any model package."""
    from .user_scr_contract import user_scr_identity
    from .user_scr_cache import validate_user_reference_cache
    identity = user_scr_identity(req)
    preparation = identity['scr_preparation_binding']
    from .windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
    if req.get('recipe_id')==WINDOWS_RECIPE:
        from .windows_user_scr import validate_cache_identity
        from .runtime_variants import validate_binding_shape
        binding=validate_binding_shape(req['runtime_binding'])
        validate_cache_identity(REPO,binding['variant_id'],req.get('windows_cache_identity'),binding['environment_identity_sha256'],snapshot_binding['snapshot_sha256'])
    if (req.get('user_id') != preparation['user_id'] or req.get('style_id') != preparation['style_id']
            or snapshot_binding.get('snapshot_sha256') != preparation['snapshot_sha256']):
        raise ValueError('generic SCR run user/style/snapshot differs from server preparation')
    verified = validate_user_reference_cache(req['scr_asset_paths'], snapshot_binding, verify_assets=False)
    if (verified['data_binding'] != identity['scr_data_binding']
            or verified['cache_binding'] != identity['scr_cache_binding']):
        raise ValueError('generic SCR prepared cache changed before model load')
    return identity


def user_scr_batch_identity(batch_inputs, data_binding, phase):
    """Project verified occurrence labels/splits into the unchanged dataset batch."""
    rows = {row['sample_id']: row for row in data_binding['rows']}
    ids = batch_inputs.get('sample_id')
    if not isinstance(ids, (list, tuple)) or not ids or len(set(ids)) != len(ids):
        raise ValueError('generic SCR batch requires unique occurrence IDs')
    if any(sid not in rows or rows[sid]['split'] != phase for sid in ids):
        raise ValueError('generic SCR batch crosses canonical split membership')
    return {**batch_inputs, 'label': [rows[sid]['label'] for sid in ids],
            'split': [rows[sid]['split'] for sid in ids]}

def validate_expected_environment_identity(expected, training_environment, runtime_environment, runtime_binding):
    """A bound CPU runtime uses its registered basic schema, not the diagnostic schema."""
    if runtime_binding is not None:
        if not isinstance(runtime_binding, dict) or not runtime_binding:
            raise ValueError('malformed explicit runtime binding')
        from .windows_user_scr import VARIANTS as WINDOWS_VARIANTS
        observed = training_environment if runtime_binding.get('variant_id') in WINDOWS_VARIANTS else runtime_environment
    else:
        observed = training_environment
    if not isinstance(observed, dict) or not isinstance(observed.get('identity_sha256'), str):
        raise ValueError('actual selected execution environment was not measured')
    if expected is not None and expected != observed['identity_sha256']:
        raise ValueError('expected environment differs from actual selected execution environment')
    return observed['identity_sha256']


def start_scr_resource_guard(target, seconds=3720):
    """Bound the actual SCR subprocess, including preparation and validation."""
    import threading,psutil
    stop=threading.Event();started=time.monotonic();process=psutil.Process()
    state={'peak_rss_bytes':0,'limit_bytes':5*2**30,'max_seconds':seconds,'hard_allocator_cap':False}
    def monitor():
        while not stop.is_set():
            state['peak_rss_bytes']=max(state['peak_rss_bytes'],process.memory_info().rss)
            if state['peak_rss_bytes']>state['limit_bytes'] or time.monotonic()-started>seconds:
                target.mkdir(parents=True,exist_ok=True)
                with (target/'SCR-RESOURCE-STOP.json').open('x') as f:
                    json.dump({'status':'BLOCKED',**state,'elapsed_seconds':time.monotonic()-started},f)
                os._exit(4)
            stop.wait(.05)
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    return stop,thread,state


from .personal_runtime_policy import bound_action
from .trainer_resume_protocol import native_path

@bound_action('train')
def run(req: dict) -> tuple[int, dict]:
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    try:assert_m2_execution_allowed(REPO)
    except (RuntimeUnavailable,OSError) as exc:
        return EXIT_INPUT, _fail_payload(getattr(exc,'code','BLOCKED_ENV'),str(exc))
    watched_code = [Path(__file__), Path(__file__).parent/"adapter.py", Path(__file__).parent/"preprocessing.py", Path(__file__).parent/"control_recipe.py", Path(__file__).parent/"adapter_topology.py", Path(__file__).parent/"offset_recipe.py", Path(__file__).parent/"spatial_topology.py", Path(__file__).parent/"spatial_recipe.py", Path(__file__).parent/"topology_dispatch.py", Path(__file__).parent/"recipe_dispatch.py", Path(__file__).parent/"runtime_variants.py", Path(__file__).parent/"environment.py", Path(__file__).parent/"deterministic_ops.py",
                    REPO/"engine/style/trainer/trainer.py", REPO/"engine/style/trainer/config.py", REPO/"engine/style/trainer/receipt.py", Path(__file__).parent/'trainer_resume_protocol.py']
    initial_code_hashes = {str(path.relative_to(REPO)):_sha(path.read_bytes()) for path in watched_code}
    for name in ('scr_contract.py','scr_recipe.py','scr_objective.py','scr_paired_objective.py','x0_objectives.py'):
        path=Path(__file__).parent/name;watched_code.append(path);initial_code_hashes[str(path.relative_to(REPO))]=_sha(path.read_bytes())
    if not isinstance(req, dict):
        return EXIT_INPUT, _fail_payload("E_INPUT_JSON", "request must be an object")
    from .user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
    from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID
    user_scr_enabled = req.get('recipe_id') in (USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID)
    if user_scr_enabled:
        for name in ('user_scr_contract.py','user_scr_cache.py','user_scr_recipe.py','user_scr_objective.py','user_scr_runtime.py','user_scr_prepare_runner.py'):
            path=Path(__file__).parent/name;watched_code.append(path);initial_code_hashes[str(path.relative_to(REPO))]=_sha(path.read_bytes())
    if req.get('recipe_id')==WINDOWS_SCR_RECIPE_ID:
        for name in ('windows_training_recipe.py','windows_training_runtime.py','windows_training_capability.py','windows_training_executor.py','windows_user_scr.py'):
            path=Path(__file__).parent/name;watched_code.append(path);initial_code_hashes[str(path.relative_to(REPO))]=_sha(path.read_bytes())
    for k in ("run_id", "out_dir", "snapshot_dir", "snapshot_sha256", "train"):
        if k not in req or req[k] in (None, ""):
            return EXIT_INPUT, _fail_payload("E_INPUT_MISSING", f"missing field: {k}")
    if not isinstance(req.get('train'), dict):
        return EXIT_INPUT, _fail_payload('E_INPUT_JSON', 'train must be an object')
    try:
        from .runtime_variants import binding_for_config
        request_device=(req.get("train") or {}).get("device","cpu")
        if request_device not in ("cpu","cuda"):
            raise ValueError("device must be the exact explicit cpu or cuda profile; no implicit coercion")
        selected_runtime=binding_for_config(REPO,req)
        if selected_runtime and Path(sys.executable).absolute()!=Path(selected_runtime["python"]).absolute():
            raise ValueError("actual child interpreter differs from the bound runtime")
    except (OSError,ValueError,TypeError,KeyError) as exc:
        return EXIT_INPUT,_fail_payload("E_RUNTIME_BINDING",str(exc))
    # 全路径绝对化（load_base_model 会 chdir 进 vendor repo，相对路径在其后全部失效）
    out_dir = Path(req["out_dir"]).resolve()
    if any((parent / "FROZEN").exists() for parent in (out_dir, *out_dir.parents)):
        return EXIT_INPUT, _fail_payload("E_FROZEN_OUTPUT", "output cannot be inside a frozen snapshot")
    snapshot_input = Path(req["snapshot_dir"]).resolve()
    if out_dir == snapshot_input or out_dir.is_relative_to(snapshot_input):
        return EXIT_INPUT, _fail_payload("E_FROZEN_OUTPUT", "output cannot be inside input snapshot")
    if any((out_dir / rel).exists() for rel in ("m2-receipt.json", "artifact/m2_adapter.pt", "receipts/engine_receipt.json", "train_run/checkpoints.json", "FROZEN")):
        return EXIT_INPUT, _fail_payload("E_RUN_EXISTS", "refusing to overwrite existing run/frozen evidence")
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = Path(req["snapshot_dir"]).resolve()
    snapshot_sha = str(req["snapshot_sha256"])
    tcfg = dict(req["train"])
    resume_record = None
    resume_binding = req.get('trainer_resume_binding')
    if resume_binding is not None:
        from .trainer_resume_protocol import validate_binding, digest, verify_manifest, trainer_identity
        from .trainer_resume_protocol import source_digest
        try:
            validate_binding(resume_binding)
            trainer_version, trainer_code = trainer_identity(REPO)
            expected = {'owner_id':req.get('user_id'), 'snapshot_sha256':req['snapshot_sha256'],
                'runtime_binding':req.get('runtime_binding'), 'source_sha256':source_digest(REPO),
                'trainer_version':trainer_version, 'trainer_code_sha256':trainer_code,
                'training_budget':req.get('training_budget')}
            if any(digest(resume_binding[k]) != digest(v) for k,v in expected.items()):
                raise ValueError('native trainer binding differs from current owner/data/runtime/source/budget')
            for key,value in resume_binding['recipe_identity'].items():
                if digest(req.get(key)) != digest(value): raise ValueError('native recipe differs: '+key)
            for key,value in resume_binding['artifact_binding'].items():
                if key not in ('sample_ids_sha256','content_font_sha256') and digest(req.get(key)) != digest(value):
                    raise ValueError('native artifact binding differs: '+key)
            if req.get('resume_checkpoint') is not None:
                checkpoint = req['resume_checkpoint']
                if set(checkpoint) != {'directory','manifest_sha256'}: raise ValueError('server checkpoint reference required')
                resume_record = verify_manifest(checkpoint['directory'], resume_binding, checkpoint['manifest_sha256'])
                from .windows_resume_runtime import authorize_checkpoint
                authorize_checkpoint(REPO, req.get('runtime_binding'), resume_binding)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EXIT_INPUT, _fail_payload(getattr(exc,'code','E_RESUME_BINDING'),str(exc))
    elif req.get('resume_checkpoint') is not None:
        return EXIT_INPUT, _fail_payload('E_RESUME_ANCHOR','server anchored full binding required')
    runtime = None
    engine_id = ENGINE_MODE.lower()

    # Verify actual content before importing ML dependencies or starting work.
    from engine.style.dataset.binding import verify_snapshot_binding
    from engine.style.dataset.schema import DatasetError
    try:
        snapshot_binding = verify_snapshot_binding(snapshot_dir, snapshot_sha)
    except DatasetError as exc:
        return EXIT_INPUT, _fail_payload(exc.code, str(exc))
    if req.get("style_id") is None or str(snapshot_binding.get("style_id")) != str(req["style_id"]):
        return EXIT_INPUT, _fail_payload("E_STYLE_BINDING", "S03 style identity differs from request")
    if user_scr_enabled:
        try:
            validate_user_scr_run_request(req, snapshot_binding)
        except (ValueError, TypeError, KeyError, OSError, DatasetError) as exc:
            return EXIT_INPUT, _fail_payload('E_USER_SCR_PREPARATION', str(exc))

    from engine.style.m2_adapter.inference import preflight
    fd_repo = Path(req.get("fd_repo") or REPO / "artifacts/style-font/FontDiffuser-Gradio").resolve()
    font_path = Path(req.get("content_font_path") or fd_repo / "ttf" / "KaiXinSongA.ttf").resolve()
    if resume_binding is not None and resume_binding['artifact_binding'].get('content_font_sha256') != _sha(font_path.read_bytes()):
        return EXIT_INPUT, _fail_payload('E_RESUME_ARTIFACT','content font changed')
    availability = preflight(fd_repo, font_path, req.get("base_npz_sha256"))
    if availability["status"] != "PASS":
        return EXIT_ENGINE, _fail_payload("E_RUNTIME_BLOCKED", json.dumps(availability, ensure_ascii=False))
    if req.get("runtime_manifest_path"):
        from engine.style.m2_adapter.runtime import verify_runtime
        runtime = verify_runtime(req["runtime_manifest_path"], fd_repo)
        engine_id = runtime["engine_id"]
    else:
        engine_id = "fontdiffuser-frozen-v1"

    import torch  # noqa: PLC0415（Trainer/数据集均需；本模块顶部不重依赖 torch）
    from engine.style.m2_adapter.adapter import (M2FontDiffuser,
                                                 GlyphPairDataset,
                                                 load_base_model,
                                                 load_snapshot_pairs)
    from engine.style.trainer.config import TrainConfig, EarlyStop, FreezePolicy
    from engine.style.trainer.trainer import Trainer

    train_pairs = load_snapshot_pairs(snapshot_dir, "train")
    val_pairs = load_snapshot_pairs(snapshot_dir, "val")
    if len(train_pairs) < 8 or len(val_pairs) < 2:
        return EXIT_INPUT, _fail_payload(
            "E_INPUT_SAMPLES", f"split too small: train={len(train_pairs)} val={len(val_pairs)}")

    from engine.style.m2_adapter.recipe_dispatch import resolve_selected_recipe
    from engine.style.m2_adapter.topology_dispatch import validate_topology_fields, TOPOLOGY_FIELDS, OFFSET_TOPOLOGY_ID
    from engine.style.m2_adapter.spatial_topology import SPATIAL_TOPOLOGY_ID
    from engine.style.m2_adapter.preprocessing import preprocessing_contract
    from engine.style.m2_adapter.deterministic_ops import policy_identity
    try:
        if any(req.get(key) is not None for key in ('vgg_npz_path', 'vgg_npz_sha256', 'vgg_manifest_path')):
            raise ValueError('VGG assets are not part of the product control recipe')
        resolved_recipe = resolve_selected_recipe(len(train_pairs), req)
        adapter_topology = validate_topology_fields(resolved_recipe)
        topology_identity = {k:resolved_recipe[k] for k in TOPOLOGY_FIELDS if k in resolved_recipe}
        req = {**req, **resolved_recipe}
        tcfg = resolved_recipe['train']
        training_recipe = resolved_recipe['training_recipe']
        training_budget = resolved_recipe['training_budget']
        preprocessing = preprocessing_contract(resolved_recipe['preprocessing_profile'])
        conditioning_dropout = tcfg['conditioning_dropout']
    except (TypeError, ValueError) as exc:
        return EXIT_INPUT, _fail_payload('E_CONTROL_RECIPE', str(exc))
    from .native_training_session import objective_metadata
    training_objective, recipe_identity, scr_enabled, project_scr_execution_evidence = objective_metadata(
        req, resolved_recipe, user_scr_enabled=user_scr_enabled)

    # ---- 基座 + wrapper（冻结基座 + hook adapter） ----
    fd_repo = Path(req.get("fd_repo") or REPO / "artifacts/style-font/FontDiffuser-Gradio").resolve()
    font_path = Path(req.get("content_font_path") or fd_repo / "ttf" / "KaiXinSongA.ttf").resolve()
    device = request_device
    torch.set_num_threads(int(tcfg.get("cpu_threads", 2)))
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32=False
    torch.backends.cudnn.allow_tf32=False
    torch.backends.cudnn.benchmark=False
    torch.backends.cudnn.deterministic=True
    if device.startswith("cuda"):
        memory_cap = float(tcfg.get("max_memory_gib", 5.0))
        if memory_cap != 5.0:
            return EXIT_INPUT, _fail_payload("E_CONTROL_RECIPE", "control runtime requires5GiB process memory cap")
        torch.cuda.set_per_process_memory_fraction(min(1.,memory_cap*2**30/torch.cuda.get_device_properties(0).total_memory),0)
        torch.cuda.reset_peak_memory_stats()
    execution_binding = req.get('runtime_binding')
    if req.get('recipe_id')=='m2-scr2n-user-windows-spatial-v1' and device=='cuda':
        from .windows_gpu_support import configure_cuda
        configure_cuda(torch)
    runtime_environment = None
    if execution_binding is not None:
        from .runtime_variants import measure_in_process
        runtime_environment = measure_in_process(REPO, execution_binding)
    from engine.style.m2_adapter.environment import execution_environment
    training_environment = execution_environment(device)
    try:
        validate_expected_environment_identity(req.get('expected_environment_identity_sha256'), training_environment,
                                              runtime_environment, execution_binding)
    except ValueError as exc:
        return EXIT_ENGINE, _fail_payload('E_RUNTIME_ENVIRONMENT_CHANGED', str(exc))
    base, args = load_base_model(fd_repo, device=device, expected_hashes=req.get("base_npz_sha256"), runtime_manifest_path=req.get("runtime_manifest_path"))
    from src import build_ddpm_scheduler  # noqa: PLC0415（S01 探针同源：scheduler 在 src）
    scheduler = build_ddpm_scheduler(args)
    from .native_training_session import training_config, build_training_session
    cfg = training_config(req, tcfg=tcfg, out_dir=out_dir, snapshot_dir=snapshot_dir,
        snapshot_sha=snapshot_sha, engine_id=engine_id, preprocessing=preprocessing,
        training_objective=training_objective, device=device)
    session = build_training_session(req, cfg=cfg, base=base, scheduler=scheduler,
        font_path=font_path, device=device, train_pairs=train_pairs, val_pairs=val_pairs,
        preprocessing=preprocessing, conditioning_dropout=conditioning_dropout,
        adapter_topology=adapter_topology, resume_binding=resume_binding, resume_record=resume_record,
        scr_enabled=scr_enabled, user_scr_enabled=user_scr_enabled,
        snapshot_binding=snapshot_binding, out_dir=out_dir)
    trainer, wrapper, train_ds, val_ds = (session[k] for k in ('trainer','wrapper','train_ds','val_ds'))
    scr, optimizer, loss_diagnostics = (session[k] for k in ('scr','optimizer','loss_diagnostics'))
    # Change skeleton flags only here, after verified real base loading/warmup.
    trainer.receipt.engineering_skeleton = False
    trainer.receipt.loaded_real_base_weights = True
    trainer.receipt.claims_quality_pass = False
    trainer.receipt.notes = ("M2 adapter run: FontDiffuser frozen base + zero-init "
                             f"hook residual adapter ({wrapper.n_hook_points} hooks); "
                             f"snapshot {snapshot_sha[:16]}…; train={len(train_pairs)} val={len(val_pairs)}")

    # 进度行（NDJSON）：epoch/loss → worker _Progress
    orig_train = trainer.train

    def train_with_progress():
        import threading
        stop_flag = {"stop": False}
        last = {"epoch": 0}
        def reporter():
            while not stop_flag["stop"]:
                e = trainer.receipt.epochs[-1] if trainer.receipt.epochs else None
                if e and e["epoch"] != last["epoch"]:
                    last["epoch"] = e["epoch"]
                    print(json.dumps({"type": "progress", "epoch": e["epoch"],
                                      "total_epochs": cfg.max_epochs,
                                      "loss": e["val_loss"], "samples_seen": e["global_step"]},
                                     ensure_ascii=False), flush=True)
                time.sleep(2)
        th = threading.Thread(target=reporter, daemon=True)
        th.start()
        try:
            return orig_train()
        finally:
            stop_flag["stop"] = True
    trainer.train = train_with_progress  # type: ignore[method-assign]

    best_export = {}
    def capture_best(epoch, step, val):
        best_export.update({"state": {k: v.detach().cpu().clone() for k, v in wrapper.adapter.state_dict().items()},
                            "epoch": epoch, "global_step": step, "validation_loss": val})
    trainer.on_best = capture_best
    validation_protocol={"schema":"deterministic-validation/1","noise":"sha256(seed:sample_id)-seeded NumPy generator, fixed per sample", "style_references":"training-side only, disjoint target label", "seed":int(tcfg.get("seed",20261001))+7, "n_validation_rows":len(val_pairs)}
    def durable_epoch(epoch,step,val):
        # Tensor-only adapter export survives controller/session interruption;
        # it does not mark an unfinished run as complete or quality-approved.
        import io
        folder=out_dir/'epoch_exports'/f'epoch-{epoch:04d}';native_path(folder).mkdir(parents=True,exist_ok=False)
        meta={"run_id":req["run_id"],"style_id":req.get("style_id"),"snapshot_sha256":snapshot_sha,
              "base_ckpt":engine_id,"base_npz_sha256":dict(req.get("base_npz_sha256") or {}),
              "runtime_identity_sha256":runtime["identity_sha256"] if runtime else None,
              "content_font_sha256":_sha(font_path.read_bytes()),"n_hook_points":wrapper.n_hook_points,"n_offset_projection_heads":wrapper.n_offset_projection_heads,"n_spatial_offset_heads":wrapper.n_spatial_offset_heads,
              "selection":{"kind":"completed_epoch_state","epoch":epoch,"global_step":step,"validation_loss":val},
              "training_completion":"EPOCH_COMPLETE_RUN_PENDING","validation_protocol":validation_protocol,
              "preprocessing":preprocessing,"training_objective":training_objective,"training_environment":training_environment,
              "training_recipe":training_recipe,"training_budget":training_budget}
        meta.update(recipe_identity)
        if scr is not None:meta.update(project_scr_execution_evidence(scr.evidence()))
        if execution_binding is not None:
            measured = measure_in_process(REPO, execution_binding)
            meta.update(runtime_binding=execution_binding, runtime_environment=measured)
        payload={"adapter_state":{k:v.detach().cpu().clone() for k,v in wrapper.adapter.state_dict().items()},"meta":meta}
        buf=io.BytesIO();torch.save(payload,buf);adapter=folder/'adapter.pt'
        with native_path(adapter).open('xb') as f:f.write(buf.getvalue())
        record={"status":"EPOCH_COMPLETE_RUN_PENDING","epoch":epoch,"global_step":step,"validation_loss":val,
                "adapter_sha256":_sha(native_path(adapter).read_bytes()),"adapter_bytes":native_path(adapter).stat().st_size,
                "snapshot_binding":snapshot_binding,"runtime_identity_sha256":meta["runtime_identity_sha256"],
                "validation_protocol":validation_protocol,"preprocessing":preprocessing,
                "training_objective":training_objective,"training_recipe":training_recipe,"training_budget":training_budget,"claims_quality_pass":False}
        record.update(recipe_identity)
        if execution_binding is not None: record.update(runtime_binding=execution_binding, runtime_environment=measured)
        with native_path(folder/'receipt.json').open('x',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2)
        print(json.dumps({"type":"durable_epoch","epoch":epoch,"global_step":step,"adapter_sha256":record["adapter_sha256"],"status":record["status"]}),flush=True)
    trainer.on_epoch_complete=durable_epoch
    try:
        receipt_s05 = train_with_progress()
    except Exception as exc:
        failure = {**_fail_payload(getattr(exc, 'code', 'E_TRAINING_FAILED'), str(exc)),
                   **recipe_identity, 'training_recipe':training_recipe, 'training_budget':training_budget,
                   'preprocessing':preprocessing, 'training_objective':training_objective,
                   'training_environment':training_environment, 'partial_checkpoints_preserved':True,
                   'candidate_exported':False}
        if execution_binding is not None:
            failure.update(runtime_binding=execution_binding, runtime_environment=runtime_environment)
        return EXIT_ENGINE, failure
    if initial_code_hashes != {str(path.relative_to(REPO)):_sha(path.read_bytes()) for path in watched_code}:
        raise RuntimeError("E_CODE_CHANGED: training implementation changed during the run")
    if resume_binding is not None and source_digest(REPO) != resume_binding['source_sha256']:
        raise RuntimeError('E_CODE_CHANGED: bound product/trainer implementation changed during the run')
    verify_snapshot_binding(snapshot_dir, snapshot_sha)
    if user_scr_enabled:
        validate_user_scr_run_request(req, snapshot_binding)

    # ---- 产物：adapter state（可训面）+ S05 checkpoint 引用 ----
    # Explicit selection: export the terminal wrapper, not the best checkpoint.
    # Never attribute best_val/best_epoch to these last-epoch weights.
    selected_epoch = receipt_s05.epochs[-1]["epoch"] if receipt_s05.epochs else 0
    selected_val = receipt_s05.epochs[-1].get("val_loss") if receipt_s05.epochs else None
    validation_matches_export = bool(receipt_s05.epochs and receipt_s05.epochs[-1]["global_step"] == trainer.global_step)
    selection = {"kind": "terminal_state", "last_completed_epoch": selected_epoch,
                 "global_step": trainer.global_step, "batch_pointer": dict(trainer.batch_ptr),
                 "validation_loss": selected_val if validation_matches_export else None,
                 "validation_matches_exported_state": validation_matches_export,
                 "best_checkpoint_exported": False}
    from engine.style.m2_adapter.control_recipe import validate_completed_budget
    try:
        validate_completed_budget(len(receipt_s05.epochs), trainer.global_step, validation_matches_export, training_budget)
        if selected_val is None or not torch.isfinite(torch.tensor(float(selected_val))):
            raise ValueError('terminal validation is missing or nonfinite')
    except ValueError as exc:
        receipt_s05.finalize('FAILED', failure_code='E_TRAINING_BUDGET_INCOMPLETE')
        receipt_s05.notes += ' | ' + str(exc)
        receipt_s05.save()
        failure = {**_fail_payload('E_TRAINING_BUDGET_INCOMPLETE', str(exc)), **recipe_identity,
                            'training_recipe':training_recipe,'training_budget':training_budget,
                            'preprocessing':preprocessing,'training_objective':training_objective,
                            'training_environment':training_environment,
                            'actual_epochs':len(receipt_s05.epochs),'actual_optimizer_steps':trainer.global_step,
                            'partial_checkpoints_preserved':True,'candidate_exported':False}
        if execution_binding is not None:
            failure.update(runtime_binding=execution_binding, runtime_environment=runtime_environment)
        return EXIT_ENGINE, failure
    terminal_selection = dict(selection)
    terminal_state = {k: v.detach().cpu().clone() for k, v in wrapper.adapter.state_dict().items()}
    export_best = bool(best_export and req.get("checkpoint_selection") == "best_validation")
    adapter_state = best_export["state"] if export_best else terminal_state
    if export_best:
        selection = {"kind": "best_validation_checkpoint", "epoch": best_export["epoch"],
                     "global_step": best_export["global_step"], "validation_loss": best_export["validation_loss"],
                     "validation_matches_exported_state": True, "best_checkpoint_exported": True}
    buf_path = out_dir / "artifact" / "m2_adapter.pt"
    buf_path.parent.mkdir(parents=True, exist_ok=True)
    import io
    buf = io.BytesIO()
    adapter_meta = {"run_id": req["run_id"], "style_id": req.get("style_id"),
                    "snapshot_sha256": snapshot_sha, "base_ckpt": engine_id,
                    "runtime_identity_sha256": runtime["identity_sha256"] if runtime else None,
                    "n_hook_points": wrapper.n_hook_points,"n_offset_projection_heads":wrapper.n_offset_projection_heads,"n_spatial_offset_heads":wrapper.n_spatial_offset_heads, "selection": selection,
                    "validation_protocol": validation_protocol,
                    "preprocessing": preprocessing, "training_objective": training_objective,
                    "training_environment":training_environment,"training_recipe":training_recipe,"training_budget":training_budget,
                    "content_font_sha256": _sha(font_path.read_bytes()),
                    "base_npz_sha256": dict(req.get("base_npz_sha256") or {})}
    adapter_meta.update(recipe_identity)
    if scr is not None:adapter_meta.update(project_scr_execution_evidence(scr.evidence()))
    if execution_binding is not None:
        runtime_environment = measure_in_process(REPO, execution_binding)
        adapter_meta.update(runtime_binding=execution_binding, runtime_environment=runtime_environment)
    torch.save({"adapter_state": adapter_state, "meta": adapter_meta}, buf)
    buf_path.write_bytes(buf.getvalue())
    files = [{"relative_path": buf_path.relative_to(out_dir).as_posix(),
              "sha256": _sha(buf_path.read_bytes()), "bytes": buf_path.stat().st_size}]
    if best_export:
        last_path = out_dir / "artifact" / "m2_adapter-last.pt"
        last_buf = io.BytesIO()
        torch.save({"adapter_state": terminal_state, "meta": {**adapter_meta, "selection": terminal_selection}}, last_buf)
        last_path.write_bytes(last_buf.getvalue())
        files.append({"relative_path": last_path.relative_to(out_dir).as_posix(),
                      "sha256": _sha(last_path.read_bytes()), "bytes": last_path.stat().st_size})
    for name in ("ckpt-best.pt", "ckpt-last.pt", "checkpoints.json"):
        src = Path(cfg.run_dir) / name
        if src.is_file():
            dst = out_dir / "train_run" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            files.append({"relative_path": dst.relative_to(out_dir).as_posix(),
                          "sha256": _sha(dst.read_bytes()), "bytes": dst.stat().st_size})

    from engine.style.m2_adapter.deterministic_ops import execution_counts
    operator_counts = execution_counts(base)
    if req.get('recipe_id')=='m2-scr2n-user-windows-spatial-v1' and device=='cuda':
        from .windows_gpu_support import GPU_LIMIT
        if max(torch.cuda.max_memory_allocated(0),torch.cuda.max_memory_reserved(0))>GPU_LIMIT:
            raise RuntimeError('E_WINDOWS_SCR_GPU_BUDGET: training allocator exceeded 3 GiB')
    if len(operator_counts) != 6 or not all(v["forward_calls"] > 0 for v in operator_counts.values()):
        raise RuntimeError("E_OPERATOR_POLICY_NOT_APPLIED")
    if device.startswith("cuda") and not all(v["cross_device_calls"] == v["forward_calls"] for v in operator_counts.values()):
        raise RuntimeError("E_OPERATOR_POLICY_NOT_APPLIED: CUDA must retain the CPU DeformConv route")
    offset_projection_calls=dict(wrapper.adapter.offset_calls)
    if adapter_topology['id']==OFFSET_TOPOLOGY_ID and (len(offset_projection_calls)!=6 or not all(v>0 for v in offset_projection_calls.values())):
        raise RuntimeError('E_ADAPTER_NOT_APPLIED: all six offset deltas must participate')
    spatial_calls=dict(wrapper.adapter.spatial_calls)
    if adapter_topology['id']==SPATIAL_TOPOLOGY_ID and (len(spatial_calls)!=6 or not all(v>0 for v in spatial_calls.values())):
        raise RuntimeError('E_ADAPTER_NOT_APPLIED: all six spatial deltas must participate')
    diagnostic_file = out_dir / "control-training-diagnostics.json"
    diagnostics = {"schema":"m2-control-training-diagnostics/1","run_origin":"ACTUAL_PRODUCT_CONTROL_RUN_QUALITY_UNREVIEWED",
                   "training_recipe":training_recipe,"training_budget":training_budget,
                   "training_stream":train_ds.access_trace,"validation_stream":val_ds.access_trace,
                   "initial_training_code_sha256":initial_code_hashes,
                   "operator_execution_policy":base._r9_operator_policy,"operator_execution_counts":operator_counts,
                   "offset_projection_hook_calls":offset_projection_calls,"spatial_offset_hook_calls":spatial_calls, **topology_identity,
                   "training_stream_sha256":_sha(json.dumps(train_ds.access_trace,sort_keys=True,separators=(",", ":")).encode()),
                   "gradient_diagnostics":trainer.gradient_diagnostics,"loss_diagnostics":loss_diagnostics,
                   "VGG_used":False, "VGG_asset_required":False,
                   "base_frozen":all(not p.requires_grad and p.grad is None for p in base.parameters()),
                   "cuda_peak_allocated":torch.cuda.max_memory_allocated() if device.startswith("cuda") else None,
                   "cuda_peak_reserved":torch.cuda.max_memory_reserved() if device.startswith("cuda") else None}
    if scr is not None:diagnostics.update(project_scr_execution_evidence(scr.evidence()),**recipe_identity)
    with diagnostic_file.open("x",encoding="utf-8") as stream:json.dump(diagnostics,stream,indent=2)
    files.append({"relative_path":diagnostic_file.name,"sha256":_sha(diagnostic_file.read_bytes()),"bytes":diagnostic_file.stat().st_size})
    receipt = {
        "ok": True, "engine_mode": ENGINE_MODE, "engine_id": engine_id,
        "operator_execution_counts": operator_counts,
        **measured_topology_counts(wrapper),
        "runner_version": RUNNER_VERSION,
        "dataset_snapshot_sha256": snapshot_sha,
        "snapshot_binding": snapshot_binding,
        "base_ckpt_sha256": dict(req.get("base_npz_sha256") or {}),
        "runtime_identity_sha256": runtime["identity_sha256"] if runtime else None,
        "runtime_manifest_path": req.get("runtime_manifest_path"),
        "model_version": f"adapter@{_sha(buf_path.read_bytes())[:16]}",
        "output": {"files": files},
        "adapter_selection": selection,
        "validation_protocol": validation_protocol,
        "preprocessing": preprocessing, "training_objective": training_objective,
        "training_environment":training_environment,"training_recipe":training_recipe,"training_budget":training_budget,
        "offset_projection_hook_calls":offset_projection_calls,"spatial_offset_hook_calls":spatial_calls,
        "conditioning_sampling": {"seen_including_warmup":train_ds.conditioning_seen,"dropped_including_warmup":train_ds.conditioning_dropped},
        "runtime_evidence": {"loaded_real_base_weights": True,
                             "operator_execution_policy":base._r9_operator_policy,"operator_execution_counts":operator_counts,
                             "base_format": "npz_allow_pickle_false",
                             "adapter_format": "torch_tensor_state",
                             "base_parameter_count": sum(p.numel() for p in base.parameters()),
                             "adapter_parameter_count": sum(p.numel() for p in wrapper.adapter.parameters()),
                             "hook_count": wrapper.n_hook_points,"offset_projection_head_count":wrapper.n_offset_projection_heads,"spatial_offset_head_count":wrapper.n_spatial_offset_heads,
                             "seed": cfg.seed,
                             "environment": receipt_s05.env,
                             "cuda_used": device.startswith("cuda"),
                             "gpu_name": torch.cuda.get_device_name() if device.startswith("cuda") else None,
                             "code_sha256": {"runner.py": _sha(Path(__file__).read_bytes()),
                                             "adapter.py": _sha((Path(__file__).parent / "adapter.py").read_bytes()),
                                             "preprocessing.py": preprocessing["implementation_sha256"],
                                             "control_recipe.py": _sha((Path(__file__).parent/"control_recipe.py").read_bytes()),
                                             "offset_recipe.py": _sha((Path(__file__).parent/"offset_recipe.py").read_bytes()),
                                             **{name:_sha((Path(__file__).parent/name).read_bytes()) for name in ("spatial_topology.py","spatial_recipe.py","topology_dispatch.py","recipe_dispatch.py")},
                                             "adapter_topology.py": _sha((Path(__file__).parent/"adapter_topology.py").read_bytes())}},
        "stats": {"confirmed_train": len(train_pairs), "val": len(val_pairs),
                  "epochs": len(receipt_s05.epochs), "best_val": receipt_s05.best_val,
                  "best_epoch": receipt_s05.best_epoch,
                  "trainable_params": sum(p.numel() for n, p in wrapper.named_parameters()
                                          if n.startswith("adapter.")),
                  "n_hook_points": wrapper.n_hook_points,"n_offset_projection_heads":wrapper.n_offset_projection_heads,"n_spatial_offset_heads":wrapper.n_spatial_offset_heads,
                  "final_status": receipt_s05.final_status},
        "process": {"device": device, "seed": cfg.seed, "dtype":"float32", "AMP":False, "TF32":False,
                    "cuda_peak_allocated": diagnostics['cuda_peak_allocated'],
                    "cuda_peak_reserved": diagnostics['cuda_peak_reserved'],
                    "initial_training_code_sha256":initial_code_hashes},
    }
    receipt.update(recipe_identity)
    if scr is not None:receipt.update(project_scr_execution_evidence(scr.evidence()))
    if execution_binding is not None: receipt.update(runtime_binding=execution_binding, runtime_environment=runtime_environment)
    return EXIT_OK, receipt


def main(argv=None) -> int:
    from .release_runtime_policy import assert_m2_execution_allowed
    from .runtime_variants import RuntimeUnavailable
    from .personal_runtime_policy import present,bootstrap
    try:
        if present(REPO):bootstrap(REPO)
        else:assert_m2_execution_allowed(REPO)
    except (RuntimeUnavailable,OSError) as exc:
        print(json.dumps(_fail_payload(getattr(exc,'code','BLOCKED_ENV'),str(exc)),ensure_ascii=False))
        return EXIT_INPUT
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(json.dumps(_fail_payload("E_INPUT_MISSING", "usage: runner REQ.json")),
              ensure_ascii=False)
        return EXIT_INPUT
    try:
        req = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps(_fail_payload("E_INPUT_JSON", f"parse failed: {e!r}")),
              ensure_ascii=False)
        return EXIT_INPUT
    try:assert_m2_execution_allowed(REPO,binding=req.get('runtime_binding') if isinstance(req,dict) else None,purpose='resume' if req.get('resume_checkpoint') is not None else 'train')
    except (OSError,ValueError) as exc:
        print(json.dumps(_fail_payload(getattr(exc,'code','E_PERSONAL_RUNTIME_BINDING'),str(exc))));return EXIT_INPUT
    if not isinstance(req, dict) or not isinstance(req.get("out_dir"), str) or not req["out_dir"].strip():
        print(json.dumps(_fail_payload("E_INPUT_MISSING", "out_dir is required")))
        return EXIT_INPUT
    target = Path(req["out_dir"]).resolve()
    protected = ("m2-receipt.json", "artifact/m2_adapter.pt", "receipts/engine_receipt.json", "train_run/checkpoints.json", "FROZEN")
    if any((target / rel).exists() for rel in protected) or any((parent / "FROZEN").exists() for parent in target.parents):
        print(json.dumps(_fail_payload("E_RUN_EXISTS", "existing run/frozen evidence is read-only")))
        return EXIT_INPUT
    if req.get('recipe_id') in ('m2-scr2n-user-snapshot-v1','m2-scr2n-user-windows-spatial-v1'):
        try:
            if req.get('recipe_id')=='m2-scr2n-user-windows-spatial-v1':
                from .windows_training_recipe import plan_training_recipe as plan_user_scr_recipe
            else:
                from .user_scr_recipe import plan_user_scr_recipe
            bounded=plan_user_scr_recipe(req['scr_data_binding']['counts']['train'], req)
            guard=start_scr_resource_guard(target, seconds=bounded['train']['max_seconds']+120)
        except (KeyError, ValueError, TypeError) as exc:
            print(json.dumps(_fail_payload('E_CONTROL_RECIPE', str(exc))));return EXIT_INPUT
    else:
        guard=start_scr_resource_guard(target) if req.get('recipe_id')=='m2-scr-two-negative-spatial-v1' else None
    try:
        code, payload = run(req)
    except Exception as e:  # noqa: BLE001 —— 未预期异常也不得 exit0
        code, payload = EXIT_ENGINE, _fail_payload(
            "E_UNEXPECTED", f"{e!r}\n{traceback.format_exc()[-800:]}")
    finally:
        if guard is not None:
            guard[0].set();guard[1].join(timeout=1)
    if guard is not None:
        payload['cpu_memory']={**guard[2],'within_limit':0<guard[2]['peak_rss_bytes']<=guard[2]['limit_bytes'],
                               'measurement':'sampled process RSS every50ms with fail-closed subprocess watchdog'}
    if payload.get("failure", {}).get("code") == "E_RUN_EXISTS" or (target / "FROZEN").exists():
        print(json.dumps(payload, ensure_ascii=False))
        return code
    out_receipt = target / "m2-receipt.json"
    out_receipt.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_receipt.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=1)
    except FileExistsError:
        print(json.dumps(_fail_payload("E_RUN_EXISTS", "receipt appeared concurrently; not overwritten")))
        return EXIT_INPUT
    print(json.dumps({"type": "result", "ok": payload.get("ok") is True,
                      "receipt": payload}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
