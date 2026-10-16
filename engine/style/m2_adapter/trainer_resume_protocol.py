"""Immutable, server anchored trainer resume metadata. No ML imports or authority minting.

The server must retain the returned manifest digest on its owned run record.
A caller supplied digest or an unanchored adjacent checkpoints.json is insufficient.
This protocol grants no Runtime or qualification permission.
"""
from __future__ import annotations
import ast
import hashlib
import json
import math
import os
from pathlib import Path

SCHEMA = "yy-full-trainer-resume-manifest/1"
BINDING_SCHEMA = "yy-full-trainer-resume-binding/1"
TRAINER_IDENTITY_FILES = ("__init__.py", "config.py", "trainer.py", "receipt.py")
TRIAL_NONCE_BYTES = 32

def new_trial_nonce():
    """Server-generated 256-bit trial identity, encoded as 64 lowercase hex."""
    return os.urandom(TRIAL_NONCE_BYTES).hex()

def valid_trial_nonce(value):
    return (isinstance(value, str) and len(value) == TRIAL_NONCE_BYTES * 2
            and all(char in "0123456789abcdef" for char in value))
REQUIRED_BINDING = frozenset(("owner_id", "run_id", "snapshot_sha256", "runtime_binding",
    "recipe_identity", "source_sha256", "training_budget", "config_hash", "trainer_version",
    "trainer_code_sha256", "artifact_binding"))

class ResumeProtocolError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"[{code}] {detail}")

def _need(ok, code, detail):
    if not ok:
        raise ResumeProtocolError(code, detail)

def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResumeProtocolError("E_RESUME_METADATA", "metadata must be finite JSON primitives") from exc

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(x in "0123456789abcdef" for x in value)

def native_path(path):
    """Use Windows extended syntax only at the I/O boundary, never as authority.

    Validation and serialized paths keep ordinary absolute E paths. This helper
    changes neither ACLs nor link traversal; callers must still validate inputs.
    """
    ordinary = Path(path).absolute()
    if os.name != "nt":
        return ordinary
    text = str(ordinary)
    if text.startswith("\\\\?\\"):
        return ordinary
    return Path("\\\\?\\" + text)

def source_digest(app):
    """Same source anchor for server and ML child, with no backend dependencies."""
    app = Path(app)
    paths = sorted([*(app / 'engine/style').rglob('*.py'), *(app / 'app/domains/styles').rglob('*.py')])
    hashes = {}
    for path in paths:
        value = hashlib.sha256()
        with native_path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                value.update(block)
        hashes[path.relative_to(app).as_posix()] = value.hexdigest()
    return digest(hashes)

def _regular(path):
    path = Path(path).absolute()
    _need(os.name != "nt" or path.drive.upper() == "E:", "E_RESUME_PATH", "resume material must be on E drive")
    for part in (path, *path.parents):
        native = native_path(part)
        _need(not native.is_symlink() and not (getattr(native, "is_junction", lambda: False)()),
              "E_RESUME_PATH", "linked resume material is rejected")
    _need(native_path(path).is_file(), "E_CKPT_MISSING", "resume material is not a regular file")
    return path

def file_sha256(path):
    h = hashlib.sha256()
    with native_path(_regular(path)).open("rb") as stream:
        for chunk in iter(lambda: stream.read(2**20), b""):
            h.update(chunk)
    return h.hexdigest()

def trainer_code_sha256(app):
    """The exact four-file Trainer code anchor; no eager Trainer/ML import."""
    directory = Path(app) / "engine/style/trainer"
    h = hashlib.sha256()
    for name in sorted(TRAINER_IDENTITY_FILES):
        h.update(name.encode("utf-8"))
        h.update(file_sha256(directory / name).encode("ascii"))
    return h.hexdigest()

def trainer_identity(app):
    """Read the literal version and exact code bytes for all binding producers."""
    path = _regular(Path(app) / "engine/style/trainer/config.py")
    tree = ast.parse(native_path(path).read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "TRAINER_VERSION" for target in node.targets)]
    _need(len(versions) == 1 and isinstance(versions[0], str) and bool(versions[0]),
          "E_RESUME_SOURCE", "one actual literal Trainer version required")
    return versions[0], trainer_code_sha256(app)

def build_binding(**fields):
    _need(set(fields) == REQUIRED_BINDING, "E_RESUME_BINDING", "complete exact binding fields are required")
    for key in ("snapshot_sha256", "source_sha256", "config_hash", "trainer_code_sha256"):
        _need(_hash(fields[key]), "E_RESUME_BINDING", f"full {key} required")
    for key in ("owner_id", "run_id", "trainer_version"):
        _need(type(fields[key]) in (str, int) and not isinstance(fields[key], bool) and str(fields[key]),
              "E_RESUME_BINDING", f"explicit {key} required")
    for key in ("runtime_binding", "recipe_identity", "training_budget", "artifact_binding"):
        _need(isinstance(fields[key], dict) and fields[key], "E_RESUME_BINDING", f"complete {key} required")
    value = {"schema": BINDING_SCHEMA, **fields}
    canonical(value)
    return value

