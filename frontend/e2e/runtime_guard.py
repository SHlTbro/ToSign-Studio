"""Fail-closed runtime controls shared by the isolated browser harnesses.

This module never stops a process discovered by port. It records and controls
only Popen objects created by the current harness invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


def configured_port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or result.stderr).strip()
    return value if result.returncode == 0 and value else None


def tree_sha256(root: Path) -> str | None:
    """Hash relative names and file contents; return None for an absent tree."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path) or ""))
    return digest.hexdigest()


def frontend_source_sha256(frontend: Path) -> str:
    digest = hashlib.sha256()
    roots = [frontend / "src"]
    files = [frontend / "package.json", frontend / "package-lock.json"]
    for root in roots:
        root_hash = tree_sha256(root)
        digest.update(str(root.relative_to(frontend)).encode("utf-8"))
        digest.update((root_hash or "MISSING").encode("ascii"))
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update((_sha256_file(path) or "MISSING").encode("ascii"))
    return digest.hexdigest()


def e2e_harness_sha256(e2e_dir: Path) -> str:
    """Hash only executable driver/support files and their run instructions."""
    digest = hashlib.sha256()
    files = sorted([*e2e_dir.glob("*.py"), e2e_dir / "README.md", e2e_dir / "requirements-e2e.txt"])
    for path in files:
        relative = path.relative_to(e2e_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path) or ""))
    return digest.hexdigest()


def session_cookie_value(cookies: list[dict]) -> str:
    """Extract the real browser session cookie without logging its value."""
    value = next((cookie.get("value") for cookie in cookies
                  if cookie.get("name") == "session" and isinstance(cookie.get("value"), str)
                  and cookie.get("value")), None)
    if value is None:
        raise RuntimeError("authenticated browser session cookie is missing")
    return value


def with_session_cookie(headers: dict[str, str] | None, session_value: str | None) -> dict[str, str]:
    """Use the current UI cookie unless a probe explicitly supplies another."""
    result = dict(headers or {})
    has_explicit_cookie = any(key.lower() == "cookie" for key in result)
    if session_value and not has_explicit_cookie:
        result["Cookie"] = f"session={session_value}"
    return result


def _port_listener_pids(port: int) -> set[int]:
    netstat = shutil.which("netstat")
    if not netstat:
        raise RuntimeError("netstat is required to verify the owner of E2E ports")
    result = subprocess.run([netstat, "-ano", "-p", "tcp"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"netstat failed ({result.returncode}): {result.stderr[-500:]}")
    owners: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if fields[-2].upper() not in {"LISTENING", "LISTEN"}:
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1])
            pid = int(fields[-1])
        except (ValueError, IndexError):
            continue
        if local_port == port:
            owners.add(pid)
    return owners


def assert_port_free(port: int) -> None:
    owners = _port_listener_pids(port)
    if owners:
        raise RuntimeError(f"E2E port {port} already has LISTENING PID(s) {sorted(owners)}; refusing to attach or stop them")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"E2E port {port} is not available on 127.0.0.1: {exc}") from exc


class Tee:
    def __init__(self, console: TextIO, log: TextIO):
        self.console = console
        self.log = log

    def write(self, value: str) -> int:
        self.console.write(value)
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self) -> None:
        self.console.flush()
        self.log.flush()


