# -*- coding: utf-8 -*-
"""S04 m1_adapter.adapter — M1 冻结基座（FontDiffuser）产品化推理适配。

边界（写死，防伪报）：
- engine_mode=M1_FROZEN：只读加载 artifacts/style-font/FontDiffuser-Gradio 三件套；
  不训练、不更新参数、不保存 checkpoint、不宣称质量。
- 无写死路径：checkpoint 目录 / style ref / 目标字 / seed 全部显式参数
  （DEFAULT_CKPT_DIR 仅为缺省值，调用方可覆盖）。
- 重依赖（torch/diffusers/vendor 代码）延迟到 _ensure_loaded() —— import 本模块本身
  不拉起 torch；主 API 进程不应 import 本模块，统一走 runner.py 子进程入口。

进程内单例：get_shared(ckpt_dir, device, content_ttf) 按 (ckpt_dir, device, content_ttf) 缓存已加载 pipeline，
避免逐字重复 1.6-2.2s 加载；不同 ckpt_dir 各自独立实例，不串缓存。

输出：96×96 白底 RGB（基座原生）→ 派生 RGBA（ink=不透明，alpha=255−L，极性经
合成回读校验，与 S01 探针同法）。派生检查失败按引擎侧失败抛 AdapterError。
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .receipts import (ENGINE_ID, ENGINE_MODE, build_receipt, model_version_from_ckpt,
                       sha256_file, sha256_image)

PROJ = Path(__file__).resolve().parents[3]
# ckpt_dir 语义 = 直接容纳三件套 .pth 的目录（repo/ckpt）；repo 根 = 其 parent（vendor 代码/ttf 所在）
_DEFAULT_REPO = PROJ / "artifacts" / "style-font" / "FontDiffuser-Gradio"
DEFAULT_CKPT_DIR = _DEFAULT_REPO / "ckpt"
DEFAULT_TTF = _DEFAULT_REPO / "ttf" / "KaiXinSongA.ttf"

INK_MIN_PX = 16           # 96×96 输出透明层非空下限（卡反例：空白输出）
COMPOSITE_TOLERANCE = 128 # 派生极性回检容忍（与 S01 探针一致）


class AdapterError(RuntimeError):
    """引擎侧失败（对应 runner 退出码 2）。code 语义见 INTERFACE_CONTRACT_DRAFT §5。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def normalize_style_reference(source):
    """Preserve alpha-only white glyph masks and ordinary RGBA compositing.

    Frozen extraction snapshots encode glyph coverage in alpha while every RGB
    pixel is white. Compositing those onto white erases the reference entirely.
    Interpret that precise representation as black ink coverage; leave ordinary
    colored/black RGBA and opaque RGB input on the existing compositing path.
    """
    from PIL import Image, ImageOps

    rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")
    if alpha.getextrema()[0] < 255 and all(lo == hi == 255 for lo, hi in rgb.getextrema()):
        return ImageOps.invert(alpha).convert("RGB"), "white_rgb_alpha_ink"
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=alpha)
    return background, "rgba_composite_white"


