"""Bounded stdout drain and deadline; terminates only the supplied child."""
import queue
import subprocess
import threading
import time


def drain_bounded(proc, deadline, cap=65536, *, on_stdout=None, on_stop=None):
    events=queue.Queue(maxsize=1024);sentinel=object()
    def reader():
        try:
            for raw in iter(proc.stdout.readline,b''):
                if raw=='' or raw==b'':break
                events.put(raw.decode('utf-8',errors='replace') if isinstance(raw,bytes) else raw)
        finally:events.put(sentinel)
    threading.Thread(target=reader,daemon=True).start()
    parts=[];used=0;done=False
    while time.monotonic()<deadline:
        try:item=events.get(timeout=min(0.2,max(0.001,deadline-time.monotonic())))
        except queue.Empty:continue
        if item is sentinel:done=True;break
        if used<cap:
            piece=item[:cap-used];parts.append(piece);used+=len(piece)
            if on_stdout is not None:on_stdout(piece)
    remaining=max(0,deadline-time.monotonic())
    try:
        code=proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        if on_stop is not None:on_stop('TIME_LIMIT')
        proc.terminate()
        try:proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=2)
        code=-9
    return code,''.join(parts)[-2000:]
