# -*- coding: utf-8 -*-
"""Release-bound volume smoke: upload 10 real PNG files, render 20 CN rows.

All assets are generated geometric fixtures. This certifies the engineering
volume path only; it does not certify real-Cos visual quality.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import e2e_full_chain as flow


def fixture_png(index: int) -> bytes:
    import io
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 960), ((40 + index * 17) % 230, (90 + index * 29) % 230, (150 + index * 11) % 230))
    draw = ImageDraw.Draw(image)
    margin = 35 + index * 2
    draw.rectangle((margin, margin, 720 - margin, 960 - margin), outline=(245, 245, 245), width=12)
    draw.ellipse((180 + index * 7, 190, 520 - index * 3, 530), outline=(250, 210, 80), width=14)
    draw.line((80, 700 + index, 640, 640 - index), fill=(255, 255, 255), width=10)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def main() -> int:
    from playwright.sync_api import sync_playwright

    flow.FAILS = []
    runtime = flow.RunContext(flow.REPO, "v02-volume", flow.BACKEND_PORT, flow.FRONTEND_PORT)
    env = dict(os.environ, YY_DB_URL=f"sqlite:///{flow.DB}", YY_STORAGE_ROOT=flow.STORAGE,
               YY_E2E_BACKEND_PYTHON=str(flow.VENV_PY), YY_AUTH_STRICT="1")
    backend_pythonpath = os.environ.get("YY_E2E_BACKEND_PYTHONPATH")
    if backend_pythonpath:
        env["PYTHONPATH"] = backend_pythonpath
    output_path = runtime.run_dir / "v02-volume-result.json"
    exit_code = 1
    browser = None
    page = None
    console_errors: list[str] = []
    request_errors: list[str] = []
    assets: list[dict] = []
    templates_by_asset: dict[str, str] = {}
    assets_by_template: dict[str, str] = {}
    job_id: str | None = None
    item_rows: list[dict] = []
    export_path: Path | None = None
    runtime.install_output_capture()
    try:
        runtime.acquire()
        backend = runtime.start("backend", [str(flow.VENV_PY), "-m", "uvicorn", "app.main:app",
                                              "--host", "127.0.0.1", "--port", str(flow.BACKEND_PORT)],
                                flow.REPO, env, flow.BACKEND_PORT)
        node = flow.shutil.which("node")
        vite = flow.REPO / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("direct Node/Vite executable is unavailable")
        frontend = runtime.start("frontend", [node, str(vite), "--port", str(flow.FRONTEND_PORT),
                                                "--strictPort", "--host", "127.0.0.1"],
                                 flow.REPO / "frontend", env, flow.FRONTEND_PORT)
        flow.check("boot-backend-owned", runtime.wait_owned_listener("backend"))
        if not flow.FAILS or flow.FAILS[-1] != "boot-backend-owned":
            pass
        if backend.poll() is not None:
            raise RuntimeError("backend exited during startup")
        flow.check("boot-backend", flow.wait_http(flow.backend_url("/api/v1/health")))
        flow.check("seed-invites", flow.seed_invites(env))
        flow.check("boot-frontend-owned", runtime.wait_owned_listener("frontend"))
        if frontend.poll() is not None:
            raise RuntimeError("frontend exited during startup")
        flow.check("boot-frontend", flow.wait_http(flow.frontend_url("/")))
        if flow.FAILS:
            raise RuntimeError(f"service preflight failed: {flow.FAILS}")

        import secrets

        password = "V02-" + secrets.token_urlsafe(12) + "!6Aa"
        st, body = flow.api("POST", "/api/v1/auth/register", json_body={
            "username": "v02admin", "password": password, "display_name": "V02 admin", "invite_code": "seed-admin"})
        flow.check("register-admin", st == 201, str(body)[:180])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=flow.low_memory_browser_args())
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("console", lambda msg: console_errors.append(msg.text[:300]) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)[:300]))
            page.on("requestfailed", lambda req: request_errors.append(f"{req.method} {req.url}: {req.failure}"[:300]))
            page.goto(flow.frontend_url("/login"))
            page.fill("#login-username", "v02admin")
            page.fill("#login-password", password)
            page.click('button[type="submit"]')
            page.wait_for_url("**/change-password", timeout=30000)
            new_password = "V02-New-" + secrets.token_urlsafe(10) + "!8Bb"
            page.wait_for_selector('input[type="password"]', timeout=20000)
            for selector, value in zip(page.locator('input[type="password"]').all(),
                                       (password, new_password, new_password)):
                selector.fill(value)
            page.click('button[type="submit"]')
            # The submit may navigate before the wait is installed; poll current URL
            # as the primary full-chain harness does.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"):
                    break
                page.wait_for_timeout(300)
            if not page.url.rstrip("/").endswith(f":{flow.FRONTEND_PORT}"):
                raise RuntimeError(f"password change did not return to home: {page.url}")
            flow.bind_api_session(page)

            fixture_dir = runtime.run_dir / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=False)
            fixture_paths = []
            for index in range(10):
                path = fixture_dir / f"cos-fixture-{index + 1:02}.png"
                path.write_bytes(fixture_png(index))
                fixture_paths.append(str(path))

            # Exercise the actual multi-file upload control from the batch page.
            page.goto(flow.frontend_url("/batch"))
            page.wait_for_selector('input[aria-label="第 1 行粉丝昵称"]', timeout=20000)
            page.set_input_files('input[type="file"]', fixture_paths)
            page.wait_for_url("**/assets*", timeout=30000)
            page.wait_for_timeout(500)
            # The read/list contract intentionally omits original filenames. This is
            # an isolated database, so the ten owner-scoped pending rows are the
            # precise set created by the real batch upload control.
            st, body = flow.api("GET", "/api/v1/assets?type=cos&status=pending&page=1&page_size=100")
            pending = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
            assets = pending
            flow.check("upload-10-assets", st == 200 and len(assets) == 10, f"status={st}, n={len(assets)}")
            if len(assets) != 10:
                raise RuntimeError(f"expected 10 uploaded assets, got {len(assets)}")

            for asset in assets:
                asset_id = asset["asset_id"]
                st, result = flow.api("POST", f"/api/v1/assets/{asset_id}/approve")
                flow.check(f"approve-{asset_id[:8]}", st == 200 and result.get("data", {}).get("status") == "approved",
                           f"status={st}")
                st, result = flow.api("POST", "/api/v1/layout-templates", json_body={
                    "name": f"V02 fixture {asset_id[:8]}", "base_asset_id": asset_id,
                    "canvas_width": 720, "canvas_height": 960,
                    "slots": [
                        {"key": "to_cn", "text_template": "To: {cn}", "region": {"x_norm": 0.08, "y_norm": 0.08, "w_norm": 0.55, "h_norm": 0.12}},
                        {"key": "blessing", "text_template": "{blessing}", "region": {"x_norm": 0.08, "y_norm": 0.72, "w_norm": 0.72, "h_norm": 0.12}},
                        {"key": "signature", "text_template": "{blogger}", "region": {"x_norm": 0.62, "y_norm": 0.84, "w_norm": 0.3, "h_norm": 0.1}},
                    ],
                })
                flow.check(f"create-template-{asset_id[:8]}", st == 201, f"status={st}")
                template_id = result.get("data", {}).get("template_id")
                if st != 201 or not template_id:
                    raise RuntimeError(f"template create failed for asset {asset_id}")
                st, result = flow.api("POST", f"/api/v1/layout-templates/{template_id}/approve")
                flow.check(f"approve-template-{asset_id[:8]}", st == 200, f"status={st}")
                templates_by_asset[asset_id] = template_id
                assets_by_template[template_id] = asset_id

            st, preset = flow.api("POST", "/api/v1/blogger-name-presets",
                                  json_body={"name": "V02 星河", "is_default": True})
            flow.check("seed-signature", st == 201, f"status={st}")

            page.goto(flow.frontend_url("/batch"))
            page.wait_for_selector('input[aria-label="第 1 行粉丝昵称"]', timeout=20000)
            for index in range(1, 20):
                page.get_by_role("button", name="新增一行").click()
            for index in range(20):
                row = index + 1
                asset = assets[index % 10]
                asset_id = asset["asset_id"]
                template_id = templates_by_asset[asset_id]
                cn = f"祝福{index + 1:02}星河"
                page.fill(f'input[aria-label="第 {row} 行粉丝昵称"]', cn)
                page.select_option(f"#asset-{index}", asset_id)
                page.select_option(f"#template-{index}", template_id)
            flow.check("20-row-form-filled", page.locator('input[aria-label$="行粉丝昵称"]').count() == 20)
            page.screenshot(path=str(runtime.run_dir / "v02-batch-20-rows.png"), full_page=True)
            page.get_by_role("button", name="正式生成").click()
            page.wait_for_url("**/batch/*/review", timeout=60000)
            job_id = page.url.split("/batch/")[1].split("/review")[0]

            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                st, detail = flow.api("GET", f"/api/v1/render-jobs/{job_id}")
                data = detail.get("data", {}) if isinstance(detail, dict) else {}
                job = data.get("job", {})
                item_rows = data.get("items", [])
                if st == 200 and len(item_rows) == 20 and job.get("status") in {"succeeded", "partial", "failed", "cancelled"}:
                    break
                time.sleep(1)
            flow.check("job-has-20-items", len(item_rows) == 20, f"n={len(item_rows)}")
            flow.check("job-terminal", job.get("status") in {"succeeded", "partial", "failed", "cancelled"}, str(job.get("status")))
            if len(item_rows) != 20:
                raise RuntimeError(f"render job returned {len(item_rows)} items")

            output_records = []
            for item in item_rows:
                rel = item.get("output_relative_path")
                file_path = Path(flow.STORAGE) / rel if rel else None
                exists = bool(file_path and file_path.is_file())
                output_records.append({"item_id": item.get("item_id"), "status": item.get("status"),
                                      "asset_id": item.get("asset_id"), "template_id": item.get("template_id"),
                                      "base_asset_id_via_template": assets_by_template.get(item.get("template_id")),
                                      "path": rel, "exists": exists,
                                      "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest() if exists else None})
            flow.check("20-server-output-files", sum(1 for record in output_records if record["exists"]) == 20,
                       f"exists={sum(1 for record in output_records if record['exists'])}")
            flow.check("10-assets-mapped-via-approved-templates",
                       len({record["base_asset_id_via_template"] for record in output_records}) == 10
                       and all(record["base_asset_id_via_template"] for record in output_records))
            flow.check("10-templates-mapped", len({record["template_id"] for record in output_records}) == 10)

            # Approve each completed item through the real browser review UI.
            for _ in range(20):
                button = page.locator('button:has-text("通过")').first
                if button.count() == 0 or not button.is_visible():
                    break
                button.click()
                page.wait_for_timeout(120)
            page.wait_for_timeout(500)
            st, detail = flow.api("GET", f"/api/v1/render-jobs/{job_id}")
            reviewed = detail.get("data", {}).get("items", []) if isinstance(detail, dict) else []
            approved = sum(1 for item in reviewed if item.get("status") == "approved")
            flow.check("reviewed-items", approved == 20, f"approved={approved}")
            page.screenshot(path=str(runtime.run_dir / "v02-review-20-items.png"), full_page=True)

            st, exported = flow.api("POST", f"/api/v1/render-jobs/{job_id}/export")
            flow.check("export-20-items", st == 200, f"status={st}")
            st, outputs = flow.api("GET", f"/api/v1/render-jobs/{job_id}/outputs")
            artifacts = outputs.get("data", {}).get("export_artifacts", []) if isinstance(outputs, dict) else []
            zip_artifact = next((item for item in artifacts if str(item.get("relative_path", "")).lower().endswith(".zip")
                                 or str(item.get("filename", "")).lower().endswith(".zip")), None)
            if zip_artifact is None and artifacts:
                zip_artifact = artifacts[0]
            if not zip_artifact:
                raise RuntimeError("export response did not contain an artifact")
            st, blob = flow.api("GET", f"/api/v1/render-jobs/{job_id}/exports/{zip_artifact['artifact_id']}/file")
            export_path = runtime.run_dir / "v02-export.zip"
            if st == 200 and isinstance(blob, bytes):
                export_path.write_bytes(blob)
            flow.check("download-export-file", st == 200 and export_path.is_file() and export_path.stat().st_size > 1000,
                       f"status={st}")
            zip_members: list[str] = []
            if export_path.is_file():
                with zipfile.ZipFile(export_path) as archive:
                    zip_members = archive.namelist()
                    png_members = [name for name in zip_members if name.lower().endswith(".png")]
                    flow.check("export-contains-20-png", len(png_members) == 20, f"pngs={len(png_members)}")
            result = {
                "run_id": runtime.run_id, "scope": "synthetic geometric engineering-volume fixtures; real-Cos visual NOT_TESTED",
                "job_id": job_id, "job_status": job.get("status"), "item_count": len(item_rows),
                "status_counts": {status: sum(1 for item in item_rows if item.get("status") == status)
                                  for status in sorted({str(item.get("status")) for item in item_rows})},
                "asset_count": len(assets), "template_count": len(templates_by_asset), "outputs": output_records,
                "export_path": str(export_path) if export_path else None,
                "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest() if export_path and export_path.is_file() else None,
                "zip_members": zip_members, "console_errors": console_errors, "request_errors": request_errors,
                "result": "PASS_ENGINEERING_ONLY" if not flow.FAILS else "FAIL",
            }
            output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            exit_code = 1 if flow.FAILS else 0
    except Exception as exc:
        print(f"HARNESS ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        exit_code = 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        print(f"# summary v02_volume: {len(flow.FAILS)} failed {flow.FAILS}")
        try:
            runtime.stop_all(exit_code)
        finally:
            runtime.restore_output_capture()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