def validate_binding(binding):
    _need(isinstance(binding, dict) and binding.get("schema") == BINDING_SCHEMA,
          "E_RESUME_BINDING", "supported resume binding required")
    return build_binding(**{k: v for k, v in binding.items() if k != "schema"})

def validate_position(position):
    _need(isinstance(position, dict) and set(position) == {"epoch", "global_step", "batch_ptr", "elapsed_seconds", "train_loader_len", "boundary"},
          "E_RESUME_POSITION", "complete safe position required")
    for key in ("epoch", "global_step", "train_loader_len"):
        _need(type(position[key]) is int and position[key] >= (1 if key == "train_loader_len" else 0),
              "E_RESUME_POSITION", f"invalid {key}")
    pointer = position["batch_ptr"]
    _need(isinstance(pointer, dict) and set(pointer) == {"epoch", "batch_idx"}
          and type(pointer["epoch"]) is int and type(pointer["batch_idx"]) is int,
          "E_RESUME_POSITION", "integer next-batch pointer required")
    completed = position["boundary"] == "completed_epoch" and pointer == {"epoch": position["epoch"], "batch_idx": 0}
    pending = position["boundary"] == "committed_batch" and pointer["epoch"] == position["epoch"] + 1 and 0 <= pointer["batch_idx"] <= position["train_loader_len"]
    _need(completed or pending, "E_RESUME_UNSAFE_BOUNDARY", "only committed optimizer boundaries can resume")
    expected_step = position["epoch"] * position["train_loader_len"] + (pointer["batch_idx"] if pending else 0)
    _need(position["global_step"] == expected_step, "E_RESUME_POSITION", "committed step disagrees with epoch/cursor")
    _need(type(position["elapsed_seconds"]) in (int, float) and math.isfinite(position["elapsed_seconds"])
          and position["elapsed_seconds"] >= 0, "E_RESUME_POSITION", "finite cumulative elapsed seconds required")
    return position

def _child(directory, relative):
    _need(isinstance(relative, str) and relative and "\\" not in relative,
          "E_RESUME_PATH", "portable relative checkpoint name required")
    rel = Path(relative)
    _need(not rel.is_absolute() and not rel.drive and all(p not in (".", "..") for p in rel.parts),
          "E_RESUME_PATH", "checkpoint cannot escape its server owned directory")
    path = Path(directory).absolute() / rel
    _need(path.is_relative_to(Path(directory).absolute()), "E_RESUME_PATH", "escaped checkpoint path")
    return _regular(path)

def _artifacts(directory, artifacts):
    _need(isinstance(artifacts, dict), "E_RESUME_ARTIFACT", "artifact digests required")
    for relative, expected in artifacts.items():
        _need(_hash(expected), "E_RESUME_ARTIFACT", "full artifact hash required")
        _need(file_sha256(_child(directory, relative)) == expected,
              "E_RESUME_ARTIFACT", "checkpoint associated artifact changed")

def write_manifest(checkpoint_dir, checkpoint_path, binding, position, artifacts=None):
    """Write once after checkpoint commit; return the digest the server must retain.

    checkpoint_dir is a server owned immutable generation directory. The returned
    record is descriptive only, and does not grant execution permissions.
    """
    binding = validate_binding(binding)
    position = validate_position(position)
    directory = Path(checkpoint_dir).absolute()
    checkpoint = _regular(checkpoint_path)
    _need(checkpoint.is_relative_to(directory), "E_RESUME_PATH", "checkpoint outside owned directory")
    artifacts = dict(artifacts or {})
    _artifacts(directory, artifacts)
    manifest = {"schema": SCHEMA, "binding": binding, "binding_sha256": digest(binding),
                "position": position, "checkpoint": {"path": checkpoint.relative_to(directory).as_posix(),
                  "sha256": file_sha256(checkpoint), "bytes": native_path(checkpoint).stat().st_size},
                "artifacts": artifacts, "claims_qualification_pass": False, "claims_quality_pass": False}
    data = canonical(manifest) + b"\n"
    sha = hashlib.sha256(data).hexdigest()
    path = directory / ("resume-" + sha + ".json")
    with native_path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"manifest_path": str(path), "manifest_sha256": sha, "checkpoint_path": str(checkpoint), "manifest": manifest}

