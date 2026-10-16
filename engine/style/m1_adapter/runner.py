# -*- coding: utf-8 -*-
"""S04 m1_adapter.runner — 子进程入口（主 API 进程不 import 本模块的重依赖链）。

用法（项目根，GPU 环境 .venv-ml）：
  .venv-ml/Scripts/python.exe -X utf8 -m engine.style.m1_adapter.runner REQ.json
REQ.json 字段：
  style_ref   必填，style ref 图路径（或 style_ref_image 同义）
  target_char 必填，恰好 1 个字符
  seed        必填，int
  out_png     必填，输出 PNG 路径
  out_receipt 选填，收据 JSON 落盘路径（缺省只打印 stdout）
  ckpt_dir    选填，覆盖默认 artifacts/style-font/FontDiffuser-Gradio（只读）
  device      选填，默认 cuda:0
  content_ttf 选填，默认基座自带 KaiXinSongA.ttf

退出码（卡 S04 语义，全部失败路径都写/打结构化收据，不假成功）：
  0 成功；2 引擎不可用/引擎侧失败（ckpt 缺失、CUDA 不可用、加载/采样失败、输出退化）；
  3 输入非法（JSON 解析、缺字段、空字/多字、码点禁用、ref 缺失、输出目录不可写、
    目标字不在内容字体 cmap）。
文件用法示例见 engine/style/m1_adapter/receipts/ 下各 run-*.json。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from .receipts import EXIT_INPUT, EXIT_OK, ENGINE_MODE, RUNNER_VERSION, sha256_file

PROJ = Path(__file__).resolve().parents[3]
RECEIPT_DIR = Path(__file__).resolve().parent / "receipts"

_FORBIDDEN = lambda cp: (0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF
                         or 0xD800 <= cp <= 0xDFFF or cp <= 0x001F)


def _fail_payload(code: str, detail: str) -> dict:
    return {"engine_mode": ENGINE_MODE, "runner_version": RUNNER_VERSION,
            "ok": False, "failure": {"code": code, "detail": detail}}


def _input_check(req: dict) -> tuple[dict, dict | None]:
    """→ (规范化请求, None) 或 (_, 失败收据)。全部输入侧校验集中在此（exit 3 语义）。"""
    for k in ("style_ref", "target_char", "seed", "out_png"):
        if k not in req or req[k] in (None, ""):
            return {}, _fail_payload("E_INPUT_MISSING", f"missing required field: {k}")
    ch = req["target_char"]
    if not isinstance(ch, str) or len(ch) != 1:
        return {}, _fail_payload("E_INPUT_CHAR",
                                 f"target_char must be exactly 1 char, got {ch!r}")
    if _FORBIDDEN(ord(ch)):
        return {}, _fail_payload("E_INPUT_CHAR",
                                 f"target_char U+{ord(ch):04X} in forbidden range")
    try:
        req["seed"] = int(req["seed"])
    except (TypeError, ValueError):
        return {}, _fail_payload("E_INPUT_SEED", f"seed must be int, got {req['seed']!r}")
    ref = Path(req["style_ref"])
    if not ref.exists():
        return {}, _fail_payload("E_INPUT_REF", f"style_ref not found: {ref}")
    out_png = Path(req["out_png"])
    try:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        probe = out_png.parent / f".wprobe_{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        return {}, _fail_payload("E_OUTPUT_UNWRITABLE",
                                 f"output dir not writable: {out_png.parent} ({e!r})")
    return req, None


def run(req: dict) -> tuple[int, dict]:
    req, fail = _input_check(req)
    if fail:
        return EXIT_INPUT, fail

    from .adapter import DEFAULT_CKPT_DIR, DEFAULT_TTF, M1Inference, AdapterError

    ckpt_dir = Path(req.get("ckpt_dir") or DEFAULT_CKPT_DIR)
    device = req.get("device") or "cuda:0"
    content_ttf = Path(req.get("content_ttf") or DEFAULT_TTF)

    started = time.strftime("%Y%m%d_%H%M%S")
    try:
        engine = M1Inference.get_shared(ckpt_dir=ckpt_dir, device=device,
                                        content_ttf=content_ttf)
        engine._ensure_loaded()
    except AdapterError as e:
        return 2, _fail_payload(e.code, str(e))
    # 字符在内容字体 cmap 检查（廉价、无 GPU；不通过算输入非法）
    try:
        if not engine.char_in_content_font(req["target_char"]):
            return 3, _fail_payload(
                "E_CHAR_NOT_IN_FONT",
                f"{req['target_char']!r} (U+{ord(req['target_char']):04X}) "
                f"not in content font {content_ttf.name}")
    except AdapterError as e:
        return 2, _fail_payload(e.code, str(e))

    try:
        _, receipt = engine.generate(style_ref_image=Path(req["style_ref"]),
                                     target_char=req["target_char"],
                                     seed=req["seed"], out_png=Path(req["out_png"]))
    except AdapterError as e:
        return 2, _fail_payload(e.code, str(e))
    except Exception as e:  # noqa: BLE001 —— 未预期异常也不得 exit0
        return 2, _fail_payload("E_SAMPLE_FAILED",
                                f"unexpected: {e!r}\n{traceback.format_exc()[-800:]}")
    receipt["ok"] = True
    receipt["process"]["started_stamp"] = started
    return EXIT_OK, receipt


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(json.dumps(_fail_payload("E_INPUT_MISSING", "usage: runner REQ.json|-")),
              ensure_ascii=False)
        return EXIT_INPUT
    src = Path(argv[0]) if argv[0] != "-" else None
    try:
        req = json.loads(src.read_text(encoding="utf-8") if src else sys.stdin.read())
    except Exception as e:  # noqa: BLE001
        print(json.dumps(_fail_payload("E_INPUT_JSON", f"request parse failed: {e!r}"),
                         ensure_ascii=False))
        return EXIT_INPUT
    if not isinstance(req, dict):
        print(json.dumps(_fail_payload("E_INPUT_JSON", "request must be a JSON object"),
                         ensure_ascii=False))
        return EXIT_INPUT

    code, payload = run(req)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    out_receipt = req.get("out_receipt")
    if out_receipt:
        try:
            Path(out_receipt).parent.mkdir(parents=True, exist_ok=True)
            Path(out_receipt).write_text(text, encoding="utf-8")
        except OSError as e:
            payload = _fail_payload("E_OUTPUT_UNWRITABLE",
                                    f"cannot write receipt {out_receipt}: {e!r}")
            code = code if code != EXIT_OK else 2
            text = json.dumps(payload, ensure_ascii=False, indent=1)
    elif code != EXIT_OK:
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        (RECEIPT_DIR / f"FAIL_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
            text, encoding="utf-8")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
