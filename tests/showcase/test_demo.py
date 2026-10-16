"""Opt-in public HTTP demo test; needs built frontend and an installed system simkai font."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get('YY_SHOWCASE_E2E') != '1', reason='set YY_SHOWCASE_E2E=1 after building frontend')
def test_fresh_m0_api_and_restart(tmp_path):
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    source = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, str(source / 'showcase' / 'demo.py'), '--output', str(tmp_path / 'demo'), '--port', str(port)], check=True, timeout=180)
    assert (tmp_path / 'demo' / 'result.json').is_file()
