# -*- coding: utf-8 -*-
"""S04 M1 冻结推理适配（m1_adapter）。

- adapter.M1Inference : 单例冻结推理器（ckpt 目录/ref/字/seed 全显式参数）
- runner              : 子进程 JSON 入口（exit 0/2/3），主 API 进程不经此 import 重依赖
- receipts            : M1_FROZEN 收据 schema（model_version=ckpt sha 前缀）
红线：只读加载 artifacts/style-font/FontDiffuser-Gradio；不训练、不存 ckpt、
不宣称质量（质量门 S08/S09）；GPU 一次一个任务。
"""
from .adapter import M1Inference, AdapterError, DEFAULT_CKPT_DIR, DEFAULT_TTF
from .receipts import ENGINE_ID, ENGINE_MODE, EXIT_OK, EXIT_ENGINE, EXIT_INPUT

__all__ = ["M1Inference", "AdapterError", "DEFAULT_CKPT_DIR", "DEFAULT_TTF",
           "ENGINE_ID", "ENGINE_MODE", "EXIT_OK", "EXIT_ENGINE", "EXIT_INPUT"]
