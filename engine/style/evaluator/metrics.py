# -*- coding: utf-8 -*-
"""S08 evaluator.metrics — 逐字自动 proxy 指标与失败分类账。

【proxy 声明，卡 S08"区分自动 proxy 和真人视觉"】
本模块全部指标是**结构代理**：
- "字形/内容正确"用输出 vs 目标 GT 的结构代理带近似（ink 比例 / 质心 / 连通域数），
  **不是 OCR、不是真人判定**；wrong_char_proxy 只是"结构与目标字不一致"的标记。
- coverage = 合成后非空非满（反例：空白输出被高背景 accuracy 奖励 / 整块填满）。
- 透明层健全 = alpha 非空非全 + RGB-alpha 一致性（合成回读），对应卡面"背景泄漏/透明"。
- 风格相似 proxy = 与 style ref 的统计距离（ink 比例/墨色均值/亮度直方），
  仅记录数值与校准参考，**不做成败判定**（风格质量由 S09 真人盲评判）。

失败分类账（每字可多标签，全部显式报告，不聚合掩盖）：
  blank / full_fill / wrong_char_proxy / decode_fail / transparency_broken

阈值不隐藏：全部带名放在 EVAL_CONFIG，随收据落盘（config_hash 可复算）。
纯 CPU、零第三方依赖（PIL 之外不 import 任何重库；连通域为 8-连通 union-find，无 scipy）。
"""
from __future__ import annotations

from PIL import Image

# 评估口径版本（bands 语义变了必须升版本，防跨版本混比——卡 S08"分数无版本映射"反例）
METRICS_VERSION = "eval-metrics/2.0-alpha-contract"

# ---- 评估配置（全部阈值显式命名；进收据 config，config_hash 可复算） ----
EVAL_CONFIG = {
    "ink_threshold": 128,          # ink = 合成 luma < 128（与 charprep ALPHA_THRESHOLD / m1 adapter 同口径）
    "blank_min_ink_px": 16,        # 绝对下限（对齐 m1_adapter.INK_MIN_PX）
    "blank_max_ratio": 0.003,      # 相对下限：ink 占比 < 0.3% → blank
    "full_fill_ratio": 0.85,       # ink 占比 > 85% → full_fill
    "composite_tolerance": 128,    # RGBA 合成回读 vs RGB luma 最大差容忍（对齐 S01/S04 口径）
    "ratio_band_low_factor": 0.4,  # 结构带：ink 比 < 0.4×目标 → 轴出带
    "ratio_band_high_factor": 1.8, # ink 比 > 1.8×目标 + abs → 轴出带
    "ratio_band_high_abs": 0.05,
    "centroid_max_dist": 0.22,     # 归一化质心欧氏距离上限（图像边长为 1）
    "cc_low_slack": 1,             # 连通域带：cc_out >= cc_tgt - slack（下限 1）
    "cc_high_factor": 2.0,         # cc_out <= cc_tgt * factor + abs
    "cc_high_abs": 2,
    "wrong_char_axes_min": 2,      # 3 轴中 ≥2 轴出带 → wrong_char_proxy
    "style_band_slack": 0.15,      # 风格 proxy 距离相对校准参考的记录带（仅记录，不成败）
}

FAIL_BLANK = "blank"
FAIL_FULL_FILL = "full_fill"
FAIL_WRONG_CHAR = "wrong_char_proxy"
FAIL_DECODE = "decode_fail"
FAIL_TRANSPARENCY = "transparency_broken"
FAIL_CLASSES = (FAIL_BLANK, FAIL_FULL_FILL, FAIL_WRONG_CHAR, FAIL_DECODE, FAIL_TRANSPARENCY)


class EvalError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


# ---------------------------------------------------------------- 基础像素工具

