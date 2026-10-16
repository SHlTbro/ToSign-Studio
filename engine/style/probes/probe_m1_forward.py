# -*- coding: utf-8 -*-
"""S01 M1 前向可行性探针：只读加载 FontDiffuser 三件套 checkpoint，冻结推理生成目标汉字。

- checkpoint：artifacts/style-font/FontDiffuser-Gradio/ckpt/*.pth（只读，先核 sha256）
- style ref：artifacts/t20_glyphs/glyphs/25.png（历史研究资产，非 B10/SEALED）
- 目标字：由 KaiXinSongA.ttf 内容渲染（character_input 模式）
- 记录：显存峰值、加载/采样耗时、输出尺寸/mode、PNG sha256、seed、透明层派生检查
- 不训练、不保存任何 checkpoint、不改装 vendor/checkpoint

运行：.venv-ml/Scripts/python.exe -X utf8 engine/style/probes/probe_m1_forward.py [--device cuda:0]
退出码：0 成功；10 E_CUDA_UNAVAILABLE；11 E_CKPT_MISSING；12 E_LOAD_FAILED；
        13 E_SAMPLE_FAILED；14 E_OUTPUT_INVALID；15 E_CHAR_NOT_IN_FONT
"""
import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]
REPO = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio"
REF_GLYPH = PROJ / "artifacts" / "t20_glyphs" / "glyphs" / "25.png"
SEED = 20260927
CHARS = ["也", "国"]

E = {
    "cuda": 10, "ckpt": 11, "load": 12, "sample": 13, "output": 14, "char": 15,
}


