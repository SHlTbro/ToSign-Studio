"""S06 StyleRun worker: real queue claiming, explicit state machine,
heartbeat, cancellation, restart recovery, GPU lock and engine subprocesses.

State machine (backend RUN_STATUSES, 6 states; the frontend 'timeout' value is
a registered contract drift, see styles-api-gap-list.md section B):

    created --start--> queued --worker claim--> running --> succeeded
                                                 |  \--> failed
    cancel at queued/running -----------------> cancelled (terminates own subprocess)

Restart protocol: only exact recorded dead executions are failed; live or
unobservable worker/child identities are preserved. Full checkpoint metadata
is retained, but independent Runtime resume qualification remains required;
``queued`` runs are simply re-claimed. No fake progress is written at any
point: engine unavailable -> ``failed/ENGINE_UNAVAILABLE``, busy GPU ->
``failed/ENGINE_BUSY``, exit 0 without a verifiable receipt ->
``failed/E_RECEIPT_ABSENT`` (a bad output with exit 0 must never pass).

Engine modes (per-run ``config.engine`` overrides env ``YY_STYLE_ENGINE``):
- ``dummy`` (default): app/domains/styles/dummy_engine.py - a controllable
  TEST HARNESS subprocess speaking the exact production contract (NDJSON
  progress lines on stdout, single-line result line, exit codes 0/2/3).
  It never trains and its receipts say so (engine_mode=DUMMY_TEST).
- ``m1``: the REAL frozen-basis inference adapter as a subprocess,
  ``-m engine.style.m1_adapter.runner`` (json contract; the API process never
  imports the research dependency chain). One generation per run sample
  (different refs x same target char, S04 DoD shape); truth comes from the
  runner's out_receipt FILE (its stdout receipt is multi-line and is only
  logged). M1 is inference, not training - loss/epoch stay None, never
  fabricated.

GPU lock convention: before a REAL engine (m1) call the worker creates
``.ai-hub/parallel-lines/line-b/gpu.lock`` (JSON: pid + task); a live lock
makes the run fail honestly with ENGINE_BUSY after the wait budget (no queue
blocking of the API, no second GPU task on the same card). The lock is
deleted in ``finally``. The dummy engine does not touch the GPU and takes no
lock.

Data discipline red line: B10 PRIMARY/SEALED material must never enter any
training path through this worker (see also models.StyleRunArtifact).
"""

from __future__ import annotations

import ctypes
import json
import os
import queue as queue_mod
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.core.audit import audit
from app.core.db import get_engine
from app.core.errors import ApiError
from app.core.storage import ensure_within_root, storage_root
from app.domains.styles import runstore
from app.domains.styles.models import StyleRun, StyleSample

PROJ = Path(__file__).resolve().parents[3]
DUMMY_ENGINE_SCRIPT = Path(__file__).resolve().parent / "dummy_engine.py"
from engine.style.m2_adapter.windows_gpu_support import gpu_lock_path
GPU_LOCK_PATH = gpu_lock_path(PROJ)

ENGINE_DUMMY = "dummy"
ENGINE_M1 = "m1"
ENGINE_M2 = "m2"
M2_MODE = "M2_ADAPTER"
M1_MODE = "M1_FROZEN"

POLL_SECONDS = 0.1
PROGRESS_MIN_INTERVAL = 0.2

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_engine_default() -> str:
    return os.environ.get("YY_STYLE_ENGINE", ENGINE_DUMMY).strip().lower()


def _engine_timeout_s(config=None) -> float:
    explicit=os.environ.get("YY_STYLE_ENGINE_TIMEOUT_S")
    if explicit is not None:return float(explicit)
    if isinstance(config,dict) and config.get("engine")=="m2":
        import math
        requested=float((config.get("train") or {}).get("max_seconds",900))
        if math.isfinite(requested) and requested>0:
            return max(900.0,min(requested+180.0,7200.0))
    return 900.0


def _gpu_lock_wait_s() -> float:
    return float(os.environ.get("YY_STYLE_GPU_LOCK_WAIT_S", "0"))


def _m1_python() -> str:
    return os.environ.get("YY_STYLE_M1_PYTHON") or str(PROJ / ".venv-ml" / "Scripts" / "python.exe")


