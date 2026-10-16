# -*- coding: utf-8 -*-
"""S03 dataset 反例/正例测试（纯 CPU，.venv-ml，无 GPU、无 torch 依赖）。

夹具来源（receipt 登记）：程序合成 manifest 行 + 程序合成 PNG（tmp 目录，PIL 伪随机像素）。
不读 materials 真图、不触 B7/B9 SEALED 图像；仅**只读**消费 R04 台账 JSON 及其 JSON 源清单。

覆盖（卡 S03 DoD：正例可复算；四类注入负例全部真实失败；禁入台账真实消费）：
 T1 正例构建/复算 index/重建同 hash/父组不跨侧
 T2 真实 R04 台账消费证明 + 真实 B10 sha 注入必须被拒
 T3 合成台账 eval-only/SEALED/v03r1/UNKNOWN 注入 + provenance 放行
 T4 hash 缺失 / 内容 hash 不匹配 / alpha PNG 缺失
 T5 跨用户（style_id）混池拒绝
 T6 近重复 family 不跨 split（多 seed）+ 篡改 split.json → FAIL
 T7 数据门不足 → INSUFFICIENT_DATA 结构化，不产快照、不凑数
 T8 快照不可变：篡改 PNG/清单/删 FROZEN/重复构建/台账变更 全部检出
 T9 重复 alpha（重命名逃逸 exact hash）→ E_DUPLICATE_ALPHA

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/dataset/tests/run_tests.py
退出码：0 全过；1 有失败。收据：tests/receipts/dataset-tests-<stamp>.json
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJ = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJ))

from PIL import Image  # noqa: E402

from engine.style.dataset import (  # noqa: E402
    DatasetError, LedgerIndex, SplitConfig, build_snapshot, default_ledger_path,
    load_snapshot, plan_splits, unlock_snapshot, validate_snapshot, verify_snapshot)
from engine.style.dataset.ledger import sha256_bytes  # noqa: E402

FIXTURE_SRC = "script-synthesized manifest rows + PIL pseudo-random PNGs in tmp dir (no materials, no B7/B9 images)"


# ---------------------------------------------------------------- fixtures
def synth_png(directory: Path, name: str, seed: int):
    rnd = random.Random(seed)
    img = Image.frombytes("L", (32, 32), bytes(rnd.randrange(256) for _ in range(32 * 32)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    p = directory / f"{name}.png"
    p.write_bytes(data)
    return p, sha256_bytes(data)


def make_row(sid, parent_sha, alpha_sha, alpha_path, *, label="星", cp=0x661F,
             style="blogA", origin="user_upload_confirmed"):
    row = {
        "sample_id": sid, "style_id": style, "label": label, "unicode_cp": cp,
        "parent_source_hash": parent_sha, "final_alpha_sha256": alpha_sha,
        "review_status": "owner_confirmed", "prep_version": "charprep/1.0.0",
        "source_bbox": {"x": 0, "y": 0, "w": 16, "h": 16, "coord_space": "pixel:parent_image"},
        "parent_width": 32, "parent_height": 32,
        "created_at": "2026-09-27T00:00:00+08:00",
        "alpha_path": str(alpha_path),
    }
    if origin:
        row["parent_origin"] = origin
    return row


def synth_ledger(path: Path) -> Path:
    """合成台账：schema 与 R04 data-usage-ledger-draft.json 同构（禁入组+家族+v03r1）。"""
    ledger = {
        "ledger_id": "synthetic-test-ledger",
        "generated_date": "2026-09-27",
        "purpose": "S03 test fixture: schema-identical to R04 draft ledger",
        "global_rules": {"unknown_history": "any unlisted image: history_status=UNKNOWN"},
        "sources": [],
        "v03r1_training_parent_hashes": {"n": 1, "note": "prior trained base (test)",
                                          "sha256": ["f" * 64]},
        "groups": [
            {"group_id": "T_EVAL_ONLY", "n": 2, "role": "eval_only",
             "training_allowed": False, "history_status": "UNKNOWN_UNLESS_LEDGER_SAYS",
             "items": [
                 {"rgt_id": "RGT-X1", "filename": "aaaa1111deadbeef.jpg", "sha256": "1" * 64},
                 {"rgt_id": "RGT-X2", "filename": "dddd4444cafebabe.jpg", "sha256": "2" * 64},
             ]},
            {"group_id": "T_SEALED", "n": 1, "role": "sealed_no_touch",
             "training_allowed": False, "history_status": "PRIOR_SEALED",
             "items": [{"benchmark_id": "SEAL-X", "path": "x/original.png", "sha256": "3" * 64}]},
            {"group_id": "T_UNASSIGNED", "n": 1, "role": "unassigned_candidate",
             "training_allowed": None, "history_status": "UNKNOWN_UNLESS_LEDGER_SAYS",
             "sub_groups": [{"sub_group_id": "NEAR_DUP_PRUNED", "n": 1,
                             "items": [{"filename": "cccc3333.jpg", "sha256": "4" * 64,
                                        "near_duplicate_of": "dddd4444"}]}]},
            {"group_id": "T_ALLOWED_NEW", "n": 2, "role": "confirmed_new_data",
             "training_allowed": True, "history_status": "OWNER_CONFIRMED_NEW",
             "items": [
                 {"benchmark_id": "NEW-A", "filename": "eeee5555aabbccdd.jpg",
                  "sha256": "a" * 64, "near_duplicate_of": "ffff6666"},
                 {"benchmark_id": "NEW-B", "filename": "ffff666611223344.jpg", "sha256": "b" * 64},
             ]},
        ],
    }
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------- tests
class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dataset-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = LedgerIndex.load(synth_ledger(self.tmp / "ledger.json"))
        # 三张"用户上传"父图（不在台账，带 provenance）+ 一张 alpha PNG/样本
        self.png_dir = self.tmp / "alphas"
        self.png_dir.mkdir()
        self.parents = {f"P{i}": sha256_bytes(f"parent-image-{i}".encode())
                        for i in (1, 2, 3)}
        self.rows = []
        labels = [("星", 0x661F), ("河", 0x6CB3), ("山", 0x5C71), ("云", 0x4E91)]
        plan = [("P1", 4), ("P2", 3), ("P3", 2)]  # 9 样本 / 3 父组
        n = 0
        for pname, count in plan:
            for k in range(count):
                n += 1
                label, cp = labels[k % 4]
                p, sha = synth_png(self.png_dir, f"s{n:02d}", seed=100 + n)
                self.rows.append(make_row(
                    f"ss_{n:06d}", self.parents[pname], sha, p, label=label, cp=cp))
        self.gates = dict(min_train_samples=4, min_val_samples=2, min_train_groups=2,
                          min_val_groups=1, min_distinct_labels=4, min_distinct_parents=3)

    def cfg(self, **kw):
        params = dict(train_ratio=0.7, seed=20260927)
        params.update(self.gates)
        params.update(kw)
        return SplitConfig(**params)

    # T1 正例：可复算 index；重建同内容 hash；父组不跨侧
    def test_t1_positive_recomputable_index(self):
        out = self.tmp / "snap1"
        res = build_snapshot(self.rows, out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(res.status, "OK", res.insufficient_reasons)
        v = validate_snapshot(out, ledger=self.ledger, gates=self.cfg())
        self.assertEqual(v.status, "OK", [f.code for f in v.failures])
        # index 可复算：重建（同输入同 seed）必须得到同一 content hash
        res2 = build_snapshot(self.rows, self.tmp / "snap2", ledger=self.ledger,
                              split_config=self.cfg())
        self.assertEqual(res.snapshot_sha256, res2.snapshot_sha256, "同输入重建必须同快照 hash")
        # created_at 秒级精度不参与内容 hash（同秒构建时间戳相同亦不破坏可复算性）
        self.assertNotIn(res.index["created_at"], res.index["content_sha256"])
        # 同源不跨侧：同 parent 的样本同侧
        rows, index, splits = load_snapshot(out)
        self.assertTrue(splits)
        by_parent = {}
        for r in rows:
            by_parent.setdefault(r["parent_source_hash"], set()).add(splits[r["sample_id"]])
        for psha, sides in by_parent.items():
            self.assertEqual(len(sides), 1, f"parent {psha[:8]} straddles {sides}")
        # 快照内 PNG 与台账外来源声明齐全
        self.assertEqual(index["n_samples"], 9)
        self.assertEqual(index["style_id"], "blogA")

    # T2 真实 R04 台账消费 + 真实 B10 sha 注入必须被拒
    def test_t2_real_ledger_consumption_and_b10_injection(self):
        real = LedgerIndex.load(default_ledger_path(PROJ))
        d = real.describe()
        self.assertEqual(d["n_groups"], 8, f"R04 台账应有 8 组: {d}")
        self.assertGreaterEqual(d["n_forbidden_hashes"], 110)
        self.assertEqual(d["n_families"], 4, f"近重复家族应有 4 组: {d['family_sizes']}")
        v03 = set(real.data["v03r1_training_parent_hashes"]["sha256"])
        self.assertLessEqual(v03, set(real.forbidden_hashes), "38 个 v03r1 底图必须全禁入")
        # 台账 JSON 源清单真实复核（只读 .json 元数据，不读任何图像）
        checks = real.verify_sources(PROJ)
        self.assertTrue(all(c["exists"] for c in checks), "台账 sources 应全部在盘")
        self.assertTrue(all(c["sha_match"] for c in checks), "台账 sources sha 应全部吻合")
        # 注入 1：真实 B10 Slice A eval-only sha（RGT-001）作为父 hash
        rgt1 = real.data["groups"][0]["items"][0]["sha256"]
        p, sha = synth_png(self.png_dir, "inject_b10", seed=7)
        row = make_row("ss_b10inject", rgt1, sha, p)
        with self.assertRaises(DatasetError) as cm:
            build_snapshot([row], self.tmp / "snap_b10", ledger=real, split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_FORBIDDEN_PARENT")
        # 注入 2：真实 SEALED sha（B9-cos_001）同样被拒（计划器路径返回结构化 FAIL）
        sealed = real.data["groups"][-1]["items"][0]["sha256"]
        row2 = make_row("ss_sealinject", sealed, sha, p)
        plan2 = plan_splits([row2], real, self.cfg())
        self.assertEqual(plan2.status, "FAIL")
        self.assertTrue(any("E_FORBIDDEN_PARENT" in r for r in plan2.reasons), plan2.reasons)
        # 注入 3：把快照 ledger 指向真实台账后，冻结快照里若混入 v03r1 底图亦被拒
        v03_sha = sorted(v03)[0]
        row3 = make_row("ss_v03inject", v03_sha, sha, p)
        with self.assertRaises(DatasetError) as cm3:
            build_snapshot([row3], self.tmp / "snap_v03", ledger=real, split_config=self.cfg())
        self.assertEqual(cm3.exception.code, "E_FORBIDDEN_PARENT")

    # T3 合成台账：eval-only / SEALED / v03r1 / UNKNOWN 注入；provenance 放行
    def test_t3_synthetic_ledger_injections_and_provenance(self):
        p, sha = synth_png(self.png_dir, "inj", seed=8)
        for bad_sha, tag in (("1" * 64, "eval_only"), ("3" * 64, "sealed"),
                             ("f" * 64, "v03r1"), ("4" * 64, "unknown_family")):
            row = make_row(f"ss_bad_{tag}", bad_sha, sha, p)
            with self.assertRaises(DatasetError, msg=tag) as cm:
                build_snapshot([row], self.tmp / f"snap_{tag}", ledger=self.ledger,
                               split_config=self.cfg())
            self.assertEqual(cm.exception.code, "E_FORBIDDEN_PARENT", tag)
        # 未列父 hash 且无 provenance → UNKNOWN 禁入（无 hash 碰撞 ≠ NEVER_USED）
        unknown_parent = sha256_bytes(b"never-seen-image")
        row = make_row("ss_unknown", unknown_parent, sha, p, origin=None)
        with self.assertRaises(DatasetError) as cm:
            build_snapshot([row], self.tmp / "snap_unknown", ledger=self.ledger,
                           split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_UNKNOWN_PARENT")
        # 带 user_upload_confirmed 声明 → 放行（进入正常数据门评估）
        row_ok = make_row("ss_prov", unknown_parent, sha, p)  # origin=user_upload_confirmed
        out = self.tmp / "snap_prov"
        res = build_snapshot([row_ok], out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(res.status, "INSUFFICIENT_DATA")  # 只有 1 样本：门不足而非报错
        # 严格模式（reject）：即使有 provenance 声明，台账未列父 hash 也一律禁入
        with self.assertRaises(DatasetError) as cm_strict:
            build_snapshot([row_ok], self.tmp / "snap_strict", ledger=self.ledger,
                           split_config=self.cfg(unknown_parent_policy="reject"))
        self.assertEqual(cm_strict.exception.code, "E_UNKNOWN_PARENT")

    # T4 hash 缺失 / 内容 hash 不匹配 / PNG 缺失
    def test_t4_hash_missing_and_mismatch(self):
        base = dict(self.rows[0])
        bad = dict(base)
        bad.pop("final_alpha_sha256")
        with self.assertRaises(DatasetError) as cm:
            build_snapshot([bad], self.tmp / "s4a", ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_ROW_MISSING_FIELD")
        wrong = dict(base, final_alpha_sha256="9" * 64)
        with self.assertRaises(DatasetError) as cm2:
            build_snapshot([wrong], self.tmp / "s4b", ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(cm2.exception.code, "E_ALPHA_HASH_MISMATCH")
        ghost = make_row("ss_ghost", self.parents["P1"], "0" * 64,
                         self.tmp / "ghost_missing.png")
        with self.assertRaises(DatasetError) as cm3:
            build_snapshot([ghost], self.tmp / "s4c", ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(cm3.exception.code, "E_ALPHA_MISSING")

    # T5 跨用户/风格混池
    def test_t5_cross_user_rejected(self):
        p, sha = synth_png(self.png_dir, "other_user", seed=9)
        rows = list(self.rows) + [make_row("ss_other", sha256_bytes(b"pu-x"), sha, p,
                                           style="blogB")]
        with self.assertRaises(DatasetError) as cm:
            build_snapshot(rows, self.tmp / "s5", ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_CROSS_USER")

    # T6 近重复 family 不跨 split（多 seed）+ 篡改 split.json 必失败
    def test_t6_family_never_straddles(self):
        # 家族对：台账 T_ALLOWED_NEW 中 a*64(NEW-A, near_duplicate_of ffff6666) 与 b*64(NEW-B)
        fam_rows = []
        n = 0
        for parent_sha, count in (("a" * 64, 2), ("b" * 64, 1)):
            for _ in range(count):
                n += 1
                p, sha = synth_png(self.png_dir, f"fam{n}", seed=200 + n)
                fam_rows.append(make_row(f"ss_fam_{n:02d}", parent_sha, sha, p))
        pool = self.rows + fam_rows  # 12 样本；家族 3 样本必须同侧
        for seed in range(20):
            plan = plan_splits(pool, self.ledger, self.cfg(seed=seed))
            self.assertEqual(plan.status, "OK", f"seed={seed}: {plan.reasons}")
            sides = {plan.assignments[r["sample_id"]] for r in fam_rows}
            self.assertEqual(len(sides), 1, f"seed={seed}: family straddles {sides}")
        # 篡改：手工把家族成员劈到两侧写进 split.json → validator 必须真实失败
        out = self.tmp / "snap_tamper"
        res = build_snapshot(pool, out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(res.status, "OK")
        unlock_snapshot(out)
        sp = out / "split.json"
        payload = json.loads(sp.read_text(encoding="utf-8"))
        a_id = fam_rows[0]["sample_id"]
        b_id = fam_rows[2]["sample_id"]
        other = "val" if payload["assignments"][a_id] == "train" else "train"
        payload["assignments"][b_id] = other
        sp.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")) + "\n", encoding="utf-8")
        v = validate_snapshot(out, ledger=self.ledger)
        self.assertEqual(v.status, "FAIL")
        codes = {f.code for f in v.failures}
        self.assertIn("E_FAMILY_CROSS_SPLIT", codes, codes)
        self.assertIn("E_INDEX_MISMATCH", codes, "篡改 split.json 必须同时被 content hash 抓住")

    # T7 数据门不足 → INSUFFICIENT_DATA 结构化；不产快照、不凑数
    def test_t7_insufficient_data(self):
        small = self.rows[:3]  # 单父组 → val 必空
        out = self.tmp / "snap_insufficient"
        res = build_snapshot(small, out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(res.status, "INSUFFICIENT_DATA")
        self.assertTrue(res.insufficient_reasons, "必须给出结构化 reasons")
        self.assertFalse(out.exists(), "INSUFFICIENT_DATA 不得产出任何快照目录")
        plan = plan_splits(small, self.ledger, self.cfg())
        self.assertEqual(plan.status, "INSUFFICIENT_DATA")
        self.assertTrue(any("val" in r for r in plan.reasons), plan.reasons)

    # T8 快照不可变
    def test_t8_immutability(self):
        out = self.tmp / "snap8"
        res = build_snapshot(self.rows, out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(res.status, "OK")
        # 8a 重复构建拒绝
        with self.assertRaises(DatasetError) as cm:
            build_snapshot(self.rows, out, ledger=self.ledger, split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_SNAPSHOT_EXISTS")
        # 8b 篡改 PNG → 文件 hash 失配
        unlock_snapshot(out)
        png = out / "samples" / f"{self.rows[0]['sample_id']}.png"
        png.write_bytes(png.read_bytes() + b"\x00")
        v = validate_snapshot(out, ledger=self.ledger)
        self.assertEqual(v.status, "FAIL")
        self.assertIn("E_FILE_HASH_MISMATCH", {f.code for f in v.failures})
        # 8c 删 FROZEN 标记
        png.write_bytes(b"")  # 恢复破坏面统一：直接重建
        shutil.rmtree(out)
        build_snapshot(self.rows, out, ledger=self.ledger, split_config=self.cfg())
        unlock_snapshot(out)
        (out / "FROZEN").unlink()
        v2 = verify_snapshot(out, ledger=self.ledger)
        self.assertIn("E_NOT_FROZEN", {f.code for f in v2.failures})
        # 8d 台账变更检测（换合成台账文件内容 → sha 变）
        shutil.rmtree(out)
        build_snapshot(self.rows, out, ledger=self.ledger, split_config=self.cfg())
        other_ledger_path = self.tmp / "ledger_v2.json"
        ledger2 = json.loads((self.tmp / "ledger.json").read_text(encoding="utf-8"))
        ledger2["generated_date"] = "2026-09-28"
        other_ledger_path.write_text(json.dumps(ledger2, ensure_ascii=False), encoding="utf-8")
        v3 = validate_snapshot(out, ledger=LedgerIndex.load(other_ledger_path))
        self.assertEqual(v3.status, "FAIL")
        self.assertIn("E_LEDGER_CHANGED", {f.code for f in v3.failures})

    # T9 重复 alpha（重命名逃逸）→ 内容 hash 去重拒绝
    def test_t9_duplicate_alpha_rejected(self):
        first = self.rows[0]
        # 同一份 alpha PNG 换 sample_id/新父图重投（重命名逃逸 exact hash 的典型手法）
        dup = make_row("ss_dup", sha256_bytes(b"new-parent-x"),
                       first["final_alpha_sha256"], first["alpha_path"])
        with self.assertRaises(DatasetError) as cm:
            build_snapshot(self.rows + [dup], self.tmp / "s9", ledger=self.ledger,
                           split_config=self.cfg())
        self.assertEqual(cm.exception.code, "E_DUPLICATE_ALPHA")


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DatasetTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    cases = []
    for t, err in getattr(result, "failures", []) + getattr(result, "errors", []):
        cases.append({"test": t.id(), "ok": False, "detail": err})
    passed = result.testsRun - len(cases)
    receipt = {
        "module": "engine/style/dataset",
        "task": "S03 训练集导出/同源分组/不可变快照",
        "command": ".venv-ml/Scripts/python.exe -X utf8 engine/style/dataset/tests/run_tests.py",
        "interpreter": sys.executable,
        "started": stamp,
        "fixture_source": FIXTURE_SRC,
        "gpu_used": False,
        "tests_total": result.testsRun,
        "passed": passed,
        "failed": len(cases),
        "exit": 0 if result.wasSuccessful() else 1,
        "cases": cases,
        "data_redline": "B10 eval_only+reserve、B7/B9 SEALED、v03r1 prior-train 全部经 R04 台账机器拒绝；"
                        "测试仅只读台账 JSON，不触任何被封存图像",
    }
    out_dir = Path(__file__).resolve().parent / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"dataset-tests-{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[dataset-tests] {passed}/{result.testsRun} passed, exit={receipt['exit']}, "
          f"receipt={out_dir / f'dataset-tests-{stamp}.json'}")
    return receipt["exit"]


if __name__ == "__main__":
    sys.exit(main())
