"""Regression: timeout cleanup must close the real Windows venv child."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'showcase'
SPEC = importlib.util.spec_from_file_location('showcase_processes', SOURCE / 'processes.py')
owned = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owned)


@pytest.mark.skipif(os.name != 'nt', reason='Windows venv redirector regression')
def test_timeout_cleanup_terminates_only_recorded_actual_child(tmp_path):
    record = tmp_path / 'child.json'
    nonce = secrets.token_hex(24)
    env = dict(os.environ, YY_SHOWCASE_PROCESS_RECORD=str(record), YY_SHOWCASE_INSTANCE=nonce)
    code = 'import sys,time;sys.path.insert(0,sys.argv[1]);from processes import record_identity;record_identity();time.sleep(60)'
    child = subprocess.Popen([sys.executable, '-c', code, str(SOURCE)], env=env)
    handle = None
    try:
        for _ in range(100):
            if record.is_file(): break
            time.sleep(.02)
        row = json.loads(record.read_text())
        api, w = owned._windows_api()
        handle = api.OpenProcess(0x1000 | 0x100000, False, row['pid'])
        assert handle and owned._created(api, w, handle) == row['created']
        child.terminate(); child.wait(timeout=10)
        # The old cleanup can return while this exact server child still lives.
        if row['pid'] != child.pid:
            assert api.WaitForSingleObject(handle, 0) == 258
        owned.terminate_owned(child, record, nonce)
        assert api.WaitForSingleObject(handle, 0) == 0
    finally:
        owned.terminate_owned(child, record, nonce)
        if handle: api.CloseHandle(handle)


@pytest.mark.skipif(os.name != 'nt', reason='Windows startup-timeout regression')
def test_health_timeout_removes_owned_listener(tmp_path):
    import socket
    demo_spec = importlib.util.spec_from_file_location('showcase_demo_timeout', SOURCE / 'demo.py')
    demo = importlib.util.module_from_spec(demo_spec)
    demo_spec.loader.exec_module(demo)
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<html>isolated timeout fixture</html>')
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    with pytest.raises(RuntimeError, match='did not become healthy'):
        demo.run(tmp_path / 'timeout-demo', port, dist, startup_timeout=.01)
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', port)) != 0
