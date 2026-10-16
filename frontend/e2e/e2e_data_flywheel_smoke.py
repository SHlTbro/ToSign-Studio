# -*- coding: utf-8 -*-
"""Isolated P4 -> candidate -> P7 review smoke for the DATA Flywheel gate.

All application traffic goes to a locally started current-source backend and
Vite dev server. The DB and storage live under this run's evidence directory.
The only owner-confirmed row is marked TEST_FIXTURE_CONFIRMATION and is never
counted as real training data or personal-style quality evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

import e2e_full_chain as flow
from runtime_guard import _port_listener_pids, _sha256_file, frontend_source_sha256


def fixture_png() -> bytes:
    """Two separated synthetic glyph-like strokes for two P4 manual boxes."""
    import io

    image = Image.new("RGB", (900, 1200), (12, 12, 12))
    draw = ImageDraw.Draw(image)
    for ox in (0, 360):
        draw.line([(250 + ox, 320), (430 + ox, 320), (340 + ox, 320), (340 + ox, 850)],
                  fill=(248, 248, 248), width=32)
        draw.line([(260 + ox, 580), (435 + ox, 580)], fill=(248, 248, 248), width=28)
        draw.line([(390 + ox, 720), (505 + ox, 850)], fill=(248, 248, 248), width=26)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def payload_data(body):
    return body.get("data", body) if isinstance(body, dict) else body


def extract_items(body):
    value = payload_data(body)
    return value.get("items", []) if isinstance(value, dict) else []


API_TRACE: list[dict] = []


def api(method, path, **kwargs):
    status, body = flow.api(method, path, **kwargs)
    API_TRACE.append({"method": method, "path": path, "status": status})
    return status, body


def venv_base_python() -> Path:
    """Run the backend on the venv's exact base interpreter, not its launcher."""
    cfg = flow.REPO / ".venv" / "pyvenv.cfg"
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("home ="):
            base_dir = Path(line.split("=", 1)[1].strip())
            executable = base_dir / "python.exe"
            if executable.is_file():
                return executable
    raise RuntimeError(f"cannot resolve venv base Python from {cfg}")


