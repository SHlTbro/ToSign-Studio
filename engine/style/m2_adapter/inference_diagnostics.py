"""Bounded diagnostic observations only; no execution authorization or receipt."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import re
import time
from pathlib import Path
from .trainer_resume_protocol import native_path

_stages = ContextVar('m2_diagnostic_stages', default=None)
_capture = ContextVar('m2_diagnostic_child', default=None)


@contextmanager
def trace_session():
    token = _stages.set({'start': time.monotonic(), 'cpu': time.process_time(), 'rows': 0})
    try:
        yield
    finally:
        _stages.reset(token)


def _emit(name, event, error=None):
    state = _stages.get()
    if state is None or state['rows'] >= 64:
        return
    state['rows'] += 1
    row = {'schema': 'm2-inference-stage/1', 'stage': name, 'event': event,
        'elapsed_seconds': round(time.monotonic() - state['start'], 6),
        'cpu_seconds': round(time.process_time() - state['cpu'], 6)}
    if error is not None:
        row['error_type'] = type(error).__name__
        if getattr(error, 'code', None) is not None:
            row['error_code'] = str(error.code)[:80]
    try:
        print(json.dumps(row, ensure_ascii=False), flush=True)
    except (OSError, ValueError):
        pass  # diagnostic stdout failure never alters the authorized operation


@contextmanager
def stage(name):
    _emit(name, 'begin')
    error = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        _emit(name, 'end', error)


def traced_stage(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def trace_entry(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with trace_session(), stage('main'):
            return function(*args, **kwargs)
    return wrapped


def active_capture():
    return _capture.get()


class ChildCapture:
    def __init__(self, cache, proc, budget_seconds, parent_prepare_seconds):
        self.cache, self.proc, self.stream = Path(cache), proc, None
        self.start = time.monotonic()
        pid = getattr(proc, 'pid', None)
        self.facts = {'schema': 'm2-child-diagnostic/1', 'diagnostic_only': True,
            'owned_pid': pid, 'process_create_time': None, 'process_create_observation': 'not_exposed',
            'child_budget_seconds': budget_seconds, 'parent_prepare_elapsed_seconds': parent_prepare_seconds,
            'drained_exit_code': None, 'stop_reason': None, 'stdout_chars': 0}
        if isinstance(pid, int) and pid > 0:
            try:
                from .personal_runtime_resources import process_start
                self.facts['process_create_time'] = process_start(pid)
                self.facts['process_create_observation'] = 'observed'
            except Exception as exc:
                self.facts.update(process_create_observation='unavailable', process_create_error_type=type(exc).__name__)
        try:
            self.stream = native_path(self.cache / 'child-stdout.log').open('x', encoding='utf-8', newline='')
        except OSError as exc:
            self.facts['diagnostic_io_error_type'] = type(exc).__name__

    def on_stdout(self, piece):
        self.facts['stdout_chars'] += len(piece)
        try:
            value = json.loads(piece)
            failure = value.get('failure') if isinstance(value, dict) else None
            code = failure.get('code') if isinstance(failure, dict) else None
            if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code):
                self.facts['runner_failure_code'] = code
        except ValueError:
            pass
        if self.stream is not None:
            try:
                self.stream.write(piece)
                self.stream.flush()
            except (OSError, ValueError) as exc:
                self.facts['diagnostic_io_error_type'] = type(exc).__name__

    def on_stop(self, reason):
        self.facts['stop_reason'] = reason

    def complete(self, code):
        self.facts['drained_exit_code'] = code
        if self.facts['stop_reason'] is None:
            self.facts['stop_reason'] = 'EXITED' if code == 0 else 'NONZERO_EXIT'

    def finish(self, error=None):
        ended = getattr(self.proc, 'returncode', None)
        self.facts.update(observed_returncode=ended, execution_end_confirmed=ended is not None,
            captured_child_elapsed_seconds=round(time.monotonic() - self.start, 6))
        if error is not None:
            self.facts['parent_error_type'] = type(error).__name__
        if self.facts['stop_reason'] is None:
            self.facts['stop_reason'] = 'CHILD_ENDED_WITH_PARENT_ERROR' if ended is not None else 'UNCONFIRMED_EXECUTION'
        try:
            if self.stream is not None:
                self.stream.close()
            with native_path(self.cache / 'child-execution.json').open('x', encoding='utf-8') as stream:
                json.dump(self.facts, stream, ensure_ascii=False, indent=2)
        except (OSError, ValueError) as exc:
            self.facts['diagnostic_io_error_type'] = type(exc).__name__


@contextmanager
def capture_child(cache, proc, *, budget_seconds, parent_prepare_seconds):
    capture = ChildCapture(cache, proc, budget_seconds, parent_prepare_seconds)
    token = _capture.set(capture)
    error = None
    try:
        yield capture
    except BaseException as exc:
        error = exc
        raise
    finally:
        _capture.reset(token)
        capture.finish(error)
