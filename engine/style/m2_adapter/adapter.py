# -*- coding: utf-8 -*-
"""M2 adapter — FontDiffuser 冻结基座 + hook 残差适配器（S05 契约实现）。

架构（S05 FreezePolicy 硬约束的合规解）：
- 基座三件套（unet/style_encoder/content_encoder）参数名都在 ``base.`` 下 → 被
  FreezePolicy(trainable_prefixes=("adapter.",)) 结构性冻结；基座整体 eval()（零
  BatchNorm 已实证——84 个 GroupNorm 无运行统计，train/eval 等价；梯度图只覆盖
  unet 内部 + adapter）。
- 可训参数 = ``adapter.*``：零初始化 1×1 conv 残差，forward hook 挂在 unet 每个
  ResnetBlock2D 输出上（懒初始化：首次前向按输出通道数建参）。零初始化 ⇒ 训练第 0 步
  行为严格等于冻结基座（M1），训练从 M1 出发而不是从随机网络出发。
- 数据：S03 不可变快照（snapshot_sha256 绑定）→ (target glyph, style ref glyph,
  content font render) 三元组 → DDPM 加噪 → 预测噪声（S01 探针同管线，96×96）。

基座权重加载安全：三件套已一次性转换为 **npz 安全格式**（无 pickle；转换时经
sha256 钉扎校验，转换器记录见 run receipt）。加载走 np.load(allow_pickle=False)。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torchvision.transforms as T
from PIL import Image, ImageDraw, ImageFont

ENGINE_MODE = "M2_ADAPTER"
ENGINE_ID = "fontdiffuser-frozen-v1"
RENDER_SIZE = 96  # S01 探针实证的前向/反向管线尺寸

_PAPER = 245.0
_INK = 15.0

# npz 安全格式基座（转换自 sha256 钉扎的三件套；npz 自身 sha256 由 runner 启动时核对）
BASE_NPZ_SHA256 = {
    "unet.npz": "edbe7f109dc23e6f",          # 完整值由 runner 以 BASE_NPZ_FULL 核对
    "style_encoder.npz": "af1ff052a2833f73",
    "content_encoder.npz": "603fa649ea8f6437",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _glyph_to_tensor(png_path: Path, *, preprocessing_profile="legacy-pil72-gray-v1") -> torch.Tensor:
    """128×128 RGBA 草稿/样本 → 96×96 [-1,1] RGB（alpha 墨迹渲染，白底黑字）。"""
    from .preprocessing import LEGACY_PROFILE, UPSTREAM_PROFILE, full_range_glyph_image
    if preprocessing_profile == UPSTREAM_PROFILE:
        return T.Compose([T.ToTensor(), T.Normalize([0.5], [0.5])])(full_range_glyph_image(png_path))
    if preprocessing_profile != LEGACY_PROFILE:
        raise ValueError("unsupported preprocessing profile")
    arr = np.asarray(Image.open(png_path).convert("RGBA"))
    alpha = arr[..., 3].astype(np.float32) / 255.0
    ink = (_PAPER - alpha * (_PAPER - _INK)).astype(np.uint8)
    img = Image.fromarray(ink, "L").convert("RGB").resize((RENDER_SIZE, RENDER_SIZE), Image.BILINEAR)
    return T.Compose([T.ToTensor(), T.Normalize([0.5], [0.5])])(img)


def render_content_char(text_label: str, font_path: Path,
                        canvas: int = RENDER_SIZE, glyph_px: int = 72, *,
                        preprocessing_profile="legacy-pil72-gray-v1") -> torch.Tensor:
    """content 图：目标字符用基座内容字体（KaiXinSongA）渲染——FontDiffuser 语义。"""
    from .preprocessing import LEGACY_PROFILE, UPSTREAM_PROFILE, upstream_content_image
    if preprocessing_profile == UPSTREAM_PROFILE:
        if canvas != RENDER_SIZE or glyph_px != 72:
            raise ValueError("profile fixes its own geometry; legacy geometry overrides are forbidden")
        return T.Compose([T.ToTensor(), T.Normalize([0.5], [0.5])])(upstream_content_image(text_label, font_path))
    if preprocessing_profile != LEGACY_PROFILE:
        raise ValueError("unsupported preprocessing profile")
    img = Image.new("L", (canvas, canvas), int(_PAPER))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path), glyph_px)
    left, top, right, bottom = d.textbbox((0, 0), text_label, font=font)
    d.text(((canvas - (right - left)) / 2 - left, (canvas - (bottom - top)) / 2 - top),
           text_label, font=font, fill=int(_INK))
    return T.Compose([T.ToTensor(), T.Normalize([0.5], [0.5])])(img.convert("RGB"))


class HookResidualAdapter(nn.Module):
    """零初始化 1×1 conv 残差（懒初始化）。参数全部在 ``blocks.*`` → 包装后为
    ``adapter.blocks.*``，与 FreezePolicy(trainable_prefixes=("adapter.",)) 对齐。"""

    def __init__(self, topology_id=None):
        super().__init__()
        from .topology_dispatch import BASELINE_TOPOLOGY_ID, topology_contract
        self.topology_id = BASELINE_TOPOLOGY_ID if topology_id is None else topology_id
        self.topology = topology_contract(self.topology_id)
        self.blocks = nn.ModuleDict()
        self.offset_blocks = nn.ModuleDict()
        self.offset_calls = {}
        self.spatial_blocks = nn.ModuleDict()
        self.spatial_calls = {}
        self.enabled = True

    @staticmethod
    def _key(module_name: str) -> str:
        return "h" + hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:10]

    def apply_residual(self, module_name: str, out: torch.Tensor) -> torch.Tensor:
        if not self.enabled:
            return out
        key = self._key(module_name)
        if key not in self.blocks:
            conv = nn.Conv2d(out.shape[1], out.shape[1], 1, bias=True)
            nn.init.zeros_(conv.weight)
            nn.init.zeros_(conv.bias)
            conv.to(device=out.device, dtype=out.dtype)
            self.blocks[key] = conv
        return out + self.blocks[key](out)

    def install_offset_head(self, module_name, module):
        from .adapter_topology import OFFSET_HEADS, offset_key
        wanted = next((h for h in OFFSET_HEADS if h['module'] == module_name), None)
        if (wanted is None or not isinstance(module, nn.Conv2d)
                or module.in_channels != wanted['in_channels'] or module.out_channels != 18
                or module.kernel_size != (1, 1) or module.stride != (1, 1)
                or module.padding != (0, 0) or module.dilation != (1, 1)
                or module.groups != 1 or module.bias is None or module.weight.dtype != torch.float32):
            raise ValueError('E_ADAPTER_TOPOLOGY: offset projection shape/precision mismatch')
        key = offset_key(module_name)
        if key in self.offset_blocks:
            raise ValueError('E_ADAPTER_TOPOLOGY: duplicate offset projection')
        device = module.weight.device
        devices = [device.index if device.index is not None else torch.cuda.current_device()] if device.type == 'cuda' else []
        # Creating a zero-initialized branch must not shift the paired noise RNG.
        with torch.random.fork_rng(devices=devices):
            delta = nn.Conv2d(module.in_channels, 18, 1, bias=True,
                              device=device, dtype=torch.float32)
            nn.init.zeros_(delta.weight)
            nn.init.zeros_(delta.bias)
        self.offset_blocks[key] = delta
        self.offset_calls[module_name] = 0

    def apply_offset(self, module_name, inputs, output):
        if not self.enabled:
            return output
        from .adapter_topology import offset_key
        if len(inputs) != 1 or not isinstance(inputs[0], torch.Tensor) or not isinstance(output, torch.Tensor):
            raise ValueError('E_ADAPTER_TOPOLOGY: offset projection tensor interface changed')
        if inputs[0].dtype != torch.float32 or output.dtype != torch.float32:
            raise ValueError('E_ADAPTER_TOPOLOGY: offset delta requires FP32')
        delta = self.offset_blocks[offset_key(module_name)](inputs[0])
        if delta.shape != output.shape:
            raise ValueError('E_ADAPTER_TOPOLOGY: offset projection output shape changed')
        self.offset_calls[module_name] += 1
        return output + delta


    def install_spatial_head(self, module_name, module):
        from .spatial_topology import SPATIAL_HEADS, spatial_key
        wanted=next((h for h in SPATIAL_HEADS if h['module']==module_name),None)
        if (wanted is None or type(module).__name__!='OffsetRefStrucInter'
                or not isinstance(getattr(module,'content_proj_in',None),nn.Conv2d)
                or module.content_proj_in.in_channels!=wanted['in_channels']
                or not isinstance(getattr(module,'proj_out',None),nn.Conv2d)
                or module.proj_out.out_channels!=18 or module.proj_out.weight.dtype!=torch.float32):
            raise ValueError('E_ADAPTER_TOPOLOGY: spatial interpreter input/output mismatch')
        key=spatial_key(module_name)
        if key in self.spatial_blocks:raise ValueError('E_ADAPTER_TOPOLOGY: duplicate spatial head')
        device=module.proj_out.weight.device
        devices=[device.index if device.index is not None else torch.cuda.current_device()] if device.type=='cuda' else []
        with torch.random.fork_rng(devices=devices):
            delta=nn.Conv2d(wanted['in_channels'],18,1,bias=True,device=device,dtype=torch.float32)
            nn.init.zeros_(delta.weight);nn.init.zeros_(delta.bias)
        self.spatial_blocks[key]=delta;self.spatial_calls[module_name]=0

    def apply_spatial(self, module_name, inputs, output):
        if not self.enabled:return output
        from .spatial_topology import SPATIAL_HEADS,spatial_key
        wanted=next((h for h in SPATIAL_HEADS if h['module']==module_name),None)
        if (wanted is None or len(inputs)!=2 or not isinstance(inputs[0],torch.Tensor)
                or not isinstance(output,torch.Tensor) or inputs[0].ndim!=4
                or inputs[0].shape[1]!=wanted['in_channels'] or inputs[0].dtype!=torch.float32
                or output.dtype!=torch.float32 or output.shape!=(inputs[0].shape[0],18,*inputs[0].shape[-2:])):
            raise ValueError('E_ADAPTER_TOPOLOGY: spatial feature/output interface')
        normalized=torch.nn.functional.group_norm(inputs[0],32,weight=None,bias=None,eps=1e-6)
        delta=self.spatial_blocks[spatial_key(module_name)](normalized)
        self.spatial_calls[module_name]+=1
        return output+delta


class M2FontDiffuser(nn.Module):
    """包装 FontDiffuserModel：基座冻结（恒 eval）、adapter 可训。

    forward(x_dict) → noise_pred；x_dict = {x_t, t, style, content, noise}。
    offset 存在 ``self.last_offset``（loss_fn 读它算辅助项）。
    """

    def __init__(self, fd_model, content_font_path: Path, *, adapter_topology_id=None):
        super().__init__()
        self.base = fd_model
        self.adapter = HookResidualAdapter(adapter_topology_id)
        self._adapter_hook_handles = []
        self.content_font_path = Path(content_font_path)
        self.last_offset: torch.Tensor | None = None
        self.last_inputs: dict | None = None
        hooked = 0
        for name, mod in self.base.unet.named_modules():
            if type(mod).__name__ == "ResnetBlock2D":
                self._adapter_hook_handles.append(mod.register_forward_hook(self._make_hook(name)))
                hooked += 1
        self.n_hook_points = hooked
        from .topology_dispatch import OFFSET_TOPOLOGY_ID, OFFSET_HEADS, RESIDUAL_MODULES
        from .spatial_topology import SPATIAL_TOPOLOGY_ID, SPATIAL_HEADS
        self.n_offset_projection_heads = 0
        self.n_spatial_offset_heads = 0
        if self.adapter.topology_id == OFFSET_TOPOLOGY_ID:
            modules = dict(self.base.unet.named_modules())
            wanted = {h['module'] for h in OFFSET_HEADS}
            actual = {n for n in modules if '.sc_interpreter_offsets.' in n and n.endswith('.proj_out')}
            if wanted != actual or {n for n,m in modules.items() if type(m).__name__=='ResnetBlock2D'} != set(RESIDUAL_MODULES):
                self.close_adapter_hooks()
                raise ValueError('E_ADAPTER_TOPOLOGY: expected22 residual and six offset projection hooks')
            try:
                for name in sorted(wanted):
                    self.adapter.install_offset_head(name, modules[name])
                    self._adapter_hook_handles.append(modules[name].register_forward_hook(self._make_offset_hook(name)))
            except Exception:
                self.close_adapter_hooks()
                raise
            self.n_offset_projection_heads = len(wanted)
        if self.adapter.topology_id == SPATIAL_TOPOLOGY_ID:
            modules=dict(self.base.unet.named_modules());wanted={h['module'] for h in SPATIAL_HEADS}
            actual={n for n,m in modules.items() if type(m).__name__=='OffsetRefStrucInter'}
            if wanted!=actual or {n for n,m in modules.items() if type(m).__name__=='ResnetBlock2D'}!=set(RESIDUAL_MODULES):
                self.close_adapter_hooks();raise ValueError('E_ADAPTER_TOPOLOGY: expected22 residual and6 spatial heads')
            try:
                for name in sorted(wanted):
                    self.adapter.install_spatial_head(name,modules[name])
                    self._adapter_hook_handles.append(modules[name].register_forward_hook(self._make_spatial_hook(name)))
            except Exception:
                self.close_adapter_hooks();raise
            self.n_spatial_offset_heads=len(wanted)
        self.base.eval()  # 冻结基座恒 eval（零 BN；dropout 关闭 → 确定性）

    def train(self, mode: bool = True):
        super().train(mode)
        self.base.eval()  # 基座永远 eval（冻结语义）
        return self

    def _make_hook(self, module_name: str):
        def hook(_mod, _inp, out):
            if isinstance(out, torch.Tensor):
                return self.adapter.apply_residual(module_name, out)
            return out
        return hook

    def _make_offset_hook(self, module_name):
        def hook(_module, inputs, output):
            return self.adapter.apply_offset(module_name, inputs, output)
        return hook

    def _make_spatial_hook(self,module_name):
        def hook(_module,inputs,output):return self.adapter.apply_spatial(module_name,inputs,output)
        return hook

    def close_adapter_hooks(self):
        """Explicit lifecycle helper for isolated parity tests; no base mutation."""
        for handle in self._adapter_hook_handles:
            handle.remove()
        self._adapter_hook_handles.clear()

    def forward(self, x: dict) -> torch.Tensor:
        device = next(self.base.unet.parameters()).device
        x = {k: (v.to(device) if isinstance(v, torch.Tensor) else v) for k, v in x.items()}
        self.last_inputs = x
        noise_pred, offset = self.base(x["x_t"], x["t"], x["style"], x["content"],
                                       content_encoder_downsample_size=3)
        self.last_offset = offset
        return noise_pred


def load_verified_adapter_state(model, state, *, enabled=True):
    """Exact topology/state validation before loading; never infer a topology."""
    from .topology_dispatch import expected_offset_shapes, RESIDUAL_HOOK_KEYS
    channels = {model.adapter._key(name): getattr(getattr(module, 'conv2', None), 'out_channels', getattr(module, 'out_channels', None))
                for name, module in model.base.unet.named_modules() if type(module).__name__ == 'ResnetBlock2D'}
    if set(channels) != set(RESIDUAL_HOOK_KEYS) or not isinstance(state, dict) or not isinstance(enabled, bool):
        raise ValueError('E_ADAPTER_SHAPE: expected22 residual hooks and a tensor-state object')
    expected = {}
    for key, width in channels.items():
        expected[f'blocks.{key}.weight'] = (width, width, 1, 1)
        expected[f'blocks.{key}.bias'] = (width,)
    expected.update(expected_offset_shapes(model.adapter.topology_id))
    if set(state) != set(expected):
        raise ValueError('E_ADAPTER_SHAPE: missing or extra topology tensors')
    for key, shape in expected.items():
        value = state[key]
        if not torch.is_tensor(value) or value.dtype != torch.float32 or tuple(value.shape) != shape:
            raise ValueError('E_ADAPTER_SHAPE: wrong shape/FP32 dtype for ' + key)
        if not torch.isfinite(value).all():
            raise ValueError('E_ADAPTER_NONFINITE: ' + key)
    device = next(model.base.unet.parameters()).device
    for key, width in channels.items():
        if key not in model.adapter.blocks:
            conv = nn.Conv2d(width, width, 1, bias=True).to(device)
            nn.init.zeros_(conv.weight); nn.init.zeros_(conv.bias)
            model.adapter.blocks[key] = conv
    if enabled:
        model.adapter.load_state_dict(state, strict=True)
    model.adapter.enabled = enabled
    return channels


class GlyphPairDataset(torch.utils.data.Dataset):
    """(x_dict, noise) 对：target=作者字形，style=同 split 另一字形（确定性轮转），
    content=KaiXinSongA 渲染的同 label 字。t/noise 按数据集 Generator 采样。"""

    def __init__(self, pairs: list[dict], content_font_path: Path, seed: int, scheduler, *, fixed_noise=False, style_pairs=None,
                 preprocessing_profile="legacy-pil72-gray-v1", conditioning_dropout=0.0):
        self.pairs = pairs  # [{glyph_path, text_label, sample_id}]
        self.font_path = Path(content_font_path)
        self.scheduler = scheduler
        self._gen = np.random.default_rng(seed)
        self.seed = int(seed)
        self.fixed_noise = bool(fixed_noise)
        self.style_pairs = style_pairs
        from .preprocessing import preprocessing_contract
        self.preprocessing = preprocessing_contract(preprocessing_profile)
        self.conditioning_dropout = float(conditioning_dropout)
        if not 0 <= self.conditioning_dropout <= 1:
            raise ValueError("conditioning_dropout must be within [0,1]")
        self.conditioning_dropped = 0
        self.conditioning_seen = 0
        self.access_trace = []

    def __len__(self):
        return len(self.pairs)

    def __getitem__(self, i: int):
        p = self.pairs[i]
        x0 = _glyph_to_tensor(Path(p["glyph_path"]), preprocessing_profile=self.preprocessing["profile"])
        pool = self.pairs if self.style_pairs is None else [r for r in self.style_pairs if r["text_label"] != p["text_label"]]
        if not pool:raise ValueError("no disjoint train-side style reference")
        style_p = pool[(i + 1) % len(pool)]
        style = _glyph_to_tensor(Path(style_p["glyph_path"]), preprocessing_profile=self.preprocessing["profile"])
        content = render_content_char(p["text_label"], self.font_path, preprocessing_profile=self.preprocessing["profile"])
        # Validation reuses exact per-sample noise/t at every epoch; training
        # keeps stochastic noise. Sample IDs, not access order, bind the seed.
        sample_seed=int.from_bytes(hashlib.sha256(f"{self.seed}:{p['sample_id']}".encode()).digest()[:8],"big")
        rng = np.random.default_rng(sample_seed) if self.fixed_noise else self._gen
        t = torch.tensor(int(rng.integers(0, self.scheduler.config.num_train_timesteps)))  # 0 维标量 → collate 后 [B]
        noise = torch.from_numpy(rng.standard_normal(x0.shape).astype(np.float32))
        x_t = self.scheduler.add_noise(x0, noise, t)
        # Blank +1 is the exact classifier-free inference null condition.
        # Preserve historical RNG sequences when dropout is disabled.
        dropped = self.conditioning_dropout > 0 and float(rng.random()) < self.conditioning_dropout
        if dropped:
            content, style = torch.ones_like(content), torch.ones_like(style)
        self.conditioning_seen += 1
        self.conditioning_dropped += int(dropped)
        self.access_trace.append({"sample_id":p["sample_id"],"style_sample_id":style_p["sample_id"],"t":int(t),
                                  "noise_sha256":hashlib.sha256(noise.numpy().tobytes()).hexdigest(),"dropped":bool(dropped)})
        return {"x_t": x_t, "t": t, "style": style, "content": content,
                "noise": noise, "author_target_x0": x0, "conditioning_dropped": dropped, "sample_id": p["sample_id"], "style_sample_id": style_p["sample_id"]}, noise


def load_base_model(fd_repo: Path, device: str = "cpu", expected_hashes: dict | None = None, runtime_manifest_path=None):
    """加载 FontDiffuser 三件套（npz 安全格式；sha256 校验先行）。"""
    fd_repo = Path(fd_repo).resolve()
    runtime = None
    if runtime_manifest_path is not None:
        from .runtime import verify_runtime
        runtime = verify_runtime(runtime_manifest_path, fd_repo)
        if expected_hashes != runtime["manifest"]["base_npz_sha256"]:
            raise ValueError("requested base hashes differ from verified runtime lineage")
    if str(fd_repo) not in sys.path:
        sys.path.insert(0, str(fd_repo))
    import os as _os
    # All paths are explicit; do not mutate the process working directory.

    def _load_state(rel: str) -> dict:
        path = fd_repo / "ckpt" / rel
        actual = sha256_bytes(path.read_bytes())
        expected = (expected_hashes or {}).get(rel)
        if not isinstance(expected, str) or len(expected) != 64 or actual != expected or (runtime is None and not expected.startswith(BASE_NPZ_SHA256[rel])):
            raise ValueError(f"full base npz sha missing or mismatch: {rel}")
        with np.load(path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
        if runtime is not None:
            from .runtime import tensor_digest
            if tensor_digest(arrays) != runtime["manifest"]["base_assets"][rel]["tensor_state_digest"]:
                raise ValueError("runtime tensor lineage digest mismatch: " + rel)
        return {k: torch.from_numpy(value) for k, value in arrays.items()}

    from configs.fontdiffuser import get_parser
    from src import FontDiffuserModel, build_unet, build_style_encoder, \
        build_content_encoder
    args = get_parser().parse_args([])
    args.style_image_size = (args.style_image_size, args.style_image_size)
    args.content_image_size = (args.content_image_size, args.content_image_size)
    unet = build_unet(args)
    unet.load_state_dict(_load_state("unet.npz"))
    se = build_style_encoder(args)
    se.load_state_dict(_load_state("style_encoder.npz"))
    ce = build_content_encoder(args)
    ce.load_state_dict(_load_state("content_encoder.npz"))
    model = FontDiffuserModel(unet=unet, style_encoder=se, content_encoder=ce).to(device)
    from .deterministic_ops import install_cpu_deform_policy
    model._r9_operator_policy = install_cpu_deform_policy(model, expected_count=6)
    return model, args


def load_snapshot_pairs(snapshot_dir: Path, split: str) -> list[dict]:
    """从 S03 快照读一个 split 的 (glyph_path, text_label, sample_id) 对。
    样本 PNG 副本在快照 samples/ 下（快照自带，不读 P4 活动目录）。"""
    snap = Path(snapshot_dir)
    split_json = json.loads((snap / "split.json").read_text(encoding="utf-8"))
    wanted = {sid for sid, side in split_json["assignments"].items() if side == split}
    pairs = []
    for line in (snap / "samples.jsonl").read_text(encoding="utf-8").splitlines():
        s = json.loads(line)
        if s["sample_id"] in wanted:
            pairs.append({"glyph_path": str(snap / s["snapshot_file"]),
                          "text_label": s["label"], "sample_id": s["sample_id"]})
    missing = [p["glyph_path"] for p in pairs if not Path(p["glyph_path"]).is_file()]
    if missing:
        raise FileNotFoundError(f"snapshot sample pngs missing: {missing[:3]}")
    return pairs
