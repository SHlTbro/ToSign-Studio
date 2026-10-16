# -*- coding: utf-8 -*-
"""S01 M2 一批反向可行性探针：同环境加载三件套，构造 micro-batch 做一步 loss.backward()。

- 只回答"能否训练"：可训练参数>0、梯度有限且贯穿三个模块、loss 有限、显存峰值、一步耗时。
- **不**保存任何 checkpoint、不做 optimizer.step()、不宣称训练开始。
- 输入构造：目标/风格字模由 fonts/presets/Mengshen-Handwritten.ttf 渲染（非 B10/SEALED），
  内容字模由 FontDiffuser 自带 KaiXinSongA.ttf 渲染；96×96 RGB，[-1,1] 归一（训练口径）。
- 训练式前向复用冻结基座代码 src.FontDiffuserModel（训练 forward）与 DDPM 加噪。

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/probes/probe_m2_backward.py
退出码：0 成功；10 E_CUDA_UNAVAILABLE；11 E_CKPT_MISSING；12 E_LOAD_FAILED；
        13 E_BACKWARD_FAILED；14 E_GRAD_INVALID；15 E_DATA_RENDER_FAILED；16 E_OOM_ALL_BATCHES
        （契约统一表见 engine/style/INTERFACE_CONTRACT_DRAFT.md，S04 实现时收敛）
"""
import argparse
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[3]
REPO = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio"
SEED = 20260927
CHARS = ["永", "木", "水", "火"]  # 字体测试常用字，避免触碰任何素材红线

E = {
    "cuda": 10, "ckpt": 11, "load": 12, "backward": 13, "grad": 14,
    "data": 15, "oom": 16,
}


def sha256_file(p: Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fail(code_key, receipt, msg, out_dir=None):
    receipt["failure"] = {"code": f"E_{code_key.upper()}", "exit": E[code_key], "detail": msg}
    out = Path(out_dir) if out_dir else Path(__file__).resolve().parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out / f"m2_receipt_FAIL_{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[FAIL] E_{code_key.upper()}: {msg}")
    return E[code_key]


def render_glyph(font, ch, size):
    """pygame.freetype 渲染白底 L 图（与 vendor utils.ttf2im 同口径），缺字返回 None。"""
    import pygame
    surface, _ = font.render(ch)
    if surface is None:
        return None
    from PIL import Image
    alpha = pygame.surfarray.pixels_alpha(surface).transpose(1, 0)
    ink = 255 - np.array(Image.fromarray(alpha))
    w, h = ink.shape[1], ink.shape[0]
    canvas = np.full((size, size), 255, dtype=np.uint8)
    if w > size or h > size:
        s = min(size / w, size / h)
        ink = np.array(Image.fromarray(ink).resize((max(1, int(w * s)), max(1, int(h * s)))))
        w, h = ink.shape[1], ink.shape[0]
    y0, x0 = (size - h) // 2, (size - w) // 2
    canvas[y0:y0 + h, x0:x0 + w] = ink[:size, :size]
    return Image.fromarray(canvas, mode="L")


def to_tensor(img_l, tfm):
    return tfm(img_l.convert("RGB"))[None, :]