class M1Inference:
    """单 (ckpt_dir, device, content_ttf) 的冻结推理器。线程不安全——worker 串行使用。"""

    _shared: dict[tuple[str, str, str], "M1Inference"] = {}

    def __init__(self, ckpt_dir=DEFAULT_CKPT_DIR, device="cuda:0",
                 content_ttf=DEFAULT_TTF):
        self.ckpt_dir = Path(ckpt_dir)
        self.device = device
        self.content_ttf = Path(content_ttf)
        self._pipe = None
        self._vendor_args = None
        self._model_version = None
        self._ckpt_detail = None
        self._load_seconds: float | None = None  # 实际发生加载的那次耗时；None=尚未加载

    # ---- 进程内单例 ----
    @classmethod
    def get_shared(cls, ckpt_dir=DEFAULT_CKPT_DIR, device="cuda:0",
                   content_ttf=DEFAULT_TTF) -> "M1Inference":
        key = (str(Path(ckpt_dir).resolve()), str(device), str(Path(content_ttf).resolve()))
        if key not in cls._shared:
            cls._shared[key] = cls(ckpt_dir, device, content_ttf)
        return cls._shared[key]

    # ---- 模型版本（ckpt sha 前缀，进程内缓存） ----
    def model_version(self) -> str:
        if self._model_version is None:
            self._require_ckpts()
            self._model_version, self._ckpt_detail = model_version_from_ckpt(self.ckpt_dir)
        return self._model_version

    def _require_ckpts(self) -> None:
        for name in ("content_encoder", "style_encoder", "unet"):
            p = self.ckpt_dir / f"{name}.pth"
            if not p.exists():
                raise AdapterError("E_CKPT_MISSING", f"checkpoint not found: {p}")
        if not self.content_ttf.exists():
            raise AdapterError("E_CKPT_MISSING", f"content ttf not found: {self.content_ttf}")

    # ---- 加载（重依赖只在这里出现） ----
    @contextmanager
    def _vendor_cwd(self):
        """vendor 代码按相对路径找 ttf/——进入其调用时临时 chdir 到 repo，退出即还原，
        避免污染调用方相对路径语义（反例：chdir 后 style_ref/out_png 失效）。"""
        old = os.getcwd()
        os.chdir(str(self.ckpt_dir.parent))
        try:
            yield
        finally:
            os.chdir(old)

    def _ensure_loaded(self) -> None:
        if self._pipe is not None:
            return
        self._require_ckpts()
        try:
            import torch
        except Exception as e:  # noqa: BLE001
            raise AdapterError("E_CUDA_UNAVAILABLE", f"torch import failed: {e!r}") from e
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise AdapterError("E_CUDA_UNAVAILABLE", "torch.cuda.is_available()=False")
        try:
            repo = str(self.ckpt_dir.parent)  # vendor 代码根（ckpt 的上级目录）
            if repo not in sys.path:
                sys.path.insert(0, repo)
            with self._vendor_cwd():
                from configs.fontdiffuser import get_parser  # noqa: PLC0415
                import sample as S  # noqa: PLC0415
                from utils import is_char_in_font  # noqa: PLC0415
                self._is_char_in_font = is_char_in_font
                self._S = S
        except Exception as e:  # noqa: BLE001
            raise AdapterError("E_LOAD_FAILED", f"vendor import failed: {e!r}") from e
        args = get_parser().parse_args([])
        args.ckpt_dir = str(self.ckpt_dir)  # vendor 语义：直接指向三件套目录
        args.ttf_path = str(self.content_ttf)
        args.device = self.device
        args.demo = True
        args.character_input = True
        args.save_image = False
        args.style_image_size = (args.style_image_size, args.style_image_size)
        args.content_image_size = (args.content_image_size, args.content_image_size)
        self._vendor_args = args
        t0 = time.time()
        try:
            with self._vendor_cwd():
                self._pipe = self._S.load_fontdiffuer_pipeline(args)
        except Exception as e:  # noqa: BLE001
            raise AdapterError("E_LOAD_FAILED", f"pipeline load failed: {e!r}") from e
        self._load_seconds = time.time() - t0
        self.model_version()  # 顺手固化 ckpt hash

    def char_in_content_font(self, ch: str) -> bool:
        self._ensure_loaded()
        return bool(self._is_char_in_font(font_path=str(self.content_ttf), char=ch))

    # ---- 推理 ----
    def generate(self, style_ref_image, target_char: str, seed: int,
                 out_png=None) -> tuple["Image.Image", dict]:
        """style_ref_image: Path 或 PIL.Image；target_char: 恰 1 个中文字符；seed: int。

        返回 (RGBA Image, receipt dict)。引擎侧失败抛 AdapterError（runner→exit 2）。
        输入非法抛 AdapterError 前应由 runner 校验——本方法只做引擎侧断言。
        """
        if not isinstance(target_char, str) or len(target_char) != 1:
            raise AdapterError("E_INPUT_CHAR", "target_char must be exactly 1 char")
        self._ensure_loaded()
        import torch
        from PIL import Image
        # style ref 归一化：任意输入 → 白底 RGB（与 S01/历史 s2 run 同法，确定性）
        # 路径一律先 resolve 成绝对路径，杜绝 vendor cwd 切换影响
        ref_path = None
        if isinstance(style_ref_image, (str, Path)):
            ref_path = Path(style_ref_image).resolve()
            with Image.open(ref_path) as ref_src:
                ref_norm, ref_normalization = normalize_style_reference(ref_src)
            ref_source_sha = sha256_file(ref_path)
        else:
            ref_norm, ref_normalization = normalize_style_reference(style_ref_image)
            ref_source_sha = sha256_image(style_ref_image)
        ref_sha = sha256_image(ref_norm)

        args = self._vendor_args
        args.seed = int(seed)
        args.content_character = target_char  # get_parser 无此字段，sampling→image_process 需要
        t1 = time.time()
        try:
            with self._vendor_cwd():
                img = self._S.sampling(args=args, pipe=self._pipe, content_image=None,
                                       style_image=ref_norm)
        except Exception as e:  # noqa: BLE001
            raise AdapterError("E_SAMPLE_FAILED", f"sampling {target_char!r} failed: {e!r}") from e
        if self.device.startswith("cuda"):
            torch.cuda.synchronize(0)
        sample_seconds = time.time() - t1
        if img is None:
            raise AdapterError("E_OUTPUT_INVALID", "sampling returned None")

        # 输出校验 + 透明层派生（ink=不透明）
        try:
            img.load()
        except Exception as e:  # noqa: BLE001
            raise AdapterError("E_OUTPUT_INVALID", f"output undecodable: {e!r}") from e
        rgb = img.convert("RGB")
        luma = rgb.convert("L")
        px = list(luma.getdata())
        # ink（深色墨）= L<128 → alpha=255−L≥128 → 不透明；与 S01 探针口径一致
        ink_px = sum(1 for v in px if v < 128)
        if not (INK_MIN_PX <= ink_px < len(px) - INK_MIN_PX):
            raise AdapterError("E_OUTPUT_INVALID",
                               f"degenerate ink {ink_px}/{len(px)} (blank or full-fill)")
        alpha = luma.point(lambda v: 255 - v)
        rgba_out = Image.merge("RGBA", (*rgb.split(), alpha))
        comp = Image.new("RGB", rgba_out.size, (255, 255, 255))
        comp.paste(rgba_out, mask=rgba_out.split()[3])
        diff = max(abs(a - b) for a, b in zip(comp.convert("L").getdata(), px))
        if diff > COMPOSITE_TOLERANCE:
            raise AdapterError("E_OUTPUT_INVALID",
                               f"rgba polarity check failed composite_max_diff={diff}")

        vram = {}
        if self.device.startswith("cuda"):
            vram = {"peak_alloc_mb": round(torch.cuda.max_memory_allocated(0) / 1048576, 1),
                    "peak_reserved_mb": round(torch.cuda.max_memory_reserved(0) / 1048576, 1)}

        out_path = Path(out_png).resolve() if out_png else None
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            rgba_out.save(out_path, format="PNG")
            out_sha = sha256_file(out_path)
        else:
            out_sha = sha256_image(rgba_out)

        receipt = build_receipt(
            ckpt_dir=self.ckpt_dir, model_version=self._model_version,
            ckpt_detail=self._ckpt_detail,
            style_ref_path=(str(ref_path) if ref_path is not None else "PIL.Image(in-memory)"),
            ref_normalized_sha=ref_sha, target_char=target_char,
            content_ttf=self.content_ttf, seed=int(seed), device=self.device,
            load_seconds=(self._load_seconds if self._load_seconds is not None else 0.0),
            sample_seconds=sample_seconds, vram=vram,
            out_png=str(out_path) if out_path else "(in-memory)",
            out_sha=out_sha, size_mode=(rgba_out.size, rgba_out.mode),
            ink_px=ink_px, composite_max_diff=diff,
            ref_source_sha=ref_source_sha, ref_normalization=ref_normalization)
        receipt["engine_mode"] = ENGINE_MODE
        receipt["engine_id"] = ENGINE_ID
        if self._load_seconds is None:  # 本轮未重新加载（进程内缓存命中）
            receipt["timings"]["load_seconds"] = 0.0
            receipt["timings"]["pipeline_cache"] = "hit"
        return rgba_out, receipt
