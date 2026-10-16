"""One native S05 session shared by product and owned resume qualification.

This module grants no authority. Its deferred ML imports retain the signed
execution guard. Product recipe validation remains in runner.py.
"""
from pathlib import Path
import json
from .trainer_resume_protocol import native_path

def objective_metadata(req, resolved_recipe, *, user_scr_enabled):
    from .topology_dispatch import TOPOLOGY_FIELDS
    from .deterministic_ops import policy_identity
    from .user_scr_recipe import RECIPE_ID as USER_SCR_RECIPE_ID
    from .windows_training_recipe import RECIPE_ID as WINDOWS_SCR_RECIPE_ID
    conditioning_dropout = resolved_recipe['train']['conditioning_dropout']
    topology_identity = {k:resolved_recipe[k] for k in TOPOLOGY_FIELDS if k in resolved_recipe}
    training_objective = {'schema': 'm2-objective/3-control', 'objective_profile': resolved_recipe['objective_profile'],
        'recipe_id': resolved_recipe['recipe_id'], 'prediction': 'epsilon', 'diffusion_loss': 'mean_squared_error',
        'conditioning_dropout': conditioning_dropout, 'unconditional_condition': 'both content and style tensors set to +1',
        'offset_loss': '0.1 * mean(offset_sum ** 2)',
        'perceptual_loss': {'coefficient': 0.0, 'enabled': False, 'asset_required': False},
        'gradient_clip_norm': 1.0, 'selection': 'terminal fixed complete budget with matching final validation',
        'operator_execution_policy': policy_identity()}
    recipe_identity = {key: resolved_recipe[key] for key in ('recipe_id', 'objective_profile', 'checkpoint_selection')}
    recipe_identity.update(topology_identity)
    training_objective.update(topology_identity)
    from .scr_recipe import RECIPE_ID as SCR_RECIPE_ID
    scr_enabled=resolved_recipe['recipe_id'] in (SCR_RECIPE_ID,USER_SCR_RECIPE_ID,WINDOWS_SCR_RECIPE_ID)
    if user_scr_enabled:
        from .user_scr_contract import user_scr_identity, project_user_scr_execution_evidence
        project_scr_execution_evidence = project_user_scr_execution_evidence
        scr_identity = user_scr_identity(req)
        recipe_identity.update(scr_identity)
        training_objective.update(schema='m2-objective/5-scr2n-user', **scr_identity)
        if req['recipe_id']==WINDOWS_SCR_RECIPE_ID:
            recipe_identity['windows_cache_identity']=req['windows_cache_identity']
            training_objective['windows_cache_identity']=req['windows_cache_identity']
    elif scr_enabled:
        from .scr_contract import validate_scr_provenance,project_scr_execution_evidence
        scr_identity=validate_scr_provenance(req)
        recipe_identity.update(scr_identity)
        training_objective.update(schema='m2-objective/4-scr2n',**scr_identity)

    return training_objective, recipe_identity, scr_enabled, project_scr_execution_evidence if scr_enabled else None

def training_config(req, *, tcfg, out_dir, snapshot_dir, snapshot_sha, engine_id,
                    preprocessing, training_objective, device):
    from engine.style.trainer.config import TrainConfig, FreezePolicy, EarlyStop
    cfg = TrainConfig(
        run_name=f"m2-{req['run_id']}",
        engine_id=engine_id,
        run_dir=str(out_dir / "train_run"),
        seed=int(tcfg.get("seed", 20261001)),
        data_snapshot_dir=str(snapshot_dir),
        data_snapshot_sha256=snapshot_sha,
        base_ckpt_sha256=dict(req.get("base_npz_sha256") or {}),
        preprocessing=preprocessing, training_objective=training_objective,
        freeze=FreezePolicy(trainable_prefixes=("adapter.",)),
        stop=EarlyStop(patience=tcfg["patience"]),
        max_epochs=tcfg["max_epochs"],
        max_steps=tcfg["max_steps"],
        max_seconds=tcfg["max_seconds"],
        optimizer={"name": "adamw", "lr": float(tcfg.get("lr", 1e-4)), "weight_decay": 0.01},
        device=device, allow_gpu=device.startswith("cuda"), gradient_clip_norm=1.0,
    )
    return cfg

