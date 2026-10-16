# -*- coding: utf-8 -*-
"""S02 charprep 反例测试（纯 CPU，.venv/Scripts/python.exe，无 torch 依赖）。

夹具来源（receipt 登记）：
(a) 程序合成 alpha 字块：fonts/presets/Mengshen-Handwritten.ttf（应用预置字体）渲染
    星(U+661F)/河(U+6CB3) 后脚本拼装——不触 materials/ B10、B7/B9 SEALED。
(b) 历史研究资产 artifacts/style-font/glyph_rebuild/clean/*.png 仅另用（只读，S04 ref 用），
    本测试不依赖。

覆盖反例（卡 S02 风险列）：
 T1 双字相连"星河"拆两个          T5 重复缩放幂等（不变细）
 T2 全透明块拒                    T6 bbox 跨坐标系/越界拒绝
 T3 整白块拒                      T7 码点不一致/组合码点/变体选择子拒绝
 T4 RGB 极性（深字浅底/浅字深底）  T8 空 manifest 拒导出
                                  T9 owner_confirmed 只能显式写入（--confirm-by 通道）

运行：.venv/Scripts/python.exe -X utf8 engine/style/charprep/tests/run_tests.py
退出码：0 全过；1 有失败。收据：tests/receipts/charprep-tests-<stamp>.json
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJ = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJ))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from engine.style.charprep.manifest import (  # noqa: E402
    Manifest, ManifestError, REVIEW_CANDIDATE)
from engine.style.charprep.normalize import (  # noqa: E402
    NormalizeError, normalize, sha256_image)
from engine.style.charprep.split import split_block  # noqa: E402

FONT_PATH = PROJ / "fonts" / "presets" / "Mengshen-Handwritten.ttf"
FIXTURE_SRC = "script-synthesized from fonts/presets/Mengshen-Handwritten.ttf (app preset, non-B10/B7/B9)"


def render_char(ch: str, size: int = 96, ink=(0, 0, 0), bg=(255, 255, 255)) -> Image.Image:
    """单字渲染成 RGB 字块（带 painted 底色）。"""
    font = ImageFont.truetype(str(FONT_PATH), size)
    pad = 8
    img = Image.new("RGB", (size + pad * 2, size + pad * 2), bg)
    ImageDraw.Draw(img).text((pad, pad), ch, font=font, fill=ink)
    return img


def crop_ink(img: Image.Image) -> Image.Image:
    a = img.convert("L").point(lambda v: 255 if v < 128 else 0)
    return img.crop(a.getbbox())


def paste_block(ch: str, overlap_px: int = 1) -> Image.Image:
    """双字横向拼块：bbox 交叠 overlap_px 像素（模拟连笔/紧凑书写）。"""
    left, right = crop_ink(render_char("星")), crop_ink(render_char("河"))
    h = max(left.height, right.height)
    w = left.width + right.width - overlap_px
    img = Image.new("RGB", (w, h), (255, 255, 255))
    img.paste(left, (0, h - left.height))
    img.paste(right, (left.width - overlap_px, h - right.height))
    return img


def norm_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    normalize(img).save(buf, format="PNG")
    return buf.getvalue()


class CharPrepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="charprep-test-"))

    # T1 双字相连
    def test_t1_split_two_connected_chars(self):
        res = split_block(paste_block("星河", overlap_px=1))
        cands = res["candidates"]
        self.assertEqual(len(cands), 2, f"expected 2 chars, got {cands}")
        for c in cands:
            self.assertIn(c["level"], ("candidate", "uncertain"))
            self.assertNotEqual(c["level"], "owner_confirmed")
        widths = sorted(c["bbox"][2] for c in cands)
        self.assertLess(widths[0] / widths[1], 1.2, f"two halves should be similar width: {widths}")

    # T2 全透明
    def test_t2_fully_transparent_rejected(self):
        img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        with self.assertRaises(NormalizeError) as cm:
            normalize(img)
        self.assertEqual(cm.exception.code, "E_EMPTY_ALPHA")

    # T3 整白块
    def test_t3_fully_white_rejected(self):
        img = Image.new("RGB", (128, 128), (255, 255, 255))
        with self.assertRaises(NormalizeError) as cm:
            normalize(img)
        self.assertEqual(cm.exception.code, "E_EMPTY_ALPHA")

    # T4 RGB 极性规范化一致
    def test_t4_rgb_polarity_consistent(self):
        dark_on_light = render_char("星", ink=(10, 10, 10), bg=(255, 255, 255))
        light_on_dark = render_char("星", ink=(245, 245, 245), bg=(0, 0, 0))
        sha1 = sha256_image(normalize(dark_on_light))
        sha2 = sha256_image(normalize(light_on_dark))
        self.assertEqual(sha1, sha2, "深字浅底与浅字深底必须规范化到同一 alpha")

    # T5 幂等（重复缩放不变细）
    def test_t5_normalize_idempotent(self):
        img = render_char("河")
        once = normalize(img)
        sha_once = sha256_image(once)
        for _ in range(3):
            self.assertEqual(sha256_image(normalize(once)), sha_once)

    # T6 bbox 跨坐标系/越界
    def test_t6_bbox_cross_coordinate_rejected(self):
        m = Manifest()
        common = dict(style_id="blogA", label="星", unicode_cp=0x661F,
                      parent_source_hash="a" * 64, final_alpha_sha256="b" * 64,
                      parent_width=512, parent_height=256)
        with self.assertRaises(ManifestError) as cm:  # 越界（y+h > parent）
            m.register(source_bbox={"x": 0, "y": 200, "w": 100, "h": 100,
                                    "coord_space": "pixel:parent_image"}, **common)
        self.assertEqual(cm.exception.code, "E_BBOX_OUT_OF_PARENT")
        with self.assertRaises(ManifestError) as cm:  # 缺坐标系声明
            m.register(source_bbox={"x": 0, "y": 0, "w": 100, "h": 100}, **common)
        self.assertEqual(cm.exception.code, "E_BBOX_COORD_SPACE")
        with self.assertRaises(ManifestError) as cm:  # 伪装别的坐标系但尺寸对不上
            m.register(source_bbox={"x": 480, "y": 0, "w": 100, "h": 100,
                                    "coord_space": "pixel:other_512x512"}, **common)
        self.assertEqual(cm.exception.code, "E_BBOX_OUT_OF_PARENT")
        # 合法 bbox 可登记
        s = m.register(source_bbox={"x": 0, "y": 0, "w": 100, "h": 100,
                                    "coord_space": "pixel:parent_image"}, **common)
        self.assertEqual(s.review_status, REVIEW_CANDIDATE)

    # T7 码点严格一致 / 组合码点 / 变体选择子
    def test_t7_codepoint_strict_match(self):
        m = Manifest()
        common = dict(style_id="blogA", parent_source_hash="a" * 64,
                      final_alpha_sha256="b" * 64,
                      source_bbox={"x": 0, "y": 0, "w": 10, "h": 10,
                                   "coord_space": "pixel:parent_image"},
                      parent_width=64, parent_height=64)
        with self.assertRaises(ManifestError) as cm:  # label=星 声明河的码点
            m.register(label="星", unicode_cp=0x6CB3, **common)
        self.assertEqual(cm.exception.code, "E_CODEPOINT_MISMATCH")
        with self.assertRaises(ManifestError) as cm:  # 组合附加符
            m.register(label="\u0301", unicode_cp=0x0301, **common)
        self.assertEqual(cm.exception.code, "E_COMBINING_REJECTED")
        with self.assertRaises(ManifestError) as cm:  # 变体选择子
            m.register(label="\uFE0F", unicode_cp=0xFE0F, **common)
        self.assertEqual(cm.exception.code, "E_CODEPOINT_FORBIDDEN")
        with self.assertRaises(ManifestError):  # 多字 label
            m.register(label="星河", unicode_cp=ord("星"), **common)

    # T8 空 manifest / 无 confirmed 拒导出
    def test_t8_empty_manifest_export_rejected(self):
        m = Manifest()
        with self.assertRaises(ManifestError) as cm:
            m.export_training("blogA")
        self.assertEqual(cm.exception.code, "E_EMPTY_EXPORT")
        # 只有 candidate 也不行
        m.register(label="星", unicode_cp=0x661F, style_id="blogA",
                   parent_source_hash="a" * 64, final_alpha_sha256="b" * 64,
                   source_bbox={"x": 0, "y": 0, "w": 10, "h": 10,
                                "coord_space": "pixel:parent_image"},
                   parent_width=64, parent_height=64)
        with self.assertRaises(ManifestError) as cm:
            m.export_training("blogA")
        self.assertEqual(cm.exception.code, "E_EMPTY_EXPORT")

    # T9 owner_confirmed 只能显式写入
    def test_t9_owner_confirm_explicit_only(self):
        m = Manifest()
        common = dict(style_id="blogA", label="河", unicode_cp=0x6CB3,
                      parent_source_hash="a" * 64, final_alpha_sha256="c" * 64,
                      source_bbox={"x": 0, "y": 0, "w": 10, "h": 10,
                                   "coord_space": "pixel:parent_image"},
                      parent_width=64, parent_height=64)
        # 1) register 无 confirmed 通道
        with self.assertRaises(ManifestError) as cm:
            m.register(review_status="owner_confirmed", **common)
        self.assertEqual(cm.exception.code, "E_REGISTER_STATUS")
        s = m.register(**common)
        # 2) confirm 缺署名拒绝
        with self.assertRaises(ManifestError) as cm:
            m.confirm(s.sample_id, "")
        self.assertEqual(cm.exception.code, "E_CONFIRM_BY")
        # 3) 显式署名确认 → 唯一 confirmed 通道；导出含该行
        m.confirm(s.sample_id, "owner-张三")
        self.assertEqual(m.samples[0].review_status, "owner_confirmed")
        self.assertEqual(m.samples[0].confirmed_by, "owner-张三")
        rows = m.export_training("blogA")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "河")
        # 4) 其他风格仍导不出（不混池）
        with self.assertRaises(ManifestError):
            m.export_training("blogB")


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CharPrepTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    cases = []
    for t, err in getattr(result, "failures", []) + getattr(result, "errors", []):
        cases.append({"test": t.id(), "ok": False, "detail": err})
    passed = result.testsRun - len(cases)
    receipt = {
        "module": "engine/style/charprep",
        "command": ".venv/Scripts/python.exe -X utf8 engine/style/charprep/tests/run_tests.py",
        "interpreter": sys.executable,
        "started": stamp,
        "fixture_source": FIXTURE_SRC,
        "font_sha256": __import__("hashlib").sha256(FONT_PATH.read_bytes()).hexdigest(),
        "tests_total": result.testsRun,
        "passed": passed,
        "failed": len(cases),
        "exit": 0 if result.wasSuccessful() else 1,
        "cases": cases,
        "data_redline": "no materials/b10-realgt, no B7/B9 SEALED input; fixtures synthesized",
    }
    out_dir = Path(__file__).resolve().parent / "receipts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"charprep-tests-{stamp}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[charprep-tests] {passed}/{result.testsRun} passed, exit={receipt['exit']}, "
          f"receipt={out_dir / f'charprep-tests-{stamp}.json'}")
    return receipt["exit"]


if __name__ == "__main__":
    sys.exit(main())
