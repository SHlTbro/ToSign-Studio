# -*- coding: utf-8 -*-
"""S08 真实烟测：glyph_rebuild/clean 历史字模 2 字 mini-holdout 走通 run_eval 全链。

数据红线：只用 artifacts/style-font/glyph_rebuild/clean/（历史研究资产，只读）——
style ref = 之_01.png（之），holdout 目标 = 天_01.png（天）+ 顺_t20.png（顺）；
ref label ∉ holdout labels（防泄漏）。不触 materials/b10-realgt、B7/B9 SEALED、
S02 manifest、S03 真实快照（后者 BLOCKED_DATA，尚未存在）。

产物：engine/style/evaluator/out/smoke_manifest.json（mini holdout 声明）。
真正的评估走 CLI（含 GPU 锁/收据/可视化包全链）：
  .venv-ml/Scripts/python.exe -X utf8 -m engine.style.evaluator.run_eval \
      --mini-manifest engine/style/evaluator/out/smoke_manifest.json --engine m1

运行（本脚本只准备 manifest，不占 GPU）：
  .venv/Scripts/python.exe -X utf8 engine/style/evaluator/tests/prepare_smoke.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[4]
CLEAN = PROJ / "artifacts" / "style-font" / "glyph_rebuild" / "clean"
OUT_DIR = Path(__file__).resolve().parents[1] / "out"

REF = ("之", "之_01.png")
TARGETS = (("天", "天_01.png"), ("顺", "顺_t20.png"))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for label, fn in TARGETS:
        p = CLEAN / fn
        if not p.is_file():
            print(f"[smoke] missing historical glyph: {p}")
            return 1
        items.append({"label": label, "target_png": str(p),
                      "sample_id": f"clean:{fn}"})
    ref_path = CLEAN / REF[1]
    if not ref_path.is_file():
        print(f"[smoke] missing style ref: {ref_path}")
        return 1
    if REF[0] in {i["label"] for i in items}:
        print("[smoke] ref label overlaps holdout labels (leakage) — abort")
        return 1
    man = {"style_ref": str(ref_path), "style_ref_label": REF[0], "items": items}
    out = OUT_DIR / "smoke_manifest.json"
    out.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[smoke] manifest ready: {out}")
    print(f"[smoke] ref={REF[0]} targets={[i['label'] for i in items]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
