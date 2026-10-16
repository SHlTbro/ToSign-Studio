# -*- coding: utf-8 -*-
"""S01 环境探针：只读盘点 .venv-ml 版本矩阵、GPU、checkpoint hash 与资产存在性。

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/probes/probe_env.py
不加载任何模型权重到 GPU，不做任何写操作（receipt 打印到 stdout）。
退出码：0=盘点完成；1=致命（无法收集）。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]
REPO = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio"
SDT = PROJ / "vendor" / "Hand2TTF-bySDT"


def sha256_prefix(p: Path, n=16):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def file_info(p: Path):
    if not p.exists():
        return None
    return {"bytes": p.stat().st_size, "sha256_16": sha256_prefix(p)}


def main():
    report = {"project_root": str(PROJ)}

    # 1. Python / 版本矩阵
    try:
        import torch, torchvision, diffusers, transformers, accelerate
        import huggingface_hub, PIL, numpy, safetensors
        report["versions"] = {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "torchvision": torchvision.__version__,
            "diffusers": diffusers.__version__,
            "transformers": transformers.__version__,
            "accelerate": accelerate.__version__,
            "huggingface_hub": huggingface_hub.__version__,
            "pillow": PIL.__version__,
            "numpy": numpy.__version__,
            "safetensors": safetensors.__version__,
        }
        # 已知历史约束：diffusers 0.22 需要 huggingface_hub<=0.25 系
        hf = tuple(int(x) for x in huggingface_hub.__version__.split(".")[:2])
        report["hf_hub_constraint_ok"] = hf <= (0, 25)
        try:
            import gradio
            report["versions"]["gradio"] = gradio.__version__
        except ImportError:
            report["versions"]["gradio"] = None  # 仅 app.py(Gradio demo) 需要
        # SDT 系依赖（vendor 声明，本 venv 未装）
        for mod in ("lmdb", "einops", "easydict", "easyocr"):
            try:
                __import__(mod)
                report.setdefault("sdt_deps_present", []).append(mod)
            except ImportError:
                report.setdefault("sdt_deps_missing", []).append(mod)
    except Exception as e:
        report["versions_error"] = repr(e)

    # 2. GPU
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            report["gpu"] = {
                "name": props.name,
                "total_mb": round(props.total_memory / 1048576),
                "cuda_runtime": torch.version.cuda,
            }
            free, total = torch.cuda.mem_get_info(0)
            report["gpu"]["free_mb_at_probe"] = round(free / 1048576)
        else:
            report["gpu"] = {"available": False}
    except Exception as e:
        report["gpu_error"] = repr(e)

    # 3. FontDiffuser 三件套 checkpoint（存在性 + hash）
    report["fontdiffuser_ckpt"] = {
        name: file_info(REPO / "ckpt" / f"{name}.pth")
        for name in ("content_encoder", "style_encoder", "unet")
    }
    report["fontdiffuser_ttf"] = {
        p.name: file_info(p) for p in sorted((REPO / "ttf").glob("*.ttf"))
    }
    report["fontdiffuser_src_present"] = all(
        (REPO / "src" / f).exists()
        for f in ("model.py", "build.py", "criterion.py", "__init__.py")
    )

    # 4. vendor Hand2TTF-bySDT
    report["sdt"] = {
        "checkpoint_iter199999": file_info(SDT / "checkpoints" / "checkpoint-iter199999.pth"),
        "environment_yml": (SDT / "environment.yml").exists(),
        "inference_entry": (SDT / "user_generate_ttf.py").exists(),
        "train_entry": (SDT / "train.py").exists(),
        "svg2ttf": (SDT / "svg2ttf.py").exists(),
    }

    # 5. 历史推理证据（只读存在性）
    s2 = PROJ / "artifacts" / "style-font" / "s2_preview"
    s2b = PROJ / "artifacts" / "style-font" / "s2b_preview"
    report["historical_inference_evidence"] = {
        "s2_run_log": file_info(s2 / "s2_run_log.txt"),
        "s2_manifest": file_info(s2 / "manifest.json"),
        "s2_generations_count": len(list((s2 / "generations").glob("*.png"))) if (s2 / "generations").exists() else 0,
        "s2b_generations_count": len(list((s2b / "generations").glob("*.png"))) if (s2b / "generations").exists() else 0,
    }

    # 6. 允许的探针输入资产（非 B10/SEALED 的历史研究资产 + 系统字体）
    glyphs = PROJ / "artifacts" / "t20_glyphs" / "glyphs"
    clean = PROJ / "artifacts" / "style-font" / "glyph_rebuild" / "clean"
    presets = PROJ / "fonts" / "presets"
    report["allowed_probe_inputs"] = {
        "t20_glyphs_count": len(list(glyphs.glob("*.png"))) if glyphs.exists() else 0,
        "glyph_rebuild_clean": sorted(p.name for p in clean.glob("*.png")) if clean.exists() else [],
        "font_presets": sorted(p.name for p in presets.glob("*.ttf")) if presets.exists() else [],
    }

    # 7. 数据红线目录存在性（只记录，不读取内容）
    report["data_boundaries_present"] = {
        "materials_b10_realgt": (PROJ / "materials" / "b10-realgt").exists(),
        "materials_b9": (PROJ / "materials" / "b9-domain-expansion").exists(),
    }

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
