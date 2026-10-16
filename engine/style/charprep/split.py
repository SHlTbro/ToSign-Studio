# -*- coding: utf-8 -*-
"""S02 charprep.split — 字块拆字（连通域 + 列聚类 + 投影谷），两级输出，不自动定案。

输入：已确认字块（RGB/RGBA）。输出：候选单字 bbox 列表，每项 level ∈ {candidate, uncertain}。
**永不输出 owner_confirmed** —— 拆字结果必须经真实视觉确认（S02 DoD），确认走 manifest.confirm。

算法（纯 numpy/PIL，无 scipy 依赖）：
1. alpha 提取（复用 normalize 极性策略）→ 二值 ink mask（ALPHA_THRESHOLD）。
2. 8-连通域标记（两遍扫描 + union-find）。
3. 组件按 x-区间重叠/近邻（MERGE_GAP_X）聚成列簇 → 一个簇 ≈ 一个候选字。
4. 簇级判定：
   - 单簇且 aspect = w/h > SPLIT_HINT_ASPECT（横向连体嫌疑）→ 列投影找谷
     （中央带 [0.35W,0.65W] 最小值，两侧需各有一个峰 ≥ PEAK_RATIO×带内最大），
     谷深 ≥ VALLEY_MIN_DEPTH 才拆，拆出部分各自 bbox；谷不明确 → 整块 uncertain。
   - 簇间宽度极不均（min/max < WIDTH_DISPERSION_MIN）→ 全部 uncertain。
   - 其余 → candidate。
任何情况下拆出的只是 candidate/uncertain；uncertain 不准导出、不准确认流水线自动通过。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .normalize import ALPHA_THRESHOLD, extract_alpha

MERGE_GAP_X = 2            # 组件 x-区间间隙 ≤ 此值并入同一列簇
SPLIT_HINT_ASPECT = 1.35   # 单簇 w/h 超此值 → 触发投影谷拆分
VALLEY_BAND = (0.35, 0.65) # 谷搜索中央带
VALLEY_MIN_DEPTH = 0.60    # 谷列 ink ≤ 带内峰 × 此值才算有效谷
PEAK_RATIO = 0.35          # 两侧峰须 ≥ 带内最大投影 × 此值
WIDTH_DISPERSION_MIN = 0.25
MIN_CLUSTER_PX = 12        # 少于此 ink 像素的簇视为噪声（水印/杂点分离信号）


class SplitError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def _binary_mask(img: Image.Image) -> np.ndarray:
    alpha, _ = extract_alpha(img)
    a = np.asarray(alpha, dtype=np.uint8)
    if int(a.max()) == 0:
        raise SplitError("E_EMPTY_ALPHA", "block has no ink")
    return (a >= ALPHA_THRESHOLD).astype(np.uint8)


def _components(mask: np.ndarray):
    """8-连通域两遍扫描，返回 list[(ys, xs)] 像素索引组。"""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    parent = [0]

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a_: int, b_: int) -> None:
        ra, rb = find(a_), find(b_)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nxt = 1
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x]:
                continue
            up = labels[y - 1, x] if y > 0 else 0
            left = labels[y, x - 1] if x > 0 else 0
            ul = labels[y - 1, x - 1] if (y > 0 and x > 0) else 0
            ur = labels[y - 1, x + 1] if (y > 0 and x + 1 < w) else 0
            neigh = [n for n in (up, left, ul, ur) if n]
            if not neigh:
                labels[y, x] = nxt
                parent.append(nxt)
                nxt += 1
            else:
                m = min(find(n) for n in neigh)
                labels[y, x] = m
                for n in neigh:
                    union(m, n)
    groups: dict[int, list[int]] = {}
    ys, xs = np.nonzero(labels)
    for y, x in zip(ys.tolist(), xs.tolist()):
        root = find(int(labels[y, x]))
        groups.setdefault(root, []).append(y * w + x)
    comps = []
    for px_list in groups.values():
        arr = np.array(px_list, dtype=np.int64)
        comps.append((arr // w, arr % w))
    comps.sort(key=lambda c: int(c[1].min()))
    return comps


def _cluster_columns(comps, w: int):
    """组件按 x-区间（+MERGE_GAP_X 容差）聚簇 → [(x0,x1, comp_idx...)]。"""
    spans = []
    for i, (ys, xs) in enumerate(comps):
        spans.append((int(xs.min()), int(xs.max()), i))
    clusters = []
    for x0, x1, i in spans:
        if clusters and x0 <= clusters[-1][1] + MERGE_GAP_X:
            c = clusters[-1]
            clusters[-1] = (c[0], max(c[1], x1), c[2] + [i])
        else:
            clusters.append((x0, x1, [i]))
    return clusters


def _valley_split(mask: np.ndarray, x0: int, x1: int):
    """列投影谷拆分 → 拆点 x 或 None。"""
    h, w = mask.shape
    proj = mask[:, x0:x1 + 1].sum(axis=0).astype(np.int64)
    n = x1 - x0 + 1
    lo, hi = int(n * VALLEY_BAND[0]), int(n * VALLEY_BAND[1])
    if hi - lo < 2:
        return None
    band = proj[lo:hi + 1]
    peak = int(band.max())
    if peak == 0:
        return None
    # 候选谷：带内最小值位置（取最靠中央的极小平台中点）
    vmin = int(band.min())
    if vmin > peak * VALLEY_MIN_DEPTH:
        return None
    idxs = np.nonzero(band == vmin)[0]
    # 两侧峰检查（谷左右 3px 外各找最大）
    vpos = lo + int(idxs[len(idxs) // 2])
    left = proj[max(0, vpos - max(3, n // 8)):vpos]
    right = proj[vpos + 1:min(n, vpos + 1 + max(3, n // 8))]
    if len(left) == 0 or len(right) == 0:
        return None
    if left.max() < peak * PEAK_RATIO or right.max() < peak * PEAK_RATIO:
        return None
    return x0 + vpos


def split_block(img: Image.Image) -> dict:
    """字块 → {"candidates": [{bbox, level, reason}...], "n_components": int}。

    bbox 为字块图内像素坐标 [x, y, w, h]；level 只取 candidate/uncertain。
    """
    mask = _binary_mask(img)
    h, w = mask.shape
    comps = [c for c in _components(mask) if len(c[0]) >= MIN_CLUSTER_PX]
    if not comps:
        raise SplitError("E_EMPTY_ALPHA", "no component above noise floor")
    clusters = _cluster_columns(comps, w)
    out = []
    if len(clusters) == 1:
        x0, x1, idx = clusters[0]
        ys_all = np.concatenate([comps[i][0] for i in idx])
        cw, ch = x1 - x0 + 1, int(ys_all.max() - ys_all.min() + 1)
        aspect = cw / max(ch, 1)
        if aspect > SPLIT_HINT_ASPECT:
            v = _valley_split(mask, x0, x1)
            if v is None:
                out.append({"bbox": [x0, int(ys_all.min()), cw, ch],
                            "level": "uncertain",
                            "reason": f"wide_blob aspect={aspect:.2f}>{SPLIT_HINT_ASPECT} no_confident_valley"})
            else:
                for (a, b, tag) in ((x0, v, "left"), (v + 1, x1, "right")):
                    sub = mask[:, a:b + 1]
                    ys2, xs2 = np.nonzero(sub)
                    if len(ys2) == 0:
                        continue
                    out.append({"bbox": [a + int(xs2.min()), int(ys2.min()),
                                         int(xs2.max() - xs2.min() + 1),
                                         int(ys2.max() - ys2.min() + 1)],
                                "level": "candidate",
                                "reason": f"projection_valley_{tag}@x={v}"})
        else:
            out.append({"bbox": [x0, int(ys_all.min()), cw, ch],
                        "level": "candidate", "reason": f"single_component aspect={aspect:.2f}"})
    else:
        widths = [c[1] - c[0] + 1 for c in clusters]
        disp = min(widths) / max(widths)
        for k, (x0, x1, idx) in enumerate(clusters):
            ys_all = np.concatenate([comps[i][0] for i in idx])
            cw = x1 - x0 + 1
            ch = int(ys_all.max() - ys_all.min() + 1)
            lvl = "candidate"
            reason = f"gap_separated cluster={k + 1}/{len(clusters)}"
            if disp < WIDTH_DISPERSION_MIN:
                lvl = "uncertain"
                reason += f" width_dispersion={disp:.2f}<{WIDTH_DISPERSION_MIN}"
            out.append({"bbox": [x0, int(ys_all.min()), cw, ch],
                        "level": lvl, "reason": reason})
    return {"candidates": out, "n_components": len(comps),
            "block_size": [w, h]}
