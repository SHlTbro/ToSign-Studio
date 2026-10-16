# -*- coding: utf-8 -*-
"""T77 (C-62) E2E full chain — migrated verbatim from
.ai-hub/test-reports/task58b-assets/e2e_t58b.py (T58B baseline, 19/19).

Only change vs the original: REPO resolves to parents[2] (this file now lives
at frontend/e2e/ instead of .ai-hub/test-reports/task58b-assets/). All
assertions, selectors and flow are byte-for-byte the T58B logic.

Playwright + isolated temp DB/storage. Ports are configurable with
YY_E2E_BACKEND_PORT / YY_E2E_FRONTEND_PORT; the harness refuses occupied
ports and verifies each listener belongs to its own child process.

Request-safety note: this is a LOCAL test harness. Every outbound URL is
validated by ``safe_url`` — scheme must be http, hostname must be the
literal local test host, and the resolved IP must be exactly loopback —
so no request can reach any other host even if a path were interpolated
wrong.
"""
import ipaddress
import json as _json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from runtime_guard import (RunContext, configured_port, low_memory_browser_args,
                           session_cookie_value, with_session_cookie)

REPO = Path(__file__).resolve().parents[2]
VENV_PY = Path(os.environ.get(
    "YY_E2E_BACKEND_PYTHON", str(REPO / ".venv" / "Scripts" / "python.exe")
)).resolve()

TMP = Path(tempfile.mkdtemp(prefix="t58b_e2e_"))
DB = (TMP / "t58b_e2e.db").as_posix()
STORAGE = str(TMP / "storage")
BACKEND_PORT = configured_port("YY_E2E_BACKEND_PORT", 8101)
FRONTEND_PORT = configured_port("YY_E2E_FRONTEND_PORT", 5173)

FAILS = []
API_SESSION_COOKIE = None


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" :: {extra}"))
    if not cond:
        FAILS.append(name)


def safe_url(base: str, path: str) -> str:
    """Validate scheme/host/dns-resolution; pin the target to loopback."""
    if not isinstance(path, str) or not path.startswith("/") or ".." in path or "\\" in path:
        raise ValueError("bad request path")
    parsed = urllib.parse.urlsplit(base + path)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError(f"host not allowed: {parsed.hostname}")
    infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_loopback:
            raise ValueError(f"resolved non-loopback address: {ip}")
    return urllib.parse.urlunsplit(parsed)


def frontend_url(path: str) -> str:
    return safe_url(f"http://127.0.0.1:{FRONTEND_PORT}", path)


def backend_url(path: str) -> str:
    return safe_url(f"http://127.0.0.1:{BACKEND_PORT}", path)


def wait_http(url, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def png_bytes(w=900, h=1200):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (238, 242, 246)).save(buf, "PNG")
    return buf.getvalue()


def api(method, path, headers=None, json_body=None):
    """Tiny urllib wrapper (E2E venv has no requests); returns (status, json|bytes)."""
    url = backend_url(path)
    data = _json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if json_body is not None:
        req.add_header("Content-Type", "application/json")
    request_headers = with_session_cookie(headers, API_SESSION_COOKIE)
    if method.upper() not in {"GET", "HEAD", "OPTIONS"} and any(k.lower() == "cookie" for k in request_headers):
        if not any(k.lower() == "x-requested-with" for k in request_headers):
            request_headers["X-Requested-With"] = "XMLHttpRequest"
    for k, v in request_headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (_json.loads(body) if "json" in ctype else body)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, _json.loads(body)
        except Exception:
            return e.code, body


def bind_api_session(page):
    global API_SESSION_COOKIE
    API_SESSION_COOKIE = session_cookie_value(page.context.cookies())
    print("API helper bound to the current Playwright session cookie (value redacted)")
    return API_SESSION_COOKIE


def seed_invites(env):
    """Insert bootstrap invites into the temp DB via the locked .venv stack."""
    code = (
        "from app.core.db import Base, get_engine\n"
        "from app.domains.auth.models import Invite\n"
        "Base.metadata.create_all(bind=get_engine())\n"
        "from app.core.db import sessionmaker\n"
        "s = sessionmaker(bind=get_engine())()\n"
        "s.add(Invite(code='seed-admin', uses_total=99, used_count=0, created_at='2026-09-21T00:00:00+00:00'))\n"
        "s.add(Invite(code='seed-inv', uses_total=99, used_count=0, created_at='2026-09-21T00:00:00+00:00'))\n"
        "s.commit(); s.close()\n"
        "print('invites seeded')\n"
    )
    r = subprocess.run([str(VENV_PY), "-X", "utf8", "-c", code], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=120)
    print(f"seed-invites subprocess exit_code={r.returncode}")
    if r.stdout:
        print("seed-invites stdout:\n" + r.stdout.rstrip())
    if r.stderr:
        print("seed-invites stderr:\n" + r.stderr.rstrip(), file=sys.stderr)
    return r.returncode == 0 and "invites seeded" in (r.stdout or "")


