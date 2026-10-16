# -*- coding: utf-8 -*-
"""S08 evaluator.gpu_lock — GPU 互斥锁（文件锁 + pid 探活；Line B 纪律：GPU 一次一个）。

锁文件：.ai-hub/parallel-lines/line-b/gpu.lock（协调台账同目录，跨 Agent 可见）。
内容 JSON：{"pid", "task", "created_at"}。占用判定用 **ctypes 探活**（Windows：
OpenProcess+GetExitCodeProcess；pid 已死 → 陈旧锁，摘除重试）。

协议（任务纪律）：占用 → 等待 3×60s → 放弃返回 None（调用方 DEFERRED_GPU_BUSY，
不抢锁、不伪造结果）。释放时校验锁内容 pid==自己，避免误删他人锁。
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import json
import os
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_PATH = PROJ / ".ai-hub" / "parallel-lines" / "line-b" / "gpu.lock"

WAIT_CYCLES = 3
WAIT_SECONDS = 60
_STALE_AFTER_SECONDS = 6 * 3600  # 探活失败兜底：超龄锁也视为陈旧

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED = 0x1000


def _pid_alive(pid: int) -> bool:
    """Windows ctypes 探活：进程存在且未退出 → True。非 Windows 回退 os.kill(pid,0)。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32  # noqa: PLW1508 —— Windows 专用探活
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return False  # 无权限/不存在都按不占用受阻处理中的"已死"分支
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True  # 查询失败保守视为存活
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 空文件/半截写入/非 JSON：按陈旧锁处理
        return None


def _lock_age_seconds(info: dict) -> float:
    try:
        t = _dt.datetime.fromisoformat(info["created_at"])
        return (_dt.datetime.now(tz=t.tzinfo) - t).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0


def acquire(task: str, lock_path: Path = DEFAULT_LOCK_PATH,
            wait_cycles: int = WAIT_CYCLES, wait_seconds: int = WAIT_SECONDS):
    """获取 GPU 锁 → LockHandle | None（None = 占用且等待耗尽 → DEFERRED_GPU_BUSY）。

    陈旧锁（持有人 pid 已死/内容损坏/超龄）摘除重试**不消耗等待圈数**；
    摘除自身失败（如被他人抢先）超过 3 次按占用处理，防死循环。
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    waited = 0
    steal_failures = 0
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = {"pid": os.getpid(), "task": task,
                       "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
            return LockHandle(lock_path, payload)
        except FileExistsError:
            info = _read_lock(lock_path)
            stale = (info is None
                     or not isinstance(info.get("pid"), int)
                     or not _pid_alive(info["pid"])
                     or _lock_age_seconds(info) > _STALE_AFTER_SECONDS)
            if stale:
                try:
                    lock_path.unlink()  # 摘陈旧锁（持有人已死/内容损坏）
                    continue
                except OSError:
                    steal_failures += 1
                    if steal_failures > 3:
                        return None  # 摘不掉（他人抢先重写）→ 按占用处理
                    continue
            if waited >= wait_cycles:
                return None
            waited += 1
            import time
            time.sleep(wait_seconds)


class LockHandle:
    def __init__(self, path: Path, payload: dict):
        self.path = path
        self.payload = payload

    def release(self) -> bool:
        """仅当锁内容仍是自己时删除（防误摘他人锁）。"""
        info = _read_lock(self.path)
        if info is not None and info.get("pid") == self.payload["pid"] \
                and info.get("task") == self.payload["task"]:
            try:
                self.path.unlink()
                return True
            except OSError:
                return False
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
