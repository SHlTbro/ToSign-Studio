# -*- coding: utf-8 -*-
"""S01 证据升级（外审 P0-B03）：M2 真实一步优化——optimizer.step 前后参数变化证明。

- 与 probe_m2_backward.py 同口径构造（B=4 起 OOM 降 2；目标/风格=Mengshen、内容=KaiXinSongA，
  非 B10/SEALED 输入），区别：执行完整 **loss.backward() + optimizer.step(Adam lr=1e-5)**，
  并对代表性参数张量做 step 前后 sha256 对比——证明至少一个参数张量实际变化。
- 仍不保存 checkpoint（无 .pth/.npz 权重产物）；一步优化=可行性证据，≠训练开始≠质量。
- GPU 锁约定：.ai-hub/parallel-lines/line-b/gpu.lock（PID+任务名）；占用则退出码 20。

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/probes/probe_m2_param_step.py
退出码：0 成功；10 E_CUDA；11 E_CKPT；12 E_LOAD；13 E_BACKWARD；14 E_GRAD；15 E_DATA；
        16 E_OOM；17 E_PARAM_UNCHANGED；20 E_GPU_LOCKED
"""
import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parents[3]
REPO = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio"
LOCK = PROJ / ".ai-hub" / "parallel-lines" / "line-b" / "gpu.lock"
SEED = 20260927
CHARS = ["永", "木", "水", "火"]
LR = 1e-5

E = {"cuda": 10, "ckpt": 11, "load": 12, "backward": 13, "grad": 14,
     "data": 15, "oom": 16, "param": 17, "lock": 20}