def composite_luma(img: Image.Image):
    """任意 PIL 图 → (合成白底 L 图, alpha 健康信息 dict)。

    RGBA/LA：alpha 合成到白底后取 L（render 实际消费的墨迹口径）；
    RGB/L：直接取 L，alpha 视为"派生"（alpha_present=False）。
    """
    info = {"alpha_present": False, "alpha_derived": True,
            "alpha_opaque_px": None, "alpha_empty": None, "alpha_full": None,
            "composite_max_diff": None}
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = rgba.split()[3]
        a_px = list(alpha.getdata())
        n = len(a_px)
        opaque = sum(1 for v in a_px if v >= 128)
        info["alpha_present"] = True
        info["alpha_derived"] = False
        info["alpha_opaque_px"] = opaque
        info["alpha_empty"] = opaque == 0
        info["alpha_full"] = opaque == n
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=alpha)
        comp_l = bg.convert("L")
        # Check the alpha compositing equation, not raw transparent RGB. Black ink
        # with transparent black background is a valid straight-alpha encoding.
        expected=[round(255*(1-a/255)+v*a/255) for a,v in zip(a_px,rgba.convert("L").getdata())]
        diff = max(abs(a-b) for a,b in zip(comp_l.getdata(),expected))
        info["composite_max_diff"] = diff
        return comp_l, info
    return img.convert("L"), info


def glyph_luma(img: Image.Image):
    """S03 glyphs encode ink in alpha; hidden RGB is not the glyph color."""
    if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
        from PIL import ImageOps
        return ImageOps.invert(img.convert('RGBA').getchannel('A'))
    return img.convert('L')


def ink_mask(l_img: Image.Image, threshold: int):
    """luma → (bytearray 0/1, w, h)。ink = luma < threshold（深色墨）。"""
    w, h = l_img.size
    px = list(l_img.getdata())
    mask = bytearray(1 if v < threshold else 0 for v in px)
    return mask, w, h


def ink_stats(mask, w: int, h: int) -> dict:
    """ink 比例 / 归一化质心（边长为 1 的坐标系）。"""
    n = w * h
    total = 0
    sx = sy = 0
    for i, v in enumerate(mask):
        if v:
            total += 1
            sx += i % w
            sy += i // w
    ratio = total / n if n else 0.0
    if total:
        centroid = (round(sx / total / w, 6), round(sy / total / h, 6))
    else:
        centroid = None
    return {"ink_px": total, "ink_ratio": round(ratio, 6), "centroid": centroid}


def connected_components(mask, w: int, h: int) -> int:
    """8-连通域计数（union-find，迭代；无 scipy 依赖，与 charprep.split 同思路）。"""
    n = w * h
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        if not mask[i]:
            continue
        x, y = i % w, i // w
        for dx, dy in ((-1, 0), (-1, -1), (0, -1), (1, -1)):  # 已扫过的左/上邻域
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if mask[j]:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    roots = set()
    for i in range(n):
        if mask[i]:
            roots.add(find(i))
    return len(roots)


def load_target_ink(path) -> dict:
    """目标 GT 图（快照 alpha PNG 或白底 RGB 历史字模）→ ink 统计 + 连通域。

    alpha 图：ink = alpha>=128（GT 的本义是 alpha 层，颜色无关）；
    无 alpha 图：ink = luma<128（与输出同口径）。
    """
    img = Image.open(path)
    img.load()
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        a = rgba.split()[3]
        w, h = rgba.size
        mask = bytearray(1 if v >= 128 else 0 for v in a.getdata())
        stats = ink_stats(mask, w, h)
    else:
        l_img, _ = composite_luma(img)
        mask, w, h = ink_mask(l_img, EVAL_CONFIG["ink_threshold"])
        stats = ink_stats(mask, w, h)
    stats["cc"] = connected_components(mask, w, h)
    stats["size"] = [w, h]
    return stats


# ---------------------------------------------------------------- 风格统计 proxy

_HIST_BINS = 8