def build_training_session(req, *, cfg, base, scheduler, font_path, device,
                           train_pairs, val_pairs, preprocessing, conditioning_dropout,
                           adapter_topology, resume_binding, resume_record,
                           scr_enabled, user_scr_enabled, snapshot_binding, out_dir, trial_trace=None):
    from .release_runtime_policy import assert_m2_execution_allowed
    assert_m2_execution_allowed(Path(__file__).resolve().parents[3])
    import torch
    from .adapter import M2FontDiffuser, GlyphPairDataset
    from .runner import user_scr_batch_identity
    from engine.style.trainer.trainer import Trainer
    tcfg = req['train']
    wrapper = M2FontDiffuser(base, font_path,adapter_topology_id=adapter_topology['id']).to(device)
    loss_diagnostics = []

    batch = int(tcfg.get("batch", 2))
    train_ds = GlyphPairDataset(train_pairs, font_path, int(tcfg.get("seed", 20261001)), scheduler, preprocessing_profile=preprocessing["profile"], conditioning_dropout=conditioning_dropout)
    val_ds = GlyphPairDataset(val_pairs, font_path, int(tcfg.get("seed", 20261001)) + 7, scheduler, fixed_noise=True, style_pairs=train_pairs, preprocessing_profile=preprocessing["profile"])
    g = torch.Generator(); g.manual_seed(int(tcfg.get("seed", 20261001)))
    from torch.utils.data import DataLoader
    batch_sampler = None
    iterator_generator = torch.Generator(); iterator_generator.manual_seed(int(tcfg.get('seed',20261001)))
    if resume_binding is not None:
        from .trainer_resume_protocol import StatefulBatchSampler
        batch_sampler = StatefulBatchSampler(len(train_ds),batch,drop_last=True,num_workers=0)
        train_loader = DataLoader(train_ds,batch_sampler=batch_sampler,generator=iterator_generator,num_workers=0)
    else:
        train_loader = DataLoader(train_ds, batch_size=batch, shuffle=True, generator=g,
                                  num_workers=0, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=batch, shuffle=False, num_workers=0)

    # ---- S05 训练循环（冻结契约由 TrainConfig 强制） ----
    (Path(cfg.run_dir)).mkdir(parents=True, exist_ok=True)  # S05 ckpt 落盘目录
    # 懒初始化预热：一次 no_grad 前向（按 collate 后的 4D batch）让全部 hook 残差参数落位
    from torch.utils.data import default_collate
    warm = default_collate([train_ds[0], train_ds[1] if len(train_ds) > 1 else train_ds[0]])
    with torch.no_grad():
        wrapper({k: (v.to(device) if hasattr(v, "to") else v) for k, v in warm[0].items()})
    if len(wrapper.adapter.state_dict())!=adapter_topology['expected_adapter_tensors'] or sum(p.numel() for p in wrapper.adapter.parameters())!=adapter_topology['expected_adapter_parameters']:
        raise ValueError('E_ADAPTER_TOPOLOGY: measured adapter topology differs from contract')
    trainable = [n for n, p in wrapper.named_parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(
        [p for n, p in wrapper.named_parameters() if n.startswith("adapter.")],
        lr=float(tcfg.get("lr", 1e-4)), weight_decay=0.01)
    if not trainable:
        from engine.style.trainer.config import ConfigError
        raise ConfigError("E_ZERO_TRAINABLE", "warmup 后仍无 adapter 可训参数")
    offset_coeff = 0.1
    scr=None
    if scr_enabled:
        if user_scr_enabled:
            from .user_scr_objective import UserSCRTwoNegativeObjective
            scr=UserSCRTwoNegativeObjective(req['scr_asset_paths'],snapshot_binding,
                expected_data_binding=req['scr_data_binding'],expected_cache_binding=req['scr_cache_binding'])
        else:
            from .scr_objective import SCRTwoNegativeObjective
            scr=SCRTwoNegativeObjective(req['scr_asset_paths'],snapshot_binding)
        scr.to(device)
        from .x0_objectives import analytic_x0

    def loss_fn(out, y):
        y = y.to(device=out.device)
        off = wrapper.last_offset
        extra = offset_coeff * off.pow(2).mean() if off is not None else out.new_zeros(())
        batch_inputs = wrapper.last_inputs
        epsilon_loss = torch.nn.functional.mse_loss(out, y)
        total = epsilon_loss + extra
        scr_raw=None
        if scr is not None:
            if user_scr_enabled:
                batch_inputs=user_scr_batch_identity(batch_inputs, req['scr_data_binding'], 'train' if wrapper.training else 'val')
            objective_x0=analytic_x0(out,batch_inputs['x_t'],batch_inputs['t'],scheduler.alphas_cumprod.to(device=out.device,dtype=torch.float32),clip=True)
            scr_raw=scr.loss(objective_x0,batch_inputs,'train' if wrapper.training else 'val').mean
            total=total+0.01*scr_raw
        with torch.no_grad():
            alpha = scheduler.alphas_cumprod.to(device=out.device, dtype=out.dtype)[batch_inputs['t']].reshape(-1, 1, 1, 1)
            raw_x0 = (batch_inputs['x_t'] - (1 - alpha).sqrt() * out) / alpha.sqrt()
            saturated = (raw_x0.abs() > 1).float().flatten(1).mean(1)
            loss_diagnostics.append({'phase': 'train' if wrapper.training else 'validation',
                'timesteps': batch_inputs['t'].detach().cpu().tolist(),
                'x0_saturation_fraction': saturated.detach().cpu().tolist(),
                'epsilon_mse': float(epsilon_loss.detach()), 'offset_term': float(extra.detach()),
                **({'scr_raw':float(scr_raw.detach()),'scr_term':float((.01*scr_raw).detach()),'total_loss':float(total.detach())} if scr_raw is not None else {}),
                'VGG_used': False})
        return total

    import copy
    dataset_fields = ('conditioning_seen','conditioning_dropped','access_trace')
    scr_fields = ('_calls','_lookups','_target_hash_checks','_prediction_feature_forwards','_prediction_images')
    def loader_state():
        return {'schema':'m2-private-loader-state/1','sampler':batch_sampler.state_dict(),
            'shuffle_rng':g.get_state(),'iterator_rng':iterator_generator.get_state(),
            'dataset_rng':copy.deepcopy(train_ds._gen.bit_generator.state),
            'train_dataset':{k:copy.deepcopy(getattr(train_ds,k)) for k in dataset_fields},
            'val_dataset':{k:copy.deepcopy(getattr(val_ds,k)) for k in dataset_fields},
            'loss_diagnostics':copy.deepcopy(loss_diagnostics),
            'scr_counters':{k:copy.deepcopy(getattr(scr,k)) for k in scr_fields} if scr is not None else None}
    def restore_loader(state):
        if set(state) != {'schema','sampler','shuffle_rng','iterator_rng','dataset_rng','train_dataset','val_dataset','loss_diagnostics','scr_counters'} or state['schema']!='m2-private-loader-state/1':
            raise ValueError('complete private loader state required')
        batch_sampler.load_state_dict(state['sampler'])
        g.set_state(state['shuffle_rng']); iterator_generator.set_state(state['iterator_rng'])
        train_ds._gen.bit_generator.state = copy.deepcopy(state['dataset_rng'])
        for dataset,key in ((train_ds,'train_dataset'),(val_ds,'val_dataset')):
            if set(state[key]) != set(dataset_fields): raise ValueError('dataset evidence omitted')
            for name,value in state[key].items(): setattr(dataset,name,copy.deepcopy(value))
        loss_diagnostics[:] = copy.deepcopy(state['loss_diagnostics'])
        if scr is not None:
            if not isinstance(state['scr_counters'],dict) or set(state['scr_counters']) != set(scr_fields):
                raise ValueError('SCR execution counters omitted')
            for name,value in state['scr_counters'].items(): setattr(scr,name,copy.deepcopy(value))
        elif state['scr_counters'] is not None: raise ValueError('SCR execution identity changed')
    def cancelled():
        control = out_dir/'control.json'
        if not native_path(control).exists(): return False
        data = json.loads(native_path(control).read_text(encoding='utf-8'))
        if data.get('run_id') != req['run_id']: raise ValueError('cancel control owner differs')
        return data.get('cancel_requested') is True
    if trial_trace is not None:
        from .personal_runtime_policy import _scope
        if not isinstance(trial_trace,list) or (_scope.get() or {}).get('purpose') != 'qualification':
            raise ValueError('private qualification trace requires authenticated qualification scope')
        actual_getter, actual_restorer = loader_state, restore_loader
        def loader_state():
            return {**actual_getter(), 'resume_trial_step_trace':copy.deepcopy(trial_trace)}
        def restore_loader(state):
            state=dict(state)
            trial_trace[:] = copy.deepcopy(state.pop('resume_trial_step_trace'))
            actual_restorer(state)
    trainer = Trainer(cfg, wrapper, optimizer, train_loader, val_loader=val_loader,
        loss_fn=loss_fn, run_id=resume_binding['run_id'] if resume_binding is not None else str(req['run_id']),
        resume_from=resume_record['checkpoint_path'] if resume_record else None,
        resume_binding=resume_binding, resume_manifest_sha256=resume_record['manifest_sha256'] if resume_record else None,
        loader_state_getter=loader_state if resume_binding is not None else None,
        loader_state_restorer=restore_loader if resume_binding is not None else None, cancel_requested=cancelled)
    if batch_sampler is not None:
        def begin_epoch(epoch):
            if batch_sampler.epoch != epoch:
                # Preserve the former shuffle generator's DataLoader base-seed
                # draw. The separate iterator generator affects no data with
                # zero workers; its extra restart draw is handled by Trainer.
                torch.empty((),dtype=torch.int64).random_(generator=g)
                batch_sampler.begin_epoch(epoch,torch.randperm(len(train_ds),generator=g).tolist())
        def commit_step(step,pointer):
            batch_sampler.commit_batch(pointer['batch_idx'])
            if batch_sampler.next_batch == len(batch_sampler) and step < cfg.max_steps:
                # RandomSampler consumed a second empty-tail permutation when
                # a complete epoch was exhausted. Retain the next epoch order.
                torch.randperm(len(train_ds),generator=g)
        trainer.on_epoch_start = begin_epoch
        trainer.on_step_complete = commit_step
        trainer.on_checkpoint_committed = lambda record: print(json.dumps({'type':'trainer_checkpoint',
            'manifest_path':record['manifest_path'],'manifest_sha256':record['manifest_sha256']},ensure_ascii=False),flush=True)
    return {'trainer':trainer,'cfg':cfg,'wrapper':wrapper,'train_ds':train_ds,
            'val_ds':val_ds,'scr':scr,'optimizer':optimizer,
            'loss_diagnostics':loss_diagnostics,'trainable':trainable}