class RunContext:
    def __init__(self, repo: Path, label: str, backend_port: int, frontend_port: int):
        if backend_port == frontend_port:
            raise ValueError("frontend and backend E2E ports must differ")
        self.repo = repo.resolve()
        self.frontend = self.repo / "frontend"
        self.label = label
        self.backend_port = backend_port
        self.frontend_port = frontend_port
        self.run_id = f"{label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.run_dir = self.repo / "frontend" / "e2e" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.lock_path = self.repo / "frontend" / "e2e" / "runs" / ".active-e2e-ports.json"
        self.children: dict[str, tuple[subprocess.Popen, TextIO, TextIO]] = {}
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.stdout_log = (self.run_dir / "harness.stdout.log").open("w", encoding="utf-8")
        self.stderr_log = (self.run_dir / "harness.stderr.log").open("w", encoding="utf-8")
        self.exit_codes: dict[str, int | None] = {}
        self.child_started_at: dict[str, str] = {}
        self._lock_owned = False

    def _acquire_lock(self) -> None:
        payload = {"schema": 1, "run_id": self.run_id, "pid": os.getpid(),
                   "ports": [self.backend_port, self.frontend_port],
                   "created_at": datetime.now(timezone.utc).isoformat()}
        for attempt in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2)
                self._lock_owned = True
                return
            except FileExistsError:
                if attempt or not self._remove_verified_stale_lock():
                    raise RuntimeError(f"another E2E harness owns port lock {self.lock_path}; refusing concurrent run")

    def _remove_verified_stale_lock(self) -> bool:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if payload.get("schema") != 1 or not isinstance(payload.get("pid"), int):
                return False
            pid = payload["pid"]
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.lock_path.unlink()
                return True
            except PermissionError:
                return False
            return False
        except (OSError, ValueError, TypeError):
            return False

    def acquire(self) -> None:
        self._acquire_lock()
        assert_port_free(self.backend_port)
        assert_port_free(self.frontend_port)
        self.write_manifest("preflight", None)

    def install_output_capture(self) -> None:
        sys.stdout = Tee(self.original_stdout, self.stdout_log)  # type: ignore[assignment]
        sys.stderr = Tee(self.original_stderr, self.stderr_log)  # type: ignore[assignment]

    def restore_output_capture(self) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        self.stdout_log.close()
        self.stderr_log.close()

    def fingerprints(self, phase: str, runner_exit_code: int | None) -> dict:
        node = shutil.which("node")
        python = sys.executable
        release_gate_rel = os.environ.get(
            "YY_E2E_RELEASE_GATE_PATH",
            ".ai-hub/parallel-lines/line-b/gates/B-GATE-RELEASE.json",
        ).replace("\\", "/")
        release_gate = self.repo / Path(release_gate_rel)
        dist, dist_source = self._dist_candidate()
        release_gate_sha = _sha256_file(release_gate)
        return {
            "schema": 1,
            "run_id": self.run_id,
            "label": self.label,
            "phase": phase,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "runner_pid": os.getpid(),
            "runner_exit_code": runner_exit_code,
            "ports": {"backend": self.backend_port, "frontend": self.frontend_port},
            "frontend_source_sha256": frontend_source_sha256(self.frontend),
            "e2e_harness_sha256": e2e_harness_sha256(self.frontend / "e2e"),
            "frontend_dist": {"path": str(dist), "source": dist_source,
                              "sha256": tree_sha256(dist) if dist is not None else None,
                              "status": "PRESENT_NOT_SERVED_BY_VITE_DEV" if dist and dist.is_dir() else "NOT_PRESENT_OR_REFUSED"},
            "backend_release": {"release_hash_placeholder": None,
                                "gate": release_gate_rel, "gate_file_sha256": release_gate_sha,
                                "status": "PRESENT_REQUIRES_ORCHESTRATOR_VALIDATION" if release_gate.is_file() else "WAITING_B-GATE-RELEASE"},
            "runtimes": {"runner_python": python, "runner_python_version": sys.version.split()[0],
                         "backend_python": os.environ.get("YY_E2E_BACKEND_PYTHON"),
                         "backend_pythonpath": os.environ.get("YY_E2E_BACKEND_PYTHONPATH"),
                         "backend_python_version": _version([os.environ["YY_E2E_BACKEND_PYTHON"], "--version"])
                         if os.environ.get("YY_E2E_BACKEND_PYTHON") else None,
                         "node": node, "node_version": _version([node, "--version"]) if node else None,
                         "low_memory": os.environ.get("YY_E2E_LOW_MEMORY", "1") != "0"},
            "child_processes": [
                {"name": name, "pid": proc.pid, "started_at": self.child_started_at.get(name),
                 "return_code": self.exit_codes.get(name),
                 "command": proc.args, "stdout": str(self.run_dir / f"{name}.stdout.log"),
                 "stderr": str(self.run_dir / f"{name}.stderr.log")}
                for name, (proc, _, _) in self.children.items()
            ],
            "port_listener_pids": {str(p): sorted(_port_listener_pids(p)) for p in (self.backend_port, self.frontend_port)},
        }

    def _dist_candidate(self) -> tuple[Path | None, str]:
        explicit = os.environ.get("YY_E2E_FRONTEND_DIST")
        source = "YY_E2E_FRONTEND_DIST" if explicit else "A-GATE-FRONTEND-DIST dist_root"
        candidate: Path | None = Path(explicit).resolve() if explicit else None
        if candidate is None:
            gate = self.repo / ".ai-hub" / "parallel-lines" / "line-a" / "A-GATE-FRONTEND-DIST-2026-09-27.json"
            try:
                payload = json.loads(gate.read_text(encoding="utf-8"))
                relative = payload.get("dist_root")
                if isinstance(relative, str):
                    candidate = (self.repo / relative).resolve()
            except (OSError, ValueError):
                candidate = None
        if candidate is None:
            candidate = (self.frontend / "dist").resolve()
            source = "frontend/dist fallback"
        try:
            candidate.relative_to(self.repo)
        except ValueError:
            return None, "REFUSED_OUTSIDE_WORKSPACE"
        return candidate, source

    def write_manifest(self, phase: str, runner_exit_code: int | None) -> None:
        manifest = self.fingerprints(phase, runner_exit_code)
        filename = "runtime-fingerprint-start.json" if phase == "preflight" else "runtime-fingerprint.json"
        (self.run_dir / filename).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def start(self, name: str, command: list[str], cwd: Path, env: dict[str, str], port: int) -> subprocess.Popen:
        assert_port_free(port)
        stdout_log = (self.run_dir / f"{name}.stdout.log").open("w", encoding="utf-8")
        stderr_log = (self.run_dir / f"{name}.stderr.log").open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=stdout_log, stderr=stderr_log)
        except Exception:
            stdout_log.close()
            stderr_log.close()
            raise
        self.children[name] = (proc, stdout_log, stderr_log)
        self.child_started_at[name] = datetime.now(timezone.utc).isoformat()
        self.write_manifest("starting", None)
        return proc

    def wait_owned_listener(self, name: str, timeout: float = 90) -> bool:
        proc = self.children[name][0]
        port = self.backend_port if name == "backend" else self.frontend_port
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            owners = _port_listener_pids(port)
            if owners == {proc.pid}:
                return True
            time.sleep(0.2)
        return False

    def stop_all(self, runner_exit_code: int | None) -> None:
        for name, (proc, stdout_log, stderr_log) in reversed(list(self.children.items())):
            if proc.poll() is None:
                proc.terminate()  # Only the exact Popen process created by this run.
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()  # Same Popen PID; never enumerate/kill by port or process name.
                    proc.wait(timeout=10)
            self.exit_codes[name] = proc.returncode
            stdout_log.close()
            stderr_log.close()
        try:
            self.write_manifest("stopped", runner_exit_code)
        finally:
            if self._lock_owned:
                try:
                    payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    if payload.get("run_id") == self.run_id:
                        self.lock_path.unlink()
                except (OSError, ValueError):
                    pass
                self._lock_owned = False


def low_memory_browser_args() -> list[str]:
    if os.environ.get("YY_E2E_LOW_MEMORY", "1") == "0":
        return []
    return ["--disable-dev-shm-usage", "--disable-gpu", "--renderer-process-limit=2", "--no-zygote"]
