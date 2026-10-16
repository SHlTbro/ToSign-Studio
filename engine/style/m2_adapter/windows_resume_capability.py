"""Independent safe-byte full Trainer resume verification; never execution authority.

No Torch imports, secret reads, pickle loading, registration or process mutation.
The server supplies a freshly built trusted context and its external monitor record.
"""
from __future__ import annotations
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import zipfile
from .trainer_resume_protocol import (canonical, digest, native_path, file_sha256,
    validate_binding, verify_manifest, StatefulBatchSampler, TRAINER_IDENTITY_FILES, valid_trial_nonce)

SCHEMA = "windows-full-trainer-resume-proof/1"
CONTEXT_SCHEMA = "windows-full-trainer-resume-context/1"
STATE_SCHEMA = "windows-full-trainer-resume-state/1"
PRODUCER = "engine/style/m2_adapter/windows_resume_probe.py"
EXECUTOR = "engine/style/m2_adapter/windows_resume_executor.py"
VERIFIER = "engine/style/m2_adapter/windows_resume_capability.py"
CPU = "windows-cp312-cu128-user-scr2n-spatial-cpu-v1"
MAX_OUTPUT = 3 * 2**30
MAX_TENSOR = 512 * 2**20
POINTS = ("initial0", "continuous2", "continuous4", "cancelled2", "restored2", "resumed4")
POINT_STEP = dict(zip(POINTS, (0, 2, 4, 2, 2, 4)))
BRANCHES = ("continuous", "cancelled", "resumed")
SOURCE_FILES = (PRODUCER, EXECUTOR, VERIFIER,
    "engine/style/m2_adapter/trainer_resume_protocol.py",
    "engine/style/trainer/__init__.py", "engine/style/trainer/config.py",
    "engine/style/trainer/trainer.py", "engine/style/trainer/receipt.py",
    "engine/style/m2_adapter/runner.py", "engine/style/m2_adapter/adapter.py",
    "engine/style/m2_adapter/user_scr_objective.py", "engine/style/m2_adapter/user_scr_cache.py",
    "engine/style/m2_adapter/windows_training_runtime.py",
    "engine/style/m2_adapter/windows_training_recipe.py",
    "engine/style/m2_adapter/personal_runtime_policy.py",
    "engine/style/m2_adapter/deterministic_ops.py",
    "engine/style/m2_adapter/native_training_session.py",
    "engine/style/m2_adapter/windows_resume_recipe.py",
    "engine/style/m2_adapter/windows_resume_runtime.py",
    "scripts/windows_scr_resume.py",
    "app/domains/styles/run_recovery.py",
    "app/domains/styles/worker.py")

class ResumeEvidenceError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"[{code}] {detail}")

def _need(condition, code, detail):
    if not condition:
        raise ResumeEvidenceError(code, detail)

def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

def _same(left, right):
    return canonical(left) == canonical(right)

def _finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0 and (not positive or value > 0)

def _regular(path, *, output=False):
    path = Path(path).absolute()
    _need(path.drive.upper() == "E:", "E_RESUME_PATH", "consumer materials must be on E")
    for part in (path, *path.parents):
        actual = native_path(part)
        _need(not actual.is_symlink() and not getattr(actual, "is_junction", lambda: False)(),
              "E_RESUME_PATH", "linked evidence is forbidden")
        if output and actual.is_dir():
            _need(not native_path(part / "FROZEN").exists(), "E_RESUME_PATH", "trial output inside frozen data")
    _need(native_path(path).is_file(), "E_RESUME_FILE", "regular evidence file required")
    return path

def _child(root, relative):
    _need(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
          "E_RESUME_PATH", "portable relative output path required")
    rel = Path(relative)
    _need(not rel.is_absolute() and not rel.drive and all(x not in (".", "..") for x in rel.parts),
          "E_RESUME_PATH", "output path escape")
    path = Path(root).absolute() / rel
    _need(path.is_relative_to(Path(root).absolute()), "E_RESUME_PATH", "output path escape")
    return _regular(path, output=True)

def _record(root, row):
    _need(isinstance(row, dict) and set(row) == {"path", "sha256", "bytes"}
          and _hash(row["sha256"]) and type(row["bytes"]) is int and 0 < row["bytes"] <= MAX_OUTPUT,
          "E_RESUME_FILE", "complete bounded file record required")
    path = _child(root, row["path"])
    _need(native_path(path).stat().st_size == row["bytes"] and file_sha256(path) == row["sha256"],
          "E_RESUME_FILE", "record does not match actual bytes")
    return path

def _output_bytes(root):
    """Count every actual trial file, including checkpoints and unused exports."""
    root = Path(root).absolute()
    _need(root.drive.upper() == "E:" and native_path(root).is_dir(), "E_RESUME_PATH", "owned E output required")
    total = 0
    for folder, directories, files in os.walk(native_path(root), followlinks=False):
        folder = Path(str(folder)[4:] if str(folder).startswith("\\\\?\\") else str(folder))
        for directory in directories:
            path = native_path(folder / directory)
            _need(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(),
                  "E_RESUME_PATH", "linked output directory forbidden")
        for name in files:
            path = _regular(folder / name, output=True)
            total += native_path(path).stat().st_size
            _need(total <= MAX_OUTPUT, "E_RESUME_RESOURCE", "whole actual output exceeds bounded trial budget")
    return total