def verify_manifest(checkpoint_dir, expected_binding, manifest_sha256, expected_artifacts=None):
    """Verify only a server anchored digest before imports or checkpoint deserialization."""
    expected_binding = validate_binding(expected_binding)
    _need(_hash(manifest_sha256), "E_RESUME_ANCHOR", "server retained immutable manifest digest required")
    path = _child(checkpoint_dir, "resume-" + manifest_sha256 + ".json")
    _need(native_path(path).stat().st_size <= 2**20, "E_RESUME_METADATA", "oversized resume metadata")
    _need(file_sha256(path) == manifest_sha256, "E_RESUME_ANCHOR", "manifest bytes changed")
    try:
        manifest = json.loads(native_path(path).read_bytes())
    except (UnicodeError, ValueError) as exc:
        raise ResumeProtocolError("E_RESUME_METADATA", "invalid manifest JSON") from exc
    _need(isinstance(manifest, dict) and set(manifest) == {"schema", "binding", "binding_sha256", "position", "checkpoint", "artifacts", "claims_qualification_pass", "claims_quality_pass"}
          and manifest["schema"] == SCHEMA, "E_RESUME_VERSION", "unsupported resume manifest")
    _need(canonical(manifest["binding"]) == canonical(expected_binding) and manifest["binding_sha256"] == digest(expected_binding),
          "E_RESUME_IDENTITY_MISMATCH", "owner/run/data/runtime/recipe/source/budget/artifact binding changed")
    _need(manifest["claims_qualification_pass"] is False and manifest["claims_quality_pass"] is False,
          "E_RESUME_AUTHORITY", "checkpoint metadata cannot assert qualification or quality")
    validate_position(manifest["position"])
    row = manifest["checkpoint"]
    _need(isinstance(row, dict) and set(row) == {"path", "sha256", "bytes"} and _hash(row["sha256"])
          and type(row["bytes"]) is int and row["bytes"] > 0, "E_RESUME_METADATA", "complete checkpoint record required")
    checkpoint = _child(checkpoint_dir, row["path"])
    _need(native_path(checkpoint).stat().st_size == row["bytes"] and file_sha256(checkpoint) == row["sha256"],
          "E_CKPT_TAMPERED", "checkpoint bytes differ from server anchored manifest")
    if expected_artifacts is not None:
        _need(canonical(manifest["artifacts"]) == canonical(expected_artifacts), "E_RESUME_ARTIFACT", "artifact binding changed")
    _artifacts(checkpoint_dir, manifest["artifacts"])
    return {"checkpoint_path": str(checkpoint), "manifest_path": str(path), "manifest_sha256": manifest_sha256, "manifest": manifest}

def validate_payload(payload, manifest, *, checkpoint_schema, trainer_version, code_version, config_hash, run_id, snapshot_sha256, train_loader_len):
    """Second boundary: check actual restricted-load payload before state mutation."""
    _need(isinstance(payload, dict) and payload.get("checkpoint_schema") == checkpoint_schema,
          "E_RESUME_VERSION", "unsupported trainer checkpoint")
    for key, expected in (("trainer_version", trainer_version), ("code_version", code_version),
                          ("config_hash", config_hash), ("run_id", run_id), ("data_snapshot_sha256", snapshot_sha256)):
        _need(type(payload.get(key)) is type(expected) and payload.get(key) == expected,
              "E_RESUME_IDENTITY_MISMATCH", "checkpoint payload differs: " + key)
    _need(canonical(payload.get("resume_binding")) == canonical(manifest["binding"]),
          "E_RESUME_IDENTITY_MISMATCH", "checkpoint payload omitted or changed bound authority")
    for key in ("epoch", "global_step", "batch_ptr", "elapsed_seconds"):
        _need(canonical(payload.get(key)) == canonical(manifest["position"][key]),
              "E_RESUME_POSITION", "payload position differs: " + key)
    _need(type(train_loader_len) is int and train_loader_len == manifest["position"]["train_loader_len"],
          "E_RESUME_LOADER", "loader batch count changed")
    _need(isinstance(payload.get("loader_state"), dict) and payload["loader_state"],
          "E_RESUME_LOADER", "complete private loader/dataset RNG state required")
    for key in ("model_state", "optimizer_state", "rng", "epochs_log"):
        _need(key in payload, "E_RESUME_PAYLOAD", "missing trainer state: " + key)
    return payload