def sha256_file(p: Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tensor_sha(t) -> str:
    import torch
    return hashlib.sha256(t.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def fail(code_key, receipt, msg, out_dir):
    receipt["failure"] = {"code": f"E_{code_key.upper()}", "exit": E[code_key], "detail": msg}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"m2step_receipt_FAIL_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[FAIL] E_{code_key.upper()}: {msg}")
    return E[code_key]


def render_glyph(font, ch, size):
    import pygame
    from PIL import Image
    surface, _ = font.render(ch)
    if surface is None:
        return None
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
    out_dir = Path(__file__).resolve().parent / "out" / f"m2step_{time.strftime('%Y%m%d_%H%M%S')}"
    receipt = {"probe": "m2_param_step", "seed": SEED, "lr": LR, "purpose":
               "S01 evidence upgrade: one real optimizer step with before/after param checksums"}

    # ---- GPU 锁 ----
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            other = int(LOCK.read_text(encoding="utf-8").strip().split()[0])
            os.kill(other, 0)  # 活性探测
            return fail("lock", receipt, f"gpu locked by live pid {other}", out_dir)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # 陈旧锁/无法探测 → 接管
    LOCK.write_text(f"{os.getpid()} m2_param_step {time.strftime('%Y%m%d_%H%M%S')}", encoding="utf-8")
    try:
        return _run(receipt, out_dir)
    finally:
        if LOCK.exists() and LOCK.read_text(encoding="utf-8").startswith(str(os.getpid())):
            LOCK.unlink()


def _run(receipt, out_dir):
    import torch
    import torch.nn.functional as F
    import torchvision.transforms as T
    import pygame

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    ckpts = {}
    for name in ("content_encoder", "style_encoder", "unet"):
        p = REPO / "ckpt" / f"{name}.pth"
        if not p.exists():
            return fail("ckpt", receipt, f"missing {p}", out_dir)
        ckpts[name] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
    receipt["ckpt"] = ckpts

    if not torch.cuda.is_available():
        return fail("cuda", receipt, "no cuda", out_dir)
    device = "cuda:0"
    receipt["gpu"] = {"name": torch.cuda.get_device_name(0), "torch": torch.__version__}

    # 数据渲染（同 probe_m2_backward 口径）
    import pygame as _pg
    _pg.init()
    ttf_style = PROJ / "fonts" / "presets" / "Mengshen-Handwritten.ttf"
    ttf_content = REPO / "ttf" / "KaiXinSongA.ttf"
    sys.path.insert(0, str(REPO))
    from utils import is_char_in_font

    size = 96
    font_s = pygame.freetype.Font(str(ttf_style), size=size)
    font_c = pygame.freetype.Font(str(ttf_content), size=size)
    usable = []
    for ch in CHARS:
        if not (is_char_in_font(font_path=str(ttf_style), char=ch)
                and is_char_in_font(font_path=str(ttf_content), char=ch)):
            continue
        im_s = render_glyph(font_s, ch, size)
        im_c = render_glyph(font_c, ch, size)
        if im_s is None or im_c is None:
            continue
        arr = np.array(im_s)
        if arr.min() >= 250 or arr.max() <= 0:
            continue
        usable.append((ch, im_s, im_c))
    if len(usable) < 2:
        return fail("data", receipt, f"only {len(usable)} renderable", out_dir)
    receipt["chars_usable"] = [u[0] for u in usable]

    os.chdir(REPO)
    try:
        from configs.fontdiffuser import get_parser
        from src import FontDiffuserModel, build_unet, build_style_encoder, \
            build_content_encoder, build_ddpm_scheduler
    except Exception as e:
        return fail("load", receipt, f"repo import failed: {e!r}\n{traceback.format_exc()}", out_dir)

    args = get_parser().parse_args([])
    args.style_image_size = (args.style_image_size, args.style_image_size)
    args.content_image_size = (args.content_image_size, args.content_image_size)
    t0 = time.time()
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
        return fail("load", receipt, f"model load failed: {e!r}", out_dir)
    receipt["load_seconds"] = round(time.time() - t0, 1)

    # 代表参数采样：每模块首/尾各一个张量（确定性）
    sample_idx = {}
    for mod in ("unet", "style_encoder", "content_encoder"):
        params = list(getattr(model, mod).parameters())
        sample_idx[mod] = {"first": params[0], "last": params[-1]}
    before = {f"{m}.{k}": tensor_sha(p) for m, d in sample_idx.items() for k, p in d.items()}

    tfm = T.Compose([T.Resize((size, size), interpolation=T.InterpolationMode.BILINEAR),
                     T.ToTensor(), T.Normalize([0.5], [0.5])])
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    opt = torch.optim.Adam(model.parameters(), lr=LR)

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

            torch.cuda.reset_peak_memory_stats(0)
            torch.cuda.synchronize(0)
            t0 = time.time()
            noise_pred, offset = model(x_t, t, style, content,
                                       content_encoder_downsample_size=args.content_encoder_downsample_size)
            loss_noise = F.mse_loss(noise_pred, noise)
            loss_offset = offset.pow(2).mean()
            loss = loss_noise + args.offset_coefficient * loss_offset
            loss.backward()
            opt.step()
            torch.cuda.synchronize(0)
            step_seconds = round(time.time() - t0, 2)
            loss_before = float(loss)

            # step 后再前向一次（同 batch/噪声）记录 loss 变化
            with torch.no_grad():
                noise_pred2, offset2 = model(x_t, t, style, content,
                                             content_encoder_downsample_size=args.content_encoder_downsample_size)
                loss_after = float(F.mse_loss(noise_pred2, noise)
                                   + args.offset_coefficient * offset2.pow(2).mean())

            after = {f"{m}.{k}": tensor_sha(p) for m, d in sample_idx.items() for k, p in d.items()}
            changed = [k for k in before if before[k] != after[k]]

            result = {
                "batch": b, "loss_before_step": loss_before, "loss_after_step": loss_after,
                "step_seconds": step_seconds,
                "vram_peak_alloc_mb": round(torch.cuda.max_memory_allocated(0) / 1048576, 1),
                "param_checksums_before": before, "param_checksums_after": after,
                "param_tensors_changed": changed, "param_tensors_changed_count": len(changed),
                "param_sampled": len(before),
            }
            receipt["one_step"] = result
            print(f"[m2step] B={b} loss {loss_before:.5f}->{loss_after:.5f} "
                  f"changed={len(changed)}/{len(before)} vram={result['vram_peak_alloc_mb']}MB "
                  f"step={step_seconds}s")
            break
        except torch.cuda.OutOfMemoryError as e:
            torch.cuda.empty_cache()
            receipt.setdefault("oom_attempts", []).append({"batch": b, "error": str(e)[:120]})
            continue
        except Exception as e:
            return fail("backward", receipt, f"{e!r}\n{traceback.format_exc()}", out_dir)

    if result is None:
        return fail("oom", receipt, "OOM at all batch sizes", out_dir)
    if result["param_tensors_changed_count"] == 0:
        return fail("param", receipt, "no parameter tensor changed after optimizer.step()", out_dir)

    receipt["no_checkpoint_saved"] = True
    receipt["statement"] = "one real optimizer step = feasibility evidence; NOT training start, NOT quality"
    receipt["finished"] = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"[m2step] OK receipt={out_dir / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