def _json(path):
    _need(native_path(path).stat().st_size <= 16 * 2**20, "E_RESUME_JSON", "oversized primitive JSON")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, "E_RESUME_JSON", "duplicate JSON field")
            result[key] = value
        return result
    try:
        result = json.loads(native_path(path).read_bytes(), object_pairs_hook=unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        canonical(result)
        return result
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ResumeEvidenceError):
            raise
        raise ResumeEvidenceError("E_RESUME_JSON", "invalid finite primitive JSON") from exc

def _numpy():
    import numpy as np
    return np

def read_npz_spec(path, expected=None):
    """Parse bounded NPY headers before allocating arrays. Never extract or pickle."""
    np = _numpy()
    result = {}
    total = 0
    try:
        with zipfile.ZipFile(native_path(_regular(path))) as archive:
            for row in archive.infolist():
                key = row.filename[:-4] if row.filename.endswith(".npy") else ""
                _need(key and key not in result and not any(c in key for c in ("/", "\\"))
                      and not row.is_dir() and row.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                      and not row.flag_bits & 1 and stat.S_IFMT(row.external_attr >> 16) != stat.S_IFLNK,
                      "E_RESUME_ARRAY", "unsafe or duplicate NPZ entry")
                total += row.file_size
                _need(total <= MAX_OUTPUT and row.file_size <= MAX_TENSOR + 16384,
                      "E_RESUME_ARRAY", "uncompressed NPZ budget exceeded")
                with archive.open(row) as stream:
                    version = np.lib.format.read_magic(stream)
                    _need(version in ((1, 0), (2, 0)), "E_RESUME_ARRAY", "unsupported NPY format")
                    reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                    shape, fortran, dtype = reader(stream, max_header_size=16384)
                    _need(not fortran and not dtype.hasobject and dtype.fields is None and dtype.subdtype is None
                          and dtype.kind in "fiub" and dtype.byteorder != ">"
                          and all(type(x) is int and x >= 0 for x in shape), "E_RESUME_ARRAY", "safe primitive C-order arrays required")
                    size = math.prod(shape) * dtype.itemsize
                    _need(size <= MAX_TENSOR and row.file_size == stream.tell() + size,
                          "E_RESUME_ARRAY", "NPY header/actual size differs")
                    result[key] = {"shape": list(shape), "dtype": str(dtype), "bytes": size}
        _need(result, "E_RESUME_ARRAY", "empty NPZ is not state evidence")
        if expected is not None:
            _need(_same(result, expected), "E_RESUME_ARRAY", "exact array keys/dtypes/shapes required")
        return result, total
    except (OSError, ValueError, zipfile.BadZipFile, EOFError) as exc:
        if isinstance(exc, ResumeEvidenceError):
            raise
        raise ResumeEvidenceError("E_RESUME_ARRAY", "invalid numeric NPZ") from exc

def _array(path, key):
    np = _numpy()
    try:
        with np.load(native_path(path), allow_pickle=False) as archive:
            array = archive[key]
            _need(not array.dtype.hasobject and array.flags.c_contiguous and bool(np.isfinite(array).all()),
                  "E_RESUME_ARRAY", "finite C-order actual numeric arrays required")
            return array
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ResumeEvidenceError):
            raise
        raise ResumeEvidenceError("E_RESUME_ARRAY", "invalid actual numeric array") from exc

def arrays_sha(arrays):
    """Public numeric observation digest for producer and independent reader."""
    np = _numpy()
    _need(isinstance(arrays, dict) and arrays, "E_RESUME_ARRAY", "nonempty numeric observation required")
    h = hashlib.sha256()
    for key in sorted(arrays):
        value = np.asarray(arrays[key])
        _need(isinstance(key, str) and not value.dtype.hasobject and value.dtype.fields is None
              and value.dtype.subdtype is None and value.dtype.kind in "fiub" and value.dtype.byteorder != ">"
              and value.flags.c_contiguous and bool(np.isfinite(value).all()),
              "E_RESUME_ARRAY", "finite primitive C-order observation required")
        h.update(canonical({"key": key, "dtype": str(value.dtype), "shape": list(value.shape)}))
        h.update(memoryview(value.reshape(-1).view("uint8")))
    return h.hexdigest()

def compare_npz_bytes(left, right, expected=None):
    left_spec, _ = read_npz_spec(left, expected)
    read_npz_spec(right, left_spec)
    for key in sorted(left_spec):
        a, b = _array(left, key), _array(right, key)
        _need(memoryview(a.reshape(-1).view("uint8")) == memoryview(b.reshape(-1).view("uint8")),
              "E_RESUME_STATE_DIFFERENCE", "actual raw numeric state differs: " + key)
    return digest(left_spec)

