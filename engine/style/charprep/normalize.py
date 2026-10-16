# -*- coding: utf-8 -*-
"""S02 charprep.normalize — 字块 alpha 规范化（E03 冻结单字规格：128×128 / fit 112 / 8px 边距）。

输入：P4 已确认字块（RGB 带 painted 底色，或 RGBA 带真 alpha）。
输出：128×128 RGBA，alpha=字形 coverage（软边缘保留），RGB=(0,0,0) 常量墨色。

显式常量（卡要求：二值化阈值/抗锯齿策略不许隐式）：
- 画布 128；fit 112；边距 8（=128-2*8，与 contracts/style-font-pipeline-v1.json
  /data_contracts/glyph_png "128×128 RGBA；等比缩放至 112×112 + 8px padding" 一致）。
- ALPHA_THRESHOLD=128 仅用于 ink 统计/裁剪判定；输出 alpha 保留灰度软边缘（抗锯齿不二值化，
  BINARIZE_OUTPUT=False）。二值化输出开关存在但默认关，若开则用同一阈值。
- 只缩小不放大（ALLOW_UPSCALE=False）→ normalize(normalize(x))==normalize(x) 幂等，
  重复缩放不变细（第二轮 fit 已满足 → 跳过重采样，像素不变）。

RGB 极性策略：真 alpha 通道存在（min<250）直接采用；否则按边缘中值亮度判底色极性：
浅底(≥BG_LIGHT_THRESHOLD)深字 → alpha=255-L；深底浅字 → alpha=L。同一字形深字浅底与
浅字深底规范化结果逐像素一致（反例测试覆盖）。
"""
from __future__ import annotations

import hashlib
import io
import statistics
from PIL import Image

# ---- E03 冻结单字规格（显式常量，勿隐式散落） ----
CANVAS_SIZE = 128
FIT_SIZE = 112
MARGIN_PX = 8  # (CANVAS_SIZE - FIT_SIZE) // 2

# ---- 二值化 / 抗锯齿策略（显式） ----
ALPHA_THRESHOLD = 128          # ink 判定阈值（0-255）
BINARIZE_OUTPUT = False        # 抗锯齿策略：保留软边缘 alpha；True 时以 ALPHA_THRESHOLD 二值化
RESAMPLE = Image.LANCZOS       # 等比缩放重采样
ALLOW_UPSCALE = False          # 只缩不放（幂等关键）

# ---- RGB 极性 ----
BG_LIGHT_THRESHOLD = 128       # 边缘中值亮度 ≥ 此值 → 浅底深字极性
OPAQUE_MIN = 250               # alpha 通道 min ≥ 此值 → 视为无真透明（走 RGB 极性推断）

INK_RGB = (0, 0, 0)            # 输出 RGBA 的 RGB 常量墨色（黑），alpha 携带 coverage


class NormalizeError(ValueError):
    """规范化拒绝（空字形/不可解码等）。code 用于测试与收据。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def sha256_image(img: Image.Image) -> str:
    """确定性 PNG 编码后取 sha256（作为 final_alpha_sha256 口径）。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return hashlib.sha256(buf.getvalue()).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_alpha(img: Image.Image):
    """→ (alpha L 模式图, 极性说明 str)。拒绝全透明/不可解码。"""
    try:
        im = img.convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise NormalizeError("E_UNDECODABLE", f"image cannot be decoded: {e!r}") from e
    a = im.getchannel("A")
    lo, hi = a.getextrema()
    if lo < OPAQUE_MIN:  # 真透明通道存在 → 直接采用（alpha 即 coverage）
        return a, "alpha_channel"
    # 无真透明 → RGB painted 底色，按极性推断
    l_img = im.convert("L")
    w, h = l_img.size
    border = []
    border.extend(l_img.getpixel((x, 0)) for x in range(w))
    border.extend(l_img.getpixel((x, h - 1)) for x in range(w))
    border.extend(l_img.getpixel((0, y)) for y in range(1, h - 1))
    border.extend(l_img.getpixel((w - 1, y)) for y in range(1, h - 1))
    bg = statistics.median(border)
    if bg >= BG_LIGHT_THRESHOLD:  # 浅底深字
        return l_img.point(lambda v: 255 - v), f"rgb_dark_on_light(bg={bg})"
    return l_img, f"rgb_light_on_dark(bg={bg})"  # 深底浅字


def normalize(img: Image.Image) -> Image.Image:
    """字块 → 128×128 RGBA（alpha=字形 coverage，等比 fit 112，居中，边距≥8）。

    幂等：对已规范化输出再次调用，像素逐位不变（不重采样、同位回贴）。
    抛 NormalizeError：E_UNDECODABLE / E_EMPTY_ALPHA（无任何非零 alpha）/ E_FULL_CANVAS
    （ink 铺满画布无留白 → 无法 fit，疑似整白块/涂鸦块）。
    """
    alpha, polarity = extract_alpha(img)
    if alpha.getextrema()[1] == 0:
        raise NormalizeError("E_EMPTY_ALPHA", "glyph has no ink (fully transparent)")
    if BINARIZE_OUTPUT:
        alpha = alpha.point(lambda v: 255 if v >= ALPHA_THRESHOLD else 0)
    # ink bbox（含软边缘：任何非零 alpha）
    bbox = alpha.point(lambda v: 255 if v > 0 else 0).getbbox()
    if bbox is None:
        raise NormalizeError("E_EMPTY_ALPHA", "no nonzero alpha after threshold pass")
    glyph = alpha.crop(bbox)
    gw, gh = glyph.size
    scale = min(FIT_SIZE / max(gw, gh), 1.0) if not ALLOW_UPSCALE else FIT_SIZE / max(gw, gh)
    if scale < 1.0:
        glyph = glyph.resize((max(1, round(gw * scale)), max(1, round(gh * scale))), RESAMPLE)
    gw, gh = glyph.size
    if gw > FIT_SIZE or gh > FIT_SIZE:
        raise NormalizeError("E_FIT_FAILED", f"glyph {gw}x{gh} exceeds fit {FIT_SIZE}")
    if gw >= CANVAS_SIZE or gh >= CANVAS_SIZE:
        raise NormalizeError("E_FULL_CANVAS", "ink spans full canvas; no margin possible")
    canvas = Image.new("L", (CANVAS_SIZE, CANVAS_SIZE), 0)
    canvas.paste(glyph, ((CANVAS_SIZE - gw) // 2, (CANVAS_SIZE - gh) // 2))
    out = Image.merge("RGBA", (
        Image.new("L", canvas.size, INK_RGB[0]),
        Image.new("L", canvas.size, INK_RGB[1]),
        Image.new("L", canvas.size, INK_RGB[2]),
        canvas,
    ))
    out.info["charprep_polarity"] = polarity  # 便于 inspect；不参与像素/哈希
    return out


def ink_stats(img: Image.Image) -> dict:
    """规范化输出的 ink 统计（按 ALPHA_THRESHOLD）。"""
    a = img.convert("RGBA").getchannel("A")
    px = list(a.getdata())
    total = len(px)
    ink = sum(1 for v in px if v >= ALPHA_THRESHOLD)
    return {"total_px": total, "ink_px": ink,
            "ink_ratio": round(ink / total, 4) if total else 0.0,
            "alpha_nonzero_px": sum(1 for v in px if v > 0)}
