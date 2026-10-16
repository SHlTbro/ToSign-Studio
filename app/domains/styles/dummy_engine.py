# -*- coding: utf-8 -*-
"""S06 DUMMY ENGINE — controllable fake engine subprocess (TEST HARNESS).

红线（写死，不许漂移）：
- 本文件是**测试假引擎**，验证的是 worker 生命周期机制（状态机/心跳/退出码/取消/
  收据/artifact 绑定），**不是训练**，不更新任何参数，不产生任何真实模型权重，
  不得在任何收据/响应里冒称 M2 训练或 M1 推理。
- 消费 B10 PRIMARY/SEALED 派生数据为红线：本引擎只读 request.json 里 worker
  给它的路径，不做任何数据集扫描；真实引擎接入时同样禁止（worker 层红线）。
- engine_mode 恒为 "DUMMY_TEST"，quality 语义恒为 none（质量门在 S08/S09）。

stdout 协议（worker 消费，S05 trainer 将对齐同一协议）：
  进度行：  {"type": "progress", "epoch": E, "total_epochs": T, "loss": L,
             "samples_seen": S}
  结果行：  {"type": "result", "ok": true|false, "receipt"| "failure": {...}}
退出码（与 engine/style/m1_adapter/runner.py 0/2/3 语义一致）：
  0 成功；2 引擎侧失败（behaviour=engine_fail）；3 输入非法（behaviour=input_fail）。

behaviour（由 request.json["behaviour"] 注入，测试可控）：
  ok          正常完成（缺省）：epochs 步进度 + artifact 载荷 + 收据
  engine_fail 模拟引擎侧失败 -> exit 2 + 失败收据
  input_fail  模拟输入非法     -> exit 3 + 失败收据
  hang        长睡眠不退出     -> 供取消/超时测试
  crash       无收据崩溃       -> 供"exit0 无收据不得假成功"反面与崩溃路径
  no_receipt  exit 0 但不写收据 -> 供"坏输出但 exit0"反例（worker 必须判失败）
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ENGINE_MODE = "DUMMY_TEST"
ENGINE_ID = "dummy-test-engine-v1"
ENGINE_VERSION = "dummy|test-harness|no-training"
# base ckpt marker：假引擎没有真实基座，这里是不许被误读成真 hash 的显式标记。
BASE_CKPT_MARKER = "dummy-test-no-real-base-ckpt"


def fail(code: str, detail: str) -> dict:
    return {"engine_mode": ENGINE_MODE, "engine_id": ENGINE_ID,
            "runner_version": ENGINE_VERSION, "ok": False,
            "failure": {"code": code, "detail": detail}}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps(fail("E_INPUT_MISSING", "usage: dummy_engine REQ.json")))
        return 3
    try:
        req = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(fail("E_INPUT_JSON", repr(exc))))
        return 3
    if not isinstance(req, dict) or not req.get("out_dir"):
        print(json.dumps(fail("E_INPUT_MISSING", "out_dir required")))
        return 3

    out_dir = Path(req["out_dir"])
    behaviour = req.get("behaviour", "ok")
    epochs = max(1, int(req.get("epochs", 3)))
    step_sleep = float(req.get("step_sleep_s", 0.15))
    run_id = req.get("run_id")

    if behaviour == "input_fail":
        payload = fail("E_INPUT_CHAR", "dummy injected invalid input")
        print(json.dumps({"type": "result", "ok": False, "failure": payload["failure"]}))
        return 3
    if behaviour == "engine_fail":
        payload = fail("E_SAMPLE_FAILED", "dummy injected engine-side failure")
        print(json.dumps({"type": "result", "ok": False, "failure": payload["failure"]}))
        return 2
    if behaviour == "hang":
        # no output, no exit: the worker must observe cancel/timeout itself
        time.sleep(float(req.get("hang_s", 600)))
        return 0
    if behaviour == "crash":
        raise RuntimeError("dummy injected hard crash (no receipt, nonzero exit)")

    # --- progress emission (real measured values of the fake work itself) ---
    seen = 0
    for epoch in range(1, epochs + 1):
        time.sleep(step_sleep)
        seen += int(req.get("samples_per_epoch", 32))
        loss = round(1.0 / epoch, 6)  # deterministic value of the dummy work
        print(json.dumps({"type": "progress", "epoch": epoch,
                          "total_epochs": epochs, "loss": loss,
                          "samples_seen": seen}), flush=True)

    if behaviour == "no_receipt":
        print(json.dumps({"type": "result", "ok": True}))
        return 0  # exit0 WITHOUT a receipt: worker must refuse to call it success

    # --- artifact payload + receipt (deterministic bytes of this run) ---
    artifact_dir = out_dir / "artifact"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload_bytes = json.dumps(
        {"kind": "DUMMY_TEST-payload", "run_id": run_id, "epochs": epochs,
         "samples_seen": seen},
        sort_keys=True).encode("utf-8")
    (artifact_dir / "style_payload.bin").write_bytes(payload_bytes)
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()

    receipt = {
        "engine_mode": ENGINE_MODE,
        "engine_id": ENGINE_ID,
        "runner_version": ENGINE_VERSION,
        "model_version": ENGINE_VERSION,
        "base_ckpt_sha256": BASE_CKPT_MARKER,
        "inputs": {"run_id": run_id, "behaviour": behaviour,
                   "epochs": epochs, "samples_seen": seen,
                   "dataset_snapshot_sha256": req.get("dataset_snapshot_sha256"),
                   "config_sha256": req.get("config_sha256")},
        "timings": {"step_sleep_s": step_sleep},
        "output": {"files": [{"relative_path": "artifact/style_payload.bin",
                              "sha256": payload_sha, "bytes": len(payload_bytes)}]},
        "process": {"pid": None, "exit_code": 0},
        "boundaries": "DUMMY_TEST 假引擎：非训练非推理；quality none（门在 S08/S09）；"
                      "B10/SEALED 禁入训练路径",
    }
    print(json.dumps({"type": "result", "ok": True, "receipt": receipt}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