def _trainer_identity(app):
    tree = ast.parse(native_path(Path(app) / "engine/style/trainer/config.py").read_text(encoding="utf-8"))
    version = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "TRAINER_VERSION" for target in node.targets):
            version = ast.literal_eval(node.value)
    _need(isinstance(version, str), "E_RESUME_SOURCE", "actual trainer version required")
    h = hashlib.sha256()
    for name in sorted(TRAINER_IDENTITY_FILES):
        h.update(name.encode())
        h.update(file_sha256(_regular(Path(app) / "engine/style/trainer" / name)).encode())
    return version, h.hexdigest()

def _source_closure(app):
    """Fresh complete Python source inventory, including parent action gates."""
    app = Path(app).absolute()
    result = {}
    for relative in ("engine", "app/domains/styles"):
        for path in native_path(app / relative).rglob("*.py"):
            # Strip the native long-path prefix without resolving symlinks.
            regular = Path(str(path)[4:] if str(path).startswith("\\\\?\\") else str(path))
            name = regular.relative_to(app).as_posix()
            result[name] = file_sha256(_regular(regular))
    _need(result, "E_RESUME_SOURCE", "complete current source closure required")
    return dict(sorted(result.items()))

def build_context(app, *, trial_nonce, resume_binding, profile_sha256, python, primary_registration_id,
                  snapshot, base_state_files, environment_identity_sha256, expected_trainer_config_hash):
    """Build from server selected inputs; this descriptive context grants no action."""
    validate_binding(resume_binding)
    _need(valid_trial_nonce(trial_nonce),
          "E_RESUME_NONCE", "fresh server launch nonce required")
    _need(isinstance(primary_registration_id, str) and len(primary_registration_id) == 32
          and all(c in "0123456789abcdef" for c in primary_registration_id), "E_RESUME_RUNTIME", "primary qualification ID required")
    for value in (profile_sha256, environment_identity_sha256, expected_trainer_config_hash):
        _need(_hash(value), "E_RESUME_BINDING", "complete independent profile/environment/config digest required")
    runtime = resume_binding["runtime_binding"]
    from .runtime_variants import validate_binding_shape
    validate_binding_shape(runtime)
    _need(runtime.get("device") == "cpu" and runtime.get("variant_id") == CPU
          and runtime.get("registration_id") == primary_registration_id
          and runtime.get("execution_profile_sha256") == profile_sha256
          and runtime.get("environment_identity_sha256") == environment_identity_sha256,
          "E_RESUME_RUNTIME", "exact independent Windows CPU runtime required")
    source_hashes = {name: file_sha256(_regular(Path(app) / name)) for name in SOURCE_FILES}
    version, code = _trainer_identity(app)
    _need(resume_binding["trainer_version"] == version and resume_binding["trainer_code_sha256"] == code,
          "E_RESUME_SOURCE", "actual trainer identity differs from owned binding")
    from engine.style.dataset.binding import verify_snapshot_binding
    snapshot_binding = verify_snapshot_binding(snapshot, resume_binding["snapshot_sha256"])
    split = _json(_regular(Path(snapshot) / "split.json"))["assignments"]
    rows = [json.loads(line) for line in native_path(_regular(Path(snapshot) / "samples.jsonl")).read_text(encoding="utf-8").splitlines() if line.strip()]
    train_rows = []
    for row in rows:
        if split[row["sample_id"]] == "train":
            train_rows.append({"sample_id": row["sample_id"], "label": row["label"],
                "png_sha256": file_sha256(_regular(Path(snapshot) / row["snapshot_file"]))})
    _need(len(train_rows) >= 10, "E_RESUME_SNAPSHOT", "four two-row steps must remain mid-epoch")
    _need(isinstance(base_state_files, dict) and set(base_state_files) == {"base.unet", "base.style_encoder", "base.content_encoder"},
          "E_RESUME_BASE", "three independent original numeric base archives required")
    original = {}
    frozen_spec = {}
    for prefix, path in sorted(base_state_files.items()):
        path = _regular(path)
        spec, _ = read_npz_spec(path)
        original[prefix] = {"path": str(path), "sha256": file_sha256(path), "spec": spec}
        frozen_spec.update({prefix + "." + key: value for key, value in spec.items()})
    from .windows_training_capability import expected_adapter_shapes
    adapter_spec = {"adapter." + key: {"shape": shape, "dtype": "float32", "bytes": math.prod(shape) * 4}
                    for key, shape in expected_adapter_shapes().items()}
    context = {"schema": CONTEXT_SCHEMA, "trial_nonce": trial_nonce, "resume_binding": resume_binding,
        "binding_sha256": digest(resume_binding), "profile_sha256": profile_sha256,
        "environment_identity_sha256": environment_identity_sha256, "primary_registration_id": primary_registration_id,
        "python_sha256": file_sha256(_regular(python)), "python_path": str(_regular(python)),
        "source_hashes": source_hashes, "source_closure": _source_closure(app), "snapshot_path": str(Path(snapshot).absolute()),
        "snapshot_binding": snapshot_binding, "train_rows": train_rows,
        "trainer_version": version, "trainer_code_sha256": code, "trainer_config_hash": expected_trainer_config_hash,
        "checkpoint_schema": "m2-trainer-checkpoint/3", "adapter_spec": adapter_spec, "frozen_spec": frozen_spec,
        "base_state_files": original, "train_protocol": {"schema": "windows-resume-four-step-trial/1",
            "continuous_steps": 4, "cancel_boundary": 2, "restored_step": 2, "resumed_terminal_step": 4,
            "batch_size": 2, "drop_last": True, "num_workers": 0, "dataset_size": len(train_rows),
            "dtype": "float32", "amp": False, "device": "cpu", "threads": 2, "interop_threads": 1},
        "resource_limits": {"max_new_output_bytes": MAX_OUTPUT, "max_seconds": 900,
            "stop_bytes": int(4.5 * 2**30), "absolute_budget_bytes": 5 * 2**30, "minimum_free_E_bytes": 4 * 2**30}}
    context["context_sha256"] = digest(context)
    return context

