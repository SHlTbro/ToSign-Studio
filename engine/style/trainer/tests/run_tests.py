# -*- coding: utf-8 -*-
"""S05 trainer 骨架 CPU 单测（ENGINEERING_SKELETON；无 GPU、微型假模型、合成 loader）。

GPU 纪律：本波禁止任何 GPU 运行——本套件全部 torch CPU 小张量（device="cpu"，
config 层 cuda 亦被 allow_gpu=False 拒绝，见 T5）。

覆盖（卡 S05 DoD-工程）：
 T1 走通 2 epoch：参数确有变化（前后指纹不同）/ loss 有限 / 收据与 checkpoint 齐全
 T2 checkpoint 可 resume：RNG 恢复一致、数据指针续训可解释、参数继续变化；
    换 seed/换快照 resume → 显式拒绝
 T3 NaN 注入 → E_LOSS_NONFINITE 显式失败 + 失败样本归档 + FAILED 收据（不伪报）
 T4 零梯度（可训参数不在前向路径）→ E_NO_GRADIENT；全冻结模型 → E_ZERO_TRAINABLE
 T5 config 红线 9 项（冻结必选/基座不可训/seed/预算/快照/GPU 默认拒绝/覆盖拒绝…）
 T6 身份 hash 语义：预算/停止规则延长不改变身份（可合法 resume 延长预算）

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/trainer/tests/run_tests.py
退出码：0 全过；1 有失败。收据：tests/receipts/trainer-tests-<stamp>.json
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path

PROJ = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJ))

import torch  # noqa: E402
from torch import nn  # noqa: E402
from torch.utils.data import DataLoader, TensorDataset  # noqa: E402

from engine.style.trainer import (  # noqa: E402
    ConfigError, EarlyStop, FreezePolicy, RunReceipt, Trainer, TrainerError,
    TrainConfig, hash_config, param_fingerprint)
from engine.style.trainer.config import BASE_CKPT_SHA256  # noqa: E402

FIXTURE_SRC = ("script-synthetic tiny nn.Linear adapter + torch.randn tensors in tmp dir; "
               "CPU only; no FontDiffuser weights, no GPU, no real dataset")


class AdapterTiny(nn.Module):
    """微型假模型：unet（基座替身，冻结、在前向路径上）+ adapter（唯一可训段）。"""

    def __init__(self, seed: int = 0):
        super().__init__()
        g = torch.Generator().manual_seed(seed)
        self.unet = nn.Linear(8, 8)
        self.adapter = nn.Sequential(OrderedDict([
            ("down", nn.Linear(8, 8)), ("act", nn.Tanh()), ("up", nn.Linear(8, 1))]))
        with torch.no_grad():
            for p in self.parameters():
                p.add_(torch.randn(p.shape, generator=g) * 0.1)

    def forward(self, x):
        return self.adapter(self.unet(x))


def make_loaders(n_train=16, n_val=8, bs=4, seed=1):
    g = torch.Generator().manual_seed(seed)
    xt = torch.randn(n_train, 8, generator=g)
    yt = torch.randn(n_train, 1, generator=g)
    xv = torch.randn(n_val, 8, generator=g)
    yv = torch.randn(n_val, 1, generator=g)
    return (DataLoader(TensorDataset(xt, yt), batch_size=bs, shuffle=False),
            DataLoader(TensorDataset(xv, yv), batch_size=bs, shuffle=False))


def make_config(tmp: Path, *, max_epochs=2, max_steps=999, seed=20260927, **kw):
    params = dict(
        run_name="cpu-test", run_dir=str(tmp / "run"), seed=seed,
        data_snapshot_dir=str(tmp / "snapshot"), data_snapshot_sha256="d" * 64,
        max_epochs=max_epochs, max_steps=max_steps, device="cpu",
        optimizer={"name": "adamw", "lr": 5e-3, "weight_decay": 0.0},
        stop=EarlyStop(patience=5),
    )
    params.update(kw)
    return TrainConfig(**params)


def fresh_trainer(tmp: Path, *, seed_model=0, **kw):
    cfg = make_config(tmp, **kw)
    model = AdapterTiny(seed=seed_model)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=cfg.optimizer["lr"], weight_decay=cfg.optimizer["weight_decay"])
    tl, vl = make_loaders(seed=1)
    return Trainer(cfg, model, opt, tl, vl), cfg, model


class TrainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trainer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # T1 真训练 2 epoch：参数变化 / loss 有限 / 产物齐全
    def test_t1_two_epochs_real_update(self):
        tr, cfg, model = fresh_trainer(self.tmp)
        fp_before = param_fingerprint(model)
        receipt = tr.train()
        fp_after = param_fingerprint(model)
        self.assertNotEqual(fp_before, fp_after, "2 epoch 后参数必须变化（真梯度证据）")
        self.assertEqual(receipt.final_status, "COMPLETED")
        self.assertEqual(len(receipt.epochs), 2)
        for e in receipt.epochs:
            self.assertTrue(e["train_loss"] == e["train_loss"], "train loss 有限（非 NaN）")
            self.assertTrue(abs(e["val_loss"]) != float("inf"))
        self.assertIsNotNone(receipt.best_val)
        # 产物：last/best checkpoint + receipt + checkpoints.json
        run_dir = self.tmp / "run"
        self.assertTrue((run_dir / "ckpt-last.pt").is_file())
        self.assertTrue((run_dir / "ckpt-best.pt").is_file())
        self.assertTrue((run_dir / "checkpoints.json").is_file())
        r2 = RunReceipt.load(run_dir / "receipt.json")
        self.assertEqual(r2.run_id, receipt.run_id)
        self.assertEqual(r2.config_hash, hash_config(cfg))
        self.assertEqual(r2.data_snapshot_sha256, "d" * 64)
        self.assertEqual(r2.base_ckpt_sha256, BASE_CKPT_SHA256)
        self.assertTrue(r2.code_version)
        self.assertTrue(r2.engineering_skeleton)
        self.assertFalse(r2.loaded_real_base_weights, "骨架不得宣称加载了真实基座权重")
        self.assertFalse(r2.claims_quality_pass, "收据永远不得宣称质量通过")

    # T2 resume：RNG/数据指针一致，续训状态可解释；换身份 → 拒绝
    def test_t2_resume_mid_epoch_and_mismatch(self):
        # Run A：预算 3 步 → 在 epoch1 的 batch_idx=2 执行后中断（4 batch/epoch；
        # ckpt 指针语义 = 下一个未执行 batch → {epoch:1, batch_idx:3}）
        tr_a, cfg_a, model_a = fresh_trainer(self.tmp, max_epochs=2, max_steps=3)
        tr_a.train()
        ckpt = torch.load(self.tmp / "run" / "ckpt-last.pt", map_location="cpu")
        self.assertEqual(ckpt["epoch"], 0)
        self.assertEqual(ckpt["batch_ptr"], {"epoch": 1, "batch_idx": 3})
        self.assertEqual(ckpt["global_step"], 3)
        self.assertIn("torch", ckpt["rng"])
        self.assertEqual(ckpt["data_index"]["global_step"], 3)
        fp_a = param_fingerprint(model_a)

        # Run B：新模型实例 + 预算延长（合法调度旋钮）→ mid-epoch 续训
        cfg_b = make_config(self.tmp, max_epochs=2, max_steps=999, resume=True)
        model_b = AdapterTiny(seed=99)  # 故意不同初始化：必须被 ckpt 覆盖
        opt_b = torch.optim.AdamW([p for p in model_b.parameters() if p.requires_grad], lr=5e-3)
        tl, vl = make_loaders(seed=1)  # 同序 loader（shuffle=False）
        tr_b = Trainer(cfg_b, model_b, opt_b, tl, vl,
                       resume_from=str(self.tmp / "run" / "ckpt-last.pt"))
        # RNG 恢复一致（续训状态可解释的机器判据）
        self.assertTrue(torch.equal(torch.get_rng_state(), ckpt["rng"]["torch"]))
        self.assertEqual(param_fingerprint(model_b), fp_a, "resume 后参数必须与 ckpt 一致")
        self.assertEqual(tr_b.batch_ptr, {"epoch": 1, "batch_idx": 3}, "数据指针应续接中断点")
        receipt_b = tr_b.train()
        self.assertEqual(receipt_b.final_status, "COMPLETED")
        self.assertEqual(tr_b.epoch, 2, "续训应完成 epoch1 剩余 + 整个 epoch2")
        self.assertEqual(tr_b.global_step, 8, "3（Run A）+ 1 + 4（续）步")
        self.assertNotEqual(param_fingerprint(model_b), fp_a, "续训参数必须继续变化")
        self.assertEqual(tr_b.batch_ptr, {"epoch": 2, "batch_idx": 0})
        self.assertEqual(len(receipt_b.epochs), 2)
        # 换 seed resume → 显式拒绝（身份不匹配）
        cfg_c = make_config(self.tmp / "c", max_epochs=2, max_steps=999, seed=42, resume=True)
        (self.tmp / "c" / "run").mkdir(parents=True, exist_ok=True)
        model_c = AdapterTiny()
        opt_c = torch.optim.AdamW([p for p in model_c.parameters() if p.requires_grad], lr=5e-3)
        tl2, vl2 = make_loaders(seed=1)
        with self.assertRaises(TrainerError) as cm:
            Trainer(cfg_c, model_c, opt_c, tl2, vl2,
                    resume_from=str(self.tmp / "run" / "ckpt-last.pt"))
        self.assertEqual(cm.exception.code, "E_RESUME_IDENTITY_MISMATCH")
        # 换数据快照 resume → 显式拒绝
        cfg_d = make_config(self.tmp / "d", max_epochs=2, max_steps=999,
                            data_snapshot_sha256="e" * 64, resume=True)
        (self.tmp / "d" / "run").mkdir(parents=True, exist_ok=True)
        model_d = AdapterTiny()
        opt_d = torch.optim.AdamW([p for p in model_d.parameters() if p.requires_grad], lr=5e-3)
        tl3, vl3 = make_loaders(seed=1)
        with self.assertRaises(TrainerError) as cm2:
            Trainer(cfg_d, model_d, opt_d, tl3, vl3,
                    resume_from=str(self.tmp / "run" / "ckpt-last.pt"))
        self.assertEqual(cm2.exception.code, "E_RESUME_IDENTITY_MISMATCH")
        # 篡改 checkpoint → 验签拒绝（不可信外来文件不加载）
        reg = json.loads((self.tmp / "run" / "checkpoints.json").read_bytes())
        ck_path = self.tmp / "run" / "ckpt-last.pt"
        unlock = ck_path.read_bytes()
        ck_path.write_bytes(unlock + b"x")
        model_e = AdapterTiny()
        opt_e = torch.optim.AdamW([p for p in model_e.parameters() if p.requires_grad], lr=5e-3)
        tl4, vl4 = make_loaders(seed=1)
        cfg_e = make_config(self.tmp, max_epochs=2, max_steps=999, resume=True)
        with self.assertRaises(TrainerError) as cm3:
            Trainer(cfg_e, model_e, opt_e, tl4, vl4,
                    resume_from=str(ck_path))
        self.assertEqual(cm3.exception.code, "E_CKPT_TAMPERED")

    # T3 NaN 注入 → 显式失败 + 归档 + FAILED 收据
    def test_t3_nan_loss_explicit_failure(self):
        tr, cfg, model = fresh_trainer(self.tmp, max_epochs=2, max_steps=999)
        fp_before = param_fingerprint(model)

        def nan_loss(out, y):
            return (out - y).square().mean() * 0.0 + float("nan")

        tr.loss_fn = nan_loss
        with self.assertRaises(TrainerError) as cm:
            tr.train()
        self.assertEqual(cm.exception.code, "E_LOSS_NONFINITE")
        self.assertEqual(tr.receipt.final_status, "FAILED")
        self.assertEqual(tr.receipt.failure_code, "E_LOSS_NONFINITE")
        self.assertTrue((self.tmp / "run" / "receipt.json").is_file())
        archived = list((self.tmp / "run" / "failed_samples").glob("*.json"))
        self.assertEqual(len(archived), 1, "失败 batch 必须归档")
        rec = json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["code"], "E_LOSS_NONFINITE")
        self.assertIn("batch_summary", rec)
        self.assertEqual(param_fingerprint(model), fp_before,
                         "NaN 步不得产生参数更新")

    # T4 零梯度防伪报
    def test_t4_zero_gradient_rejected(self):
        # 4a：可训参数存在但不在前向路径 → 梯度断流
        cfg = make_config(self.tmp, max_epochs=1, max_steps=99)
        model = AdapterTiny()
        # 把 adapter 换成"死参数"：前向只走 unet（输出降维到 [B,1] 避免 mse 广播）
        class DeadAdapter(nn.Module):
            def __init__(self, m):
                super().__init__()
                self.unet = m.unet
                self.adapter = m.adapter

            def forward(self, x):
                return self.unet(x).mean(dim=1, keepdim=True)  # adapter 不在前向路径
        dead = DeadAdapter(model)
        opt = torch.optim.AdamW([p for p in dead.parameters() if p.requires_grad], lr=5e-3)
        tl, vl = make_loaders(seed=2)
        tr = Trainer(cfg, dead, opt, tl, vl)
        with self.assertRaises(TrainerError) as cm:
            tr.train()
        self.assertEqual(cm.exception.code, "E_NO_GRADIENT")
        self.assertEqual(tr.receipt.final_status, "FAILED")
        # 4b：模型不含任何匹配 trainable_prefixes 的参数 → E_ZERO_TRAINABLE
        class BaseOnly(nn.Module):
            def __init__(self):
                super().__init__()
                self.unet = nn.Linear(8, 8)

            def forward(self, x):
                return self.unet(x).mean(dim=1, keepdim=True)
        base_only = BaseOnly()
        cfg2 = make_config(self.tmp / "fb", max_epochs=1, max_steps=9)
        dummy = nn.Parameter(torch.zeros(1))  # optimizer 需非空参数表；Trainer 会先拒
        with self.assertRaises(ConfigError) as cm2:
            Trainer(cfg2, base_only, torch.optim.AdamW([dummy]), *make_loaders(seed=2))
        self.assertEqual(cm2.exception.code, "E_ZERO_TRAINABLE")

    # T5 config 红线（卡 S05：缺一不可）
    def test_t5_config_red_lines(self):
        def snap_cfg(**kw):
            return make_config(self.tmp / "t5", **kw)
        with self.assertRaises(ConfigError) as c:  # 冻结不可关
            FreezePolicy(freeze_base=False)
        self.assertEqual(c.exception.code, "E_FREEZE_REQUIRED")
        with self.assertRaises(ConfigError) as c:  # 可训面不得触及基座
            FreezePolicy(trainable_prefixes=("unet.",))
        self.assertEqual(c.exception.code, "E_TRAINABLE_HITS_BASE")
        with self.assertRaises(ConfigError) as c:  # 可训面不得为空
            FreezePolicy(trainable_prefixes=())
        self.assertEqual(c.exception.code, "E_NO_TRAINABLE_SCOPE")
        with self.assertRaises(ConfigError) as c:  # 冻结标识不可改
            FreezePolicy(source_type="OTHER")
        self.assertEqual(c.exception.code, "E_SOURCE_TYPE_FROZEN")
        with self.assertRaises(ConfigError) as c:
            snap_cfg(seed=-1)
        self.assertEqual(c.exception.code, "E_SEED_REQUIRED")
        with self.assertRaises(ConfigError) as c:
            snap_cfg(max_epochs=0)
        self.assertEqual(c.exception.code, "E_BUDGET_REQUIRED")
        with self.assertRaises(ConfigError) as c:
            TrainConfig(run_dir=str(self.tmp / "t5b"), seed=1,
                        max_epochs=1, max_steps=1)  # 缺快照绑定
        self.assertEqual(c.exception.code, "E_SNAPSHOT_REQUIRED")
        with self.assertRaises(ConfigError) as c:
            snap_cfg(device="cuda:0")
        self.assertEqual(c.exception.code, "E_GPU_NOT_ALLOWED")
        with self.assertRaises(ConfigError) as c:
            snap_cfg(engine_id="some-other-engine")
        self.assertEqual(c.exception.code, "E_ENGINE_ID")
        # 新 run 指向非空 run_dir → 拒绝覆盖
        occupied = self.tmp / "occupied"
        (occupied / "run").mkdir(parents=True)
        (occupied / "run" / "some-file").write_text("x", encoding="ascii")
        with self.assertRaises(ConfigError) as c:
            TrainConfig(run_dir=str(occupied / "run"), seed=1,
                        data_snapshot_dir="/x", data_snapshot_sha256="d" * 64,
                        max_epochs=1, max_steps=1)

    # T6 冻结语义 + 身份 hash 语义
    def test_t6_freeze_apply_and_identity_hash(self):
        model = AdapterTiny()
        fp = FreezePolicy()
        trainable, frozen = fp.apply(model)
        self.assertTrue(all(n.startswith("adapter.") for n in trainable), trainable)
        self.assertIn("unet.weight", frozen)
        self.assertFalse(model.unet.weight.requires_grad)
        self.assertTrue(model.adapter.down.weight.requires_grad)
        # 预算/停止规则延长不改变身份 hash（可合法 resume 延长预算）
        cfg_a = make_config(self.tmp, max_epochs=2, max_steps=10)
        cfg_b = make_config(self.tmp, max_epochs=99, max_steps=100000)
        self.assertEqual(hash_config(cfg_a), hash_config(cfg_b))
        # 换优化器/换 seed → 身份必变
        cfg_c = make_config(self.tmp, seed=7)
        cfg_d = make_config(self.tmp, optimizer={"name": "adamw", "lr": 9e-3, "weight_decay": 0.0})
        self.assertNotEqual(hash_config(cfg_a), hash_config(cfg_c))
        self.assertNotEqual(hash_config(cfg_a), hash_config(cfg_d))


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TrainerTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    cases = []
    for t, err in getattr(result, "failures", []) + getattr(result, "errors", []):
        cases.append({"test": t.id(), "ok": False, "detail": err})
    passed = result.testsRun - len(cases)
    receipt = {
        "module": "engine/style/trainer",
        "task": "S05 M2 训练骨架（ENGINEERING_SKELETON，不跑 GPU、不加载基座权重）",
        "command": ".venv-ml/Scripts/python.exe -X utf8 engine/style/trainer/tests/run_tests.py",
        "interpreter": sys.executable,
        "started": stamp,
        "fixture_source": FIXTURE_SRC,
        "gpu_used": False,
        "device": "cpu",
        "tests_total": result.testsRun,
        "passed": passed,
        "failed": len(cases),
        "exit": 0 if result.wasSuccessful() else 1,
        "cases": cases,
        "boundary": "训练骨架可跑通 ≠ 真实训练开始 ≠ 质量达标；真实训练待 S03 数据快照 + Owner 预算预注册 + GPU 波次",
    }
    out_dir = Path(__file__).resolve().parent / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"trainer-tests-{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[trainer-tests] {passed}/{result.testsRun} passed, exit={receipt['exit']}, "
          f"receipt={out_dir / f'trainer-tests-{stamp}.json'}")
    return receipt["exit"]


if __name__ == "__main__":
    sys.exit(main())
