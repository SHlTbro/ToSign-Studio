# -*- coding: utf-8 -*-
"""P4 real-browser pixel persistence and restart round-trip.

Uses current workspace frontend source with the Release-gated app/runtime,
strict auth, and an isolated temporary SQLite database/storage root. The test
fixture is synthetic because this acceptance is about byte/pixel persistence,
not handwriting or real-Cos quality.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

import e2e_full_chain as flow
from runtime_guard import _port_listener_pids, _sha256_file, frontend_source_sha256


def fixture_png() -> bytes:
    """A high-contrast authorized test image with a non-solid glyph shape."""
    import io

    image = Image.new("RGB", (900, 1200), (12, 12, 12))
    draw = ImageDraw.Draw(image)
    # A large, connected, open glyph-like mark for the real Pillow separator.
    draw.line([(330, 320), (540, 320), (420, 320), (420, 850)], fill=(248, 248, 248), width=34)
    draw.line([(310, 585), (555, 585)], fill=(248, 248, 248), width=30)
    draw.line([(485, 720), (610, 850)], fill=(248, 248, 248), width=28)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def pil_rgba(data: bytes) -> Image.Image:
    with Image.open(__import__("io").BytesIO(data)) as image:
        return image.convert("RGBA")


def pixel_delta(left: Image.Image, right: Image.Image) -> dict:
    a = left.convert("RGBA")
    b = right.convert("RGBA")
    if a.size != b.size:
        return {"dimensions_match": False, "left_dimensions": list(a.size), "right_dimensions": list(b.size),
                "rgba_different_pixel_count": None, "alpha_different_pixel_count": None, "bbox": None}
    pa = list(a.getdata())
    pb = list(b.getdata())
    changed = []
    alpha_changed = 0
    width, height = a.size
    for index, (x, y) in enumerate(zip(pa, pb)):
        if x != y:
            changed.append(index)
        if x[3] != y[3]:
            alpha_changed += 1
    if changed:
        xs = [i % width for i in changed]
        ys = [i // width for i in changed]
        bbox = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
    else:
        bbox = None
    return {"dimensions_match": True, "dimensions": [width, height],
            "rgba_different_pixel_count": len(changed),
            "alpha_different_pixel_count": alpha_changed, "bbox": bbox}


def sha_pixels(image: Image.Image) -> str:
    return hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()


def get_training_png(image_id: int) -> tuple[int, bytes | dict]:
    return flow.api("GET", f"/api/v1/training-sets/images/{image_id}/file")


def main() -> int:
    from playwright.sync_api import sync_playwright

    flow.FAILS = []
    flow.API_SESSION_COOKIE = None
    runtime = flow.RunContext(flow.REPO, "p4-roundtrip", flow.BACKEND_PORT, flow.FRONTEND_PORT)
    env = dict(os.environ, YY_DB_URL=f"sqlite:///{flow.DB}", YY_STORAGE_ROOT=flow.STORAGE,
               YY_AUTH_STRICT="1", YY_E2E_BACKEND_PYTHON=str(flow.VENV_PY))
    backend_pythonpath = os.environ.get("YY_E2E_BACKEND_PYTHONPATH")
    if backend_pythonpath:
        env["PYTHONPATH"] = backend_pythonpath

    gate_path = flow.REPO / ".ai-hub" / "parallel-lines" / "line-b" / "gates" / "B-GATE-RELEASE.json"
    p4_path = flow.REPO / "frontend" / "src" / "pages" / "P4_Extract.vue"
    result: dict = {
        "schema": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "P4 authenticated pixel persistence only; synthetic test fixture; no style-quality claim",
        "gate_sha256": _sha256_file(gate_path),
        "p4_source_sha256": _sha256_file(p4_path),
        "frontend_source_sha256": frontend_source_sha256(flow.REPO / "frontend"),
        "ports": {"backend": flow.BACKEND_PORT, "frontend": flow.FRONTEND_PORT},
        "strict_auth": env["YY_AUTH_STRICT"],
        "db_path": flow.DB,
        "storage_root": flow.STORAGE,
        "checks": {},
    }
    exit_code = 1
    browser = None
    page = None
    page_errors: list[str] = []
    server_errors: list[str] = []
    submitted_png = b""
    extracted_png = b""
    saved_png = b""
    first_file_sha = None
    sample_id = None
    style_id = None
    training_image_id = None
    asset_id = None
    password = ""
    new_password = ""
    backend = None
    restarted_backend = None
    runtime.install_output_capture()
    try:
        runtime.acquire()
        backend_command = [str(flow.VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", str(flow.BACKEND_PORT)]
        backend = runtime.start("backend", backend_command, flow.REPO, env, flow.BACKEND_PORT)
        node = flow.shutil.which("node")
        vite = flow.REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("direct Node/Vite executable is unavailable")
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(flow.FRONTEND_PORT),
                                               "--strictPort", "--host", "127.0.0.1"],
                                 flow.REPO / "frontend", env, flow.FRONTEND_PORT)
        owned = runtime.wait_owned_listener("backend")
        flow.check("backend-owned-listener", owned)
        ready = owned and backend.poll() is None and flow.wait_http(flow.backend_url("/api/v1/health"))
        flow.check("backend-health", ready)
        if not ready:
            raise RuntimeError("Release backend did not become healthy under its owned PID")
        flow.check("seed-invites", flow.seed_invites(env))
        flow.check("frontend-owned-listener", runtime.wait_owned_listener("frontend"))
        flow.check("frontend-health", frontend.poll() is None and flow.wait_http(flow.frontend_url("/")))
        if flow.FAILS:
            raise RuntimeError(f"service preflight failed: {flow.FAILS}")

        password = "P4-RT-" + secrets.token_urlsafe(12) + "!7Aa"
        st, body = flow.api("POST", "/api/v1/auth/register", json_body={
            "username": "p4roundtrip", "password": password, "display_name": "P4 Roundtrip",
            "invite_code": "seed-admin"})
        flow.check("register-first-admin", st == 201, f"status={st}")
        if st != 201:
            raise RuntimeError(f"isolated admin registration failed: {body}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=flow.low_memory_browser_args())
            page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)[:500]))
            page.on("response", lambda response: server_errors.append(
                f"{response.status} {response.url}"[:500]) if response.status >= 500 else None)

            page.goto(flow.frontend_url("/login"), wait_until="domcontentloaded")
            page.fill("#login-username", "p4roundtrip")
            page.fill("#login-password", password)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            new_password = "P4-RT-New-" + secrets.token_urlsafe(10) + "!8Bb"
            password_fields = page.locator('input[type="password"]')
            password_fields.nth(0).fill(password)
            password_fields.nth(1).fill(new_password)
            password_fields.nth(2).fill(new_password)
            page.click('button[type="submit"]')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"):
                page.wait_for_timeout(250)
            flow.check("admin-login-and-password-change", page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"), page.url)
            flow.bind_api_session(page)

            # Create an owned, usable style through the real Release API.
            st, body = flow.api("POST", "/api/v1/styles", json_body={
                "name": "P4 pixel roundtrip fixture", "source_type": "manual"})
            style = body.get("data", {}) if isinstance(body, dict) else {}
            style_id = style.get("style_id")
            flow.check("create-style", st == 201 and isinstance(style_id, int), f"status={st}")
            if not style_id:
                raise RuntimeError(f"style creation failed: {body}")
            st, body = flow.api("POST", f"/api/v1/styles/{style_id}/approve")
            flow.check("approve-style", st == 200 and body.get("data", {}).get("status") == "approved", f"status={st}")

            # Upload the asset through P2's real browser file input, then use the
            # authenticated admin approval route before entering P4.
            fixture_path = runtime.run_dir / "p4-source-fixture.png"
            fixture_path.write_bytes(fixture_png())
            page.goto(flow.frontend_url("/assets"))
            page.set_input_files('input[type="file"]', str(fixture_path))
            page.wait_for_selector('button[aria-label^="资产"]', timeout=30000)
            st, body = flow.api("GET", "/api/v1/assets?type=cos&status=pending&page=1&page_size=100")
            pending = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
            flow.check("single-owned-pending-asset", st == 200 and len(pending) == 1, f"status={st}, count={len(pending)}")
            if len(pending) != 1:
                raise RuntimeError(f"expected one isolated pending asset, got {len(pending)}")
            asset_id = pending[0]["asset_id"]
            st, body = flow.api("POST", f"/api/v1/assets/{asset_id}/approve")
            flow.check("asset-approved", st == 200 and body.get("data", {}).get("status") == "approved", f"status={st}")

            # Direct P4 browser path, manual region selection, then server extract.
            p4_url = flow.frontend_url(f"/extract/{asset_id}")
            page.goto(p4_url)
            page.wait_for_selector('[data-testid="p4-stage"] img', timeout=30000)
            original_loaded = page.locator('[data-testid="p4-stage"] img').evaluate(
                "img => img.complete && img.naturalWidth === 900 && img.naturalHeight === 1200")
            flow.check("p4-original-image-loaded", original_loaded)
            stage = page.locator('[data-testid="p4-stage"]')
            stage_box = stage.bounding_box()
            if not stage_box:
                raise RuntimeError("P4 source stage has no browser bounding box")
            page.get_by_test_id("p4-manual-toggle").click()
            page.mouse.move(stage_box["x"] + stage_box["width"] * 0.24,
                            stage_box["y"] + stage_box["height"] * 0.20)
            page.mouse.down()
            page.mouse.move(stage_box["x"] + stage_box["width"] * 0.76,
                            stage_box["y"] + stage_box["height"] * 0.80, steps=8)
            page.mouse.up()
            flow.check("manual-box-selected", page.get_by_test_id("p4-selected-count").inner_text() == "1")
            page.get_by_test_id("p4-extract").click()
            page.wait_for_selector('[data-testid="p4-draft-section"]', timeout=30000)
            page.get_by_test_id("p4-compare-before").click()
            before_img = page.locator('[data-testid="p4-alpha-before"] img')
            before_src = before_img.get_attribute("src")
            if not before_src:
                raise RuntimeError("P4 extracted draft image has no source URL")
            draft_path = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(before_src).path
            st, draft_body = flow.api("GET", draft_path)
            flow.check("draft-file-readable", st == 200 and isinstance(draft_body, bytes) and len(draft_body) > 100,
                       f"status={st}")
            if st != 200 or not isinstance(draft_body, bytes):
                raise RuntimeError(f"draft readback failed: {st} {draft_body}")
            extracted_png = draft_body
            (runtime.run_dir / "p4-extracted-draft.png").write_bytes(extracted_png)

            page.get_by_test_id("p4-compare-after").click()
            canvas = page.get_by_test_id("p4-canvas")
            canvas.wait_for(state="visible", timeout=20000)
            page.wait_for_function("document.querySelector('[data-testid=p4-canvas]')?.dataset.ready === 'true'", timeout=20000)
            initial_canvas_data = canvas.evaluate("canvas => canvas.toDataURL('image/png')")
            initial_canvas_png = base64.b64decode(initial_canvas_data.split(",", 1)[1])
            flow.check("draft-loaded-into-canvas", pil_rgba(initial_canvas_png).size == (128, 128))

            # Actual browser mouse input (pointer events) adds a small brush mark
            # inside the known transparent 8px left canvas margin. The image
            # normalization contract guarantees this margin on the draft.
            page.get_by_test_id("p4-brush").click()
            canvas_box = canvas.bounding_box()
            if not canvas_box:
                raise RuntimeError("P4 canvas has no browser bounding box")
            page.mouse.click(canvas_box["x"] + 2, canvas_box["y"] + canvas_box["height"] / 2)
            page.wait_for_function("document.querySelector('[data-testid=p4-edit-count]')?.textContent === '1'", timeout=5000)
            flow.check("pointer-edit-count", page.get_by_test_id("p4-edit-count").inner_text() == "1")

            # Switching before/after must preserve the edited bitmap. Read the
            # exact final PNG from browser canvas immediately before confirm.
            page.get_by_test_id("p4-compare-before").click()
            page.get_by_test_id("p4-compare-after").click()
            page.wait_for_function("document.querySelector('[data-testid=p4-canvas]')?.dataset.ready === 'true'", timeout=20000)
            submitted_data_url = canvas.evaluate("canvas => canvas.toDataURL('image/png')")
            submitted_png = base64.b64decode(submitted_data_url.split(",", 1)[1])
            (runtime.run_dir / "p4-browser-canvas-before-submit.png").write_bytes(submitted_png)
            original_image = pil_rgba(extracted_png)
            submitted_image = pil_rgba(submitted_png)
            draft_delta = pixel_delta(original_image, submitted_image)
            result["pixel_delta_draft_to_submitted"] = draft_delta
            result["submitted_canvas"] = {"dimensions": list(submitted_image.size), "sha256_file": hashlib.sha256(submitted_png).hexdigest(),
                                           "sha256_rgba_pixels": sha_pixels(submitted_image)}
            flow.check("real-pointer-created-alpha-pixel-delta",
                       draft_delta.get("rgba_different_pixel_count", 0) > 0
                       and draft_delta.get("alpha_different_pixel_count", 0) > 0, str(draft_delta))

            page.get_by_test_id("p4-confirm-register").click()
            page.get_by_test_id("p4-register-modal").wait_for(state="visible", timeout=10000)
            page.get_by_test_id("p4-style-select").select_option(str(style_id))
            unique_label = "P4像素往返" + secrets.token_hex(3)
            page.get_by_test_id("p4-text-label").fill(unique_label)
            page.get_by_test_id("p4-register-final").click()
            page.get_by_test_id("p4-register-done").wait_for(state="visible", timeout=30000)
            flow.check("p4-confirm-register-completed", True)

            st, samples_body = flow.api("GET", f"/api/v1/styles/{style_id}/samples")
            samples = samples_body.get("data", {}).get("items", []) if isinstance(samples_body, dict) else []
            matches = [row for row in samples if row.get("text_label") == unique_label]
            flow.check("registered-style-sample-found", st == 200 and len(matches) == 1, f"status={st}, count={len(matches)}")
            if len(matches) != 1:
                raise RuntimeError(f"could not resolve the registered StyleSample: {samples_body}")
            saved_sample = matches[0]
            sample_id = saved_sample.get("sample_id")
            training_image_id = saved_sample.get("source_training_image_id")
            result["sample_id"] = sample_id
            result["style_id"] = style_id
            result["training_image_id"] = training_image_id
            flow.check("sample-links-training-image", isinstance(training_image_id, int), str(saved_sample))
            if not isinstance(training_image_id, int):
                raise RuntimeError(f"StyleSample has no source_training_image_id: {saved_sample}")

            st, saved_png = get_training_png(training_image_id)
            flow.check("server-training-image-readable", st == 200 and isinstance(saved_png, bytes) and len(saved_png) > 100,
                       f"status={st}")
            if st != 200 or not isinstance(saved_png, bytes):
                raise RuntimeError(f"training-image file GET failed: {st}")
            (runtime.run_dir / "p4-server-training-image.png").write_bytes(saved_png)
            first_file_sha = hashlib.sha256(saved_png).hexdigest()
            saved_image = pil_rgba(saved_png)
            save_delta = pixel_delta(submitted_image, saved_image)
            result["submitted_vs_server"] = save_delta
            result["server_image"] = {"sha256_file": first_file_sha, "sha256_rgba_pixels": sha_pixels(saved_image),
                                      "dimensions": list(saved_image.size)}
            flow.check("server-pixels-exactly-match-submitted-canvas",
                       save_delta.get("rgba_different_pixel_count") == 0 and save_delta.get("dimensions_match") is True,
                       str(save_delta))
            flow.check("server-pixels-differ-from-extracted-draft",
                       pixel_delta(original_image, saved_image).get("rgba_different_pixel_count", 0) > 0)
            flow.check("sample-hash-matches-file", saved_sample.get("sha256") == first_file_sha,
                       f"sample={saved_sample.get('sha256')} file={first_file_sha}")

            # Browser refresh must reread the same sample id and same file.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="p4-saved-samples"]', timeout=30000)
            page.wait_for_function("""() => {
              const imgs = [...document.querySelectorAll('[data-testid=p4-saved-sample-image]')];
              return imgs.length > 0 && imgs.every(img => img.complete && img.naturalWidth > 0);
            }""", timeout=30000)
            st, refreshed_list = flow.api("GET", f"/api/v1/styles/{style_id}/samples")
            refreshed = [row for row in refreshed_list.get("data", {}).get("items", [])
                         if row.get("sample_id") == sample_id]
            flow.check("same-sample-after-page-refresh", st == 200 and len(refreshed) == 1
                       and refreshed[0].get("source_training_image_id") == training_image_id)
            st, refreshed_png = get_training_png(training_image_id)
            flow.check("same-file-after-page-refresh", st == 200 and hashlib.sha256(refreshed_png).hexdigest() == first_file_sha)

            # Stop only the backend process this harness created; retain the same
            # isolated DB/storage. A cleared browser session forces a real relogin.
            page.context.clear_cookies()
            page.goto("about:blank")
            backend.terminate()
            backend.wait(timeout=10)
            flow.check("owned-backend-stopped", backend.poll() is not None)
            restarted_backend = runtime.start("backend-restart", backend_command, flow.REPO, env, flow.BACKEND_PORT)
            # wait_owned_listener() maps only the canonical names "backend"
            # and "frontend"; this restart process needs the backend port too.
            restart_deadline = time.monotonic() + 90
            restart_owned = False
            while time.monotonic() < restart_deadline:
                if restarted_backend.poll() is not None:
                    break
                if _port_listener_pids(flow.BACKEND_PORT) == {restarted_backend.pid}:
                    restart_owned = True
                    break
                time.sleep(0.2)
            flow.check("restarted-backend-owned-listener", restart_owned)
            flow.check("restarted-backend-health", restart_owned and restarted_backend.poll() is None
                       and flow.wait_http(flow.backend_url("/api/v1/health")))
            if flow.FAILS:
                raise RuntimeError(f"P4 assertions failed before/at backend restart: {flow.FAILS}")

            page.goto(flow.frontend_url("/login"), wait_until="domcontentloaded")
            page.fill("#login-username", "p4roundtrip")
            page.fill("#login-password", new_password)
            page.click('button[type="submit"]')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"):
                page.wait_for_timeout(250)
            flow.check("relogin-after-backend-restart", page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"), page.url)
            flow.bind_api_session(page)
            page.goto(flow.frontend_url(f"/extract/{asset_id}"))
            page.wait_for_selector('[data-testid="p4-stage"] img', timeout=30000)
            page.wait_for_selector('[data-testid="p4-saved-samples"]', timeout=30000)
            st, after_restart_list = flow.api("GET", f"/api/v1/styles/{style_id}/samples")
            after_restart = [row for row in after_restart_list.get("data", {}).get("items", [])
                             if row.get("sample_id") == sample_id]
            flow.check("same-sample-after-backend-restart", st == 200 and len(after_restart) == 1
                       and after_restart[0].get("source_training_image_id") == training_image_id)
            st, after_restart_png = get_training_png(training_image_id)
            restart_sha = hashlib.sha256(after_restart_png).hexdigest() if isinstance(after_restart_png, bytes) else None
            flow.check("same-file-after-backend-restart", st == 200 and restart_sha == first_file_sha)
            if st == 200 and isinstance(after_restart_png, bytes):
                restart_image = pil_rgba(after_restart_png)
                result["after_restart"] = {
                    "sample_id": after_restart[0].get("sample_id"),
                    "training_image_id": after_restart[0].get("source_training_image_id"),
                    "sha256_file": restart_sha,
                    "sha256_rgba_pixels": sha_pixels(restart_image),
                    "dimensions": list(restart_image.size),
                    "pixels_equal_initial_saved_file": pixel_delta(saved_image, restart_image).get("rgba_different_pixel_count") == 0,
                }
                flow.check("same-pixels-after-backend-restart",
                           pixel_delta(saved_image, restart_image).get("rgba_different_pixel_count") == 0)
            page.screenshot(path=str(runtime.run_dir / "p4-after-restart.png"), full_page=True)

            result["page_errors"] = page_errors
            result["server_5xx"] = server_errors
            flow.check("no-uncaught-page-errors", len(page_errors) == 0, str(page_errors))
            flow.check("no-server-5xx", len(server_errors) == 0, str(server_errors))
            result["asset_id"] = asset_id
            result["db_path"] = flow.DB
            result["storage_root"] = flow.STORAGE
            result["checks"] = {"failed": list(flow.FAILS)}
            exit_code = 1 if flow.FAILS else 0
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
        result["page_errors"] = page_errors
        result["server_5xx"] = server_errors
        result["checks"] = {"failed": list(flow.FAILS)}
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

    final_fingerprint = runtime.fingerprints("stopped", exit_code)
    result["runtime"] = final_fingerprint
    result["runner_exit_code"] = exit_code
    result["ended_at"] = datetime.now(timezone.utc).isoformat()
    result["checks"] = {"failed": list(flow.FAILS)}
    result["result"] = "PASS" if exit_code == 0 else "FAIL"
    json_path = runtime.run_dir / "p4-roundtrip-result.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    delta = result.get("pixel_delta_draft_to_submitted", {})
    persisted = result.get("submitted_vs_server", {})
    md = [
        "# P4 pixel round-trip acceptance receipt",
        "",
        f"- Result: **{result['result']}**; runner exit `{exit_code}`.",
        f"- Run: `{runtime.run_id}`; ports: backend `{flow.BACKEND_PORT}`, frontend `{flow.FRONTEND_PORT}`.",
        f"- Release Gate SHA-256: `{result.get('gate_sha256')}`.",
        f"- P4_Extract.vue SHA-256: `{result.get('p4_source_sha256')}`.",
        f"- Frontend source fingerprint: `{result.get('frontend_source_sha256')}`.",
        f"- StyleSample `{sample_id}`; style `{style_id}`; training image `{training_image_id}`; asset `{asset_id}`.",
        f"- Extracted draft → submitted browser canvas: RGBA changed `{delta.get('rgba_different_pixel_count')}`, alpha changed `{delta.get('alpha_different_pixel_count')}`, bbox `{delta.get('bbox')}`.",
        f"- Submitted browser canvas → saved server file: RGBA changed `{persisted.get('rgba_different_pixel_count')}`, alpha changed `{persisted.get('alpha_different_pixel_count')}`, bbox `{persisted.get('bbox')}`.",
        f"- Server file SHA-256: `{(result.get('server_image') or {}).get('sha256_file')}`; pixel SHA-256: `{(result.get('server_image') or {}).get('sha256_rgba_pixels')}`.",
        f"- Same image id/hash/pixels after page refresh and backend restart: `{(result.get('after_restart') or {}).get('pixels_equal_initial_saved_file')}`.",
        f"- Evidence directory: `{runtime.run_dir}`.",
        "",
        "P4 business source was not modified by this test. Fixture is synthetic and says nothing about personal-style quality.",
    ]
    (runtime.run_dir / "p4-roundtrip-receipt.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"# summary p4_roundtrip: {len(flow.FAILS)} failed {flow.FAILS}")
    print(f"P4_RESULT={result['result']} RUNNER_EXIT={exit_code} RUN_DIR={runtime.run_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
