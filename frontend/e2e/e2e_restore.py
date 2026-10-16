# -*- coding: utf-8 -*-
"""B1-12 E2E — P11 restore full chain (depends on B1-06 done state).

Reuses the e2e_full_chain.py harness pattern (safe_url loopback pinning,
self-managed temp DB/storage, configurable ports and Popen-owned cleanup).

Chain under test (manifest B1-12):
  admin UI login -> create backup on P11 -> row visible in list -> verify
  (checksum) -> RESTORE text gate (locked until exact "RESTORE") -> execute
  restore -> CC-17: all sessions revoked (old cookie 401, UI back to /login)
  -> admin re-login succeeds.

Content-correctness checks (C-59 lesson: existence != correctness):
  - restore swaps pool dirs from the backup bundle, so an asset uploaded
    BEFORE the backup (asset A) must still exist on disk after restore while
    an asset uploaded AFTER the backup (asset B) must be gone from the
    storage pool (its DB row survives — the DB is not reverted, D-T70-2 —
    which is exactly why the disk, not the API list, is the oracle here).
  - audit-logs must contain a `restore` row with object_ref == backup_id.
"""
import ipaddress
import json as _json
import os
import secrets
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

TMP = Path(tempfile.mkdtemp(prefix="b1_restore_e2e_"))
DB = (TMP / "b1_restore_e2e.db").as_posix()
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


