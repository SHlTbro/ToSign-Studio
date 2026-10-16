# -*- coding: utf-8 -*-
"""S08 evaluator 反例/夹具测试（纯 CPU，.venv 即可；不触 GPU、不用真实数据集）。

夹具来源（receipt 登记）：全部程序合成（PIL 画横条/方框/对角线假字模），不触
materials/b10-realgt、B7/B9 SEALED、charprep manifest、S03 真实快照。

覆盖（卡 S08 风险/DoD）：
 T0 正控：完美生成器 → 2/2 pass_proxy（防评估器把一切都判败的反向缺陷）
 T1 空白输出 → blank 失败，不假通过（卡"空白被高背景 accuracy 奖励"）
 T2 整块填满 → full_fill 失败
 T3 错字冒充（要 甲 给 乙 的形）→ wrong_char_proxy（结构 proxy，非 OCR）
 T4 透明层：全不透明底泄漏 / alpha=0 幽灵图 → transparency_broken；
    生成器异常 → gen_error 入账不假通过
 T5 空 holdout → INSUFFICIENT_EVAL_SET 显式（loader 拒 + API 层收据），不凑数
 T6 收据篡改（指标/文件 hash/删文件）→ verify_receipt 复算失败
 T7 参考字泄漏（ref label ∈ holdout labels）→ E_REF_LEAKAGE 拒绝
 T8 GPU 锁：占用→等待 0 圈即 None；陈旧锁（死 pid）→摘除；误删他人锁→拒

运行：.venv/Scripts/python.exe -X utf8 engine/style/evaluator/tests/run_tests.py
退出码：0 全过；1 有失败。收据：tests/receipts/evaluator-tests-<stamp>.json
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

PROJ = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJ))

from PIL import Image, ImageDraw  # noqa: E402

from engine.style.evaluator import run_eval as RE  # noqa: E402
from engine.style.evaluator import gpu_lock  # noqa: E402
from engine.style.evaluator import metrics as M  # noqa: E402
from engine.style.evaluator.receipt import (  # noqa: E402
    STATUS_INSUFFICIENT, STATUS_OK, load_receipt, verify_receipt)

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
OUT = TESTS_DIR / "out"
FIXTURE_SRC = "script-synthesized (PIL bar/box/diagonal pseudo-glyphs); no B10/B7/B9, no real dataset"


# ---------------------------------------------------------------- 夹具绘制

def draw_bar() -> Image.Image:
    """假字"甲"：横条（ink≈0.047，质心 (0.5,0.125)，连通域 1）。"""
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((16, 12, 111, 19), fill=(0, 0, 0))
    return img


def draw_box() -> Image.Image:
    """假字"乙"：空心方框（ink≈0.172，质心 (0.5,0.5)，连通域 1）。"""
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((16, 16, 111, 111), fill=(0, 0, 0))
    d.rectangle((24, 24, 103, 103), fill=(255, 255, 255))
    return img


def draw_diag() -> Image.Image:
    """假 ref"丙"：对角线。"""
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.line((16, 16, 111, 111), fill=(0, 0, 0), width=6)
    return img


def rgba_from(img: Image.Image, alpha_value: int | None = None) -> Image.Image:
    rgba = img.convert("RGBA")
    if alpha_value is None:
        return rgba
    a = rgba.split()[3].point(lambda v: alpha_value)
    return Image.merge("RGBA", (*rgba.convert("RGB").split(), a))


TARGETS = {"甲": draw_bar, "乙": draw_box}


def make_manifest(name: str, items: list[dict], ref_label: str = "丙") -> Path:
    """mini holdout manifest（收据如实标注 mini_holdout，不冒充 S03 快照）。"""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    ref_png = FIXTURES / "ref_丙.png"
    if not ref_png.is_file():
        draw_diag().save(ref_png)
    man = {"style_ref": str(ref_png), "style_ref_label": ref_label, "items": items}
    p = FIXTURES / f"{name}.json"
    p.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def default_items() -> list[dict]:
    out = []
    for label, fn in TARGETS.items():
        png = FIXTURES / f"target_{label}.png"
        if not png.is_file():
            fn().save(png)
        out.append({"label": label, "target_png": str(png),
                    "sample_id": f"fix:{label}"})
    return out


# ---------------------------------------------------------------- 假生成句柄

def _gen_receipt(model: str = "fixture/1.0") -> dict:
    return {"engine_id": "fixture-gen", "engine_mode": "FIXTURE",
            "model_version": model, "runner_version": "fixture/1.0",
            "inputs": {}, "timings": {}, "vram": {}, "output": {}}


def good_gen(ref, ch, seed, out_png):
    return TARGETS[ch]().copy(), _gen_receipt()


def blank_gen(ref, ch, seed, out_png):
    return Image.new("RGBA", (96, 96), (255, 255, 255, 255)), _gen_receipt()


def full_gen(ref, ch, seed, out_png):
    return Image.new("RGBA", (96, 96), (0, 0, 0, 255)), _gen_receipt()


def cheat_gen(ref, ch, seed, out_png):
    """错字冒充：要 甲 给 乙 的形，要 乙 给 甲 的形（输出顶着请求的 label 落盘）。"""
    other = draw_box if ch == "甲" else draw_bar
    return other().convert("RGBA"), _gen_receipt()


def opaque_leak_gen(ref, ch, seed, out_png):
    """背景泄漏：整图 alpha=255（含白底）——render 会把方形底全画出来。"""
    return rgba_from(TARGETS[ch](), alpha_value=255), _gen_receipt()


def ghost_gen(ref, ch, seed, out_png):
    """幽灵图：RGB 有墨、alpha 全 0——合成后全透明（空白 + RGB-alpha 不一致）。"""
    return rgba_from(TARGETS[ch](), alpha_value=0), _gen_receipt()


def dead_gen(ref, ch, seed, out_png):
    raise RuntimeError("[E_SAMPLE_FAILED] fixture engine exploded")


def run_fixture(gen, name: str, items=None):
    items = default_items() if items is None else items
    refs = [{"label": "丙", "png": FIXTURES / "ref_丙.png", "sample_id": "fix:ref",
             "final_alpha_sha256": None}]
    meta = {"kind": "mini_holdout", "holdout_labels": sorted(i["label"] for i in items)}
    return RE.run_eval(gen, items, refs, meta, out_dir=OUT / name, seed=42,
                       device="cpu", engine_name="fixture",
                       stamp=time.strftime("%Y%m%d_%H%M%S"))


# ---------------------------------------------------------------- 测试体

class EvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        FIXTURES.mkdir(parents=True, exist_ok=True)
        if not (FIXTURES / "ref_丙.png").is_file():
            draw_diag().save(FIXTURES / "ref_丙.png")
        default_items()

    def _receipt_of(self, name: str) -> dict:
        dirs = sorted((OUT / name).glob("eval_*/receipt.json"))
        self.assertTrue(dirs, f"no receipt under {OUT / name}")
        return load_receipt(dirs[-1])

    def test_T0_positive_control(self):
        status, rc = run_fixture(good_gen, "t0_good")
        self.assertEqual(status, STATUS_OK)
        self.assertEqual(rc["summary"]["n_generated"], 2)
        self.assertEqual(rc["summary"]["n_pass_proxy"], 2)
        self.assertEqual(rc["verdict"], None)  # 恒 None：verdict 属 S09
        self.assertEqual(rc["per_char"][0]["fail_classes"], [])
        ok, reasons = verify_receipt(load_receipt(
            OUT / "t0_good" / rc["eval_id"] / "receipt.json"))
        self.assertTrue(ok, reasons)

    def test_T1_blank_not_fake_pass(self):
        status, rc = run_fixture(blank_gen, "t1_blank")
        self.assertEqual(status, STATUS_OK)  # 评估完成≠通过；结果 NEGATIVE 如实
        self.assertEqual(rc["summary"]["n_pass_proxy"], 0)
        self.assertEqual(rc["summary"]["fail_class_counts"].get("blank"), 2)
        for entry in rc["per_char"]:
            self.assertIn("blank", entry["fail_classes"])
            self.assertFalse(entry["pass_proxy"])

    def test_T2_full_fill(self):
        _, rc = run_fixture(full_gen, "t2_full")
        self.assertEqual(rc["summary"]["n_pass_proxy"], 0)
        self.assertEqual(rc["summary"]["fail_class_counts"].get("full_fill"), 2)

    def test_T3_wrong_char_impersonation(self):
        _, rc = run_fixture(cheat_gen, "t3_cheat")
        self.assertEqual(rc["summary"]["n_pass_proxy"], 0)
        self.assertEqual(rc["summary"]["fail_class_counts"].get("wrong_char_proxy"), 2)
        for entry in rc["per_char"]:
            self.assertIn("wrong_char_proxy", entry["fail_classes"])
            diag = entry["metrics"]["cross_match_diagnostic"]
            self.assertIsNotNone(diag.get("nearest_other"))

    def test_T4_transparency_and_gen_error(self):
        _, rc_leak = run_fixture(opaque_leak_gen, "t4_leak")
        self.assertEqual(rc_leak["summary"]["fail_class_counts"].get("transparency_broken"), 2)
        self.assertNotIn("blank", rc_leak["summary"]["fail_class_counts"])  # 墨量正常，纯透明缺陷
        _, rc_ghost = run_fixture(ghost_gen, "t4_ghost")
        self.assertIn("transparency_broken", rc_ghost["summary"]["fail_class_counts"])
        self.assertIn("blank", rc_ghost["summary"]["fail_class_counts"])
        _, rc_dead = run_fixture(dead_gen, "t4_dead")
        self.assertEqual(rc_dead["summary"]["n_gen_error"], 2)
        self.assertEqual(rc_dead["summary"]["n_pass_proxy"], 0)
        for entry in rc_dead["per_char"]:
            self.assertEqual(entry["status"], "gen_error")
            self.assertIn("gen_error_code", entry)

    def test_T5_empty_holdout_insufficient(self):
        with self.assertRaises(RE.EvalInputError) as ctx:
            RE.load_eval_items_mini(make_manifest("empty", []))
        self.assertEqual(ctx.exception.code, "E_EMPTY_HOLDOUT")
        # API 层：空 items → INSUFFICIENT 收据（verdict=None、n=0），不假报完成
        status, rc = run_fixture(good_gen, "t5_empty", items=[])
        self.assertEqual(status, STATUS_INSUFFICIENT)
        self.assertEqual(rc["summary"]["n_targets"], 0)
        self.assertIsNone(rc["verdict"])
        ok, _ = verify_receipt(load_receipt(OUT / "t5_empty" / rc["eval_id"] / "receipt.json"))
        self.assertTrue(ok)

    def test_T6_receipt_tamper_detected(self):
        run_fixture(good_gen, "t6_tamper")
        rc = self._receipt_of("t6_tamper")
        ok, _ = verify_receipt(rc)
        self.assertTrue(ok)  # 未动 → 过

        t1 = json.loads(json.dumps(rc))
        t1["summary"]["n_pass_proxy"] = 99  # 篡改汇总
        ok, reasons = verify_receipt(t1)
        self.assertFalse(ok)
        self.assertTrue(any("receipt_sha256" in r for r in reasons))

        t2 = json.loads(json.dumps(rc))
        t2["per_char"][0]["metrics"]["coverage"]["ink_ratio"] = 0.5  # 篡改逐字指标
        ok, _ = verify_receipt(t2)
        self.assertFalse(ok)

        t3 = json.loads(json.dumps(rc))
        k = sorted(t3["files"])[0]
        t3["files"][k]["sha256"] = "0" * 64  # 文件表 hash 被改 → 内容复算必失败
        ok, reasons = verify_receipt(t3)
        self.assertFalse(ok)
        self.assertTrue(any(k in r for r in reasons))

        t4 = json.loads(json.dumps(rc))
        t4.pop("receipt_sha256")  # 删 self-hash
        ok, _ = verify_receipt(t4)
        self.assertFalse(ok)

    def test_T7_ref_leakage_rejected(self):
        items = default_items()
        man = make_manifest("leak", items, ref_label="甲")  # ref label ∈ holdout labels
        with self.assertRaises(RE.EvalInputError) as ctx:
            RE.load_eval_items_mini(man)
        self.assertEqual(ctx.exception.code, "E_REF_LEAKAGE")

    def test_T8_gpu_lock_protocol(self):
        import os
        lp = OUT / "gpu.lock"
        h1 = gpu_lock.acquire("t8-a", lock_path=lp, wait_cycles=0, wait_seconds=0)
        self.assertIsNotNone(h1)
        info = json.loads(lp.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], os.getpid())
        # 占用：第二个申请者 0 圈即放弃（真实 GPU 场景=3×60s 后 DEFERRED_GPU_BUSY）
        h2 = gpu_lock.acquire("t8-b", lock_path=lp, wait_cycles=0, wait_seconds=0)
        self.assertIsNone(h2)
        self.assertTrue(h1.release())
        self.assertFalse(lp.exists())
        # 误删他人锁：持锁期间锁内容被换成交他人 → release 拒绝删除
        h3 = gpu_lock.acquire("t8-c", lock_path=lp, wait_cycles=0, wait_seconds=0)
        self.assertIsNotNone(h3)
        lp.write_text(json.dumps({"pid": -1, "task": "other",
                                  "created_at": "2026-09-28T00:00:00+08:00"}),
                      encoding="utf-8")
        self.assertFalse(h3.release())  # 内容 pid≠自己 → 不删
        self.assertTrue(lp.exists())
        # 陈旧锁（pid 已死/损坏）→ 摘除后可获取
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()  # 进程已退出 → pid 确定死亡
        lp.write_text(json.dumps({"pid": p.pid, "task": "dead",
                                  "created_at": "2026-09-28T00:00:00+08:00"}),
                      encoding="utf-8")
        h4 = gpu_lock.acquire("t8-d", lock_path=lp, wait_cycles=0, wait_seconds=0)
        self.assertIsNotNone(h4)
        h4.release()
        self.assertFalse(lp.exists())

    def test_T9_metrics_version_and_config_frozen(self):
        _, rc = run_fixture(good_gen, "t9_meta")
        self.assertEqual(rc["metrics_version"], M.METRICS_VERSION)
        self.assertEqual(rc["config"], M.EVAL_CONFIG)  # 阈值全显式落盘，无隐藏参数
        self.assertIn("proxy", rc["boundaries"])
        self.assertIn("S09", rc["boundaries"])
        engine = rc["engine"]
        self.assertEqual(engine["engine_id"], "fixture-gen")
        self.assertEqual(engine["model_version"], "fixture/1.0")


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EvaluatorTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    cases = []
    for t, err in getattr(result, "failures", []) + getattr(result, "errors", []):
        cases.append({"test": t.id(), "ok": False, "detail": err})
    passed = result.testsRun - len(cases)
    receipt = {
        "module": "engine/style/evaluator",
        "command": ".venv/Scripts/python.exe -X utf8 engine/style/evaluator/tests/run_tests.py",
        "interpreter": sys.executable,
        "started": stamp,
        "fixture_source": FIXTURE_SRC,
        "tests_total": result.testsRun,
        "passed": passed,
        "failed": len(cases),
        "exit": 0 if result.wasSuccessful() else 1,
        "cases": cases,
        "data_redline": "no materials/b10-realgt, no B7/B9 SEALED, no real dataset; "
                        "fixtures synthesized; GPU untouched",
    }
    out_dir = TESTS_DIR / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"evaluator-tests-{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[evaluator-tests] {passed}/{result.testsRun} passed, exit={receipt['exit']}, "
          f"receipt={out_dir / f'evaluator-tests-{stamp}.json'}")
    return receipt["exit"]


if __name__ == "__main__":
    sys.exit(main())