def main() -> int:
    from playwright.sync_api import sync_playwright

    flow.FAILS = []
    flow.API_SESSION_COOKIE = None
    backend_port = int(os.environ.get("YY_E2E_BACKEND_PORT", "8101"))
    frontend_port = int(os.environ.get("YY_E2E_FRONTEND_PORT", "5174"))
    runtime = flow.RunContext(flow.REPO, "data-flywheel", backend_port, frontend_port)
    runtime_dir = runtime.run_dir
    db_path = (runtime_dir / "flywheel.db").as_posix()
    storage = runtime_dir / "storage"
    backend_python = venv_base_python()
    site_packages = flow.REPO / ".venv" / "Lib" / "site-packages"
    env = dict(os.environ)
    env.update({
        "YY_DB_URL": f"sqlite:///{db_path}",
        "YY_STORAGE_ROOT": str(storage),
        "YY_AUTH_STRICT": "1",
        "YY_E2E_BACKEND_PYTHON": str(backend_python),
        "YY_E2E_FRONTEND_DIST": "",
        "PYTHONPATH": os.pathsep.join([str(flow.REPO), str(site_packages)]),
    })
    api_gate_rel = ".ai-hub/parallel-lines/line-b/gates/B-GATE-API-BASE-2026-09-30-v5-r2.json"
    style_gate_rel = ".ai-hub/parallel-lines/line-b/gates/B-GATE-STYLE-DATA-FLYWHEEL-2026-09-30-r3.json"
    release_gate_rel = ".ai-hub/parallel-lines/line-b/gates/B-GATE-RELEASE-2026-09-30-r3.json"
    manifest_rel = ".ai-hub/parallel-lines/line-b/gates/release-source-manifest-v2.1-2026-09-30.json"
    api_gate = json.loads((flow.REPO / api_gate_rel).read_text(encoding="utf-8"))
    flywheel_gate = json.loads((flow.REPO / style_gate_rel).read_text(encoding="utf-8"))
    release_gate = json.loads((flow.REPO / release_gate_rel).read_text(encoding="utf-8"))
    manifest = json.loads((flow.REPO / manifest_rel).read_text(encoding="utf-8"))
    api_gate_path = flow.REPO / api_gate_rel
    style_gate_path = flow.REPO / style_gate_rel
    release_gate_path = flow.REPO / release_gate_rel
    manifest_path = flow.REPO / manifest_rel
    api_gate_sha = _sha256_file(api_gate_path)
    style_gate_sha = _sha256_file(style_gate_path)
    release_gate_sha = _sha256_file(release_gate_path)
    manifest_sha = _sha256_file(manifest_path)
    manifest_tree = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
    release_pins = release_gate["pins"]
    api_snapshot_rel = api_gate["openapi_v5"]["snapshot_rel"]
    api_snapshot = flow.REPO / api_snapshot_rel
    api_snapshot_sha = _sha256_file(api_snapshot)
    openapi_receipt_rel = api_gate["openapi_v5"]["receipt"]
    openapi_receipt = json.loads((flow.REPO / openapi_receipt_rel).read_text(encoding="utf-8"))
    gate_bindings_valid = (
        api_gate.get("status") == "PUBLISHED"
        and flywheel_gate.get("status") == "PUBLISHED"
        and release_gate.get("status") == "PUBLISHED"
        and release_pins["api_base_v5"]["path"] == api_gate_rel
        and release_pins["api_base_v5"]["sha256"] == api_gate_sha
        and release_pins["style_data_r2"]["path"] == style_gate_rel
        and release_pins["style_data_r2"]["sha256"] == style_gate_sha
        and release_pins["openapi_v5_snapshot"]["path"] == api_snapshot_rel
        and release_pins["openapi_v5_snapshot"]["sha256"] == api_snapshot_sha
        and api_gate["openapi_v5"]["snapshot_file_sha256"] == api_snapshot_sha
        and flywheel_gate["api_base_v5"]["schema_hash_sha256"] == api_snapshot_sha
        and release_pins["source_manifest_v2"]["path"] == manifest_rel
        and release_pins["source_manifest_v2"]["sha256"] == manifest_sha
        and release_pins["source_manifest_v2"]["tree_hash"] == manifest_tree
        and release_pins["source_manifest_v2"]["file_count"] == len(manifest["files"])
    )
    env["YY_E2E_RELEASE_GATE_PATH"] = release_gate_rel
    os.environ["YY_E2E_RELEASE_GATE_PATH"] = release_gate_rel
    p4_source = flow.REPO / "frontend" / "src" / "pages" / "P4_Extract.vue"
    p7_source = flow.REPO / "frontend" / "src" / "pages" / "P7_Training.vue"
    result = {
        "schema": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated current-source P4 registration -> P7 candidate list/reject -> TEST_FIXTURE_CONFIRMATION confirm",
        "fixture_classification": "synthetic TEST_FIXTURE_CONFIRMATION only; not M2 data or personal-style quality evidence",
        "api_gate_path": api_gate_rel,
        "api_gate_sha256": api_gate_sha,
        "style_gate_path": style_gate_rel,
        "gate_sha256": style_gate_sha,
        "release_gate_path": release_gate_rel,
        "release_gate_sha256": release_gate_sha,
        "source_manifest_path": manifest_rel,
        "source_manifest_sha256": manifest_sha,
        "source_manifest_tree_sha256": manifest_tree,
        "source_manifest_file_count": len(manifest["files"]),
        "gate_bindings_valid_before_runtime": gate_bindings_valid,
        "api_snapshot_sha256_actual": api_snapshot_sha,
        "api_gate_declared_schema_sha256": api_gate["openapi_v5"]["snapshot_file_sha256"],
        "openapi_receipt_sha256_claim": openapi_receipt["schema_hash_sha256"],
        "p4_source_sha256": _sha256_file(p4_source),
        "p7_source_sha256": _sha256_file(p7_source),
        "frontend_source_sha256": frontend_source_sha256(flow.REPO / "frontend"),
        "ports": {"backend": backend_port, "frontend": frontend_port},
        "strict_auth": env["YY_AUTH_STRICT"],
        "db_path": db_path,
        "storage_root": str(storage),
        "checks": {},
        "api_trace": [],
        "browser_responses": [],
        "page_errors": [],
        "console_errors": [],
    }
    browser = None
    page = None
    browser_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    browser_responses: list[dict] = []
    candidate_ids: list[str] = []
    style_id = None
    asset_id = None
    test_confirmed_id = None
    marker_payloads: list[dict] = []
    runner_exit = 1
    runtime.install_output_capture()
    try:
        flow.check("r3-gates-and-source-manifest-pinned", gate_bindings_valid)
        if not gate_bindings_valid:
            raise RuntimeError("current API/Style/Release r3 or source-manifest v2.1 pins do not match")
        runtime.acquire()
        backend_command = [str(backend_python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                           "--port", str(backend_port)]
        backend = runtime.start("backend", backend_command, flow.REPO, env, backend_port)
        node = shutil.which("node")
        vite = flow.REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("Node/Vite executable is unavailable")
        frontend_env = dict(env)
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(frontend_port), "--strictPort",
                                               "--host", "127.0.0.1"],
                                 flow.REPO / "frontend", frontend_env, frontend_port)
        flow.check("owned-backend-listener", runtime.wait_owned_listener("backend"))
        flow.check("backend-health", backend.poll() is None and flow.wait_http(flow.backend_url("/api/v1/health")))
        flow.check("owned-vite-listener", runtime.wait_owned_listener("frontend"))
        flow.check("vite-health", frontend.poll() is None and flow.wait_http(flow.frontend_url("/")))
        flow.check("isolated-invite-seed", flow.seed_invites(env))
        if flow.FAILS:
            raise RuntimeError(f"service preflight failed: {flow.FAILS}")

        username = "flywheel" + secrets.token_hex(3)
        password = "Flywheel-" + secrets.token_urlsafe(9) + "!7Aa"
        st, body = api("POST", "/api/v1/auth/register", json_body={
            "username": username, "password": password, "display_name": username, "invite_code": "seed-admin"})
        flow.check("isolated-first-admin-register", st == 201, f"status={st}")
        if st != 201:
            raise RuntimeError(f"isolated admin registration failed: {body}")

        fixture_file = runtime_dir / "flywheel-source.png"
        fixture_file.write_bytes(fixture_png())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=flow.low_memory_browser_args())
            page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            page.on("pageerror", lambda exc: browser_errors.append(str(exc)[:500]))
            page.on("console", lambda msg: console_errors.append(msg.text[:500]) if msg.type == "error" else None)
            def record_response(response):
                browser_responses.append({"method": response.request.method, "url": response.url,
                                          "status": response.status})
                if response.status >= 500:
                    server_errors.append(f"{response.status} {response.url}"[:500])
            page.on("response", record_response)

            page.goto(flow.frontend_url("/login"), wait_until="domcontentloaded")
            page.fill("#login-username", username)
            page.fill("#login-password", password)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            next_password = "Flywheel-New-" + secrets.token_urlsafe(8) + "!8Bb"
            page.fill("#old-password", password)
            page.fill("#new-password", next_password)
            page.fill("#confirm-password", next_password)
            page.click('button[type="submit"]')
            page.wait_for_url(lambda url: "/change-password" not in url, timeout=30000)
            flow.bind_api_session(page)
            flow.check("browser-login-and-required-password-change", "/change-password" not in page.url)

            st, style_body = api("POST", "/api/v1/styles", json_body={
                "name": "Flywheel isolated handwriting fixture", "source_type": "HANDWRITING_ASSET"})
            style = payload_data(style_body)
            style_id = style.get("style_id") if isinstance(style, dict) else None
            flow.check("create-handwriting-style", st == 201 and isinstance(style_id, int), f"status={st}")
            if not style_id:
                raise RuntimeError(f"style create failed: {style_body}")
            st, approval = api("POST", f"/api/v1/styles/{style_id}/approve")
            flow.check("approve-handwriting-style", st == 200, f"status={st}")

            # The approved COS asset is uploaded by the browser's real P2 input.
            page.goto(flow.frontend_url("/assets"), wait_until="domcontentloaded")
            page.set_input_files('input[type="file"]', str(fixture_file))
            page.wait_for_selector('button[aria-label^="资产"]', timeout=30000)
            st, asset_body = api("GET", "/api/v1/assets?type=cos&status=pending&page=1&page_size=100")
            pending = payload_data(asset_body).get("items", [])
            flow.check("one-pending-isolated-source-asset", st == 200 and len(pending) == 1,
                       f"status={st}, items={len(pending)}")
            if len(pending) != 1:
                raise RuntimeError(f"expected one pending asset: {asset_body}")
            asset_id = pending[0]["asset_id"]
            st, approved = api("POST", f"/api/v1/assets/{asset_id}/approve")
            flow.check("approve-source-asset", st == 200, f"status={st}")

            page.goto(flow.frontend_url(f"/extract/{asset_id}"), wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="p4-stage"] img', timeout=30000)
            loaded = page.locator('[data-testid="p4-stage"] img').evaluate(
                "img => img.complete && img.naturalWidth === 900 && img.naturalHeight === 1200")
            flow.check("p4-source-image-loaded", loaded)
            stage = page.get_by_test_id("p4-stage")
            stage_box = stage.bounding_box()
            if not stage_box:
                raise RuntimeError("P4 stage has no browser bounds")
            page.get_by_test_id("p4-manual-toggle").click()
            # Two real pointer-drawn normalized boxes around separated synthetic glyphs.
            for x0, x1 in ((0.20, 0.48), (0.54, 0.83)):
                page.mouse.move(stage_box["x"] + stage_box["width"] * x0,
                                stage_box["y"] + stage_box["height"] * 0.20)
                page.mouse.down()
                page.mouse.move(stage_box["x"] + stage_box["width"] * x1,
                                stage_box["y"] + stage_box["height"] * 0.80, steps=8)
                page.mouse.up()
            flow.check("two-manual-boxes-selected", page.get_by_test_id("p4-selected-count").inner_text() == "2")
            page.get_by_test_id("p4-extract").click()
            page.wait_for_selector('[data-testid="p4-draft-section"]', timeout=30000)
            page.wait_for_function("document.querySelectorAll('[data-testid=p4-draft-section] button').length >= 3",
                                   timeout=30000)
            draft_buttons = page.get_by_role("button", name=__import__("re").compile(r"^框 "))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and draft_buttons.count() != 2:
                page.wait_for_timeout(100)
            flow.check("two-extracted-drafts", draft_buttons.count() == 2, f"count={draft_buttons.count()}")

            # Register both drafts through P4; each remains candidate until the P7 decision.
            for index, label in enumerate(("永", "山")):
                page.get_by_role("button", name=__import__("re").compile(r"^框 ")).nth(index).click()
                page.get_by_test_id("p4-compare-before").click()
                page.get_by_test_id("p4-compare-after").click()
                page.wait_for_function("document.querySelector('[data-testid=p4-canvas]')?.dataset.ready === 'true'",
                                       timeout=20000)
                page.get_by_test_id("p4-confirm-register").click()
                page.get_by_test_id("p4-register-modal").wait_for(state="visible", timeout=10000)
                page.get_by_test_id("p4-style-select").select_option(str(style_id))
                page.get_by_test_id("p4-text-label").fill(label)
                page.get_by_test_id("p4-register-final").click()
                page.get_by_test_id("p4-register-done").wait_for(state="visible", timeout=30000)
                page.get_by_role("button", name="继续").click()

            st, candidate_body = api("GET", f"/api/v1/training-samples?style_id={style_id}&page=1&page_size=50&review_status=candidate")
            candidates = extract_items(candidate_body)
            flow.check("p4-registered-two-real-candidates", st == 200 and len(candidates) == 2,
                       f"status={st}, count={len(candidates)}")
            if len(candidates) != 2:
                raise RuntimeError(f"P4 candidates not found: {candidate_body}")
            candidate_ids = [str(row["sample_id"]) for row in candidates]
            flow.check("p4-candidates-have-source-provenance",
                       all(row.get("provenance_complete") and row.get("source_asset_id") == asset_id for row in candidates))

            page.goto(flow.frontend_url("/training"), wait_until="domcontentloaded")
            page.get_by_role("heading", name=__import__("re").compile("本人字符确认")).wait_for(timeout=30000)
            page.get_by_label(f"选择候选 {candidate_ids[0]}").wait_for(timeout=30000)
            # Correct the label in P7, then reject the same candidate through the real UI.
            page.get_by_label(f"选择候选 {candidate_ids[0]}").check()
            page.get_by_label(f"修正候选字符 {candidate_ids[0]}").fill("林")
            page.get_by_role("button", name="排除选中的 1 个候选").click()
            page.get_by_label("排除理由").fill("TEST_FIXTURE_REJECTION: engineering-only synthetic glyph")
            page.get_by_label("本人已逐项复核").check()
            with page.expect_response(lambda response: response.request.method == "POST" and
                                      response.url.endswith("/api/v1/training-samples/batch-review")) as reject_info:
                page.get_by_role("button", name="提交本人复核").click()
            reject_response = reject_info.value
            flow.check("p7-reject-through-ui", reject_response.status == 200, f"status={reject_response.status}")
            st, rejected_body = api("GET", f"/api/v1/training-samples?style_id={style_id}&page=1&page_size=50&review_status=rejected")
            rejected = extract_items(rejected_body)
            flow.check("label-correction-and-reject-persisted", st == 200 and len(rejected) == 1 and
                       rejected[0].get("sample_id") == candidate_ids[0] and rejected[0].get("review_status") == "rejected",
                       f"status={st}, ui_corrected_label=林, rows={rejected}")

            # The real P7 confirm button issues the request. The test-only route
            # tags that one request so its result cannot be mistaken for real GT.
            test_confirmed_id = candidate_ids[1]
            page.get_by_label(f"选择候选 {test_confirmed_id}").check()
            marked_payloads: list[dict] = []
            def mark_fixture_confirmation(route):
                payload = route.request.post_data_json
                for item in payload.get("items", []):
                    if item.get("decision") == "confirm":
                        item["reason"] = "TEST_FIXTURE_CONFIRMATION"
                marked_payloads.append(payload)
                route.continue_(post_data=json.dumps(payload, ensure_ascii=False))
            page.route("**/api/v1/training-samples/batch-review", mark_fixture_confirmation)
            page.get_by_role("button", name="确认选中的 1 个字符").click()
            page.get_by_label("本人已逐项复核").check()
            with page.expect_response(lambda response: response.request.method == "POST" and
                                      response.url.endswith("/api/v1/training-samples/batch-review")) as confirm_info:
                page.get_by_role("button", name="提交本人复核").click()
            confirm_response = confirm_info.value
            page.unroute("**/api/v1/training-samples/batch-review", mark_fixture_confirmation)
            flow.check("p7-test-fixture-confirm-through-ui", confirm_response.status == 200 and
                       len(marked_payloads) == 1 and marked_payloads[0]["items"][0].get("reason") == "TEST_FIXTURE_CONFIRMATION",
                       f"status={confirm_response.status}, marked={marked_payloads}")
            st, confirmed_body = api("GET", f"/api/v1/training-samples?style_id={style_id}&page=1&page_size=50&review_status=owner_confirmed")
            confirmed = extract_items(confirmed_body)
            fixture_confirmed = next((row for row in confirmed if row.get("sample_id") == test_confirmed_id), None)
            flow.check("test-fixture-marker-and-reviewer-persisted",
                       st == 200 and fixture_confirmed is not None and
                       fixture_confirmed.get("review_reason") == "TEST_FIXTURE_CONFIRMATION" and
                       fixture_confirmed.get("owner_confirmed_by") is not None and
                       fixture_confirmed.get("owner_confirmed_at") is not None)
            page.reload(wait_until="domcontentloaded")
            owner_heading = page.locator("#owner-confirm-heading")
            owner_heading.wait_for(state="visible", timeout=30000)
            page.wait_for_function(
                """() => {
                    const text = document.querySelector('section[aria-labelledby="owner-confirm-heading"]')?.innerText || '';
                    return text.includes('候选 0') && text.includes('已确认字 1') && text.includes('已排除 1');
                }""",
                timeout=30000,
            )
            p7_after_refresh = page.locator('section[aria-labelledby="owner-confirm-heading"]').inner_text()
            flow.check("p7-f5-restores-candidate-review-counts",
                       "候选 0" in p7_after_refresh and "已确认字 1" in p7_after_refresh
                       and "已排除 1" in p7_after_refresh,
                       p7_after_refresh)
            st, confirmed_after_refresh_body = api(
                "GET", f"/api/v1/training-samples?style_id={style_id}&page=1&page_size=50&review_status=owner_confirmed")
            confirmed_after_refresh = extract_items(confirmed_after_refresh_body)
            fixture_after_refresh = next(
                (row for row in confirmed_after_refresh if row.get("sample_id") == test_confirmed_id), None)
            flow.check("p7-f5-retains-test-fixture-confirmation-marker",
                       st == 200 and fixture_after_refresh is not None
                       and fixture_after_refresh.get("review_reason") == "TEST_FIXTURE_CONFIRMATION"
                       and fixture_after_refresh.get("owner_confirmed_by") is not None
                       and fixture_after_refresh.get("owner_confirmed_at") is not None,
                       f"status={st}, row={fixture_after_refresh}")
            page.wait_for_timeout(400)
            page.screenshot(path=str(runtime_dir / "flywheel-p7-review.png"), full_page=True)
            result["browser_responses"] = browser_responses
            result["api_trace"] = API_TRACE
            result["fixture_confirmed_sample_id"] = test_confirmed_id
            result["p7_after_refresh_heading"] = p7_after_refresh
            result["candidate_ids"] = candidate_ids
            result["style_id"] = style_id
            result["asset_id"] = asset_id
            result["page_errors"] = browser_errors
            result["console_errors"] = console_errors
            result["server_5xx"] = server_errors
            flow.check("no-uncaught-page-errors", not browser_errors, str(browser_errors))
            flow.check("no-http-5xx", not server_errors, str(server_errors))
            if flow.FAILS:
                raise RuntimeError(f"browser smoke assertions failed: {flow.FAILS}")
            runner_exit = 0
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            runtime.stop_all(runner_exit)
        finally:
            runtime.restore_output_capture()

    result["checks"] = {"failed": list(flow.FAILS)}
    result.setdefault("api_trace", API_TRACE)
    result["runtime"] = runtime.fingerprints("stopped", runner_exit)
    result["runner_exit_code"] = runner_exit
    result["ended_at"] = datetime.now(timezone.utc).isoformat()
    result["result"] = "PASS" if runner_exit == 0 else "FAIL"
    output = runtime_dir / "data-flywheel-result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt = [
        "# Data Flywheel isolated browser smoke",
        "",
        f"- Result: **{result['result']}**; runner exit `{runner_exit}`.",
        f"- B-GATE-API-BASE v5-r2 SHA-256: `{result['api_gate_sha256']}`; B-GATE-STYLE-DATA-FLYWHEEL r3 SHA-256: `{result['gate_sha256']}`; B-GATE-RELEASE r3 SHA-256: `{result['release_gate_sha256']}`.",
        f"- Source manifest `{result['source_manifest_path']}` SHA-256: `{result['source_manifest_sha256']}`; tree `{result['source_manifest_tree_sha256']}`; files `{result['source_manifest_file_count']}`.",
        f"- API snapshot actual SHA-256: `{result['api_snapshot_sha256_actual']}`; Gate declaration: `{result['api_gate_declared_schema_sha256']}`; OpenAPI receipt raw-byte hash: `{result['openapi_receipt_sha256_claim']}`.",
        f"- Isolated style `{style_id}`, approved source asset `{asset_id}`, P4 candidates `{candidate_ids}`.",
        f"- Rejected after P7 label correction; synthetic confirmation `{test_confirmed_id}` stored with reason `TEST_FIXTURE_CONFIRMATION`.",
        f"- `/training` F5 persistence: `{result.get('p7_after_refresh_heading')}`; the synthetic confirmation marker was re-read from the isolated API.",
        f"- Strict auth `{env['YY_AUTH_STRICT']}`; DB `{db_path}`; storage `{storage}`.",
        f"- Ports backend `{backend_port}`, frontend `{frontend_port}`; evidence directory `{runtime_dir}`.",
        "- This smoke exercises UI/API contract only. The confirmed row is a synthetic test fixture and is not Owner quality approval or M2-ready training data.",
        "",
    ]
    (runtime_dir / "data-flywheel-receipt.md").write_text("\n".join(receipt), encoding="utf-8")
    print(f"DATA_FLYWHEEL_RESULT={result['result']} RUNNER_EXIT={runner_exit} RUN_DIR={runtime_dir}")
    return runner_exit


if __name__ == "__main__":
    raise SystemExit(main())
