# -*- coding: utf-8 -*-
"""P7 browser integration against the published package backend.

The one synthetic glyph in this isolated run is an explicitly permitted
M1_FROZEN engineering fixture. It is not M2 training data or style-quality
evidence. All application requests go to the package backend over HTTP; only
the run database and storage are isolated under this E2E run directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from runtime_guard import (RunContext, low_memory_browser_args,
                           session_cookie_value, with_session_cookie,
                           _port_listener_pids)

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "installer" / "package" / "yy-studio"
PKG_PY = PKG / "runtime" / "Scripts" / "python.exe"
ML_PY = REPO / ".venv-ml" / "Scripts" / "python.exe"
BACKEND_PORT = int(os.environ.get("YY_E2E_BACKEND_PORT", "8101"))
FRONTEND_PORT = int(os.environ.get("YY_E2E_FRONTEND_PORT", "5173"))
RELEASE_GATE = REPO / ".ai-hub" / "parallel-lines" / "line-b" / "gates" / "B-GATE-RELEASE.json"
RELEASE_GATE_SHA = hashlib.sha256(RELEASE_GATE.read_bytes()).hexdigest()
FAILS: list[str] = []
API_TRACE: list[dict] = []
SESSION_COOKIE: str | None = None


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"))
    if not ok:
        FAILS.append(name)


def api(method: str, path: str, *, payload=None, headers=None, timeout=30):
    global SESSION_COOKIE
    url = f"http://127.0.0.1:{BACKEND_PORT}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request_headers = with_session_cookie(headers, SESSION_COOKIE)
    if SESSION_COOKIE and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        request_headers.setdefault("X-Requested-With", "XMLHttpRequest")
    for key, value in request_headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
        content_type = error.headers.get("Content-Type", "")
    try:
        body = json.loads(raw) if "json" in content_type else raw
    except (ValueError, UnicodeDecodeError):
        body = raw
    API_TRACE.append({"method": method, "path": path, "status": status,
                      "body": body if isinstance(body, dict) else {"bytes": len(body)}})
    return status, body


def data(body):
    return body.get("data", body) if isinstance(body, dict) else body


def make_fixture_png() -> bytes:
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (256, 256), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.line([(55, 40), (190, 52)], fill=(15, 15, 15, 255), width=13)
    draw.line([(124, 35), (117, 214)], fill=(15, 15, 15, 255), width=13)
    draw.line([(57, 118), (194, 132)], fill=(20, 20, 20, 255), width=11)
    draw.ellipse((77, 147, 177, 225), outline=(15, 15, 15, 255), width=11)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def upload_fixture(png: bytes):
    boundary = "----P7Fixture" + secrets.token_hex(8)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"p7-m1-fixture.png\"\r\n"
            "Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{BACKEND_PORT}/api/v1/training-sets/images", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Cookie", f"session={SESSION_COOKIE}")
    request.add_header("X-Requested-With", "XMLHttpRequest")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    parsed = json.loads(raw)
    API_TRACE.append({"method": "POST", "path": "/api/v1/training-sets/images",
                      "status": status, "body": parsed})
    return status, parsed


def seed_isolated_db(env: dict, run_dir: Path) -> bool:
    code = (
        "from app.core.db import Base,get_engine,sessionmaker\n"
        "from app.domains.auth.models import Invite\n"
        "Base.metadata.create_all(bind=get_engine())\n"
        "s=sessionmaker(bind=get_engine())(); s.add(Invite(code='p7-e2e-first',uses_total=2,used_count=0,created_at='2026-09-28T00:00:00+00:00')); s.commit(); s.close()\n"
        "print('P7 isolated invite ready')\n"
    )
    result = subprocess.run([str(PKG_PY), "-X", "utf8", "-c", code], cwd=PKG,
                            env=env, capture_output=True, text=True, timeout=120)
    print(f"seed_db exit_code={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr[-500:]}")
    return result.returncode == 0 and "P7 isolated invite ready" in result.stdout


def wait_for_listener(port: int, launcher: subprocess.Popen, timeout: float = 60) -> set[int]:
    """The packaged Windows venv launcher delegates to its base Python PID."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owners = _port_listener_pids(port)
        if owners:
            if launcher.poll() is not None:
                raise RuntimeError(f"launcher exited before listener was ready (rc={launcher.returncode})")
            return owners
        if launcher.poll() is not None:
            return set()
        time.sleep(0.2)
    return set()