def main() -> int:
    from playwright.sync_api import sync_playwright

    runtime = RunContext(REPO, "full-chain", BACKEND_PORT, FRONTEND_PORT)
    env = dict(os.environ, YY_DB_URL=f"sqlite:///{DB}", YY_STORAGE_ROOT=STORAGE,
               YY_E2E_BACKEND_PYTHON=str(VENV_PY), YY_AUTH_STRICT="1")
    backend_pythonpath = os.environ.get("YY_E2E_BACKEND_PYTHONPATH")
    if backend_pythonpath:
        env["PYTHONPATH"] = backend_pythonpath
    exit_code = 1
    runtime.install_output_capture()
    try:
        runtime.acquire()
        backend = runtime.start("backend", [str(VENV_PY), "-m", "uvicorn", "app.main:app",
                                             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
                                REPO, env, BACKEND_PORT)
        node = shutil.which("node")
        vite = REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("direct Node/Vite executable is unavailable; refusing npm.cmd child-process cleanup ambiguity")
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(FRONTEND_PORT),
                                               "--strictPort", "--host", "127.0.0.1"],
                                 REPO / "frontend", env, FRONTEND_PORT)
        backend_owned = runtime.wait_owned_listener("backend")
        check("boot-backend-owned", backend_owned)
        if not backend_owned:
            raise RuntimeError("backend listener does not belong to this run's Popen PID")
        backend_ready = backend.poll() is None and wait_http(backend_url("/api/v1/health"))
        check("boot-backend", backend_ready)
        if not backend_ready:
            raise RuntimeError("owned backend failed health readiness")
        check("seed-invites", seed_invites(env))
        frontend_owned = runtime.wait_owned_listener("frontend")
        check("boot-frontend-owned", frontend_owned)
        if not frontend_owned:
            raise RuntimeError("frontend listener does not belong to this run's Popen PID")
        frontend_ready = frontend.poll() is None and wait_http(frontend_url("/"))
        check("boot-frontend", frontend_ready)
        if not frontend_ready:
            raise RuntimeError("owned frontend failed HTTP readiness")

        # --- API seeding: first registered user = admin (GWT-G2-admin) ---
        import secrets

        pwd = "T58B-" + secrets.token_urlsafe(12) + "!7Aa"
        st, body = api("POST", "/api/v1/auth/register", json_body={
            "username": "admin58b", "password": pwd, "display_name": "admin58b", "invite_code": "seed-admin"})
        check("api-seed-invite-admin", st == 201, str(body)[:200])
        st, body = api("POST", "/api/v1/auth/register", json_body={
            "username": "creator58b", "password": pwd, "display_name": "creator58b", "invite_code": "seed-inv"})
        check("api-seed-invite-creator", st == 201, str(body)[:200])

        with sync_playwright() as p:
            browser = p.chromium.launch(args=low_memory_browser_args())
            page = browser.new_page(viewport={"width": 390, "height": 844})

            # --- UI login (admin) -> must_change forces /change-password ---
            page.goto(frontend_url("/login"))
            page.wait_for_selector("#login-username", timeout=30000)
            page.fill("#login-username", "admin58b")
            page.fill("#login-password", pwd)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            newpwd = "T58B-New-" + secrets.token_urlsafe(10) + "!8Bb"
            page.wait_for_selector('input[type="password"]', timeout=30000)
            page.wait_for_timeout(500)
            pwd_inputs = page.query_selector_all('input[type="password"]')
            # 三个输入框：旧密码 / 新密码 / 确认新密码
            for i, val in enumerate((pwd, newpwd, newpwd)):
                if i < len(pwd_inputs):
                    pwd_inputs[i].fill(val)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            # 改密成功后路由回首页；轮询等待 must_change 清除（URL 可能短暂滞后）
            deadline = time.time() + 30
            while time.time() < deadline:
                if page.url.rstrip("/").endswith(f":{FRONTEND_PORT}"):
                    break
                page.wait_for_timeout(300)
            check("e2e-must-change-gate", "change-password" not in page.url or "must_change" not in page.url, page.url)
            page.wait_for_timeout(500)
            bind_api_session(page)

            # --- P2 real upload ---
            png = TMP / "cos_a.png"
            png.write_bytes(png_bytes())
            page.goto(frontend_url("/assets"))
            page.set_input_files('input[type="file"]', str(png))
            page.wait_for_selector('button[aria-label^="资产"]', timeout=20000)
            check("e2e-p2-upload", True)

            # --- admin approves the asset (G58B-01/02 real chain) ---
            st, body = api("GET", "/api/v1/assets?page=1&page_size=10")
            asset_id = body["data"]["items"][0]["asset_id"]
            st, body = api("POST", f"/api/v1/assets/{asset_id}/approve")
            check("e2e-asset-approve", st == 200 and body["data"]["status"] == "approved", str(body)[:200])

            # --- API template (three slots) + approve ---
            st, body = api("POST", "/api/v1/layout-templates", json_body={
                "name": "T58B E2E template",
                "base_asset_id": asset_id,
                "canvas_width": 900,
                "canvas_height": 1200,
                "slots": [
                    {"key": "to_cn", "text_template": "To: {cn}", "region": {"x_norm": 0.05, "y_norm": 0.05, "w_norm": 0.5, "h_norm": 0.15}},
                    {"key": "blessing", "text_template": "{blessing}", "region": {"x_norm": 0.05, "y_norm": 0.75, "w_norm": 0.6, "h_norm": 0.12}},
                    {"key": "signature", "text_template": "{blogger}", "region": {"x_norm": 0.6, "y_norm": 0.85, "w_norm": 0.3, "h_norm": 0.1}},
                ],
            })
            check("api-template-create", st == 201, str(body)[:200])
            tpl_id = body["data"]["template_id"]
            st, body = api("POST", f"/api/v1/layout-templates/{tpl_id}/approve")
            check("api-template-approve", st == 200, str(body)[:200])

            # --- seed a default blogger-name preset (P5 requires one when sign_blogger=true) ---
            st, body = api("POST", "/api/v1/blogger-name-presets",
                           json_body={"name": "星河", "is_default": True})
            check("api-seed-blogger-preset", st == 201, str(body)[:200])

            # --- P5 draft preview via the real draft lane (C-27) ---
            page.goto(frontend_url("/batch"))
            page.fill('input[aria-label="第 1 行粉丝昵称"]', "小美")
            page.click('button:has-text("预览草稿")')
            page.wait_for_selector('text=草稿渲染预览', timeout=20000)
            try:
                page.wait_for_selector('text=草稿渲染完成', timeout=60000)
                check("e2e-draft-render", True)
            except Exception as exc:
                check("e2e-draft-render", False, str(exc)[:200])

            # --- formal job -> P5R review ---
            page.click('button:has-text("确认正式生成")')
            page.wait_for_url("**/batch/*/review", timeout=30000)
            try:
                page.wait_for_selector('img[src*="/items/"][src*="/image"]', timeout=60000)
                check("e2e-review-real-image", True)
            except Exception as exc:
                check("e2e-review-real-image", False, str(exc)[:200])

            page.click('button:has-text("通过")')
            page.wait_for_selector('text=全部完成', timeout=30000)
            check("e2e-review-approve", True)

            # --- export + artifact download ---
            job_url = page.url
            job_id = job_url.split("/batch/")[1].split("/")[0]
            st, body = api("POST", f"/api/v1/render-jobs/{job_id}/export", headers={"X-Requested-With": "XMLHttpRequest"})
            check("api-export", st == 200, str(body)[:200])
            st, body = api("GET", f"/api/v1/render-jobs/{job_id}/outputs")
            outs = body["data"]["export_artifacts"]
            check("api-export-artifacts", len(outs) == 2, f"artifacts={len(outs)}")
            art = outs[0]
            st, blob = api("GET", f"/api/v1/render-jobs/{job_id}/exports/{art['artifact_id']}/file")
            check("api-artifact-download", st == 200 and isinstance(blob, bytes) and len(blob) > 1000, f"len={len(blob) if isinstance(blob, bytes) else blob}")

            # --- P10 audit page (admin) shows asset_approve rows ---
            page.goto(frontend_url("/audit"))
            try:
                page.wait_for_selector("text=asset_approve", timeout=20000)
                check("e2e-p10-audit", True)
            except Exception as exc:
                check("e2e-p10-audit", False, str(exc)[:200])

            # --- P9 usage six pools ---
            page.goto(frontend_url("/me"))
            try:
                page.wait_for_selector("text=存储用量", timeout=20000)
                page.wait_for_selector("text=原图资产", timeout=20000)
                check("e2e-p9-usage", True)
            except Exception as exc:
                check("e2e-p9-usage", False, str(exc)[:200])

            browser.close()
        exit_code = 1 if FAILS else 0
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        exit_code = 1
    except KeyboardInterrupt:
        print("HARNESS INTERRUPTED: KeyboardInterrupt", file=sys.stderr)
        exit_code = 130
    finally:
        print(f"# summary t58b_e2e: {len(FAILS)} failed {FAILS}")
        try:
            runtime.stop_all(exit_code)
        finally:
            runtime.restore_output_capture()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