def style_features(l_img: Image.Image) -> dict:
    """白底 L 图 → 统计特征（ink 比例 / 墨内均值亮度 / 全图亮度 8-bin 直方）。"""
    mask, w, h = ink_mask(l_img, EVAL_CONFIG["ink_threshold"])
    stats = ink_stats(mask, w, h)
    px = list(l_img.getdata())
    hist = [0] * _HIST_BINS
    for v in px:
        hist[min(v * _HIST_BINS // 256, _HIST_BINS - 1)] += 1
    n = len(px)
    ink_sum = ink_n = 0
    for v, m in zip(px, mask):
        if m:
            ink_sum += v
            ink_n += 1
    return {
        "ink_ratio": stats["ink_ratio"],
        "ink_mean_luma": round(ink_sum / ink_n, 3) if ink_n else None,
        "luma_hist": [round(c / n, 6) for c in hist],
    }


def style_distance(fa: dict, fb: dict) -> float:
    """统计距离 proxy ∈ [0, ~3]：ink 比差 ×2 + 墨色亮度差/255 + 0.5×直方 L1。仅记录。"""
    d = abs(fa["ink_ratio"] - fb["ink_ratio"]) * 2.0
    la, lb = fa["ink_mean_luma"], fb["ink_mean_luma"]
    if la is not None and lb is not None:
        d += abs(la - lb) / 255.0
    else:
        d += 1.0  # 一方无 ink（空白）——距离按最坏记
    d += 0.5 * sum(abs(x - y) for x, y in zip(fa["luma_hist"], fb["luma_hist"]))
    return round(d, 6)


# ---------------------------------------------------------------- 逐字主指标

def evaluate_char(out_img: Image.Image, target_stats: dict,
                  ref_lumas: list[Image.Image], other_target_stats: list[dict],
                  config: dict = EVAL_CONFIG) -> dict:
    """单字评估 → {metrics, fail_classes, pass_proxy}。

    out_img           生成输出（RGBA 96×96 或任意可解码图）
    target_stats      目标 GT 的 load_target_ink 结果
    ref_lumas         style ref 白底 L 图列表（风格 proxy 用）
    other_target_stats 同批其他目标 GT 统计（cross-match 诊断用；可为空）
    """
    metrics_out: dict = {}
    fail: list[str] = []

    # 1) coverage + 透明层（在合成白底 luma 上判，render 消费口径）
    try:
        out_l, alpha_info = composite_luma(out_img)
    except Exception as e:  # noqa: BLE001
        return {"metrics": {"decode": f"composite failed: {e!r}"},
                "fail_classes": [FAIL_DECODE], "pass_proxy": False}
    mask, w, h = ink_mask(out_l, config["ink_threshold"])
    stats = ink_stats(mask, w, h)
    n_px = w * h
    blank_floor = max(config["blank_min_ink_px"],
                      int(config["blank_max_ratio"] * n_px) + 1)
    metrics_out["size"] = [w, h]
    metrics_out["coverage"] = {
        "ink_px": stats["ink_px"], "ink_ratio": stats["ink_ratio"],
        "blank_floor_px": blank_floor, "full_fill_ratio": config["full_fill_ratio"],
    }
    if stats["ink_px"] < blank_floor:
        fail.append(FAIL_BLANK)
    if stats["ink_ratio"] > config["full_fill_ratio"]:
        fail.append(FAIL_FULL_FILL)

    # 2) 透明层健全
    metrics_out["transparency"] = alpha_info
    if alpha_info["alpha_present"]:
        if alpha_info["alpha_empty"] or alpha_info["alpha_full"]:
            fail.append(FAIL_TRANSPARENCY)
        if alpha_info["composite_max_diff"] is not None and \
           alpha_info["composite_max_diff"] > config["composite_tolerance"]:
            fail.append(FAIL_TRANSPARENCY)

    # 3) 结构 proxy vs 目标 GT（3 轴：ink 比 / 质心 / 连通域；proxy 非人判）
    axes = {}
    r_t = target_stats["ink_ratio"]
    lo = max(blank_floor / n_px, config["ratio_band_low_factor"] * r_t)
    hi = config["ratio_band_high_factor"] * r_t + config["ratio_band_high_abs"]
    axes["ink_ratio"] = {"out": stats["ink_ratio"], "target": r_t,
                         "band": [round(lo, 6), round(hi, 6)],
                         "out_of_band": not (lo <= stats["ink_ratio"] <= hi)}
    if stats["centroid"] is None or target_stats["centroid"] is None:
        cdist = None
        axes["centroid"] = {"out": stats["centroid"], "target": target_stats["centroid"],
                            "dist": None, "out_of_band": True}
    else:
        cdist = ((stats["centroid"][0] - target_stats["centroid"][0]) ** 2
                 + (stats["centroid"][1] - target_stats["centroid"][1]) ** 2) ** 0.5
        cdist = round(cdist, 6)
        axes["centroid"] = {"out": stats["centroid"], "target": target_stats["centroid"],
                            "dist": cdist,
                            "out_of_band": cdist > config["centroid_max_dist"]}
    cc_out = connected_components(mask, w, h)
    cc_lo = max(1, target_stats["cc"] - config["cc_low_slack"])
    cc_hi = int(config["cc_high_factor"] * target_stats["cc"] + config["cc_high_abs"])
    axes["cc"] = {"out": cc_out, "target": target_stats["cc"],
                  "band": [cc_lo, cc_hi], "out_of_band": not (cc_lo <= cc_out <= cc_hi)}
    metrics_out["structure_proxy"] = {
        "declaration": "结构代理（ink比/质心/连通域），非 OCR、非真人字形判定",
        "axes": axes,
    }
    n_out_of_band = sum(1 for a in axes.values() if a["out_of_band"])
    if n_out_of_band >= config["wrong_char_axes_min"]:
        fail.append(FAIL_WRONG_CHAR)
    metrics_out["structure_proxy"]["axes_out_of_band"] = n_out_of_band

    # 4) cross-match 诊断（输出结构离哪个目标更近；仅诊断，不单独定败）
    def _struct_dist(st: dict) -> float | None:
        if stats["centroid"] is None or st["centroid"] is None:
            return None
        d = ((stats["centroid"][0] - st["centroid"][0]) ** 2
             + (stats["centroid"][1] - st["centroid"][1]) ** 2) ** 0.5
        return round(d + abs(stats["ink_ratio"] - st["ink_ratio"]), 6)

    d_own = _struct_dist(target_stats)
    cross = {"own_target_dist": d_own, "nearest_other": None}
    if other_target_stats:
        dists = [(_struct_dist(st), i) for i, st in enumerate(other_target_stats)
                 if _struct_dist(st) is not None]
        if dists:
            dmin, imin = min(dists)
            cross["nearest_other"] = {"index": imin, "dist": dmin,
                                      "beats_own": bool(d_own is not None and dmin < d_own)}
    metrics_out["cross_match_diagnostic"] = cross

    # 5) 风格相似 proxy（统计距离；仅记录 + 校准参考，不做成败判定）
    out_feat = style_features(out_l)
    ref_feats = [style_features(r) for r in ref_lumas]
    if ref_feats:
        d_ref = round(sum(style_distance(out_feat, rf) for rf in ref_feats) / len(ref_feats), 6)
        metrics_out["style_proxy"] = {
            "declaration": "统计距离 proxy（ink比/墨色/亮度直方），非风格质量判定；质量归 S09 真人盲评",
            "out_vs_ref": d_ref, "refs": len(ref_feats),
            "style_band_slack": config["style_band_slack"],
            # 校准参考（目标 GT 自身 vs ref 的距离）由 run_eval 聚合层计算补入
            "calibration_target_vs_ref": None,
        }
    else:
        metrics_out["style_proxy"] = {"declaration": "无 style ref，未计算", "out_vs_ref": None}

    fail = [c for c in FAIL_CLASSES if c in fail]  # 规范顺序去重
    return {"metrics": metrics_out, "fail_classes": fail, "pass_proxy": not fail}