# ---------------------------------------------------------------------------
# GPU lock (.ai-hub/parallel-lines/line-b/gpu.lock)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Liveness probe that is safe on Windows (os.kill(pid, 0) would
    TERMINATE the process there - ctypes OpenProcess instead)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def gpu_lock_read() -> dict | None:
    try:
        return json.loads(GPU_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_GPU_LEASE_LOCAL = threading.local()


def gpu_lock_acquire(task: str, wait_s: float = 0.0) -> tuple[bool, str]:
    """Shared nonce + process birth lease. Never auto-delete an old GPU lock."""
    from engine.style.m2_adapter.windows_gpu_support import acquire_gpu_lease
    deadline=time.monotonic()+max(0.0,wait_s)
    while True:
        try:
            _GPU_LEASE_LOCAL.lease=acquire_gpu_lease(PROJ,task)
            return True,'acquired'
        except (OSError,ValueError) as exc:
            if getattr(exc,'code',None)!='E_WINDOWS_SCR_BUSY':return False,str(exc)
            if time.monotonic()>=deadline:return False,'GPU lease exists; explicit dead-owner recovery required'
            time.sleep(min(.2,max(0.0,deadline-time.monotonic())))


def gpu_lock_release() -> None:
    """Release only the exact token acquired by this worker through its handle."""
    lease=getattr(_GPU_LEASE_LOCAL,'lease',None)
    if lease is None:return
    from engine.style.m2_adapter.windows_gpu_support import release_gpu_lease
    release_gpu_lease(lease)
    _GPU_LEASE_LOCAL.lease=None


# ---------------------------------------------------------------------------
# progress heartbeat
# ---------------------------------------------------------------------------

class _Progress:
    """Worker-side heartbeat accumulator. Percentages only from real totals,
    eta only from a measured rate - both None otherwise (anti-fabrication)."""

    def __init__(self, run_id: int, user_id: int, engine_mode: str, stage: str):
        self.run_id = run_id
        self.user_id = user_id
        self.engine_mode = engine_mode
        self.stage = stage
        self.epoch = None
        self.total_epochs = None
        self.loss = None
        self.samples_seen = None
        self.units_done = 0
        self.units_total = None
        self.unit = "steps"
        self.failure = None
        self._started = time.monotonic()
        self._last_unit_at = None
        self._dirty = True

    def apply_engine_line(self, line: dict) -> None:
        if line.get("type") != "progress":
            return
        self.epoch = line.get("epoch")
        self.total_epochs = line.get("total_epochs")
        self.loss = line.get("loss")
        self.samples_seen = line.get("samples_seen")
        if self.units_total is None and isinstance(self.total_epochs, int):
            self.units_total = self.total_epochs
            self.unit = "epochs"
        if isinstance(self.epoch, int) and not isinstance(self.epoch, bool):
            self._note_unit(self.epoch)
        self._dirty = True

    def note_unit(self, done: int, total: int | None, unit: str) -> None:
        self.units_done = max(self.units_done, done)
        self.units_total = total
        self.unit = unit
        self._note_unit(done)
        self._dirty = True

    def _note_unit(self, done: int) -> None:
        if done > self.units_done:
            self.units_done = done
            self._last_unit_at = time.monotonic()

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self._dirty = True

    def set_failure(self, code: str, detail: str) -> None:
        self.failure = {"code": code, "detail": detail[:400]}
        self._dirty = True

    def _eta(self) -> float | None:
        """Measured-rate eta only; None when unknowable. Never negative."""
        if (self.units_done >= 1 and self._last_unit_at is not None
                and self.units_total is not None and self.units_total > self.units_done):
            elapsed = self._last_unit_at - self._started
            rate = elapsed / self.units_done
            if rate > 0:
                return round(max(0.0, rate * (self.units_total - self.units_done)), 1)
        return None

    def payload(self) -> dict:
        total = self.units_total
        percent = None
        if total is not None and total > 0 and self.units_done <= total:
            percent = round(100.0 * self.units_done / total, 1)
        body = {
            "run_id": self.run_id,
            "status": "running",
            "stage": self.stage,
            "engine_mode": self.engine_mode,
            "epoch": self.epoch,
            "total_epochs": self.total_epochs,
            "loss": self.loss,
            "eta_s": self._eta(),
            "percent": percent,
            "samples_seen": self.samples_seen,
            "units_done": self.units_done,
            "units_total": self.units_total,
            "unit": self.unit,
        }
        if self.failure is not None:
            body["failure"] = self.failure
        return body

    def flush(self, force: bool = False) -> None:
        if self._dirty or force:
            runstore.write_progress(self.user_id, self.run_id, self.payload())
            self._dirty = False


def _freeze_progress(user_id: int, run_id: int, status: str, prog: "_Progress | None") -> None:
    """Terminal freeze: status flips, eta/percent stop growing forever."""
    if prog is not None:
        body = prog.payload()
    else:
        body = {"run_id": run_id, "stage": None, "engine_mode": None, "epoch": None,
                "total_epochs": None, "loss": None, "eta_s": None, "percent": None,
                "samples_seen": None, "units_done": 0, "units_total": None,
                "unit": "steps"}
    body["status"] = status
    body["eta_s"] = None  # completed/cancelled runs must not keep counting down
    body.pop("worker_pid", None)
    runstore.write_progress(user_id, run_id, body)


# ---------------------------------------------------------------------------
# engine subprocess plumbing
# ---------------------------------------------------------------------------

def _sample_abs_paths(session, run: StyleRun) -> list[tuple[int, Path]]:
    """Locked sample set -> contained on-disk paths, in sample_id order.

    Ownership hardening (S06): the styles register_sample flow accepts caller
    relative paths, so the worker must never follow one OUTSIDE the run
    owner's tree - a foreign path would let a run consume another user's file
    as its style reference (cross-user leak through the engine). Both
    historical layouts are accepted: ``users/{uid}/...`` and the legacy
    ``{uid}/...`` pool form. Anything else -> ApiError -> run failed
    E_INPUT_REF. Raises ApiError(INVALID_REQUEST) on root escape."""
    ids = json.loads(run.sample_ids or "[]")
    rows = (
        session.query(StyleSample)
        .filter(StyleSample.sample_id.in_(ids))
        .order_by(StyleSample.sample_id.asc())
        .all()
    )
    root = storage_root()
    owner_users = os.path.normcase(str((root / "users" / str(run.user_id)).resolve()))
    owner_legacy = os.path.normcase(str((root / str(run.user_id)).resolve()))
    out: list[tuple[int, Path]] = []
    for row in rows:
        path = ensure_within_root(root / row.relative_path, root)
        norm = os.path.normcase(str(path))
        if not (norm == owner_users or norm.startswith(owner_users + os.sep)
                or norm == owner_legacy or norm.startswith(owner_legacy + os.sep)):
            raise ApiError(
                400,
                "INVALID_REQUEST",
                f"sample {row.sample_id} path escapes the run owner's storage tree",
            )
        out.append((row.sample_id, path))
    return out


def _build_dummy_argv(req_path: Path) -> list[str]:
    return [sys.executable, "-X", "utf8", str(DUMMY_ENGINE_SCRIPT), str(req_path)]


def _build_m1_argv(req_path: Path) -> list[str]:
    return [_m1_python(), "-X", "utf8", "-m", "engine.style.m1_adapter.runner", str(req_path)]


def _m2_python(config=None) -> str:
    from engine.style.m2_adapter.runtime import runtime_python
    from engine.style.m2_adapter.runtime_variants import binding_for_config
    resolved=binding_for_config(PROJ,config or {})
    return resolved['python'] if resolved else str(runtime_python((config or {}).get('runtime_manifest_path')))


def _build_m2_argv(req_path: Path, config=None) -> list[str]:
    return [_m2_python(config), "-X", "utf8", "-m", "engine.style.m2_adapter.runner", str(req_path)]


def _spawn(argv: list[str], cwd: Path, runtime_binding=None):
    from engine.style.m2_adapter.runtime_variants import bound_subprocess_env
    from engine.style.m2_adapter.windows_user_scr import VARIANTS
    env=bound_subprocess_env(runtime_binding)
    if isinstance(runtime_binding,dict) and runtime_binding.get('variant_id') in VARIANTS:
        from engine.style.m2_adapter.windows_training_runtime import _env
        env=_env(Path(argv[-1]).parent/'windows-scr-private-env',runtime_binding['variant_id'])
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(a) for a in argv],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
    )


