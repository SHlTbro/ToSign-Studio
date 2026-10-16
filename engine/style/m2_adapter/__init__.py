# -*- coding: utf-8 -*-
"""engine.style.m2_adapter — FontDiffuser M2 适配器训练（S05，冻结基座 + hook 残差适配器）。

红线（S05 契约 + S01 实证）：
- 基座三件套（unet/style_encoder/content_encoder）结构性冻结（eval 模式 + requires_grad=False）；
- 可训参数只在 ``adapter.*`` 命名空间（零初始化 1×1 残差 conv，hook 在 unet 各 ResnetBlock2D
  输出上——初始行为=基座 M1，训练从 M1 出发而非从随机网络出发）；
- 数据只读 S03 不可变快照（snapshot_sha256 必须与 config 一致，禁止直读 P4 活动目录）。
"""
# Keep binding/preflight/CLI failures usable without importing the ML stack.
__all__ = ["M2FontDiffuser", "GlyphPairDataset", "load_base_model", "render_content_char", "main"]


def __getattr__(name):
    if name == "main":
        from .runner import main
        return main
    if name in __all__:
        from . import adapter
        return getattr(adapter, name)
    raise AttributeError(name)