def _state(root, point, records, context):
    _need(isinstance(records, dict) and set(records) == {"state_json", "state_npz", "frozen_npz"},
          "E_RESUME_EXPORT", "three actual safe exports required at each observation")
    paths = {name: _record(root, record) for name, record in records.items()}
    body = _json(paths["state_json"])
    _need(isinstance(body, dict) and set(body) == {"schema", "trial_nonce", "binding_sha256", "point", "checkpoint_metadata", "rng", "loader_state", "training_state", "step_trace"}
          and body["schema"] == STATE_SCHEMA and body["trial_nonce"] == context["trial_nonce"]
          and body["binding_sha256"] == context["binding_sha256"] and body["point"] == point,
          "E_RESUME_EXPORT", "fresh exact-source point identity required")
    step = POINT_STEP[point]
    meta = body["checkpoint_metadata"]
    _need(isinstance(meta, dict) and set(meta) == {"checkpoint_schema", "trainer_version", "code_version", "config_hash", "run_id", "data_snapshot_sha256", "resume_binding", "frozen_model_sha256", "model_structure", "epoch", "global_step", "batch_ptr"},
          "E_RESUME_CHECKPOINT", "complete actual checkpoint metadata required")
    expected = {"checkpoint_schema": context["checkpoint_schema"], "trainer_version": context["trainer_version"],
        "code_version": context["trainer_code_sha256"], "config_hash": context["trainer_config_hash"],
        "run_id": context["resume_binding"]["run_id"], "data_snapshot_sha256": context["resume_binding"]["snapshot_sha256"],
        "resume_binding": context["resume_binding"], "epoch": 0, "global_step": step,
        "batch_ptr": {"epoch": 1, "batch_idx": step}}
    _need(all(_same(meta.get(key), value) for key, value in expected.items()),
          "E_RESUME_CHECKPOINT", "checkpoint version/config/source/run/snapshot/committed position differs")
    spec, uncompressed = read_npz_spec(paths["state_npz"])
    wanted = {"model::" + name: row for name, row in context["adapter_spec"].items()}
    for key in ("rng::torch_cpu", "rng::shuffle", "rng::iterator"):
        row = spec.get(key, {})
        _need(row.get("dtype") == "uint8" and isinstance(row.get("shape"), list) and len(row["shape"]) == 1
              and 16 <= row["shape"][0] <= 65536, "E_RESUME_RNG", "actual bounded CPU generator byte states required")
        wanted[key] = row
    for name, row in context["adapter_spec"].items():
        if step:
            wanted["adam_exp_avg::" + name] = row
            wanted["adam_exp_avg_sq::" + name] = row
            entry = spec.get("adam_step::" + name, {})
            _need(entry.get("shape") == [] and entry.get("dtype") in ("float32", "float64", "uint64", "int64"),
                  "E_RESUME_OPTIMIZER", "actual scalar AdamW step required")
            wanted["adam_step::" + name] = entry
    _need(_same(spec, wanted), "E_RESUME_ARRAY", "exact adapter/AdamW/RNG export keys required")
    for key in wanted:
        array = _array(paths["state_npz"], key)
        if key.startswith("adam_step::"):
            _need(float(array) == step, "E_RESUME_OPTIMIZER", "AdamW actual step differs")
        if not step and key.startswith("model::"):
            _need(bool((_numpy().asarray(array) == 0).all()), "E_RESUME_INITIAL", "fresh adapter must be zero initialized")
    frozen_spec, frozen_bytes = read_npz_spec(paths["frozen_npz"], context["frozen_spec"])
    torch_types = {"float32": "torch.float32", "float64": "torch.float64", "int64": "torch.int64", "int32": "torch.int32", "uint8": "torch.uint8", "bool": "torch.bool"}
    _need(all(row["dtype"] in torch_types for row in {**context["adapter_spec"], **frozen_spec}.values()),
          "E_RESUME_MODEL", "unsupported numeric model tensor dtype")
    structure = {name: {"shape": row["shape"], "dtype": torch_types[row["dtype"]],
        "layout": "torch.strided", "numel": math.prod(row["shape"])}
        for name, row in {**context["adapter_spec"], **frozen_spec}.items()}
    _need(_same(meta["model_structure"], structure), "E_RESUME_MODEL", "complete actual model structure differs")
    frozen_hash = hashlib.sha256()
    for name in sorted(frozen_spec):
        array = _array(paths["frozen_npz"], name)
        metadata = {"name": name, "dtype": torch_types[frozen_spec[name]["dtype"]], "shape": frozen_spec[name]["shape"], "layout": "torch.strided"}
        frozen_hash.update(canonical(metadata));frozen_hash.update(memoryview(array.reshape(-1).view("uint8")))
    _need(meta["frozen_model_sha256"] == frozen_hash.hexdigest(), "E_RESUME_FROZEN_MODEL", "reported frozen hash differs from actual numeric bytes")
    rng = body["rng"]
    _need(isinstance(rng, dict) and set(rng) == {"schema", "python", "numpy"}
          and rng["schema"] == "global-rng-primitives/1" and isinstance(rng["python"], list)
          and isinstance(rng["numpy"], list) and len(rng["numpy"]) == 5,
          "E_RESUME_RNG", "global RNG primitive states required")
    py = rng["python"];nr = rng["numpy"]
    _need(len(py) == 3 and type(py[0]) is int and py[0] == 3 and isinstance(py[1], list)
          and len(py[1]) == 625 and all(type(value) is int and 0 <= value <= 2**32-1 for value in py[1][:-1])
          and type(py[1][-1]) is int and 0 <= py[1][-1] <= 624
          and (py[2] is None or type(py[2]) is float and math.isfinite(py[2]))
          and nr[0] == "MT19937" and isinstance(nr[1], list) and len(nr[1]) == 624
          and all(type(value) is int and 0 <= value <= 2**32-1 for value in nr[1])
          and type(nr[2]) is int and 0 <= nr[2] <= 624 and type(nr[3]) is int and nr[3] in (0,1)
          and type(nr[4]) in (int,float) and math.isfinite(nr[4]), "E_RESUME_RNG", "actual complete global RNG layout required")
    loader = body["loader_state"]
    _need(isinstance(loader, dict) and {"schema", "sampler", "dataset_rng", "train_dataset", "val_dataset", "scr_counters", "loss_diagnostics", "resume_trial_step_trace"} == set(loader) and loader["schema"] == "m2-private-loader-state/1",
          "E_RESUME_LOADER", "all private loader and cumulative evidence required")
    sampler = StatefulBatchSampler(len(context["train_rows"]), 2, drop_last=True, num_workers=0)
    sampler.load_state_dict(loader["sampler"])
    _need(sampler.epoch == 1 and sampler.next_batch == step, "E_RESUME_LOADER", "sampler cursor differs from committed optimizer step")
    private_rng = loader["dataset_rng"]
    _need(isinstance(private_rng, dict) and set(private_rng) == {"bit_generator", "state", "has_uint32", "uinteger"}
          and private_rng["bit_generator"] == "PCG64" and isinstance(private_rng["state"], dict)
          and set(private_rng["state"]) == {"state", "inc"}
          and all(type(value) is int and 0 <= value < 2**128 for value in private_rng["state"].values())
          and private_rng["state"]["inc"] % 2 == 1 and type(private_rng["has_uint32"]) is int
          and private_rng["has_uint32"] in (0, 1) and type(private_rng["uinteger"]) is int
          and 0 <= private_rng["uinteger"] < 2**32, "E_RESUME_RNG", "complete private dataset PCG64 state required")
    traces = body["step_trace"]
    _need(isinstance(traces, list) and len(traces) == step, "E_RESUME_STEPS", "complete cumulative actual optimizer trace required")
    for index, row in enumerate(traces, 1):
        _need(isinstance(row, dict) and set(row) == {"step", "sample_ids", "noise_sha256", "timesteps", "loss", "adapter_sha256", "optimizer_sha256"}
              and type(row["step"]) is int and row["step"] == index and _finite(row["loss"]),
              "E_RESUME_STEPS", "exact ordered finite update trace required")
        expected_ids = [context["train_rows"][value]["sample_id"] for value in sampler.permutation[2*(index-1):2*index]]
        _need(_same(row["sample_ids"], expected_ids) and isinstance(row["noise_sha256"], list) and len(row["noise_sha256"]) == 2
              and all(_hash(value) for value in row["noise_sha256"]) and isinstance(row["timesteps"], list) and len(row["timesteps"]) == 2
              and all(type(value) is int and value >= 0 for value in row["timesteps"])
              and _hash(row["adapter_sha256"]) and _hash(row["optimizer_sha256"]),
              "E_RESUME_STEPS", "trace sample order/noise/timestep/state digest differs")
    training = body["training_state"]
    _need(isinstance(training, dict) and set(training) == {"epoch_losses", "best_val", "best_epoch", "gradient_diagnostics", "epochs_log", "scaler_state"}
          and training["scaler_state"] is None and training["epochs_log"] == [] and training["best_val"] is None
          and type(training["best_epoch"]) is int and training["best_epoch"] == -1,
          "E_RESUME_HISTORY", "complete actual four-step FP32 training state required")
    _need(isinstance(training["epoch_losses"], list) and len(training["epoch_losses"]) == step
          and all(_finite(value) for value in training["epoch_losses"])
          and isinstance(training["gradient_diagnostics"], list) and len(training["gradient_diagnostics"]) == step
          and isinstance(loader["loss_diagnostics"], list) and len(loader["loss_diagnostics"]) == step
          and _same(loader["resume_trial_step_trace"], traces),
          "E_RESUME_HISTORY", "cumulative live loss/gradient/step evidence required")
    for index, row in enumerate(training["gradient_diagnostics"], 1):
        _need(isinstance(row, dict) and set(row) == {"step", "unclipped_norm", "clipped"}
              and type(row["step"]) is int and row["step"] == index and _finite(row["unclipped_norm"], True)
              and type(row["clipped"]) is bool, "E_RESUME_HISTORY", "actual ordered finite gradients required")
    for split_name, expected_seen in (("train_dataset", 2 + step * 2), ("val_dataset", 0)):
        counters = loader[split_name]
        _need(isinstance(counters, dict) and {"conditioning_seen", "conditioning_dropped", "access_trace"} == set(counters)
              and type(counters["conditioning_seen"]) is int and counters["conditioning_seen"] == expected_seen
              and type(counters["conditioning_dropped"]) is int and 0 <= counters["conditioning_dropped"] <= counters["conditioning_seen"]
              and isinstance(counters["access_trace"], list) and len(counters["access_trace"]) == counters["conditioning_seen"],
              "E_RESUME_HISTORY", "actual dataset access evidence required")
    access = loader["train_dataset"]["access_trace"]
    indices = [0, 1] + sampler.permutation[:step * 2]
    for position, (row, dataset_index) in enumerate(zip(access, indices)):
        _need(isinstance(row, dict) and set(row) == {"sample_id", "style_sample_id", "t", "noise_sha256", "dropped"}
              and row["sample_id"] == context["train_rows"][dataset_index]["sample_id"]
              and row["style_sample_id"] == context["train_rows"][(dataset_index + 1) % len(context["train_rows"])]["sample_id"]
              and type(row["t"]) is int and row["t"] >= 0 and _hash(row["noise_sha256"])
              and type(row["dropped"]) is bool, "E_RESUME_HISTORY", "warmup/committed actual dataset access order differs")
        if position >= 2:
            trace = traces[(position - 2) // 2]; column = (position - 2) % 2
            _need(row["t"] == trace["timesteps"][column] and row["noise_sha256"] == trace["noise_sha256"][column],
                  "E_RESUME_STEPS", "actual dataset target-noise/timestep differs from step trace")
    _need(sum(row["dropped"] for row in access) == loader["train_dataset"]["conditioning_dropped"],
          "E_RESUME_HISTORY", "actual conditioning dropout count differs")
    scr = loader["scr_counters"]
    _need(isinstance(scr, dict) and set(scr) == {"_calls", "_lookups", "_target_hash_checks", "_prediction_feature_forwards", "_prediction_images"}
          and _same(scr["_calls"], {"train": step, "val": 0}) and _same(scr["_lookups"], {"train": 2*step, "val": 0})
          and type(scr["_target_hash_checks"]) is int and scr["_target_hash_checks"] == 2*step
          and type(scr["_prediction_feature_forwards"]) is int and scr["_prediction_feature_forwards"] == step
          and type(scr["_prediction_images"]) is int and scr["_prediction_images"] == 2*step,
          "E_RESUME_HISTORY", "actual cumulative SCR forwards/lookups/target checks required")
    for index, diagnostic in enumerate(loader["loss_diagnostics"]):
        _need(isinstance(diagnostic, dict) and diagnostic.get("phase") == "train"
              and all(_finite(diagnostic.get(key)) for key in ("epsilon_mse", "offset_term", "scr_raw", "scr_term", "total_loss"))
              and math.isclose(diagnostic["scr_term"], .01*diagnostic["scr_raw"], rel_tol=1e-6, abs_tol=1e-8)
              and math.isclose(diagnostic["total_loss"], diagnostic["epsilon_mse"] + diagnostic["offset_term"] + diagnostic["scr_term"], rel_tol=1e-5, abs_tol=1e-7)
              and diagnostic.get("VGG_used") is False and _same(diagnostic.get("timesteps"), traces[index]["timesteps"])
              and _same(diagnostic["total_loss"], training["epoch_losses"][index]) and _same(diagnostic["total_loss"], traces[index]["loss"]),
              "E_RESUME_HISTORY", "actual SCR objective/trace/epoch loss evidence differs")
    if step:
        model_arrays = {key: _array(paths["state_npz"], key) for key in spec if key.startswith("model::")}
        adam_arrays = {key: _array(paths["state_npz"], key) for key in spec if key.startswith("adam_")}
        _need(traces[-1]["adapter_sha256"] == arrays_sha(model_arrays)
              and traces[-1]["optimizer_sha256"] == arrays_sha(adam_arrays),
              "E_RESUME_STEPS", "terminal trace hashes differ from independently read actual arrays")
    return {"body": body, "paths": paths, "spec": spec, "uncompressed": uncompressed + frozen_bytes}

def _resources(receipt, branches, context):
    _need(isinstance(receipt, dict) and receipt.get("schema") == "windows-trainer-resume-resource/1"
          and receipt.get("returncode") == 0 and type(receipt.get("returncode")) is int
          and receipt.get("stop_reason") is None and receipt.get("monitor_failure") is None
          and receipt.get("cleanup_failures") == [], "E_RESUME_RESOURCE", "external successful owned monitor required")
    expected = {**context["resource_limits"], "GPU_used": False, "existing_processes_stopped": False,
        "existing_tmp_data_deleted": False, "only_identity_verified_owned_processes_terminated": True, "retry_count": 0}
    _need(all(_same(receipt.get(key), value) for key, value in expected.items()), "E_RESUME_RESOURCE", "resource/process boundaries differ")
    peak = receipt.get("peak", {})
    _need(all(type(peak.get(key)) is int and peak[key] >= 0 for key in ("tree_rss_bytes", "tmp_used_bytes", "combined_bytes", "new_output_bytes"))
          and 0 < peak["tree_rss_bytes"] <= peak["combined_bytes"] == peak["tree_rss_bytes"] + peak["tmp_used_bytes"] < context["resource_limits"]["stop_bytes"]
          and peak["new_output_bytes"] <= MAX_OUTPUT and _finite(receipt.get("elapsed_seconds"), True)
          and receipt["elapsed_seconds"] <= 900 and type(receipt.get("free_E_before_bytes")) is int
          and receipt["free_E_before_bytes"] >= 4 * 2**30, "E_RESUME_RESOURCE", "actual monitored resource observations exceed trial budget")
    owned = receipt.get("owned_processes")
    _need(isinstance(owned, dict) and set(owned) == set(BRANCHES) and isinstance(branches, dict) and set(branches) == set(BRANCHES),
          "E_RESUME_PROCESS", "three distinct externally owned branch identities required")
    identities = set()
    for role in BRANCHES:
        row = branches[role];observed = owned[role]
        _need(isinstance(row, dict) and set(row) == {"pid", "create_time", "launch_nonce", "status", "optimizer_steps", "restored_step"}
              and type(row["pid"]) is int and row["pid"] > 0 and _finite(row["create_time"], True)
              and row["launch_nonce"] == context["trial_nonce"] + ":" + role,
              "E_RESUME_PROCESS", "fresh branch PID/birth/launch identity required")
        _need(isinstance(observed, dict) and all(_same(observed.get(key), row[key]) for key in ("pid", "create_time", "launch_nonce")),
              "E_RESUME_PROCESS", "branch does not match external owned process monitor")
        identities.add((row["pid"], row["create_time"]))
        expected = {"status": "CANCELLED" if role == "cancelled" else "COMPLETED",
            "optimizer_steps": 2 if role == "cancelled" else 4, "restored_step": 2 if role == "resumed" else 0}
        _need(all(_same(row.get(key), value) for key, value in expected.items()), "E_RESUME_STEPS", "actual branch outcome or step budget differs")
    _need(len(identities) == 3 and len({pid for pid, birth in identities}) == 3,
          "E_RESUME_PROCESS", "repeat process cannot substitute for a real restart")

def verify_exports(app, out, proof, expected_context, observed_resource):
    """Validate actual safe exports against external context. Cannot mint a capability."""
    _need(isinstance(proof, dict) and proof.get("schema") == SCHEMA and proof.get("synthetic_test_only") is False
          and proof.get("GPU_used") is False, "E_RESUME_PROOF", "fresh actual CPU proof required; fixtures cannot qualify")
    _need(isinstance(expected_context, dict) and expected_context.get("schema") == CONTEXT_SCHEMA
          and expected_context.get("context_sha256") == digest({k: v for k, v in expected_context.items() if k != "context_sha256"}),
          "E_RESUME_CONTEXT", "complete server selected context required")
    context = expected_context
    _need(proof.get("trial_nonce") == context["trial_nonce"] and proof.get("context_sha256") == context["context_sha256"],
          "E_RESUME_CONTEXT", "stale trial or context replay")
    _need(all(file_sha256(_regular(Path(app) / path)) == expected for path, expected in context["source_hashes"].items())
          and set(context["source_hashes"]) == set(SOURCE_FILES), "E_RESUME_SOURCE", "source changed after owned launch")
    _need(_same(context["source_closure"], _source_closure(app)), "E_RESUME_SOURCE", "source closure changed after owned launch")
    _need(file_sha256(_regular(context["python_path"])) == context["python_sha256"],
          "E_RESUME_RUNTIME", "selected interpreter changed after launch")
    from engine.style.dataset.binding import verify_snapshot_binding
    _need(_same(verify_snapshot_binding(context["snapshot_path"], context["resume_binding"]["snapshot_sha256"]),
                context["snapshot_binding"]), "E_RESUME_SNAPSHOT", "frozen snapshot changed after launch")
    _need(_same(proof.get("resource_receipt"), observed_resource), "E_RESUME_RESOURCE", "proof substituted its own resource report")
    _resources(observed_resource, proof.get("branches"), context)
    records = proof.get("points")
    _need(isinstance(records, dict) and set(records) == set(POINTS), "E_RESUME_EXPORT", "all six distinct observation points required")
    unique = set()
    frozen_refs = {}
    identities = set()
    size = 0
    for point in POINTS:
        for name, row in records[point].items():
            path = _record(out, row)
            identifier = os.path.normcase(str(path))
            actual_stat = native_path(path).stat()
            inode = (actual_stat.st_dev, actual_stat.st_ino)
            if name == "frozen_npz":
                frozen_refs.setdefault(identifier, []).append(point)
                if identifier in unique:
                    continue
            _need(identifier not in unique and (actual_stat.st_ino == 0 or inode not in identities),
                  "E_RESUME_REPLAY", "an old physical export cannot substitute for a new observation")
            unique.add(identifier);identities.add(inode);size += row["bytes"]
    _need(len(frozen_refs) == 5 and sorted(sorted(points) for points in frozen_refs.values()) == sorted([["initial0"], ["continuous2", "continuous4"], ["cancelled2"], ["restored2"], ["resumed4"]]),
          "E_RESUME_REPLAY", "five independently written branch-before/after base observations required")
    _need(size <= MAX_OUTPUT, "E_RESUME_RESOURCE", "actual exported bytes exceed output budget")
    # The monitored parent reserves 2 MiB for its three final JSON receipts,
    # written after the heavy read. They are present on later re-verification.
    final_receipts = 0
    for name in ("RESOURCE.json", "PROOF.json", "CAPABILITY.json"):
        path = Path(out).absolute() / name
        if native_path(path).exists():
            final_receipts += native_path(_regular(path, output=True)).stat().st_size
    _need(final_receipts <= 2 * 2**20, "E_RESUME_RESOURCE", "final receipt reserve exceeded")
    _need(_output_bytes(out) - final_receipts <= observed_resource["peak"]["new_output_bytes"],
          "E_RESUME_RESOURCE", "external monitor did not cover all actual output bytes")
    states = {point: _state(out, point, records[point], context) for point in POINTS}
    uncompressed = sum(read_npz_spec(Path(identifier))[1] for identifier in unique if identifier.lower().endswith(".npz"))
    _need(uncompressed <= MAX_OUTPUT, "E_RESUME_RESOURCE", "total unique uncompressed numeric bytes exceed budget")
    for prefix, original in context["base_state_files"].items():
        path = _regular(original["path"])
        _need(file_sha256(path) == original["sha256"], "E_RESUME_BASE", "original delivered base changed")
        read_npz_spec(path, original["spec"])
        for key in original["spec"]:
            expected = _array(path, key)
            for point in POINTS:
                actual = _array(states[point]["paths"]["frozen_npz"], prefix + "." + key)
                _need(memoryview(expected.reshape(-1).view("uint8")) == memoryview(actual.reshape(-1).view("uint8")),
                      "E_RESUME_FROZEN_MODEL", "actual frozen base changed at " + point)
    for left, right in (("continuous2", "cancelled2"), ("cancelled2", "restored2"), ("continuous4", "resumed4")):
        compare_npz_bytes(states[left]["paths"]["state_npz"], states[right]["paths"]["state_npz"], states[left]["spec"])
        for key in ("rng", "loader_state", "training_state", "step_trace", "checkpoint_metadata"):
            _need(_same(states[left]["body"][key], states[right]["body"][key]),
                  "E_RESUME_STATE_DIFFERENCE", "actual primitive state/trace differs: " + key)
    _need(_same(states["continuous2"]["body"]["step_trace"], states["continuous4"]["body"]["step_trace"][:2])
          and _same(states["cancelled2"]["body"]["step_trace"], states["resumed4"]["body"]["step_trace"][:2]),
          "E_RESUME_STEPS", "prefix trace was rewritten after restart")
    checkpoint = proof.get("checkpoint")
    _need(isinstance(checkpoint, dict) and set(checkpoint) == {"generation_dir", "manifest_sha256", "manifest_path"},
          "E_RESUME_CHECKPOINT", "actual immutable checkpoint required")
    manifest_file = _child(out, checkpoint["manifest_path"])
    generation = manifest_file.parent
    _need(generation.relative_to(Path(out).absolute()).as_posix() == checkpoint["generation_dir"],
          "E_RESUME_CHECKPOINT", "checkpoint generation path differs")
    actual = verify_manifest(generation, context["resume_binding"], checkpoint["manifest_sha256"])
    _need(actual["manifest_path"] == str(manifest_file) and actual["manifest"]["position"]["global_step"] == 2
          and actual["manifest"]["position"]["batch_ptr"] == {"epoch": 1, "batch_idx": 2},
          "E_RESUME_CHECKPOINT", "cancel boundary checkpoint differs from actual safe exports")
    return {"status": "VALIDATED_LOCAL_BYTES_ONLY", "context_sha256": context["context_sha256"],
        "trial_nonce": context["trial_nonce"], "continuous_steps": 4, "cancelled_steps": 2, "resumed_steps": 4,
        "observed_numeric_exports": 11, "observed_named_adapter_tensors": len(context["adapter_spec"]),
        "server_capability_created": False, "may_prepare": False, "may_train": False,
        "may_infer": False, "may_resume": False, "may_publish": False, "claims_quality_pass": False}
