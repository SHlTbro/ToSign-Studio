# -*- coding: utf-8 -*-
"""D08 P1 critical direct-route, refresh, auth and role smoke."""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

import e2e_full_chain as flow
from runtime_guard import _sha256_file, frontend_source_sha256


def fixture_png() -> bytes:
    import io
    image = Image.new("RGB", (540, 960), (18, 18, 18))
    ImageDraw.Draw(image).line([(180, 160), (360, 160), (270, 160), (270, 760)], fill="white", width=28)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def path_of(page) -> str:
    from urllib.parse import urlsplit
    return urlsplit(page.url).path


def login(page, username: str, password: str, new_password: str) -> str:
    page.goto(flow.frontend_url("/login"), wait_until="domcontentloaded")
    page.fill("#login-username", username)
    page.fill("#login-password", password)
    page.click('button[type="submit"]')
    page.wait_for_function("['/change-password', '/'].includes(window.location.pathname)", timeout=30000)
    if path_of(page) != "/change-password":
        return password
    fields = page.locator('input[type="password"]')
    fields.nth(0).fill(password)
    fields.nth(1).fill(new_password)
    fields.nth(2).fill(new_password)
    page.click('button[type="submit"]')
    page.wait_for_function("window.location.pathname === '/'", timeout=30000)
    return new_password