def main() -> int:
    global SESSION_COOKIE
    from playwright.sync_api import sync_playwright

    if not PKG_PY.is_file() or not ML_PY.is_file():
        print("required package or M1_FROZEN Python runtime is absent", file=sys.stderr)
        return 2
    runtime = RunContext(REPO, "p7-style-run", BACKEND_PORT, FRONTEND_PORT)
    temp_root = runtime.run_dir / "tmp"
    temp_root.mkdir()
    os.environ["TEMP"] = str(temp_root)
    os.environ["TMP"] = str(temp_root)
    os.environ["YY_E2E_BACKEND_PYTHON"] = str(PKG_PY)
    os.environ["YY_E2E_BACKEND_PYTHONPATH"] = str(REPO)
    db_path = (runtime.run_dir / "p7.db").as_posix()
    storage = runtime.run_dir / "storage"
    env = dict(os.environ)
    env.update({
        "YY_DB_URL": f"sqlite:///{db_path}",
        "YY_STORAGE_ROOT": str(storage),
        "YY_PORT": str(BACKEND_PORT),
        "YY_HOST": "127.0.0.1",
        "YY_AUTH_STRICT": "1",
        "YY_STYLE_M1_PYTHON": str(ML_PY),
        "YY_STYLE_ENGINE_TIMEOUT_S": "240",
        "YY_FRONTEND_DIST": "",
        "PYTHONPATH": str(REPO),
        "YY_E2E_BACKEND_PYTHON": str(PKG_PY),
        "YY_E2E_BACKEND_PYTHONPATH": str(REPO),
    })
    exit_code = 1
    runtime.install_output_capture()
    try:
        runtime.acquire()
        backend = runtime.start("backend", [str(PKG_PY), "-m", "uvicorn", "app.main:app",
                                            "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
                                PKG, env, BACKEND_PORT)
        node = shutil.which("node")
        vite = REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("Node/Vite executable not found")
        front_env = dict(env)
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(FRONTEND_PORT),
                                               "--strictPort", "--host", "127.0.0.1"],
                                 REPO / "frontend", front_env, FRONTEND_PORT)
        backend_listener_pids = wait_for_listener(BACKEND_PORT, backend, 90)
        if not backend_listener_pids:
            raise RuntimeError(f"package backend did not listen; launcher_pid={backend.pid}; rc={backend.poll()}")
        if not runtime.wait_owned_listener("frontend", 90):
            raise RuntimeError(f"Vite failed to own port; pid={frontend.pid}; rc={frontend.poll()}")
        check("package backend and Vite own their reserved listeners",
              backend.poll() is None and frontend.poll() is None,
              f"backend_launcher_pid={backend.pid}; backend_listener_pids={sorted(backend_listener_pids)}; frontend_pid={frontend.pid}")
        check("isolated invite seed", seed_isolated_db(env, runtime.run_dir))

        username = "p7e2e" + secrets.token_hex(3)
        initial_password = "P7-Start-" + secrets.token_urlsafe(8) + "7Aa"
        status, body = api("POST", "/api/v1/auth/register", payload={
            "username": username, "password": initial_password,
            "display_name": username, "invite_code": "p7-e2e-first"})
        check("register isolated owner through package API", status == 201, str(body)[:300])
        if status != 201:
            raise RuntimeError("cannot register isolated owner")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=low_memory_browser_args())
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            console_errors: list[str] = []
            browser_responses: list[dict] = []
            login_completed = False
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            def record_response(response):
                if "/api/v1/style-runs/" not in response.url and response.status < 400:
                    return
                try:
                    body = response.json()
                except Exception:
                    body = None
                browser_responses.append({"method": response.request.method, "url": response.url,
                                          "status": response.status, "authenticated": login_completed,
                                          "body": body})
            page.on("response", record_response)
            page.goto(f"http://127.0.0.1:{FRONTEND_PORT}/login", wait_until="domcontentloaded")
            page.fill("#login-username", username)
            page.fill("#login-password", initial_password)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            next_password = "P7-New-" + secrets.token_urlsafe(8) + "8Bb"
            page.fill("#old-password", initial_password)
            page.fill("#new-password", next_password)
            page.fill("#confirm-password", next_password)
            page.click('button[type="submit"]')
            page.wait_for_url(lambda url: "/change-password" not in url, timeout=30000)
            SESSION_COOKIE = session_cookie_value(page.context.cookies())
            login_completed = True
            check("real login and mandatory password change", "/change-password" not in page.url)

            png = make_fixture_png()
            upload_status, upload_body = upload_fixture(png)
            image_id = data(upload_body).get("items", [{}])[0].get("image_id") if upload_status == 200 else None
            check("upload isolated synthetic M1_FROZEN fixture through package API", upload_status == 200 and image_id is not None,
                  str(upload_body)[:300])
            if image_id is None:
                raise RuntimeError("fixture upload failed")
            status, style_body = api("POST", "/api/v1/styles", payload={
                "source_type": "HANDWRITING_ASSET", "name": "P7 M1 engineering fixture"})
            style_id = data(style_body).get("style_id") if status == 201 else None
            check("create isolated style over package API", status == 201 and style_id is not None,
                  str(style_body)[:300])
            if style_id is None:
                raise RuntimeError("fixture style create failed")
            status, sample_body = api("POST", f"/api/v1/styles/{style_id}/samples", payload={
                "source_training_image_id": image_id, "text_label": "永",
                "touchup_confirmed": True})
            sample_id = data(sample_body).get("sample_id") if status == 201 else None
            check("register linked, confirmed engineering StyleSample", status == 201 and sample_id is not None,
                  str(sample_body)[:300])
            if sample_id is None:
                raise RuntimeError("fixture StyleSample registration failed")
            # This is a temporary, isolated fixture confirmation to exercise the
            # M1_FROZEN reader contract; it is not represented as owner-approved GT.
            page.goto(f"http://127.0.0.1:{FRONTEND_PORT}/training", wait_until="domcontentloaded")
            page.wait_for_selector("text=真实推理运行", timeout=30000)
            page.locator("h2").filter(has_text="StyleSample · P7 M1 engineering fixture").wait_for(timeout=30000)
            page.get_by_text("可用于 M1").last.wait_for(timeout=30000)
            create = page.get_by_role("button", name="创建新运行")
            create.click()
            page.get_by_role("button", name="启动真实运行").wait_for(timeout=30000)
            run_id = int(page.locator("text=/已创建 · #\\d+/").inner_text().split("#")[-1])
            status, created = api("GET", f"/api/v1/style-runs/{run_id}")
            check("P7 create run returned real service run", status == 200 and data(created).get("status") == "created",
                  str(created)[:300])
            state_sequence: list[str] = [data(created).get("status", "unknown")]
            page.get_by_role("button", name="启动真实运行").click()
            page.wait_for_timeout(250)
            start_response = next((item for item in reversed(browser_responses)
                                   if item["method"] == "POST" and item["url"].endswith(f"/style-runs/{run_id}/start")), None)
            if start_response and isinstance(start_response.get("body"), dict):
                started_status = data(start_response["body"]).get("status")
                if started_status:
                    state_sequence.append(started_status)
            page.wait_for_timeout(500)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(f"text=/#\\s*{run_id}/", timeout=30000)
            page.wait_for_selector("text=服务端进度", timeout=30000)
            deadline = time.time() + 260
            while time.time() < deadline:
                status, run_body = api("GET", f"/api/v1/style-runs/{run_id}")
                current = data(run_body).get("status") if status == 200 else "read-error"
                if not state_sequence or state_sequence[-1] != current:
                    state_sequence.append(current)
                if current in {"succeeded", "failed", "cancelled"}:
                    break
                page.wait_for_timeout(1000)
            status, progress_body = api("GET", f"/api/v1/style-runs/{run_id}/progress")
            status_m, metrics_body = api("GET", f"/api/v1/style-runs/{run_id}/metrics")
            status_e, eval_body = api("GET", f"/api/v1/style-runs/{run_id}/evaluate")
            terminal = state_sequence[-1] if state_sequence else "missing"
            check("create/start/queued then refresh recovers same real run", run_id > 0 and "queued" in state_sequence and
                  f"#{run_id}" in page.locator("main").inner_text(), str(state_sequence))
            check("real M1 worker reaches succeeded", terminal == "succeeded", str(state_sequence))
            check("progress and metrics are service responses", status == 200 and status_m == 200 and
                  data(progress_body).get("status") == "succeeded" and data(metrics_body).get("state") == "available",
                  f"progress={str(progress_body)[:180]} metrics={str(metrics_body)[:180]}")
            check("M1 evaluate truth remains unavailable, not a fabricated quality verdict",
                  status_e == 200 and data(eval_body).get("state") == "unavailable" and
                  data(eval_body).get("reason") == "EVAL_NOT_PRODUCED", str(eval_body)[:300])
            page.wait_for_selector("text=人工盲评与发布门", timeout=30000)
            with page.expect_response(lambda response: response.request.method == "POST" and
                                      response.url.endswith(f"/style-runs/{run_id}/publish"), timeout=30000) as publish_info:
                page.get_by_role("button", name="请求发布（由服务端复核盲评与 artifact）").click()
            publish_response = publish_info.value
            page.locator('[role="alert"]').filter(has_text="BLOCKED_OWNER").wait_for(timeout=30000)
            check("P7 shows server publish error rather than a local success", publish_response.status == 409,
                  f"status={publish_response.status}")
            blocked = any(row["method"] == "POST" and row["url"].endswith(f"/style-runs/{run_id}/publish")
                          and row["status"] == 409 and "RUN_BLIND_REVIEW_REQUIRED" in json.dumps(row.get("body"))
                          for row in browser_responses)
            check("publish without human blind review is blocked by service", blocked)

            # Separate run verifies the cancellable branch without fabricating a
            # terminal state; start and cancel immediately while it is queued.
            page.get_by_role("button", name="刷新/恢复").wait_for(timeout=30000)
            page.get_by_role("button", name="创建新运行").click()
            page.get_by_role("button", name="启动真实运行").wait_for(timeout=30000)
            cancel_id_text = page.locator("main").inner_text()
            cancel_run_id = int(cancel_id_text.split("已创建 · #")[-1].split()[0])
            page.get_by_role("button", name="启动真实运行").click()
            cancel_button = page.get_by_role("button", name="取消运行")
            cancel_button.wait_for(timeout=10000)
            cancel_button.click()
            page.wait_for_selector("text=已取消", timeout=30000)
            status, cancel_body = api("GET", f"/api/v1/style-runs/{cancel_run_id}")
            check("second real run cancels through UI and service", status == 200 and
                  data(cancel_body).get("status") == "cancelled", str(cancel_body)[:300])
            page.screenshot(path=str(runtime.run_dir / "p7-training-final.png"), full_page=True)
            unexpected_http_errors = [row for row in browser_responses if row["status"] >= 500]
            unauthorized_after_login = [row for row in browser_responses
                                        if row["status"] == 401 and row["authenticated"]]
            expected_http_console = all(
                (("401 (Unauthorized)" in message and any(row["status"] == 401 and
                  row["url"].endswith("/api/v1/auth/me") and not row["authenticated"] for row in browser_responses)) or
                 ("409 (Conflict)" in message and any(row["status"] == 409 and
                  row["url"].endswith(f"/style-runs/{run_id}/publish") for row in browser_responses)))
                for message in console_errors if "Failed to load resource" in message
            )
            unexpected_console_errors = [message for message in console_errors
                                         if "Failed to load resource" not in message]
            check("browser has no uncaught JS errors, unexplained console errors, or HTTP 5xx",
                  not unexpected_console_errors and expected_http_console and
                  not unexpected_http_errors and not unauthorized_after_login,
                  f"console={console_errors[:8]} http5xx={unexpected_http_errors[:4]} unauthorized_after_login={unauthorized_after_login[:2]}")
            browser.close()

        evidence = {
            "schema": 1,
            "run_id": runtime.run_id,
            "runner_exit_code": 1 if FAILS else 0,
            "backend_release_gate_sha256": RELEASE_GATE_SHA,
            "runtime": runtime.fingerprints("evidence", None),
            "package_backend_launcher_pid": backend.pid,
            "package_backend_listener_pids": sorted(backend_listener_pids),
            "fixture": {"classification": "synthetic isolated M1_FROZEN engineering fixture; not M2 training or quality evidence",
                        "style_id": style_id, "sample_id": sample_id, "training_image_id": image_id,
                        "png_sha256": hashlib.sha256(png).hexdigest()},
            "runs": {"completed_run_id": run_id, "state_sequence": state_sequence,
                     "cancel_run_id": cancel_run_id},
            "api_trace": API_TRACE,
            "browser_api_responses": browser_responses,
            "console_errors": console_errors,
        }
        (runtime.run_dir / "p7-browser-evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
        exit_code = 1 if FAILS else 0
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        exit_code = 1
    finally:
        print(f"# summary e2e_p7_style_run: {len(FAILS)} failed {FAILS}")
        try:
            runtime.stop_all(exit_code)
        finally:
            runtime.restore_output_capture()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
