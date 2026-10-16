# -*- coding: utf-8 -*-
"""S02 charprep.cli — 命令行入口：inspect / split / normalize / register / confirm / export。

用法（项目根，纯 CPU：.venv/Scripts/python.exe -X utf8）：
  python -m engine.style.charprep.cli inspect   BLOCK.png
  python -m engine.style.charprep.cli split     BLOCK.png [--json OUT.json]
  python -m engine.style.charprep.cli normalize BLOCK.png -o OUT.png
  python -m engine.style.charprep.cli register  NORMALIZED.png
        --label 星 --unicode U+661F --style-id blogA
        --parent-hash <64hex> --parent-w 512 --parent-h 256
        --bbox 10,20,64,80 [--coord-space pixel:parent_image]
        [--status candidate|uncertain] [--source-ref ...] --manifest M.json
  python -m engine.style.charprep.cli confirm   --manifest M.json --sample-id ss_000001
        --confirm-by 张三          # ← owner_confirmed 的唯一写入通道
  python -m engine.style.charprep.cli export    --manifest M.json --style-id blogA -o train.json

退出码：0 成功；1 校验/拒绝（含 E_* ManifestError/NormalizeError/SplitError）；2 IO/找不到文件。
硬规则在模块层强制：register 无 confirmed 通道；confirm 必带 --confirm-by；
export 仅 owner_confirmed 且单 style_id；空导出拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manifest import Manifest, ManifestError, REVIEW_CANDIDATE, REVIEW_UNCERTAIN
from .normalize import NormalizeError, ink_stats, normalize, sha256_file, sha256_image
from .split import SplitError, split_block

EXIT_OK, EXIT_REJECT, EXIT_IO = 0, 1, 2


def _load_image(path: str):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    from PIL import Image
    return Image.open(p)


def cmd_inspect(args) -> int:
    im = _load_image(args.image)
    alpha, polarity = None, ""
    try:
        from .normalize import extract_alpha
        alpha, polarity = extract_alpha(im)
    except NormalizeError as e:
        print(json.dumps({"image": args.image, "error": e.code, "detail": str(e)},
                         ensure_ascii=False, indent=1))
        return EXIT_REJECT
    st = ink_stats(im)
    print(json.dumps({"image": args.image, "size": list(im.size), "mode": im.mode,
                      "polarity": polarity, **st}, ensure_ascii=False, indent=1))
    return EXIT_OK


def cmd_split(args) -> int:
    im = _load_image(args.image)
    result = split_block(im)
    result["image"] = args.image
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    lv = {c["level"] for c in result["candidates"]}
    print(f"[split] {len(result['candidates'])} candidate(s), levels={sorted(lv)} "
          f"（拆字结果仅供人工确认，模块不定案）", file=sys.stderr)
    return EXIT_OK


def cmd_normalize(args) -> int:
    im = _load_image(args.image)
    out = normalize(im)
    out_p = Path(args.out)
    if out_p.parent and not out_p.parent.exists():
        out_p.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_p, format="PNG")
    sha = sha256_image(out)
    st = ink_stats(out)
    print(json.dumps({"in": args.image, "out": str(out_p), "size": list(out.size),
                      "mode": out.mode, "final_alpha_sha256": sha, **st},
                     ensure_ascii=False, indent=1))
    return EXIT_OK


def cmd_register(args) -> int:
    im = _load_image(args.image)  # 必须是 normalize 输出的 128×128 RGBA
    if im.size != (128, 128):
        raise ManifestError("E_NOT_NORMALIZED", f"{args.image} is {im.size}, register 需要 normalize 后的 128×128")
    u = args.unicode.strip()
    if u.lower().startswith("u+"):
        u = u[2:]
    try:
        cp = int(u, 16)
        x, y, w, h = (int(v) for v in args.bbox.split(","))
    except ValueError as e:
        raise ManifestError("E_ARG_FORMAT",
                            f"--unicode/--bbox 解析失败（--unicode 需 U+4E4B 或 4E4B）: {e!r}") from e
    m = Manifest.load(args.manifest) if Path(args.manifest).exists() else Manifest()
    s = m.register(
        style_id=args.style_id, label=args.label, unicode_cp=cp,
        parent_source_hash=args.parent_hash, source_bbox={
            "x": x, "y": y, "w": w, "h": h,
            "coord_space": args.coord_space},
        parent_width=args.parent_w, parent_height=args.parent_h,
        final_alpha_sha256=sha256_image(im), review_status=args.status,
        source_ref=args.source_ref, notes=args.notes)
    m.save(args.manifest)
    print(json.dumps({"registered": s.sample_id, "review_status": s.review_status,
                      "label": s.label, "codepoint_u": f"U+{s.unicode_cp:04X}",
                      "style_id": s.style_id, "manifest": args.manifest,
                      "counts": m.counts()}, ensure_ascii=False, indent=1))
    return EXIT_OK


def cmd_confirm(args) -> int:
    m = Manifest.load(args.manifest)
    s = m.confirm(args.sample_id, args.confirm_by)
    m.save(args.manifest)
    print(json.dumps({"sample_id": s.sample_id, "review_status": s.review_status,
                      "confirmed_by": s.confirmed_by, "confirmed_at": s.confirmed_at},
                     ensure_ascii=False, indent=1))
    return EXIT_OK


def cmd_export(args) -> int:
    m = Manifest.load(args.manifest)
    rows = m.export_training(args.style_id)
    text = json.dumps({"style_id": args.style_id, "prep_version": m.samples[0].prep_version
                       if m.samples else None, "n": len(rows), "rows": rows},
                      ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({"style_id": args.style_id, "n": len(rows),
                      "out": args.out or "(stdout)"}, ensure_ascii=False))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="charprep", description="S02 字符准备 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inspect", help="字块极性/ink 统计")
    sp.add_argument("image")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("split", help="拆字（candidate/uncertain 两级，不定案）")
    sp.add_argument("image")
    sp.add_argument("--json", help="结果另存 JSON")
    sp.set_defaults(fn=cmd_split)

    sp = sub.add_parser("normalize", help="alpha 规范化 → 128×128 RGBA")
    sp.add_argument("image")
    sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(fn=cmd_normalize)

    sp = sub.add_parser("register", help="登记训练级样本（初始 candidate/uncertain）")
    sp.add_argument("image")
    sp.add_argument("--label", required=True)
    sp.add_argument("--unicode", required=True, help="U+661F 形式；必须与 label 严格一致")
    sp.add_argument("--style-id", required=True)
    sp.add_argument("--parent-hash", required=True, help="父字块文件 sha256（64hex）")
    sp.add_argument("--parent-w", type=int, required=True)
    sp.add_argument("--parent-h", type=int, required=True)
    sp.add_argument("--bbox", required=True, help="x,y,w,h（父图坐标）")
    sp.add_argument("--coord-space", default="pixel:parent_image")
    sp.add_argument("--status", choices=[REVIEW_CANDIDATE, REVIEW_UNCERTAIN],
                    default=REVIEW_CANDIDATE)
    sp.add_argument("--source-ref", default="")
    sp.add_argument("--notes", default="")
    sp.add_argument("--manifest", required=True)
    sp.set_defaults(fn=cmd_register)

    sp = sub.add_parser("confirm", help="显式人工确认（唯一 owner_confirmed 通道）")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--sample-id", required=True)
    sp.add_argument("--confirm-by", required=True, help="确认人署名（必填）")
    sp.set_defaults(fn=cmd_confirm)

    sp = sub.add_parser("export", help="导出训练 manifest（仅 owner_confirmed，单 style_id）")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--style-id", required=True)
    sp.add_argument("-o", "--out", default="")
    sp.set_defaults(fn=cmd_export)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ManifestError, NormalizeError, SplitError) as e:
        print(json.dumps({"rejected": e.code, "detail": str(e)}, ensure_ascii=False))
        return EXIT_REJECT
    except FileNotFoundError as e:
        print(json.dumps({"rejected": "E_IO", "detail": str(e)}, ensure_ascii=False))
        return EXIT_IO


if __name__ == "__main__":
    sys.exit(main())