def sha256_file(p: Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fail(code_key, receipt, msg):
    receipt["failure"] = {"code": f"E_{code_key.upper()}", "exit": E[code_key], "detail": msg}
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out / f"m1_receipt_FAIL_{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[FAIL] E_{code_key.upper()}: {msg}")
    return E[code_key]


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--device", default="cuda:0")
    cli.add_argument("--chars", default=",".join(CHARS))
    cli.add_argument("--seed", type=int, default=SEED)
    cli_args = cli.parse_args()
    chars = [c for c in cli_args.chars.split(",") if c]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent / "out" / f"m1_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt = {"probe": "m1_forward", "started": stamp, "seed": cli_args.seed,
               "chars_requested": chars, "ref_glyph": str(REF_GLYPH), "outputs": []}

    # 1. checkpoint 存在性与 hash（只读）
    ckpts = {}
    for name in ("content_encoder", "style_encoder", "unet"):
        p = REPO / "ckpt" / f"{name}.pth"
        if not p.exists():
            return fail("ckpt", receipt, f"missing {p}")
        t0 = time.time()
        ckpts[name] = {"bytes": p.stat().st_size, "sha256": sha256_file(p),
                       "hash_seconds": round(time.time() - t0, 1)}
    receipt["ckpt"] = ckpts
    if not REF_GLYPH.exists():
        return fail("ckpt", receipt, f"missing style ref {REF_GLYPH}")

    # 2. 环境
    try:
        import torch
        from PIL import Image
    except Exception as e:
        return fail("load", receipt, f"import failed: {e!r}")
    if not torch.cuda.is_available() and cli_args.device.startswith("cuda"):
        return fail("cuda", receipt, "torch.cuda.is_available()=False")
    device = cli_args.device if torch.cuda.is_available() else "cpu"
    free, total = torch.cuda.mem_get_info(0)
    receipt["gpu"] = {"name": torch.cuda.get_device_name(0),
                      "free_mb_before": round(free / 1048576),
                      "total_mb": round(total / 1048576)}
    receipt["torch"] = torch.__version__

    # 3. style ref 归一化（RGBA→白底 RGB，与历史 s2 run 同法），写本探针 out 目录
    from PIL import Image
    im = Image.open(REF_GLYPH).convert("RGBA")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    bg.paste(im, mask=im.split()[3])
    ref_png = out_dir / "style_ref_25.png"
    bg.save(ref_png)
    receipt["ref_sha256"] = sha256_file(ref_png)

    # 4. 加载 pipeline（vendor 代码只读 import）
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)  # ttf 相对路径
    try:
        from configs.fontdiffuser import get_parser
        import sample as S
        from utils import is_char_in_font
    except Exception as e:
        return fail("load", receipt, f"repo import failed: {e!r}\n{traceback.format_exc()}")

    args = get_parser().parse_args([])
    args.ckpt_dir = str(REPO / "ckpt")
    args.ttf_path = str(REPO / "ttf" / "KaiXinSongA.ttf")
    args.device = device
    args.demo = True
    args.character_input = True
    args.seed = cli_args.seed
    args.save_image = False
    args.style_image_size = (args.style_image_size, args.style_image_size)
    args.content_image_size = (args.content_image_size, args.content_image_size)

    t0 = time.time()
    try:
        pipe = S.load_fontdiffuer_pipeline(args)
    except Exception as e:
        return fail("load", receipt, f"pipeline load failed: {e!r}\n{traceback.format_exc()}")
    load_seconds = round(time.time() - t0, 1)
    receipt["load_seconds"] = load_seconds
    print(f"[m1] pipeline loaded in {load_seconds}s")

    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats(0)

    # 5. 逐字采样
    style_img = Image.open(ref_png).convert("RGB")
    alpha_report_ok = True
    for ch in chars:
        if not is_char_in_font(font_path=str(args.ttf_path), char=ch):
            receipt["outputs"].append({"char": ch, "skipped": "not_in_font"})
            continue
        args.seed = cli_args.seed  # 每字固定 seed，保证可复现
        args.content_character = ch  # get_parser 不含该字段，sampling→image_process 需要
        t1 = time.time()
        try:
            img = S.sampling(args=args, pipe=pipe, content_image=None, style_image=style_img)
        except Exception as e:
            return fail("sample", receipt, f"sampling '{ch}' failed: {e!r}\n{traceback.format_exc()}")
        if device.startswith("cuda"):
            torch.cuda.synchronize(0)
        seconds = round(time.time() - t1, 2)
        if img is None:
            receipt["outputs"].append({"char": ch, "skipped": "sampling_returned_None"})
            continue

        fn = out_dir / f"m1_{ch}_ref25_seed{cli_args.seed}.png"
        img.save(fn)
        # 输出可解码性 + 透明层派生正确性（白底 RGB → ink alpha）
        try:
            check = Image.open(fn)
            check.verify()
            im2 = Image.open(fn)
            size, mode = im2.size, im2.mode
            g = im2.convert("L")
            px = list(g.getdata())
            total_px = len(px)
            opaque = sum(1 for v in px if v < 128)  # ink（深色）→ alpha 高
            if not (0 < opaque < total_px):
                alpha_ok, alpha_note = False, f"degenerate ink ratio {opaque}/{total_px}"
            else:
                # 极性回检：ink→不透明，合成回白底应还原原图
                rgba = Image.merge("RGBA", (*im2.convert("RGB").split(), g.point(lambda v: 255 - v)))
                comp = Image.new("RGB", rgba.size, (255, 255, 255))
                comp.paste(rgba, mask=rgba.split()[3])
                diff = max(abs(a - b) for a, b in zip(comp.convert("L").getdata(), px))
                alpha_ok = diff <= 128  # 二值化容忍度
                alpha_note = f"composite_max_diff={diff}"
            if not alpha_ok:
                alpha_report_ok = False
            receipt["outputs"].append({
                "char": ch, "file": fn.name, "seconds": seconds, "size": size, "mode": mode,
                "png_sha256": sha256_file(fn), "ink_px": opaque, "alpha_derivation_ok": alpha_ok,
                "alpha_note": alpha_note,
                "vram_peak_alloc_mb": round(torch.cuda.max_memory_allocated(0) / 1048576, 1) if device.startswith("cuda") else None,
                "vram_peak_reserved_mb": round(torch.cuda.max_memory_reserved(0) / 1048576, 1) if device.startswith("cuda") else None,
            })
            print(f"[m1] {ch}: {seconds}s size={size} mode={mode} "
                  f"vram_peak={receipt['outputs'][-1]['vram_peak_alloc_mb']}MB sha256={receipt['outputs'][-1]['png_sha256'][:12]}")
        except Exception as e:
            return fail("output", receipt, f"output validation '{ch}' failed: {e!r}")

    done = [o for o in receipt["outputs"] if "file" in o]
    if not done:
        return fail("char", receipt, "no char produced output (all not-in-font or None)")

    receipt["alpha_report_ok"] = alpha_report_ok
    receipt["finished"] = time.strftime("%Y%m%d_%H%M%S")
    (out_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"[m1] OK: {len(done)}/{len(chars)} chars, receipt={out_dir / 'receipt.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