def main() -> int:
    from playwright.sync_api import sync_playwright

    flow.FAILS = []
    flow.API_SESSION_COOKIE = None
    runtime = flow.RunContext(flow.REPO, "d08-critical-routes", flow.BACKEND_PORT, flow.FRONTEND_PORT)
    env = dict(os.environ, YY_DB_URL=f"sqlite:///{flow.DB}", YY_STORAGE_ROOT=flow.STORAGE,
               YY_AUTH_STRICT="1", YY_E2E_BACKEND_PYTHON=str(flow.VENV_PY))
    if os.environ.get("YY_E2E_BACKEND_PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["YY_E2E_BACKEND_PYTHONPATH"]
    gate_path = flow.REPO / ".ai-hub" / "parallel-lines" / "line-b" / "gates" / "B-GATE-RELEASE.json"
    result = {"schema": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "D08 17 route direct/refresh + unauth/admin-role smoke; no exhaustive state matrix",
              "gate_sha256": _sha256_file(gate_path),
              "frontend_source_sha256": frontend_source_sha256(flow.REPO / "frontend"),
              "routes": [], "page_errors": [], "server_5xx": [], "checks": {}}
    errors: list[str] = []
    server_errors: list[str] = []
    browser = None
    exit_code = 1
    runtime.install_output_capture()
    try:
        runtime.acquire()
        backend = runtime.start("backend", [str(flow.VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(flow.BACKEND_PORT)], flow.REPO, env, flow.BACKEND_PORT)
        node = flow.shutil.which("node")
        vite = flow.REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("direct Node/Vite executable is unavailable")
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(flow.FRONTEND_PORT), "--strictPort", "--host", "127.0.0.1"], flow.REPO / "frontend", env, flow.FRONTEND_PORT)
        flow.check("backend-owned-listener", runtime.wait_owned_listener("backend"))
        flow.check("backend-health", backend.poll() is None and flow.wait_http(flow.backend_url("/api/v1/health")))
        flow.check("seed-invites", flow.seed_invites(env))
        flow.check("frontend-owned-listener", runtime.wait_owned_listener("frontend"))
        flow.check("frontend-health", frontend.poll() is None and flow.wait_http(flow.frontend_url("/")))
        if flow.FAILS:
            raise RuntimeError(f"service preflight failed: {flow.FAILS}")

        admin_pass = "D08-Admin-" + secrets.token_urlsafe(9) + "!Aa7"
        viewer_pass = "D08-Viewer-" + secrets.token_urlsafe(9) + "!Aa7"
        st, body = flow.api("POST", "/api/v1/auth/register", json_body={"username": "d08admin", "password": admin_pass, "display_name": "D08 Admin", "invite_code": "seed-admin"})
        flow.check("register-admin", st == 201, f"status={st}")
        st, body = flow.api("POST", "/api/v1/auth/register", json_body={"username": "d08viewer", "password": viewer_pass, "display_name": "D08 Viewer", "invite_code": "seed-inv"})
        flow.check("register-viewer", st == 201, f"status={st}")
        if flow.FAILS:
            raise RuntimeError(f"account setup failed: {flow.FAILS}")

        routes = ["/login", "/register", "/change-password", "/", "/assets",
                  "/assets/{asset_id}/annotate", "/extract/{asset_id}", "/batch",
                  "/batch/d08-missing-job/review", "/styles", "/styles/new", "/training",
                  "/blessings", "/me", "/audit", "/admin/backup", "/404"]
        with sync_playwright() as p:
            browser = p.chromium.launch(args=flow.low_memory_browser_args())
            admin_context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=1)
            admin = admin_context.new_page()
            admin.on("pageerror", lambda exc: errors.append(str(exc)[:500]))
            admin.on("response", lambda r: server_errors.append(f"{r.status} {r.url}"[:500]) if r.status >= 500 else None)
            admin_next = "D08-Admin-New-" + secrets.token_urlsafe(8) + "!Bb8"
            login(admin, "d08admin", admin_pass, admin_next)
            flow.bind_api_session(admin)

            # Create a real owned COS asset for annotate/extract direct paths.
            fixture = runtime.run_dir / "d08-route-asset.png"
            fixture.write_bytes(fixture_png())
            admin.goto(flow.frontend_url("/assets"), wait_until="domcontentloaded")
            admin.set_input_files('input[type="file"]', str(fixture))
            admin.wait_for_selector('button[aria-label^="资产"]', timeout=30000)
            st, pending_body = flow.api("GET", "/api/v1/assets?type=cos&status=pending&page=1&page_size=100")
            pending = pending_body.get("data", {}).get("items", []) if isinstance(pending_body, dict) else []
            flow.check("route-asset-uploaded", st == 200 and len(pending) == 1, f"status={st}, count={len(pending)}")
            if len(pending) != 1:
                raise RuntimeError("could not resolve route asset")
            asset_id = pending[0]["asset_id"]
            st, approved = flow.api("POST", f"/api/v1/assets/{asset_id}/approve")
            flow.check("route-asset-approved", st == 200, f"status={st}")

            concrete_routes = [route.format(asset_id=asset_id) for route in routes]
            for route in concrete_routes:
                admin.goto(flow.frontend_url(route), wait_until="domcontentloaded", timeout=30000)
                admin.wait_for_timeout(200)
                initial_path = path_of(admin)
                admin.reload(wait_until="domcontentloaded", timeout=30000)
                admin.wait_for_timeout(200)
                refreshed_path = path_of(admin)
                expected_path = "/" if route in {"/login", "/register"} else route
                passed = initial_path == expected_path and refreshed_path == expected_path
                flow.check(f"admin-direct-refresh:{route}", passed, f"direct={initial_path}; refresh={refreshed_path}")
                result["routes"].append({"path": route, "direct_path": initial_path, "refresh_path": refreshed_path, "pass": passed})
            flow.check("17-explicit-routes-covered", len(concrete_routes) == 17, str(len(concrete_routes)))

            # No-session access to every protected route must return to login
            # with a redirect retaining the requested route.
            guest = browser.new_page(viewport={"width": 390, "height": 844})
            guest.on("pageerror", lambda exc: errors.append(str(exc)[:500]))
            guest.on("response", lambda r: server_errors.append(f"{r.status} {r.url}"[:500]) if r.status >= 500 else None)
            protected = [r for r in concrete_routes if r not in {"/login", "/register", "/change-password", "/404"}]
            for route in protected:
                guest.goto(flow.frontend_url(route), wait_until="domcontentloaded", timeout=30000)
                try:
                    guest.wait_for_function("window.location.pathname === '/login'", timeout=10000)
                except Exception:
                    pass
                ok = path_of(guest) == "/login" and "redirect=" in guest.url
                flow.check(f"unauth-guard:{route}", ok, guest.url)

            # The regular account must see the intentional 404 for admin-only routes.
            viewer_context = browser.new_context(viewport={"width": 390, "height": 844})
            viewer = viewer_context.new_page()
            viewer.on("pageerror", lambda exc: errors.append(str(exc)[:500]))
            viewer.on("response", lambda r: server_errors.append(f"{r.status} {r.url}"[:500]) if r.status >= 500 else None)
            viewer_next = "D08-Viewer-New-" + secrets.token_urlsafe(8) + "!Cc9"
            login(viewer, "d08viewer", viewer_pass, viewer_next)
            for route in ("/audit", "/admin/backup"):
                viewer.goto(flow.frontend_url(route), wait_until="domcontentloaded")
                try:
                    viewer.wait_for_function("window.location.pathname === '/404'", timeout=10000)
                except Exception:
                    pass
                ok = path_of(viewer) == "/404"
                flow.check(f"nonadmin-guard:{route}", ok, viewer.url)
            # Isolate browser-history checks from prior guard redirects. These
            # six are the explicitly prioritized parameterized/admin routes.
            history_page = admin_context.new_page()
            history_page.on("pageerror", lambda exc: errors.append(str(exc)[:500]))
            history_page.on("response", lambda r: server_errors.append(f"{r.status} {r.url}"[:500]) if r.status >= 500 else None)
            back_routes = [f"/assets/{asset_id}/annotate", f"/extract/{asset_id}", "/batch",
                           "/batch/d08-missing-job/review", "/audit", "/admin/backup"]
            for route in back_routes:
                history_page.goto(flow.frontend_url("/me"), wait_until="domcontentloaded")
                history_page.goto(flow.frontend_url(route), wait_until="domcontentloaded")
                arrived = path_of(history_page) == route
                history_page.go_back(wait_until="domcontentloaded")
                try:
                    history_page.wait_for_function("window.location.pathname === '/me'", timeout=10000)
                except Exception:
                    pass
                returned = path_of(history_page) == "/me"
                flow.check(f"back-navigation:{route}", arrived and returned,
                           f"arrived={arrived}; returned={returned}; url={history_page.url}")

            result["page_errors"] = errors
            result["server_5xx"] = server_errors
            flow.check("no-uncaught-page-errors", not errors, str(errors))
            flow.check("no-unexplained-server-5xx", not server_errors, str(server_errors))
            result["runtime_pid_backend"] = backend.pid
            result["runtime_pid_frontend"] = frontend.pid
            result["checks"] = {"failed": list(flow.FAILS)}
            exit_code = 1 if flow.FAILS else 0
            admin_context.close()
            viewer_context.close()
            guest.close()
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
        result["page_errors"] = errors
        result["server_5xx"] = server_errors
        exit_code = 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            runtime.stop_all(exit_code)
        finally:
            runtime.restore_output_capture()
    result["runner_exit_code"] = exit_code
    result["runtime"] = runtime.fingerprints("stopped", exit_code)
    result["ended_at"] = datetime.now(timezone.utc).isoformat()
    result["checks"] = {"failed": list(flow.FAILS)}
    (runtime.run_dir / "d08-critical-routes-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (runtime.run_dir / "d08-critical-routes-receipt.md").write_text(
        "# D08 critical browser route smoke\n\n"
        f"- Result: **{'PASS' if exit_code == 0 else 'FAIL'}**; runner exit `{exit_code}`.\n"
        f"- Explicit routes: `{len(result.get('routes', []))}/17`; unauth protected-route guard and non-admin admin-route guard exercised.\n"
        f"- Release Gate SHA-256: `{result.get('gate_sha256')}`; frontend source fingerprint: `{result.get('frontend_source_sha256')}`.\n"
        f"- Runtime evidence: `{runtime.run_dir}`.\n"
        "- This is a critical-path smoke, not exhaustive state, keyboard, focus, copy or visual coverage.\n", encoding="utf-8")
    print(f"# summary d08_critical_routes: {len(flow.FAILS)} failed {flow.FAILS}")
    print(f"D08_RESULT={'PASS' if exit_code == 0 else 'FAIL'} RUNNER_EXIT={exit_code} RUN_DIR={runtime.run_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