def png_bytes(w=600, h=800, color=(238, 242, 246)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
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


def pool_files() -> set:
    """Relative paths of every file under user 1's storage pool dir."""
    base = Path(STORAGE) / "1"
    out = set()
    if base.is_dir():
        for fp in base.rglob("*"):
            if fp.is_file():
                out.add(fp.relative_to(base).as_posix())
    return out


def main() -> int:
    from playwright.sync_api import sync_playwright

    runtime = RunContext(REPO, "restore", BACKEND_PORT, FRONTEND_PORT)
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

        # first registered user = admin (same seeding convention as full_chain)
        pwd = "B1RST-" + secrets.token_urlsafe(12) + "!7Aa"
        st, body = api("POST", "/api/v1/auth/register", json_body={
            "username": "adminR", "password": pwd, "display_name": "adminR", "invite_code": "seed-admin"})
        check("api-seed-invite-admin", st == 201, str(body)[:200])

        with sync_playwright() as p:
            browser = p.chromium.launch(args=low_memory_browser_args())
            page = browser.new_page(viewport={"width": 390, "height": 844})

            # --- UI login (admin) -> must_change forces /change-password ---
            page.goto(frontend_url("/login"))
            page.wait_for_selector("#login-username", timeout=30000)
            page.fill("#login-username", "adminR")
            page.fill("#login-password", pwd)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            newpwd = "B1RST-New-" + secrets.token_urlsafe(10) + "!8Bb"
            page.wait_for_selector('input[type="password"]', timeout=30000)
            page.wait_for_timeout(500)
            pwd_inputs = page.query_selector_all('input[type="password"]')
            for i, val in enumerate((pwd, newpwd, newpwd)):
                if i < len(pwd_inputs):
                    pwd_inputs[i].fill(val)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            deadline = time.time() + 30
            while time.time() < deadline:
                if page.url.rstrip("/").endswith(f":{FRONTEND_PORT}"):
                    break
                page.wait_for_timeout(300)
            check("e2e-ui-login-admin", "change-password" not in page.url, page.url)
            page.wait_for_timeout(500)
            bind_api_session(page)

            # --- asset A uploaded BEFORE the backup (must survive restore) ---
            png_a = TMP / "asset_a.png"
            png_a.write_bytes(png_bytes(color=(200, 220, 240)))
            page.goto(frontend_url("/assets"))
            page.set_input_files('input[type="file"]', str(png_a))
            page.wait_for_selector('button[aria-label^="资产"]', timeout=20000)
            check("e2e-upload-asset-a", True)
            files_before_backup = pool_files()
            check("e2e-asset-a-on-disk", len(files_before_backup) >= 1, str(files_before_backup))

            # --- P11: create backup -> row visible in list ---
            page.goto(frontend_url("/admin/backup"))
            try:
                page.wait_for_selector("text=暂无备份快照", timeout=20000)
                check("e2e-p11-list-empty", True)
            except Exception as exc:
                check("e2e-p11-list-empty", False, str(exc)[:200])
            page.click('button:has-text("立即创建全量冷备份")')
            try:
                page.wait_for_selector("text=succeeded", timeout=60000)
                check("e2e-p11-create-backup", True)
            except Exception as exc:
                check("e2e-p11-create-backup", False, str(exc)[:200])

            st, body = api("GET", "/api/v1/backups")
            items = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
            backup_id = items[0]["backup_id"] if items else ""
            check("e2e-p11-list-visible", st == 200 and items and items[0]["status"] == "succeeded",
                  f"st={st} n={len(items)}")

            # --- P11: verify (checksum) -> toast 校验通过 ---
            page.click('button:has-text("校验")')
            try:
                page.wait_for_selector("text=校验通过", timeout=30000)
                check("e2e-p11-verify-ok", True)
            except Exception as exc:
                check("e2e-p11-verify-ok", False, str(exc)[:200])
            st, body = api("POST", f"/api/v1/backups/{backup_id}/verify")
            check("e2e-p11-verify-api", st == 200 and isinstance(body, dict)
                  and body.get("data", {}).get("status") == "checksum_ok", f"st={st} {str(body)[:150]}")

            # --- asset B uploaded AFTER the backup (must be wiped by restore) ---
            png_b = TMP / "asset_b.png"
            png_b.write_bytes(png_bytes(color=(240, 200, 200)))
            page.goto(frontend_url("/assets"))
            page.set_input_files('input[type="file"]', str(png_b))
            page.wait_for_selector('button[aria-label^="资产"]', timeout=20000)
            page.wait_for_timeout(800)
            files_after = pool_files()
            marker = sorted(files_after - files_before_backup)
            check("e2e-upload-asset-b", len(files_after) > len(files_before_backup) and len(marker) >= 1,
                  f"before={sorted(files_before_backup)} after={sorted(files_after)}")

            # --- capture the admin session cookie (for the CC-17 401 probe) ---
            sess = API_SESSION_COOKIE
            check("e2e-capture-session-cookie", bool(sess), "real UI cookie captured; value redacted")

            # --- RESTORE text gate ---
            page.goto(frontend_url("/admin/backup"))
            page.wait_for_selector('button:has-text("恢复快照")', timeout=20000)
            page.click('button:has-text("恢复快照")')
            page.wait_for_selector("#restore-gate", timeout=20000)
            confirm_btn = page.query_selector('button:has-text("确认恢复")')
            check("e2e-restore-gate-locked", confirm_btn is not None and confirm_btn.is_disabled(),
                  "confirm button state with empty gate input")
            page.fill("#restore-gate", "restore")
            page.wait_for_timeout(200)
            check("e2e-restore-gate-case-sensitive", confirm_btn is not None and confirm_btn.is_disabled(),
                  "lowercase 'restore' must not unlock")
            page.fill("#restore-gate", "RESTORE")
            page.wait_for_timeout(200)
            check("e2e-restore-gate-unlock", confirm_btn is not None and not confirm_btn.is_disabled(),
                  "exact RESTORE must unlock")

            # --- execute restore -> CC-17 back to /login ---
            page.click('button:has-text("确认恢复")')
            try:
                page.wait_for_url("**/login", timeout=120000)
                check("e2e-restore-executed", True, page.url)
            except Exception as exc:
                check("e2e-restore-executed", False, str(exc)[:200] + " url=" + page.url)

            # --- CC-17: old session revoked (API-level 401 on /auth/me) ---
            page.wait_for_timeout(1000)
            if sess:
                st, body = api("GET", "/api/v1/auth/me", headers={"Cookie": f"session={sess}"})
                check("e2e-cc17-old-session-401", st == 401, f"st={st} {str(body)[:150]}")
            else:
                check("e2e-cc17-old-session-401", False, "no session cookie captured")

            # --- content correctness: asset B wiped, asset A survives on disk ---
            files_restored = pool_files()
            check("e2e-cc17-marker-gone", all(m not in files_restored for m in marker),
                  f"marker={marker} restored={sorted(files_restored)}")
            check("e2e-asset-a-survives", files_before_backup.issubset(files_restored),
                  f"before={sorted(files_before_backup)} restored={sorted(files_restored)}")

            # --- admin re-login succeeds ---
            page.goto(frontend_url("/login"))
            page.wait_for_selector("#login-username", timeout=30000)
            page.fill("#login-username", "adminR")
            page.fill("#login-password", newpwd)
            page.wait_for_timeout(300)
            page.click('button[type="submit"]')
            try:
                page.wait_for_url("**/change-password", timeout=15000)
                # must_change is false now; if we land here the login itself still worked
                check("e2e-relogin-admin", True, "relogin reached router")
            except Exception:
                check("e2e-relogin-admin", "/login" not in page.url, page.url)
            new_session = bind_api_session(page)
            check("e2e-session-refreshed-after-restore", new_session != sess,
                  "fresh UI session cookie differs from revoked cookie")
            page.goto(frontend_url("/admin/backup"))
            try:
                page.wait_for_selector('[data-test="worker-cards"]', timeout=20000)
                check("e2e-relogin-admin-page", True)
            except Exception as exc:
                check("e2e-relogin-admin-page", False, str(exc)[:200])

            # --- audit row: action=restore, object_ref == backup_id ---
            st, body = api("GET", f"/api/v1/audit-logs?action=restore&object_ref={backup_id}")
            aitems = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
            check("e2e-audit-restore-row", st == 200 and len(aitems) >= 1,
                  f"st={st} n={len(aitems)}")

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
        print(f"# summary b1_restore_e2e: {len(FAILS)} failed {FAILS}")
        try:
            runtime.stop_all(exit_code)
        finally:
            runtime.restore_output_capture()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
