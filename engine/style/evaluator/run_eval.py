# -*- coding: utf-8 -*-
"""S08 evaluator.run_eval — 评估主链：数据快照 holdout + 生成句柄 → 逐字指标 + 可视化包 + 收据。

输入：
- S03 数据快照目录（index.json/samples.jsonl/split.json/samples/*.png）：val 侧为 holdout
  目标，style ref 只从 train 侧取且**与 holdout 目标字零交集**（防"参考字泄漏 test"，
  命中交集 → E_REF_LEAKAGE 拒绝评估）；
- 或 mini holdout manifest（JSON：style_ref + items[{label, target_png}]），供探针/烟测——
  收据 input.kind 如实区分 snapshot_v1 / mini_holdout，mini 结果不冒充 S03 全量评估。
- 生成句柄：``gen(style_ref_image, target_char, seed, out_png) -> (PIL.Image, 收据 dict)``。
  M1 冻结基座（m1_adapter.M1Inference.generate）即此签名；未来 M2 同签名接入即评。
  同口径比较 M0/M1/M2 = 各引擎各跑一次 run_eval，收据 eval_id 互引。

输出（out_dir/<eval_id>/）：chars/*.png（预测冻结）、gen/*.gen.json（逐字生成收据）、
viz/grid.png（版本化拼图）、receipt.json（self-hash 防篡改）。

退出码：0 评估链路完成（自动指标可负，完成≠质量通过）；2 评估器内部错误；
3 输入非法；4 INSUFFICIENT_EVAL_SET（holdout 为空/不足显式返回，不凑数）；
5 DEFERRED_GPU_BUSY（GPU 锁被占用，等待 3×60s 后放弃）。

用法（项目根，.venv-ml）：
  .venv-ml/Scripts/python.exe -X utf8 -m engine.style.evaluator.run_eval \
      --snapshot DIR --engine m1 --out-dir engine/style/evaluator/out
  .venv-ml/Scripts/python.exe -X utf8 -m engine.style.evaluator.run_eval \
      --mini-manifest JSON --engine m1 ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import traceback
from pathlib import Path

from engine.style.dataset.snapshot import load_snapshot  # 只读消费（S03 schema）
from engine.style.dataset.schema import canonical_json, sha256_bytes

from . import EVALUATOR_VERSION
from . import metrics as M
from .receipt import (STATUS_FAILED, STATUS_INSUFFICIENT, STATUS_OK,
                      build_receipt, finalize_receipt, new_eval_id, save_receipt,
                      verify_receipt)

PROJ = Path(__file__).resolve().parents[3]
DEFAULT_OUT_ROOT = Path(__file__).resolve().parent / "out"

EXIT_OK = 0
EXIT_INTERNAL = 2
EXIT_INPUT = 3
EXIT_INSUFFICIENT = 4
EXIT_GPU_BUSY = 5

# 最低 holdout 数据门（不足 → INSUFFICIENT，显式；不复制样本凑数——卡 S08 反例）
MIN_HOLDOUT_CHARS = 2


# ---------------------------------------------------------------- 评估集装载

def load_eval_items_snapshot(snapshot_dir: Path, *, k_style_refs: int = 1, reference_label: str | None = None):
    """S03 快照 → (eval_items, input_meta)。

    holdout 目标 = val 侧样本（同 label 多样本取 sample_id 最小者，评 distinct label）；
    style ref   = train 侧样本（label 与 holdout 零交集，否则 E_REF_LEAKAGE）。
    """
    snapshot_dir = Path(snapshot_dir)
    rows, index, splits = load_snapshot(snapshot_dir)
    from engine.style.dataset.binding import verify_snapshot_binding
    verify_snapshot_binding(snapshot_dir, index.get("snapshot_sha256"))
    if not index.get("has_split") or splits is None:
        raise EvalInputError("E_NO_SPLIT", "snapshot has no split.json（无 holdout 可评，"
                                           "不得拿 train 自评自）")
    val = sorted((r for r in rows if splits.get(r["sample_id"]) == "val"), key=lambda r: r["sample_id"])
    train = sorted((r for r in rows if splits.get(r["sample_id"]) == "train"), key=lambda r: r["sample_id"])
    if not val:
        raise EvalInputError("E_EMPTY_HOLDOUT", "val side is empty → INSUFFICIENT（显式，不凑数）")

    by_label: dict[str, dict] = {}
    for r in val:
        by_label.setdefault(r["label"], r)  # sorted 后即 sample_id 最小
    items = []
    for label in sorted(by_label):
        r = by_label[label]
        png = snapshot_dir / r["snapshot_file"]
        if not png.is_file():
            raise EvalInputError("E_TARGET_MISSING",
                                 f"holdout target png missing: {png}")
        items.append({"label": label, "target_png": png, "sample_id": r["sample_id"],
                      "final_alpha_sha256": r.get("final_alpha_sha256")})
    if len(items) < MIN_HOLDOUT_CHARS:
        raise EvalInputError("E_HOLDOUT_TOO_SMALL",
                             f"distinct holdout chars = {len(items)} < {MIN_HOLDOUT_CHARS}"
                             " → INSUFFICIENT（显式）")

    target_labels = {it["label"] for it in items}
    ref_rows = [r for r in train if r["label"] not in target_labels]
    if not ref_rows:
        raise EvalInputError("E_REF_LEAKAGE",
                             "no train-side sample with label disjoint from holdout"
                             "（参考字泄漏 holdout → 拒绝评估；这是 S08 明列反例）")
    if reference_label is not None:
        preferred=[r for r in ref_rows if r["label"]==reference_label]
        if not preferred:raise EvalInputError("E_REF_MISSING", "requested reference is absent from disjoint training rows")
        ref_rows=preferred+[r for r in ref_rows if r["label"]!=reference_label]
    refs = []
    for r in ref_rows[:k_style_refs]:
        png = snapshot_dir / r["snapshot_file"]
        if not png.is_file():
            raise EvalInputError("E_REF_MISSING", f"style ref png missing: {png}")
        refs.append({"label": r["label"], "png": png, "sample_id": r["sample_id"],
                     "final_alpha_sha256": r.get("final_alpha_sha256")})

    meta = {
        "kind": "snapshot_v1",
        "snapshot_dir": str(snapshot_dir),
        "snapshot_id": index.get("snapshot_id"),
        "snapshot_sha256": index.get("snapshot_sha256"),
        "index_sha256": sha256_bytes((snapshot_dir / "index.json").read_bytes()),
        "n_rows": len(rows), "n_val_rows": len(val), "n_train_rows": len(train),
        "holdout_labels": sorted(target_labels),
        "style_refs": [{k: str(v) if isinstance(v, Path) else v for k, v in r.items()}
                       for r in refs],
        "leakage_check": "pass（ref labels ∩ holdout labels = ∅）",
    }
    return items, refs, meta


def load_eval_items_mini(manifest_path: Path):
    """mini holdout manifest → (eval_items, refs, input_meta)。探针/烟测专用，收据如实标注。"""
    manifest_path = Path(manifest_path)
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    ref_png = Path(man["style_ref"])
    if not ref_png.is_file():
        raise EvalInputError("E_INPUT_REF", f"style_ref not found: {ref_png}")
    refs = [{"label": man.get("style_ref_label", "?"), "png": ref_png,
             "sample_id": "mini_ref", "final_alpha_sha256": None}]
    items = []
    for it in man.get("items", []):
        png = Path(it["target_png"])
        if not png.is_file():
            raise EvalInputError("E_TARGET_MISSING", f"target png missing: {png}")
        items.append({"label": it["label"], "target_png": png,
                      "sample_id": it.get("sample_id", f"mini:{it['label']}"),
                      "final_alpha_sha256": None})
    if not items:
        raise EvalInputError("E_EMPTY_HOLDOUT", "mini manifest has no items → INSUFFICIENT")
    meta = {
        "kind": "mini_holdout",
        "declaration": "mini holdout（历史研究字模，非 S03 快照全量评估）；结果不可冒充正式评估",
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "holdout_labels": sorted(it["label"] for it in items),
        "style_refs": [{k: str(v) if isinstance(v, Path) else v for k, v in r.items()}
                       for r in refs],
        "leakage_check": ("pass（ref label ∉ holdout labels）"
                          if refs[0]["label"] not in {it["label"] for it in items}
                          else "FAIL（ref label ∈ holdout labels）"),
    }
    if refs[0]["label"] in {it["label"] for it in items}:
        raise EvalInputError("E_REF_LEAKAGE", "mini ref label overlaps holdout labels")
    return items, refs, meta


class EvalInputError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


# ---------------------------------------------------------------- 生成句柄

def make_m1_generator(ckpt_dir=None, device: str = "cuda:0", content_ttf=None):
    """M1 冻结基座生成句柄（签名 = gen(style_ref_image, target_char, seed, out_png)）。

    import 本函数不拉起 torch（m1_adapter.adapter 重依赖延迟加载）；只读加载三件套。
    """
    from engine.style.m1_adapter.adapter import M1Inference  # 延迟（torch 延迟在其中）

    kwargs = {}
    if ckpt_dir is not None:
        kwargs["ckpt_dir"] = ckpt_dir
    if content_ttf is not None:
        kwargs["content_ttf"] = content_ttf
    engine = M1Inference.get_shared(device=device, **kwargs)

    def gen(style_ref_image, target_char, seed, out_png):
        return engine.generate(style_ref_image=style_ref_image,
                               target_char=target_char, seed=seed, out_png=out_png)

    gen.engine_id = "fontdiffuser-frozen-v1"
    gen.engine_mode = "M1_FROZEN"
    return gen


# ---------------------------------------------------------------- 可视化包

def _label_text(it: dict) -> str:
    cp = f"U+{ord(it['label']):04X}"
    return f"{cp} {it['sample_id']}"[:38]


def build_grid(rows: list[dict], refs: list[dict], out_png: Path) -> None:
    """拼图：每行 = [目标 GT | 生成输出 | style ref]，列头 + 逐行 U+标签 + 判定。

    标签用 ASCII（U+XXXX/sample_id/fail classes），避免 PIL 默认位图字体缺 CJK。
    """
    from PIL import Image, ImageDraw

    cell = 128
    pad = 6
    head = 18
    row_lab = 14
    cols = 3 + max(0, len(refs))
    W = pad + cols * (cell + pad)
    H = head + len(rows) * (cell + row_lab + pad) + pad
    canvas = Image.new("RGB", (W, H), (240, 240, 240))
    d = ImageDraw.Draw(canvas)
    heads = ["TARGET GT", "OUTPUT", *[f"STYLE REF {i + 1}" for i in range(len(refs))]]
    for ci, htxt in enumerate(heads):
        d.text((pad + ci * (cell + pad) + 2, 3), htxt, fill=(30, 30, 30))
    for ri, row in enumerate(rows):
        y = head + ri * (cell + row_lab + pad)
        def _paste(img, ci):
            im = M.glyph_luma(img).convert("RGB").resize((cell, cell))
            canvas.paste(im, (pad + ci * (cell + pad), y))
        _paste(row["target_img"], 0)
        _paste(row["out_img"], 1)
        for k, ref in enumerate(refs):
            _paste(ref["img"], 2 + k)
        verdict = "PASS-proxy" if row["pass_proxy"] else ",".join(row["fail_classes"])
        color = (16, 96, 32) if row["pass_proxy"] else (160, 24, 24)
        d.text((pad + (cell + pad) + 2, y + cell + 1), verdict, fill=color)
        d.text((pad + 2, y + cell + 1), _label_text(row["item"]), fill=(30, 30, 30))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, format="PNG")


# ---------------------------------------------------------------- 主链

def preserve_generation_receipt(receipt):
    """Freeze the complete producer JSON value, without defaults or coercion.

    Execution/profile proofs are extensible producer metadata. An allowlist here
    silently discarded real proofs before the downstream validators saw them.
    Reject non-JSON values rather than stringify or omit unsupported evidence.
    """
    def check(value, depth=0):
        if depth > 128:
            raise ValueError("generation receipt exceeds the JSON nesting limit")
        kind = type(value)
        if value is None or kind in (str, bool, int):
            return
        if kind is float:
            if not math.isfinite(value):
                raise ValueError("generation receipt contains a nonfinite number")
            return
        if kind is list:
            for item in value:
                check(item, depth + 1)
            return
        if kind is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("generation receipt object keys must be strings")
                check(item, depth + 1)
            return
        raise ValueError("generation receipt contains a non-JSON value")

    try:
        if type(receipt) is not dict:
            raise ValueError("generation receipt must be a JSON object")
        check(receipt)
        encoded = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")  # Reject malformed Unicode before file writes.
        return json.loads(encoded)  # Detached copy, including future proof fields.
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise EvalInputError("E_GEN_RECEIPT_INVALID", str(exc)) from exc


def run_eval(generator, items: list[dict], refs: list[dict], input_meta: dict, *,
             out_dir=DEFAULT_OUT_ROOT, seed: int = 20260928, device: str = "cuda:0",
             engine_name: str = "unknown", bands: dict | None = None,
             stamp: str | None = None) -> tuple[str, dict]:
    """逐字生成+评估 → (status, receipt)。status ∈ OK/INSUFFICIENT_EVAL_SET/EVAL_FAILED。

    完成 ≠ 质量通过：自动指标允许 NEGATIVE/INCONCLUSIVE，verdict 恒 None（S09 域）。
    """
    config = dict(M.EVAL_CONFIG)
    if bands:
        unknown = sorted(set(bands) - set(config))
        if unknown:
            raise EvalInputError("E_CONFIG_UNKNOWN", f"unknown band keys {unknown}")
        config.update(bands)
    config_sha = sha256_bytes(canonical_json(config).encode("utf-8"))

    eval_id = new_eval_id(stamp)
    run_dir = Path(out_dir) / eval_id
    suffix = 1
    while (run_dir / "receipt.json").is_file():  # 同秒重跑不覆盖旧收据
        suffix += 1
        run_dir = Path(out_dir) / f"{eval_id}_r{suffix}"
    chars_dir = run_dir / "chars"
    gen_dir = run_dir / "gen"
    viz_dir = run_dir / "viz"
    for d in (chars_dir, gen_dir, viz_dir):
        d.mkdir(parents=True, exist_ok=True)

    per_char: list[dict] = []
    files: dict[str, dict] = {}
    engine_meta: dict | None = None
    engine_mismatch: list[str] = []

    # 预装载目标 GT 统计与 ref 特征（CPU）
    try:
        target_stats = {it["label"]: M.load_target_ink(it["target_png"]) for it in items}
    except Exception as e:  # noqa: BLE001 —— 目标 GT 不可读属输入侧问题
        raise EvalInputError("E_TARGET_UNREADABLE", f"target GT unreadable: {e!r}") from e
    from PIL import Image
    ref_imgs = []
    for r in refs:
        im = Image.open(r["png"])
        im.load()
        ref_l = M.glyph_luma(im)
        ref_imgs.append({"img": ref_l})
        r["png_sha256"] = sha256_bytes(Path(r["png"]).read_bytes())

    n_generated = n_gen_error = n_pass = 0
    fail_counts = {c: 0 for c in M.FAIL_CLASSES}
    rows_for_grid: list[dict] = []

    for it in items:
        label = it["label"]
        safe = f"{ord(label):04X}"
        out_png = chars_dir / f"{label}_U+{safe}.png"
        entry: dict = {
            "label": label, "label_cp": f"U+{safe}", "sample_id": it["sample_id"],
            "target_png": str(it["target_png"]),
            "target_png_sha256": sha256_bytes(Path(it["target_png"]).read_bytes()),
            "seed": int(seed), "style_ref": str(refs[0]["png"]) if refs else None,
        }
        try:
            img, gen_receipt = generator(refs[0]["png"] if refs else None, label,
                                         int(seed), out_png)
            gen_receipt = preserve_generation_receipt(gen_receipt)
        except Exception as e:  # noqa: BLE001 —— 生成失败显式入账，不假通过
            n_gen_error += 1
            code = getattr(e, "code", "E_GEN_UNEXPECTED")
            entry.update({"status": "gen_error", "gen_error_code": code,
                          "gen_error_detail": str(e)[:500],
                          "fail_classes": [M.FAIL_DECODE], "pass_proxy": False})
            per_char.append(entry)
            continue
        if img is None:
            n_gen_error += 1
            entry.update({"status": "gen_error", "gen_error_code": "E_GEN_NULL",
                          "fail_classes": [M.FAIL_DECODE], "pass_proxy": False})
            per_char.append(entry)
            continue

        # 生成收据一致性（同一 eval 内 engine/model 必须一致，防串引擎混报）
        gid = gen_receipt.get("engine_id", "unknown")
        gmv = gen_receipt.get("model_version", "unknown")
        model_identity={key:gen_receipt[key] for key in ('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity','runtime_binding','deployment_binding','training_runtime_binding','training_snapshot_binding','training_manifest_sha256','training_receipt_sha256','training_admission_supplement_sha256') if gen_receipt.get(key) is not None}
        if engine_meta is None:
            engine_meta = {"engine_id": gid, "model_version": gmv,
                           "engine_mode": gen_receipt.get("engine_mode"),
                           "ckpt_dir": gen_receipt.get("ckpt_dir"),**model_identity}
        elif engine_meta["engine_id"] != gid or engine_meta["model_version"] != gmv:
            engine_mismatch.append(f"{label}: {gid}/{gmv}")
        elif model_identity!={key:engine_meta[key] for key in ('recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity','runtime_binding','deployment_binding','training_runtime_binding','training_snapshot_binding','training_manifest_sha256','training_receipt_sha256','training_admission_supplement_sha256') if engine_meta.get(key) is not None}:
            engine_mismatch.append(f"{label}: model topology/recipe/runtime identity differs")

        # 输出冻结：写盘 + hash（用生成器返回的图统一落盘，hash 以盘上字节为准）
        out_png.parent.mkdir(parents=True, exist_ok=True)
        if not out_png.is_file():
            img.save(out_png, format="PNG")
        out_sha = sha256_bytes(out_png.read_bytes())

        others = [target_stats[l] for l in sorted(target_stats) if l != label]
        res = M.evaluate_char(img, target_stats[label],
                             [r["img"] for r in ref_imgs], others, config=config)
        # 校准参考：目标 GT 自身与 ref 的统计距离（风格 proxy 的解读基准）
        tgt_l = M.glyph_luma(Image.open(it["target_png"]))
        if ref_imgs:
            cal = round(sum(M.style_distance(M.style_features(tgt_l), M.style_features(r["img"]))
                            for r in ref_imgs) / len(ref_imgs), 6)
            res["metrics"]["style_proxy"]["calibration_target_vs_ref"] = cal

        entry.update({
            "status": "ok", "out_png": str(out_png), "out_png_sha256": out_sha,
            "gen_receipt": gen_receipt,
            "metrics": res["metrics"], "fail_classes": res["fail_classes"],
            "pass_proxy": res["pass_proxy"],
        })
        n_generated += 1
        for c in res["fail_classes"]:
            fail_counts[c] += 1
        if res["pass_proxy"]:
            n_pass += 1
        rows_for_grid.append({"item": it, "target_img": Image.open(it["target_png"]),
                              "out_img": img, "fail_classes": res["fail_classes"],
                              "pass_proxy": res["pass_proxy"]})
        per_char.append(entry)

    # 生成收据逐字冻结
    for entry in per_char:
        if entry.get("status") == "ok" and entry.get("gen_receipt"):
            gp = gen_dir / f"gen_{entry['label_cp']}.json"
            gp.write_text(json.dumps(entry["gen_receipt"], ensure_ascii=False, indent=1),
                          encoding="utf-8")
            files[f"gen/{gp.name}"] = {"path": str(gp), "sha256": sha256_bytes(gp.read_bytes())}

    grid_png = viz_dir / "grid.png"
    if rows_for_grid:
        build_grid(rows_for_grid, ref_imgs, grid_png)
        files["viz/grid.png"] = {"path": str(grid_png), "sha256": sha256_bytes(grid_png.read_bytes())}
    for entry in per_char:
        if entry.get("status") == "ok":
            p = Path(entry["out_png"])
            files[f"chars/{p.name}"] = {"path": str(p), "sha256": entry["out_png_sha256"]}

    n_items = len(items)
    if n_items == 0 or n_generated + n_gen_error == 0:
        status = STATUS_INSUFFICIENT
    else:
        status = STATUS_OK
    summary = {
        "n_targets": n_items, "n_generated": n_generated, "n_gen_error": n_gen_error,
        "n_pass_proxy": n_pass,
        "n_fail_proxy": n_generated - n_pass,
        "fail_class_counts": {k: v for k, v in fail_counts.items() if v},
        "declaration": "n_pass_proxy 是自动结构 proxy 通过数，不是质量通过数（verdict 属 S09）",
    }
    notes = []
    if engine_mismatch:
        notes.append(f"ENGINE_MISMATCH: {engine_mismatch[:5]}")
        status = STATUS_FAILED
    inp = dict(input_meta)
    inp["min_holdout_chars_gate"] = MIN_HOLDOUT_CHARS
    inp["holdout_gate"] = ("pass" if len(items) >= MIN_HOLDOUT_CHARS
                           else "INSUFFICIENT（显式不足，不凑数）")
    receipt = build_receipt(
        eval_id=eval_id, status=status, evaluator_version=EVALUATOR_VERSION,
        metrics_version=M.METRICS_VERSION, config=config, config_sha256=config_sha,
        inp=inp, engine=engine_meta or {"engine_id": "none", "model_version": "none"},
        per_char=per_char, summary=summary, files=files,
        notes=notes + [f"seed={seed}", f"device={device}", f"engine_name={engine_name}"])
    finalize_receipt(receipt)
    rp = run_dir / "receipt.json"
    save_receipt(receipt, rp)
    ok, reasons = verify_receipt(json.loads(rp.read_text(encoding="utf-8")))
    if not ok:  # 自校验失败 = 评估器自身缺陷，显式炸出
        raise RuntimeError(f"receipt self-check failed: {reasons}")
    receipt["_self_check"] = "pass"
    return status, receipt


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="run_eval")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--snapshot", help="S03 数据快照目录（val=holdout）")
    src.add_argument("--mini-manifest", help="mini holdout manifest JSON（烟测/探针）")
    ap.add_argument("--engine", default="m1", choices=["m1", "m2"], help="真实生成引擎；M2 必须显式配置已验证模型资产")
    ap.add_argument("--m2-config", help="M2 inference configuration JSON (no network downloads)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_ROOT))
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--gpu-lock", default=str(PROJ / ".ai-hub" / "parallel-lines" / "line-b" / "gpu.lock"))
    ap.add_argument("--no-gpu-lock", action="store_true", help="仅夹具测试可用；真实 GPU 评估必须走锁")
    args = ap.parse_args(argv)

    lock = None
    if not args.no_gpu_lock:
        from .gpu_lock import acquire
        lock = acquire("s08-run-eval", lock_path=Path(args.gpu_lock))
        if lock is None:
            print(json.dumps({"status": "DEFERRED_GPU_BUSY",
                              "detail": f"gpu lock busy after 3x60s: {args.gpu_lock}"},
                             ensure_ascii=False))
            return EXIT_GPU_BUSY
    try:
        try:
            if args.snapshot:
                items, refs, meta = load_eval_items_snapshot(Path(args.snapshot))
            else:
                items, refs, meta = load_eval_items_mini(Path(args.mini_manifest))
        except EvalInputError as e:
            print(json.dumps({"status": "INPUT_ERROR", "code": e.code, "detail": str(e)},
                             ensure_ascii=False))
            if e.code in ("E_EMPTY_HOLDOUT", "E_HOLDOUT_TOO_SMALL"):
                return EXIT_INSUFFICIENT
            return EXIT_INPUT

        try:
            if args.engine == "m2":
                if not args.m2_config:
                    raise EvalInputError("E_INPUT_MISSING", "--m2-config is required")
                config = json.loads(Path(args.m2_config).read_text(encoding="utf-8"))
                from engine.style.m2_adapter.inference import M2Inference
                engine = M2Inference(**config)
                gen = engine.generate
            else:
                gen = make_m1_generator(device=args.device)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"status": "EVAL_FAILED", "detail": f"engine init: {e!r}"},
                             ensure_ascii=False))
            return EXIT_INTERNAL
        try:
            status, receipt = run_eval(gen, items, refs, meta, out_dir=Path(args.out_dir),
                                       seed=args.seed, device=args.device,
                                       engine_name=args.engine)
        except EvalInputError as e:
            print(json.dumps({"status": "INPUT_ERROR", "code": e.code, "detail": str(e)},
                             ensure_ascii=False))
            return EXIT_INPUT
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            print(json.dumps({"status": "EVAL_FAILED", "detail": repr(e)}, ensure_ascii=False))
            return EXIT_INTERNAL

        print(json.dumps({"status": status, "eval_id": receipt["eval_id"],
                          "receipt": str(Path(args.out_dir) / receipt["eval_id"] / "receipt.json"),
                          "summary": receipt["summary"]}, ensure_ascii=False, indent=1))
        return EXIT_OK if status in (STATUS_OK, STATUS_INSUFFICIENT) else EXIT_INTERNAL
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())