def main():
    import torch
    import torch.nn.functional as F
    import torchvision.transforms as T
    from PIL import Image
    import pygame

    receipt = {"probe": "m2_backward", "seed": SEED, "chars_requested": CHARS}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent / "out" / f"m2_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. checkpoint 存在性 + hash（只读）
    ckpts = {}
    for name in ("content_encoder", "style_encoder", "unet"):
        p = REPO / "ckpt" / f"{name}.pth"
        if not p.exists():
            return fail("ckpt", receipt, f"missing {p}", out_dir)
        ckpts[name] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
    receipt["ckpt"] = ckpts

    if not torch.cuda.is_available():
        return fail("cuda", receipt, "torch.cuda.is_available()=False", out_dir)
    device = "cuda:0"
    free, total = torch.cuda.mem_get_info(0)
    receipt["gpu"] = {"name": torch.cuda.get_device_name(0),
                      "free_mb_before": round(free / 1048576),
                      "total_mb": round(total / 1048576)}
    receipt["torch"] = torch.__version__

    # 2. 数据渲染（目标=风格=Mengshen，内容=KaiXinSongA；缺字剔除）
    pygame.init()
    ttf_style = PROJ / "fonts" / "presets" / "Mengshen-Handwritten.ttf"
    ttf_content = REPO / "ttf" / "KaiXinSongA.ttf"
    # 复用冻结基座 utils 的字体 cmap 检查（只读 import，不改装）
    sys.path.insert(0, str(REPO))
    from utils import is_char_in_font

    def in_font(path, ch):
        return is_char_in_font(font_path=str(path), char=ch)

    size = 96
    font_s = pygame.freetype.Font(str(ttf_style), size=size)
    font_c = pygame.freetype.Font(str(ttf_content), size=size)
    usable = []
    for ch in CHARS:
        if not (in_font(ttf_style, ch) and in_font(ttf_content, ch)):
            continue
        im_s = render_glyph(font_s, ch, size)
        im_c = render_glyph(font_c, ch, size)
        if im_s is None or im_c is None:
            continue
        arr = np.array(im_s)
        if arr.min() >= 250 or arr.max() <= 0:  # 全白/全黑剔除
            continue
        im_s.save(out_dir / f"target_{ch}.png")
        usable.append((ch, im_s, im_c))
    if len(usable) < 2:
        return fail("data", receipt, f"only {len(usable)} renderable chars of {CHARS}", out_dir)
    receipt["chars_usable"] = [u[0] for u in usable]

    # 3. 加载冻结基座（训练式 forward 类）
    import os
    os.chdir(REPO)
    try:
        from configs.fontdiffuser import get_parser
        from src import (FontDiffuserModel, build_unet, build_style_encoder,
                         build_content_encoder, build_ddpm_scheduler)
    except Exception as e:
        return fail("load", receipt, f"repo import failed: {e!r}\n{traceback.format_exc()}", out_dir)

    args = get_parser().parse_args([])
    # 与 sample.arg_parse 同口径：build_* 期望 (size,size) 元组
    args.style_image_size = (args.style_image_size, args.style_image_size)
    args.content_image_size = (args.content_image_size, args.content_image_size)
    t_load0 = time.time()
    try:
        unet = build_unet(args)
        unet.load_state_dict(torch.load(str(REPO / "ckpt" / "unet.pth"), map_location="cpu"))
        style_encoder = build_style_encoder(args)
        style_encoder.load_state_dict(torch.load(str(REPO / "ckpt" / "style_encoder.pth"), map_location="cpu"))
        content_encoder = build_content_encoder(args)
        content_encoder.load_state_dict(torch.load(str(REPO / "ckpt" / "content_encoder.pth"), map_location="cpu"))
        model = FontDiffuserModel(unet=unet, style_encoder=style_encoder,
                                  content_encoder=content_encoder).to(device)
        scheduler = build_ddpm_scheduler(args)
    except Exception as e:
        return fail("load", receipt, f"model load failed: {e!r}\n{traceback.format_exc()}", out_dir)
    receipt["load_seconds"] = round(time.time() - t_load0, 1)

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_p = sum(p.numel() for p in model.parameters())
    receipt["params_total_m"] = round(total_p / 1e6, 2)
    receipt["params_trainable_m"] = round(trainable / 1e6, 2)

    # 4. micro-batch 一步反向（B=4 起，OOM 降 2）
    tfm = T.Compose([T.Resize((size, size), interpolation=T.InterpolationMode.BILINEAR),
                     T.ToTensor(), T.Normalize([0.5], [0.5])])
    torch.manual_seed(SEED)
    np.random.seed(SEED)

    result = None
    for batch in (4, 2):
        sub = usable[:batch]
        b = len(sub)
        try:
            x0 = torch.cat([to_tensor(im_s, tfm) for _, im_s, _ in sub], 0).to(device)
            content = torch.cat([to_tensor(im_c, tfm) for _, _, im_c in sub], 0).to(device)
            style = x0.clone()
            t = torch.randint(0, scheduler.config.num_train_timesteps, (b,), device=device).long()
            noise = torch.randn_like(x0)
            x_t = scheduler.add_noise(x0, noise, t)

            if device.startswith("cuda"):
                torch.cuda.reset_peak_memory_stats(0)
                torch.cuda.synchronize(0)
            t0 = time.time()
            noise_pred, offset = model(x_t, t, style, content,
                                       content_encoder_downsample_size=args.content_encoder_downsample_size)
            loss_noise = F.mse_loss(noise_pred, noise)
            loss_offset = offset.pow(2).mean()
            loss = loss_noise + args.offset_coefficient * loss_offset
            loss.backward()
            if device.startswith("cuda"):
                torch.cuda.synchronize(0)
            step_seconds = round(time.time() - t0, 2)

            grad_report = {}
            finite = True
            nonzero = 0
            for mod in ("unet", "style_encoder", "content_encoder"):
                m = getattr(model, mod)
                gs = [p.grad for p in m.parameters() if p.grad is not None]
                has = len(gs) > 0
                all_finite = all(torch.isfinite(g).all().item() for g in gs)
                any_nonzero = any(g.abs().sum().item() > 0 for g in gs)
                finite = finite and all_finite
                nonzero += int(any_nonzero)
                grad_report[mod] = {"has_grads": has, "finite": all_finite,
                                    "nonzero": any_nonzero,
                                    "norm": float(torch.sqrt(sum((g ** 2).sum() for g in gs))) if gs else None}
            result = {
                "batch": b, "loss_noise": float(loss_noise), "loss_offset": float(loss_offset),
                "loss_total": float(loss), "loss_finite": bool(torch.isfinite(loss).item()),
                "step_seconds": step_seconds,
                "vram_peak_alloc_mb": round(torch.cuda.max_memory_allocated(0) / 1048576, 1),
                "vram_peak_reserved_mb": round(torch.cuda.max_memory_reserved(0) / 1048576, 1),
                "grad_report": grad_report, "grad_modules_nonzero": nonzero,
                "grad_all_finite": finite,
            }
            receipt["backward"] = result
            print(f"[m2] B={b} loss={float(loss):.4f} step={step_seconds}s "
                  f"vram_peak={result['vram_peak_alloc_mb']}MB grad_modules_nonzero={nonzero}/3")
            break
        except torch.cuda.OutOfMemoryError as e:
            torch.cuda.empty_cache()
            receipt.setdefault("oom_attempts", []).append(
                {"batch": b, "error": str(e).splitlines()[0] if str(e) else repr(e)})
            print(f"[m2] OOM at batch={b}, retrying smaller")
            continue
        except Exception as e:
            return fail("backward", receipt,
                        f"forward/backward failed at batch={batch}: {e!r}\n{traceback.format_exc()}", out_dir)

    if result is None:
        return fail("oom", receipt, "OOM at all micro-batch sizes (4,2)", out_dir)

    if not (result["loss_finite"] and result["grad_all_finite"]
            and result["grad_modules_nonzero"] == 3 and trainable > 0):
        return fail("grad", receipt, f"grad/loss invalid: {json.dumps(result, ensure_ascii=False)}", out_dir)

    # 明确声明：本探针不保存 checkpoint、不执行 optimizer.step()
    receipt["no_checkpoint_saved"] = True
    receipt["no_optimizer_step"] = True
    receipt["finished"] = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"[m2] OK receipt={out_dir / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
