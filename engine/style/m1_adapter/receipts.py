# -*- coding: utf-8 -*-
"""S04 m1_adapter.receipts — 推理收据 schema（M1_FROZEN 语义写死在字段里）。

收据字段（engine_mode 恒为 "M1_FROZEN"，本适配器**无训练语义**：不更新参数、
不保存 checkpoint、不宣称质量——质量门在 S08/S09）：

- engine_mode / engine_id / model_version
    model_version = "fontdiffuser|u:<unet_sha12>|s:<style_sha12>|c:<content_sha12>"
    （ckpt 三件套 sha256 前缀，进程内缓存）
- inputs: style_ref 归一化后 sha256、ref 原文件 sha256、target_char、content_ttf sha、seed、device
- timings: load_seconds / sample_seconds（首字含预热不可外推为稳态）
- vram: peak alloc/reserved MB（cuda 时）
- output: png sha256、size、mode、ink_px（alpha≥128）、rgba 派生检查（composite_max_diff）
- process: runner 版本、started/finished、exit_code
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
from pathlib import Path

ENGINE_MODE = "M1_FROZEN"
ENGINE_ID = "fontdiffuser-frozen-v1"
CKPT_FILES = ("content_encoder", "style_encoder", "unet")  # 与 vendor 命名一致
RUNNER_VERSION = "m1_adapter/1.1.0"
STYLE_PREPROCESSING_PROFILE = "white-alpha-ink-or-rgba-composite-v2"

# 进程退出码（runner 语义，卡 S04 指定）
EXIT_OK = 0        # 成功，收据完整
EXIT_ENGINE = 2    # 引擎不可用/引擎侧失败（ckpt 缺失、CUDA 不可用、加载/采样/输出退化）
EXIT_INPUT = 3     # 输入非法（JSON/缺字段/空字或多字/缺 ref/输出目录不可写/字符不在内容字体）


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_image(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return hashlib.sha256(buf.getvalue()).hexdigest()


def application_code_hashes() -> dict[str, str]:
    """Lightweight identity usable by the API without importing ML dependencies."""
    root = Path(__file__).resolve().parent
    return {name: sha256_file(root / name) for name in ("adapter.py", "receipts.py", "runner.py")}


def model_version_from_ckpt(ckpt_dir) -> tuple[str, dict]:
    """→ (model_version 字符串, {name: {bytes, sha256}})。"""
    parts, detail = [], {}
    for name in CKPT_FILES:
        p = Path(ckpt_dir) / f"{name}.pth"
        if not p.exists():
            raise FileNotFoundError(f"missing checkpoint: {p}")
        sha = sha256_file(p)
        detail[name] = {"bytes": p.stat().st_size, "sha256": sha}
        parts.append(f"{name[0]}:{sha[:12]}")
    return "fontdiffuser|" + "|".join(parts), detail


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def build_receipt(*, ckpt_dir, model_version, ckpt_detail, style_ref_path,
                  ref_normalized_sha, target_char, content_ttf, seed, device,
                  load_seconds, sample_seconds, vram, out_png, out_sha, size_mode,
                  ink_px, composite_max_diff, ref_source_sha=None, ref_normalization=None) -> dict:
    return {
        "engine_mode": ENGINE_MODE,
        "engine_id": ENGINE_ID,
        "runner_version": RUNNER_VERSION,
        "model_version": model_version,
        "application_code_sha256": application_code_hashes(),
        "ckpt_dir": str(ckpt_dir),
        "ckpt_detail": ckpt_detail,
        "inputs": {
            "style_ref_path": str(style_ref_path),
            "style_ref_sha256_normalized": ref_normalized_sha,
            "style_ref_sha256": ref_source_sha,
            "style_preprocessing_profile": STYLE_PREPROCESSING_PROFILE,
            "style_normalization": ref_normalization,
            "target_char": target_char,
            "target_char_cp": f"U+{ord(target_char):04X}",
            "content_ttf": str(Path(content_ttf).resolve()),
            "content_ttf_sha256": sha256_file(content_ttf),
            "seed": int(seed),
            "device": device,
        },
        "timings": {"load_seconds": round(load_seconds, 2),
                    "sample_seconds": round(sample_seconds, 2)},
        "vram": vram,
        "output": {"png": str(out_png), "png_sha256": out_sha,
                   "size": list(size_mode[0]), "mode": size_mode[1],
                   "ink_px": ink_px, "rgba_composite_max_diff": composite_max_diff},
        "process": {"started": _now(), "finished": _now()},
        "boundaries": "M1 真实推理≠训练完成；输出≠质量达标（质量门 S08/S09）；不训练/不存 ckpt",
    }