_READER_DONE = "__reader_done__"


def _stdout_reader(proc, lines: "queue_mod.Queue[str]") -> None:
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            lines.put(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    finally:
        lines.put(_READER_DONE)


def _parse_json_line(line: str) -> dict | None:
    """Single-line JSON objects only (the dummy engine's stdout protocol).
    Multi-line receipts (m1 runner prints indent=1) are LOG material here;
    their truth is the out_receipt file."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# claiming / recovery / lifecycle
# ---------------------------------------------------------------------------

def _next_queued_run_id(session) -> int | None:
    """Peek the FIFO head (no state change; the claim itself is atomic)."""
    row = (
        session.query(StyleRun)
        .filter(StyleRun.status == "queued")
        .order_by(StyleRun.run_id.asc())
        .first()
    )
    return row.run_id if row is not None else None


def _claim(session, run_id: int) -> bool:
    """Atomic conditional UPDATE queued->running: two workers / two processes
    can never take the same run (risk list: 重复领任务 / 两实例同 run)."""
    updated = (
        session.query(StyleRun)
        .filter(StyleRun.run_id == run_id, StyleRun.status == "queued")
        .update({"status": "running", "updated_at": _now()})
    )
    if updated:
        from .run_recovery import record_execution
        record_execution(session, run_id)
    session.commit()
    if updated:
        audit(session, action="style_run_start", actor="system",
              object_ref=f"style_runs/{run_id}", result="claimed")
        session.commit()
    return bool(updated)


def recover_interrupted(session) -> int:
    """Never fail another live worker, or release a lease with unknown ownership."""
    from .run_recovery import execution_state, archive_dead_cpu_lease, archive_dead_gpu_lease, record_execution
    stuck = session.query(StyleRun).filter(StyleRun.status.in_(['running', 'cancelled'])).all()
    recovered = 0
    for run in stuck:
        if execution_state(session, run.run_id) != 'EXECUTION_DEAD':
            continue
        try:
            archive_dead_gpu_lease(session, run, PROJ)
            archive_dead_cpu_lease(session, run, PROJ)
        except (OSError, ValueError, KeyError): continue
        record_execution(session, run.run_id, phase='ended')
        if run.status == 'cancelled': continue
        changed = session.query(StyleRun).filter_by(run_id=run.run_id, status='running').update(
            {'status':'failed', 'updated_at':_now()}, synchronize_session=False)
        if not changed: continue
        recovered += 1
        audit(session, action="worker_recovered", actor="system",
              object_ref=f"style_runs/{run.run_id}", result="failed WORKER_RESTARTED")
        _freeze_progress(run.user_id, run.run_id, "failed", None)
    session.commit()
    return recovered


def _run_status_in_db(run_id: int) -> str | None:
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        row = s.get(StyleRun, run_id)
        return row.status if row is not None else None
    finally:
        s.close()


def _finalize(session, run: StyleRun, status: str, code: str, detail: str,
              prog: "_Progress | None") -> None:
    """Single terminal writer. Cancel observed in the DB wins over a result
    that raced in (取消恰好完成: the cancel is the owner's explicit intent; the
    engine output files stay on disk but the run is NOT marked succeeded)."""
    session.query(StyleRun).filter_by(run_id=run.run_id, status='running').update(
        {'status':status, 'updated_at':_now()}, synchronize_session=False)
    session.flush()
    session.refresh(run)
    status = run.status
    _freeze_progress(run.user_id, run.run_id, status, prog)
    action = {
        "succeeded": "style_run_succeeded",
        "failed": "style_run_failed",
        "cancelled": "style_run_cancelled",
    }[status]
    audit(session, action=action, actor="system",
          object_ref=f"style_runs/{run.run_id}",
          result=f"{code}: {detail[:180]}" if code else detail[:180])
    session.commit()


def _base_ckpt_binding(last_receipt: dict):
    """Base-checkpoint binding for the artifact manifest. The m1 receipt
    carries the measured hash set under ``ckpt_detail`` (normalized here to
    name->sha256); the dummy carries its explicit no-real-base marker. Only
    measured values or the explicit marker - never a fabricated hash."""
    detail = last_receipt.get("ckpt_detail")
    if isinstance(detail, dict) and detail:
        return {name: entry.get("sha256") for name, entry in detail.items()
                if isinstance(entry, dict) and entry.get("sha256")}
    marker = last_receipt.get("base_ckpt_sha256")
    return marker if marker else "unknown"


def _write_receipt_and_manifest(run: StyleRun, engine_mode: str, engine_id: str,
                                engine_version: str, base_ckpt, steps: list[dict],
                                dataset_snapshot_sha256: str, config_sha256: str,
                                artifact_files: list[dict], snapshot_binding: dict | None = None,
                                training_receipt: dict | None = None) -> str:
    """Write receipts/engine_receipt.json + artifact/artifact.json; return the
    manifest sha256 (the S10 publish binding anchor)."""
    base_dir = runstore.run_dir(run.user_id, run.run_id)
    if runstore.receipt_path(run.user_id, run.run_id).exists() or runstore.artifact_manifest_path(run.user_id, run.run_id).exists():
        raise ValueError("refusing to overwrite historical engine receipt/artifact")
    receipt_body = {
        "ok": True,
        "engine_mode": engine_mode,
        "engine_id": engine_id,
        "engine_version": engine_version,
        "steps": steps,
        "dataset_snapshot_sha256": dataset_snapshot_sha256,
        "config_sha256": config_sha256,
        "finished_at": _now(),
    }
    if snapshot_binding is not None:
        receipt_body["snapshot_binding"] = snapshot_binding
        if training_receipt is not None:
            receipt_body["runtime_evidence"] = training_receipt.get("runtime_evidence")
            receipt_body["adapter_selection"] = training_receipt.get("adapter_selection")
            receipt_body["runtime_identity_sha256"] = training_receipt.get("runtime_identity_sha256")
            receipt_body["preprocessing"] = training_receipt.get("preprocessing")
            receipt_body["training_objective"] = training_receipt.get("training_objective")
            receipt_body["training_receipt"] = {"relative_path": "m2-receipt.json",
                                                "sha256": runstore.sha256_file(base_dir / "m2-receipt.json")}
    runtime_binding=json.loads(run.config or '{}').get('runtime_binding')
    if runtime_binding is not None:receipt_body['runtime_binding']=runtime_binding
    if training_receipt:
        for key in ('training_recipe','training_budget','recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity'):
            if key in training_receipt:receipt_body[key]=training_receipt[key]
    runstore.write_json_atomic(runstore.receipt_path(run.user_id, run.run_id), receipt_body)
    manifest = {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "engine_mode": engine_mode,
        "engine_id": engine_id,
        "engine_version": engine_version,
        "base_ckpt_sha256": base_ckpt,
        "dataset_snapshot_sha256": dataset_snapshot_sha256,
        "config_sha256": config_sha256,
        "metrics_receipt_relative_path": runstore.receipt_path(run.user_id, run.run_id).relative_to(base_dir).as_posix(),
        "metrics_receipt_sha256": runstore.sha256_file(runstore.receipt_path(run.user_id, run.run_id)),
        "files": artifact_files,
        "quality_gate": "BLOCKED_OWNER",
        "data_discipline": "B10 PRIMARY/SEALED 禁入训练路径；本 run 未消费任何 B10/SEALED 派生数据",
        "created_at": _now(),
    }
    if snapshot_binding is not None:
        manifest["snapshot_binding"] = snapshot_binding
        manifest["inference_defaults"] = {"reference_label": json.loads(run.config or "{}").get("acceptance_reference_label", "永")}
        if training_receipt is not None:
            manifest["runtime_manifest_path"] = training_receipt.get("runtime_manifest_path")
            manifest["runtime_identity_sha256"] = training_receipt.get("runtime_identity_sha256")
            manifest["preprocessing"] = training_receipt.get("preprocessing")
            manifest["training_objective"] = training_receipt.get("training_objective")
    if runtime_binding is not None:manifest["runtime_binding"]=runtime_binding
    if training_receipt:
        for key in ('training_recipe','training_budget','recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity'):
            if key in training_receipt:manifest[key]=training_receipt[key]
    manifest_path = runstore.artifact_manifest_path(run.user_id, run.run_id)
    runstore.write_json_atomic(manifest_path, manifest)
    return runstore.sha256_file(manifest_path)


def _pump(proc, run: StyleRun, prog: "_Progress", deadline: float, resume_binding=None) -> tuple[int, dict | None, dict | None]:
    """Drain one engine subprocess: bounded log copy, NDJSON progress lines,
    cancel/timeout watch (terminates OUR OWN child only), single-line result
    capture. Returns (exit_code, result_receipt, failure)."""
    lines_q: "queue_mod.Queue[str]" = queue_mod.Queue()
    reader = threading.Thread(target=_stdout_reader, args=(proc, lines_q), daemon=True)
    reader.start()
    result_receipt: dict | None = None
    failure: dict | None = None
    log_buf: list[str] = []
    last_flush = time.monotonic()
    cancel_deadline = None
    reader_done = False
    from .run_recovery import record_execution
    owned = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        record_execution(owned, run.run_id, child=proc, phase='executing'); owned.commit()
    finally: owned.close()

    def drain():
        nonlocal log_buf, last_flush
        if log_buf:
            runstore.append_engine_log(run.user_id, run.run_id, "".join(log_buf))
            log_buf = []
        if time.monotonic() - last_flush >= PROGRESS_MIN_INTERVAL:
            prog.flush()
            last_flush = time.monotonic()

    while True:
        try:
            line = lines_q.get(timeout=POLL_SECONDS)
        except queue_mod.Empty:
            line = None
        if line == _READER_DONE:
            reader_done = True
            line = None
        if line is not None:
            log_buf.append(line)
            obj = _parse_json_line(line)
            if obj is not None:
                if obj.get("type") == "progress":
                    prog.apply_engine_line(obj)
                elif obj.get('type') == 'trainer_checkpoint' and resume_binding is not None:
                    from .run_recovery import register_checkpoint
                    anchor = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
                    try:
                        register_checkpoint(anchor, run, obj, resume_binding); anchor.commit()
                    except (ValueError, OSError, KeyError) as exc:
                        anchor.rollback()
                        failure = {'code':'E_CHECKPOINT_ANCHOR', 'detail':str(exc)}
                    finally: anchor.close()
                elif obj.get("type") == "result":
                    if obj.get("ok") is True:
                        result_receipt = obj.get("receipt")
                    elif failure is None:
                        failure = dict(obj.get("failure")
                                       or {"code": "E_ENGINE", "detail": "engine reported failure"})
        cancelled = _run_status_in_db(run.run_id) == "cancelled"
        timed_out = time.monotonic() > deadline
        if cancelled or timed_out:
            if failure is None:
                failure = ({"code": "CANCELLED", "detail": "cancel observed while engine running"}
                           if cancelled else
                           {"code": "ENGINE_TIMEOUT", "detail": "engine exceeded the run deadline"})
            if resume_binding is not None and cancel_deadline is None:
                runstore.write_json_atomic(runstore.run_dir(run.user_id,run.run_id)/'control.json',
                    {'run_id':run.run_id, 'cancel_requested':True, 'reason':failure['code']})
                cancel_deadline = time.monotonic()+30
            if cancel_deadline is not None and time.monotonic() < cancel_deadline and proc.poll() is None:
                drain(); continue
            if proc.poll() is not None:
                drain()
                if reader_done: break
                continue
            try:
                proc.terminate()  # terminate OUR OWN child only
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass
            drain()
            break
        drain()
        if reader_done and proc.poll() is not None:
            break

    drain()
    exit_code = proc.wait()
    return exit_code, result_receipt, failure


def _verify_artifact_files(base_dir: Path, files: list[dict]) -> dict | None:
    """Every manifest file must exist and recompute to its recorded hash
    (bad-output-but-exit-0 must not pass)."""
    for entry in files:
        path = base_dir / entry["relative_path"]
        if not path.is_file():
            return {"code": "E_OUTPUT_INVALID",
                    "detail": f"receipt lists missing file: {entry['relative_path']}"}
        if runstore.sha256_file(path) != entry["sha256"]:
            return {"code": "E_OUTPUT_INVALID",
                    "detail": f"hash mismatch on {entry['relative_path']}"}
    return None


def execute_run(run_id: int) -> str:
    """Synchronous full lifecycle of one run: atomic claim -> engine
    subprocess -> heartbeat -> terminal state. Used by the resident loop AND
    directly by tests/probes (deterministic, no thread timing). Full failure
    containment: the queue path never surfaces an exception."""
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    proc = None
    claimed = False
    gpu_locked = False
    cpu_lease = None
    scr_gpu_lease = None
    try:
        if not _claim(s, run_id):
            return "skipped"  # not queued anymore / another worker took it
        claimed = True
        run = s.get(StyleRun, run_id)
        if run is None:
            return "skipped"
        user_id, config = run.user_id, json.loads(run.config or "{}")
        engine_mode = str(config.get("engine") or _env_engine_default()).lower()
        started_mono = time.monotonic()

        base_dir = runstore.run_dir(user_id, run_id, makedirs=True)
        prog = _Progress(run_id, user_id,
                         M1_MODE if engine_mode == ENGINE_M1 else engine_mode.upper(),
                         "preparing")
        prog.flush(force=True)

        def fail_now(code: str, detail: str) -> str:
            prog.set_stage("failed")
            prog.set_failure(code, detail)
            _finalize(s, run, "failed", code, detail, prog)
            return run.status

        if engine_mode not in (ENGINE_DUMMY, ENGINE_M1, ENGINE_M2):
            return fail_now("E_CONFIG", f"unknown engine {engine_mode!r}")

        from engine.style.m2_adapter.user_scr_contract import RECIPE_ID as USER_SCR_RECIPE_ID
        from engine.style.m2_adapter.windows_training_recipe import RECIPE_ID as WINDOWS_RECIPE
        if config.get('recipe_id')==WINDOWS_RECIPE:
            from .windows_scr_product import canonical
            try:canonical(s,user_id,config,json.loads(run.sample_ids or '[]'),require_existing=True)
            except (ValueError,OSError,KeyError,TypeError) as exc:return fail_now(getattr(exc,'code','STALE_CACHE'),str(exc))
        if config.get('recipe_id') == USER_SCR_RECIPE_ID:
            from .user_scr_product import canonical_user_scr_config, UserSCRProductError
            try:canonical_user_scr_config(s,user_id,config,json.loads(run.sample_ids or '[]'),require_existing=True)
            except UserSCRProductError as exc:return fail_now(exc.code,str(exc))

        # real engine -> GPU lock first (dummy is a CPU-only test harness: no lock)
        if engine_mode in (ENGINE_M1, ENGINE_M2):
            if config.get('recipe_id')==WINDOWS_RECIPE:
                from engine.style.m2_adapter.windows_training_runtime import acquire_cpu_lease
                try:
                    cpu_lease=acquire_cpu_lease(PROJ,f'style_run/{run_id}')
                    if config['runtime_binding']['device']=='cuda':
                        from engine.style.m2_adapter.windows_gpu_support import acquire_gpu_lease
                        scr_gpu_lease=acquire_gpu_lease(PROJ,f'style_run/{run_id}')
                except ValueError as exc:return fail_now('ENGINE_BUSY',str(exc))
                from .run_recovery import record_execution
                record_execution(s, run_id, cpu_lease=cpu_lease, gpu_lease=scr_gpu_lease); s.commit()
            else:
                ok, reason = gpu_lock_acquire(f"style_run/{run_id}", _gpu_lock_wait_s())
                if not ok:return fail_now("ENGINE_BUSY", f"GPU busy: {reason}")
                gpu_locked = True
                # Persist the SAME owned token before any legacy child can start.
                # A restarted worker must be able to archive only this dead run.
                lease=getattr(_GPU_LEASE_LOCAL,'lease',None)
                if lease is None:raise ValueError('owned legacy GPU lease is unavailable')
                from .run_recovery import record_execution
                record_execution(s, run_id, gpu_lease=lease); s.commit()
            interpreter = _m1_python() if engine_mode == ENGINE_M1 else _m2_python(config)
            if not Path(interpreter).exists():
                return fail_now("ENGINE_UNAVAILABLE",
                                f"{engine_mode} interpreter not found: {interpreter}")

        # M2 consumes only the immutable S03 snapshot; live P4 files are not
        # an input dependency and their legacy list digest is not authority.
        samples = []
        dataset_snapshot_sha256 = None
        if engine_mode != ENGINE_M2:
            try:
                samples = _sample_abs_paths(s, run)
            except ApiError as exc:
                return fail_now("E_INPUT_REF", exc.detail)
            if not samples:
                return fail_now("E_INPUT_SAMPLES", "run has no resolvable samples")
            for sample_id, path in samples:
                if not path.is_file():
                    return fail_now("E_INPUT_REF", f"sample {sample_id} file missing: {path}")
    
            dataset_snapshot = [
                {"sample_id": sid, "sha256": sha}
                for sid, sha in (
                    s.query(StyleSample.sample_id, StyleSample.sha256)
                    .filter(StyleSample.sample_id.in_([sid for sid, _ in samples]))
                    .order_by(StyleSample.sample_id.asc())
                    .all()
                )
            ]
            dataset_snapshot_sha256 = runstore.canonical_sha256(dataset_snapshot)
        # M2 authority is the verified frozen S03 content, never the legacy
        # StyleSample list above. Keep that list digest for dummy/M1 only.
        snapshot_binding = None
        if engine_mode == ENGINE_M2:
            from engine.style.dataset.binding import verify_snapshot_binding
            from engine.style.dataset.schema import DatasetError
            try:
                snapshot_binding = verify_snapshot_binding(
                    config.get("snapshot_dir"), config.get("snapshot_sha256"), base_dir=PROJ)
            except DatasetError as exc:
                return fail_now(exc.code, str(exc))
            from app.domains.styles.models import StyleProfile
            profile = s.query(StyleProfile).filter(
                StyleProfile.style_id == config.get("style_id"),
                StyleProfile.user_id == user_id).first()
            if profile is None or profile.status != "approved" or profile.source_type != "HANDWRITING_ASSET":
                return fail_now("E_STYLE_BINDING", "M2 requires this user's approved handwriting style")
            if str(snapshot_binding.get("style_id")) != str(profile.style_id):
                return fail_now("E_STYLE_BINDING", "S03 style identity differs from the requested style")
            dataset_snapshot_sha256 = snapshot_binding["snapshot_sha256"]
        config_sha256 = runstore.canonical_sha256(config)

        deadline = started_mono + _engine_timeout_s(config)
        argv_builder = (_build_dummy_argv if engine_mode == ENGINE_DUMMY
                        else _build_m2_argv if engine_mode == ENGINE_M2 else _build_m1_argv)
        prog.set_stage("engine")
        if engine_mode == ENGINE_M1:
            # m1 units are chars (worker-driven); dummy totals arrive via the
            # engine's own progress lines - presetting them here would fake the
            # percentage denominator (probe G2 caught exactly that).
            prog.note_unit(0, len(samples), "chars")
        prog.flush(force=True)

        steps: list[dict] = []
        artifact_files: list[dict] = []
        last_receipt: dict | None = None
        failure: dict | None = None
        exit_code = 0

        if engine_mode == ENGINE_DUMMY:
            req = {
                "run_id": run_id,
                "out_dir": str(base_dir),
                "dataset_snapshot_sha256": dataset_snapshot_sha256,
                "config_sha256": config_sha256,
                "behaviour": config.get("behaviour", "ok"),
                "epochs": int(config.get("epochs", 3)),
                "step_sleep_s": float(config.get("step_sleep_s", 0.15)),
                "samples_per_epoch": int(config.get("samples_per_epoch", 32)),
                "hang_s": float(config.get("hang_s", 600)),
            }
            req_path = base_dir / "request.json"
            runstore.write_json_atomic(req_path, req)
            proc = _spawn(argv_builder(req_path), PROJ)
            exit_code, receipt, failure = _pump(proc, run, prog, deadline)
            if receipt is not None and failure is None:
                last_receipt = receipt
                artifact_files = [
                    {"relative_path": f["relative_path"], "sha256": f["sha256"],
                     "bytes": f.get("bytes")}
                    for f in receipt.get("output", {}).get("files", [])
                ]
        elif engine_mode == ENGINE_M2:
            # M2_ADAPTER: one training run over the run's confirmed samples;
            # data authority = the pinned S03 snapshot (runner re-verifies sha)
            req = {
                "run_id": run_id,
                "out_dir": str(base_dir),
                "style_id": config.get("style_id"),
                "user_id": user_id,
                "snapshot_dir": snapshot_binding["snapshot_dir"],
                "snapshot_sha256": dataset_snapshot_sha256,
                "train": {"device":"cpu", **(config.get("train") or {})},
                "fd_repo": config.get("fd_repo"),
                "content_font_path": config.get("content_font_path"),
                "base_npz_sha256": config.get("base_npz_sha256"),
                "runtime_manifest_path": config.get("runtime_manifest_path"),
                "runtime_identity_sha256": config.get("runtime_identity_sha256"),
                "runtime_binding": config.get("runtime_binding"),
                "expected_environment_identity_sha256": (config.get("runtime_binding") or {}).get("environment_identity_sha256"),
                "preprocessing_profile": config.get("preprocessing_profile", "legacy-pil72-gray-v1"),
            }
            for key in ('recipe_id','objective_profile','checkpoint_selection','training_recipe','training_budget','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity'):
                if key in config:req[key]=config[key]
            if 'scr_asset_paths' in config:req['scr_asset_paths']=config['scr_asset_paths']
            if config.get('recipe_id') in (USER_SCR_RECIPE_ID,WINDOWS_RECIPE):
                req['user_id'] = user_id
            missing = [k for k in ("snapshot_dir", "snapshot_sha256") if not req.get(k)]
            if missing:
                return fail_now("E_INPUT_MISSING", f"m2 config missing: {missing}")
            req_path = base_dir / "request.json"
            from .run_recovery import checkpoint_binding, verified_checkpoint, assert_resume_qualified
            if isinstance(config.get('runtime_binding'),dict) and config['runtime_binding']:
                req['trainer_resume_binding'] = checkpoint_binding(run)
            marker = config.get('_server_continuation', {})
            if marker.get('mode') == 'resume':
                assert_resume_qualified(config, req.get('trainer_resume_binding'))
                parent = s.get(StyleRun, marker['parent_run_id'])
                saved, checked = verified_checkpoint(s, parent, marker['checkpoint_id'])
                req['resume_checkpoint'] = {'directory':str(Path(checked['checkpoint_path']).parent),
                    'manifest_sha256':saved.manifest_sha256}
            req['control_path'] = str(base_dir/'control.json')
            runstore.write_json_atomic(req_path, req)
            prog.note_unit(0, int((config.get("train") or {}).get("max_epochs", 0)) or None,
                           "epochs") if (config.get("train") or {}).get("max_epochs") else None
            proc = _spawn([interpreter,"-X","utf8","-m","engine.style.m2_adapter.runner",str(req_path)], PROJ, config.get("runtime_binding"))
            exit_code, last_receipt, failure = _pump(proc, run, prog, deadline, req.get('trainer_resume_binding'))
            proc = None
            if failure is None and exit_code == 0:
                step_receipt = runstore.read_json(base_dir / "m2-receipt.json")
                if step_receipt is None or step_receipt.get("ok") is not True:
                    failure = {"code": "E_RECEIPT_ABSENT",
                               "detail": "m2 runner exit 0 without a receipt"}
                else:
                    last_receipt = step_receipt
                    artifact_files = [
                        {"relative_path": f["relative_path"], "sha256": f["sha256"],
                         "bytes": f.get("bytes")}
                        for f in step_receipt.get("output", {}).get("files", [])
                    ]

        else:  # M1_FROZEN: one real generation per locked sample
            target_char = config.get("target_char")
            seed = config.get("seed")
            if not isinstance(target_char, str) or len(target_char) != 1:
                return fail_now("E_INPUT_CHAR", "m1 mode requires config.target_char (exactly 1 char)")
            if not isinstance(seed, int) or isinstance(seed, bool):
                return fail_now("E_INPUT_SEED", "m1 mode requires an integer config.seed")
            for index, (sample_id, ref) in enumerate(samples):
                if _run_status_in_db(run_id) == "cancelled":
                    failure = {"code": "CANCELLED", "detail": "cancel observed"}
                    break
                out_png = base_dir / "artifact" / f"m1_{sample_id}.png"
                out_receipt = base_dir / "receipts" / f"m1_{sample_id}.json"
                req = {
                    "style_ref": str(ref),
                    "target_char": target_char,
                    "seed": seed,
                    "out_png": str(out_png),
                    "out_receipt": str(out_receipt),
                }
                req_path = base_dir / f"request_{sample_id}.json"
                runstore.write_json_atomic(req_path, req)
                prog.note_unit(index, len(samples), "chars")
                prog.flush(force=True)
                proc = _spawn(argv_builder(req_path), PROJ)
                exit_code, _stdout_receipt, failure = _pump(proc, run, prog, deadline)
                proc = None
                if failure is not None or exit_code != 0:
                    step_receipt = runstore.read_json(out_receipt)
                    if step_receipt is not None and step_receipt.get("failure"):
                        failure = step_receipt["failure"]  # runner's own failure code
                    elif failure is None:
                        failure = {"code": "E_ENGINE", "detail": f"runner exit {exit_code}"}
                    break
                step_receipt = runstore.read_json(out_receipt)
                if step_receipt is None or step_receipt.get("ok") is not True:
                    failure = {"code": "E_RECEIPT_ABSENT",
                               "detail": f"m1 runner exit 0 without a receipt (sample {sample_id})"}
                    break
                if not out_png.is_file() or runstore.sha256_file(out_png) != step_receipt.get("output", {}).get("png_sha256"):
                    failure = {"code": "E_OUTPUT_INVALID",
                               "detail": f"m1 receipt/png mismatch (sample {sample_id})"}
                    break
                steps.append({"sample_id": sample_id, "exit_code": 0,
                              "receipt_relative_path": out_receipt.relative_to(base_dir).as_posix(),
                              "receipt_sha256": runstore.sha256_file(out_receipt)})
                artifact_files.append({
                    "relative_path": out_png.relative_to(base_dir).as_posix(),
                    "sha256": runstore.sha256_file(out_png),
                    "bytes": out_png.stat().st_size,
                })
                last_receipt = step_receipt
                prog.note_unit(index + 1, len(samples), "chars")
                prog.flush(force=True)

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

        # ---- terminal mapping (exit codes 0/2/3 + receipt/output truth) ----
        if failure is None and exit_code == 3:
            failure = {"code": "E_INPUT", "detail": "engine rejected the input (exit 3)"}
        elif failure is None and exit_code == 2:
            failure = {"code": "E_ENGINE", "detail": "engine-side failure (exit 2)"}
        elif failure is None and exit_code != 0:
            failure = {"code": "E_ENGINE_CRASH", "detail": f"engine exit {exit_code}"}
        elif failure is None and last_receipt is None:
            failure = {"code": "E_RECEIPT_ABSENT",
                       "detail": "exit 0 without a verifiable receipt - refused to fake success"}
        if failure is None:
            failure = _verify_artifact_files(base_dir, artifact_files)

        if failure is not None:
            code = failure.get("code", "E_ENGINE")
            if code == "CANCELLED":
                prog.set_stage("cancelled")
                _finalize(s, run, "cancelled", code, failure.get("detail", ""), prog)
                return "cancelled"
            return fail_now(code, failure.get("detail", ""))

        if engine_mode == ENGINE_M2:
            from .m2_topology import validate_topology_lineage
            try:validate_topology_lineage(config,last_receipt)
            except (ValueError,KeyError,TypeError) as exc:
                return fail_now('E_ADAPTER_TOPOLOGY_RECEIPT',str(exc))
            for key in ('training_recipe','training_budget','recipe_id','objective_profile','checkpoint_selection','adapter_topology_id','adapter_topology','adapter_topology_sha256','scr_objective','scr_assets','scr_data_binding','scr_cache_binding','scr_preparation_binding','windows_cache_identity'):
                if config.get(key) is not None and last_receipt.get(key)!=config[key]:
                    return fail_now('E_CONTROL_RECIPE_RECEIPT',key+' differs from immutable run config')
            if config.get('runtime_binding') is not None:
                from engine.style.m2_adapter.runtime_variants import binding_for_config,verify_execution_receipt
                try:
                    binding_for_config(PROJ,config)
                    verify_execution_receipt(config['runtime_binding'],last_receipt,'training')
                except (OSError,ValueError,KeyError) as exc:return fail_now('E_RUNTIME_BINDING',str(exc))
            from engine.style.dataset.binding import verify_m2_receipt_binding
            try:
                # Reject drift during training and a stale/misbound engine receipt.
                current_binding = verify_snapshot_binding(
                    snapshot_binding["snapshot_dir"], dataset_snapshot_sha256)
                verify_m2_receipt_binding(last_receipt, current_binding)
                from engine.style.m2_adapter.preprocessing import verify_preprocessing_contract
                try:
                    if last_receipt.get("preprocessing") is None:
                        raise ValueError("new training receipt is missing preprocessing identity")
                    measured_preprocessing = verify_preprocessing_contract(last_receipt.get("preprocessing"))
                    requested_profile = config.get("preprocessing_profile", "legacy-pil72-gray-v1")
                    if measured_preprocessing["profile"] != requested_profile:
                        raise ValueError("training receipt preprocessing differs from requested profile")
                except ValueError as exc:
                    return fail_now("E_PREPROCESSING_IDENTITY", str(exc))
                training_receipt_path = base_dir / "m2-receipt.json"
                artifact_files.append({"relative_path": "m2-receipt.json",
                                       "sha256": runstore.sha256_file(training_receipt_path),
                                       "bytes": training_receipt_path.stat().st_size})
            except DatasetError as exc:
                return fail_now(exc.code, str(exc))

        manifest_sha = _write_receipt_and_manifest(
            run, last_receipt.get("engine_mode", engine_mode.upper()),
            last_receipt.get("engine_id", ""),
            last_receipt.get("model_version") or last_receipt.get("engine_version") or "",
            _base_ckpt_binding(last_receipt),
            steps, dataset_snapshot_sha256, config_sha256, artifact_files, snapshot_binding,
            last_receipt if engine_mode == ENGINE_M2 else None)
        prog.set_stage("finalizing")
        _finalize(s, run, "succeeded", "OK",
                  f"artifact manifest sha256={manifest_sha[:16]}…", prog)
        return run.status
    except Exception as exc:  # noqa: BLE001 - containment is the point
        try:
            s.rollback()
            run = s.get(StyleRun, run_id)
            if run is not None and run.status == "running":
                _finalize(s,run,'failed','WORKER_ERROR',str(exc),None)
        except Exception:  # noqa: BLE001 - never let the worker die
            s.rollback()
        return "failed"
    finally:
        if claimed and proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired): pass
        ended = proc is None or proc.poll() is not None
        if gpu_locked and ended:
            gpu_lock_release()
        try:
            if scr_gpu_lease is not None and ended:
                from engine.style.m2_adapter.windows_gpu_support import release_gpu_lease
                release_gpu_lease(scr_gpu_lease)
            if cpu_lease is not None and ended:
                from engine.style.m2_adapter.windows_training_runtime import release_cpu_lease
                release_cpu_lease(cpu_lease)
            if claimed and ended:
                from .run_recovery import record_execution
                s.rollback()
                if s.get(StyleRun,run_id) is not None:
                    record_execution(s,run_id,phase='ended'); s.commit()
        except (ValueError, OSError) as cleanup_error:
            # Preserve changed leases. This owned attempt stays unresolved until
            # its exact process/lease identities can be independently checked.
            s.rollback()
            if claimed:
                import uuid
                runstore.write_json_atomic(base_dir/f'cleanup-failure-{uuid.uuid4().hex}.json',
                    {'code':'EXECUTION_CLEANUP_UNRESOLVED','detail':str(cleanup_error)})
        finally:
            s.close()


# ---------------------------------------------------------------------------
# resident worker (mirrors app.domains.render.service.start_worker)
# ---------------------------------------------------------------------------

def _worker_loop() -> None:
    while not _stop_event.is_set():
        s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
        try:
            run_id = _next_queued_run_id(s)
        except Exception:  # noqa: BLE001
            run_id = None
        finally:
            s.close()
        if run_id is not None:
            execute_run(run_id)
        else:
            _stop_event.wait(POLL_SECONDS * 5)


def start_worker() -> threading.Thread | None:
    """create_app() wiring: one resident daemon worker per process
    (YY_STYLE_WORKER=0 disables it - used by probes that drive execute_run
    directly). Restart recovery runs synchronously before the thread starts."""
    global _worker_thread
    if os.environ.get("YY_STYLE_WORKER", "1") == "0":
        return None
    if _worker_thread is not None and _worker_thread.is_alive():
        return _worker_thread
    _stop_event.clear()
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        recover_interrupted(s)
    finally:
        s.close()
    _worker_thread = threading.Thread(target=_worker_loop, name="yy-style-worker", daemon=True)
    _worker_thread.start()
    return _worker_thread


def stop_worker(timeout: float = 5.0) -> None:
    """Probe/test hook: stop the resident loop (the daemon thread dies with
    the process anyway; this only makes tests deterministic)."""
    _stop_event.set()
    global _worker_thread
    thread = _worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _worker_thread = None


def queue_status() -> dict:
    """Read-only telemetry (same shape family as render queue_status)."""
    s = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)()
    try:
        queued = s.query(StyleRun).filter(StyleRun.status == "queued").count()
        running = s.query(StyleRun).filter(StyleRun.status == "running").count()
    finally:
        s.close()
    return {
        "worker_id": "style-worker-01",
        "alive": bool(_worker_thread is not None and _worker_thread.is_alive()),
        "queue_depth": queued,
        "running": running,
        "engine_default": _env_engine_default(),
    }