class StatefulBatchSampler:
    """A committed cursor over a complete permutation; data fetching never commits.

    num_workers must be zero so prefetched data cannot advance private dataset RNG.
    The caller supplies its existing randperm result and persists that RNG itself.
    """
    schema = "yy-stateful-batch-sampler/1"
    _yy_stateful_batches = True

    def __init__(self, size, batch_size, *, drop_last=True, num_workers=0):
        _need(type(size) is int and size > 0 and type(batch_size) is int and 0 < batch_size <= size,
              "E_RESUME_SAMPLER", "positive dataset and batch size required")
        _need(type(drop_last) is bool and type(num_workers) is int and num_workers == 0,
              "E_RESUME_SAMPLER", "explicit drop_last and zero workers required")
        self.size, self.batch_size, self.drop_last = size, batch_size, drop_last
        self.epoch, self.next_batch, self.permutation = 0, 0, []

    def __len__(self):
        return self.size // self.batch_size if self.drop_last else (self.size + self.batch_size - 1) // self.batch_size

    def begin_epoch(self, epoch, permutation):
        _need(type(epoch) is int and epoch > 0 and epoch == self.epoch + 1,
              "E_RESUME_SAMPLER", "epochs must advance exactly once")
        _need(self.epoch == 0 or self.next_batch == len(self), "E_RESUME_SAMPLER", "uncommitted epoch cannot be replaced")
        permutation = list(permutation)
        _need(len(permutation) == self.size and all(type(x) is int for x in permutation)
              and sorted(permutation) == list(range(self.size)), "E_RESUME_SAMPLER", "complete exact permutation required")
        self.epoch, self.next_batch, self.permutation = epoch, 0, permutation

    def __iter__(self):
        _need(self.epoch > 0 and len(self.permutation) == self.size, "E_RESUME_SAMPLER", "begin_epoch required")
        for index in range(self.next_batch, len(self)):
            yield self.permutation[index*self.batch_size:min(self.size, (index+1)*self.batch_size)]

    def commit_batch(self, next_batch=None):
        expected = self.next_batch + 1
        _need(self.epoch > 0 and self.next_batch < len(self) and (next_batch is None or type(next_batch) is int and next_batch == expected),
              "E_RESUME_SAMPLER", "duplicate or out of order batch commit")
        self.next_batch = expected

    def state_dict(self):
        return {"schema": self.schema, "epoch": self.epoch, "next_batch": self.next_batch,
                "size": self.size, "batch_size": self.batch_size, "drop_last": self.drop_last,
                "num_workers": 0, "permutation": list(self.permutation)}

    def load_state_dict(self, state):
        _need(isinstance(state, dict) and set(state) == {"schema", "epoch", "next_batch", "size", "batch_size", "drop_last", "num_workers", "permutation"},
              "E_RESUME_SAMPLER", "complete sampler state required")
        for key, expected in (("schema", self.schema), ("size", self.size), ("batch_size", self.batch_size), ("drop_last", self.drop_last), ("num_workers", 0)):
            _need(type(state[key]) is type(expected) and state[key] == expected, "E_RESUME_SAMPLER", "sampler identity changed: " + key)
        _need(type(state["epoch"]) is int and state["epoch"] > 0 and type(state["next_batch"]) is int and 0 <= state["next_batch"] <= len(self),
              "E_RESUME_SAMPLER", "invalid epoch or cursor")
        permutation = state["permutation"]
        _need(isinstance(permutation, list) and len(permutation) == self.size and all(type(x) is int for x in permutation)
              and sorted(permutation) == list(range(self.size)), "E_RESUME_SAMPLER", "invalid saved permutation")
        self.epoch, self.next_batch, self.permutation = state["epoch"], state["next_batch"], list(permutation)


def validate_trainable_model_metadata(payload, expected_structure, trainable_names, frozen_model_sha256):
    """Pure metadata check; the Trainer separately validates actual tensor values."""
    _need(isinstance(expected_structure, dict) and expected_structure,
          "E_RESUME_MODEL", "complete model structure required")
    for name, row in expected_structure.items():
        _need(isinstance(name, str) and isinstance(row, dict) and set(row) == {"shape", "dtype", "layout", "numel"}
              and isinstance(row["shape"], list) and all(type(x) is int and x >= 0 for x in row["shape"])
              and type(row["numel"]) is int and row["numel"] == math.prod(row["shape"])
              and isinstance(row["dtype"], str) and row["layout"] == "torch.strided",
              "E_RESUME_MODEL", "complete exact tensor structure required")
    _need(isinstance(payload, dict) and payload.get("model_state_scope") == "trainable-only"
          and canonical(payload.get("model_structure")) == canonical(expected_structure),
          "E_RESUME_MODEL", "frozen/trainable model structure changed")
    names = set(trainable_names)
    _need(names and names.issubset(expected_structure) and isinstance(payload.get("model_state"), dict)
          and set(payload["model_state"]) == names, "E_RESUME_MODEL", "checkpoint must contain exact trainable state only")
    _need(_hash(frozen_model_sha256) and payload.get("frozen_model_sha256") == frozen_model_sha256,
          "E_RESUME_FROZEN_MODEL", "reloaded frozen model contents changed")
    return payload
