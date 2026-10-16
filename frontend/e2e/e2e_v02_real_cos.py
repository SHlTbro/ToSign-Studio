# -*- coding: utf-8 -*-
"""Release-bound V02 real-Cos product layout and export acceptance."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

import e2e_full_chain as flow
from runtime_guard import _sha256_file, frontend_source_sha256


REAL_COS_IDS = ["cos_003", "cos_004", "cos_005", "cos_009", "cos_011",
                "cos_016", "cos_020", "cos_025", "cos_026", "cos_027"]
CN_TEXTS = ["星河", "清禾", "予安", "子墨", "若溪", "慕白", "云舒", "知夏", "景行", "安然",
            "明月", "星野", "书瑶", "嘉宁", "一诺", "初晴", "亦辰", "沐阳", "听澜", "小满"]


def eligible_real_cos():
    root = flow.REPO / "materials" / "b9-domain-expansion"
    benchmark_path = root / "fixed_benchmark_v2.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in benchmark["images"]}
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    metadata_by_id = {row["id"]: row for row in metadata}
    intake = json.loads((flow.REPO / "materials" / "b10-realgt" / "intake_manifest.json").read_text(encoding="utf-8"))
    b10_forbidden = {row["sha256"] for row in intake.get("images", [])
                     if row.get("eligible_primary_realgt") or row.get("reserve_eligible")}
    selected = []
    for cos_id in REAL_COS_IDS:
        row = rows.get(cos_id)
        meta = metadata_by_id.get(cos_id)
        if not row or row.get("role") != "development":
            raise RuntimeError(f"refusing non-development/SEALED image: {cos_id}")
        if not meta or not meta.get("rights_gate") == "private_local_research_accepted":
            raise RuntimeError(f"real-Cos local-use rights receipt missing for {cos_id}")
        source = root / row["path"]
        if not source.is_file() or _sha256_file(source) != row["sha256"]:
            raise RuntimeError(f"B9 development source hash mismatch: {cos_id}")
        if row["sha256"] in b10_forbidden:
            raise RuntimeError(f"B10 PRIMARY/RESERVE overlap: {cos_id}")
        with Image.open(source) as image:
            width, height = image.size
        selected.append({"id": cos_id, "path": source, "sha256": row["sha256"],
                         "role": row["role"], "rights_gate": meta["rights_gate"],
                         "width": width, "height": height, "size_bytes": source.stat().st_size,
                         "domain": row.get("domain")})
    if len(selected) != 10:
        raise RuntimeError(f"expected 10 eligible real photos, got {len(selected)}")
    return benchmark_path, selected


def template_slots(index: int):
    # Coordinates are normalized against each original image's own dimensions;
    # they are persisted as actual approved layout-template records.
    to_cn_x = 0.62 if index in {2, 4, 5, 8} else 0.04
    return [
        {"key": "to_cn", "text_template": "To: {cn}",
         "region": {"x_norm": to_cn_x, "y_norm": 0.035, "w_norm": 0.34 if to_cn_x else 0.56, "h_norm": 0.12}},
        {"key": "blessing", "text_template": "{blessing}",
         "region": {"x_norm": 0.04, "y_norm": 0.82, "w_norm": 0.62, "h_norm": 0.11}},
        {"key": "signature", "text_template": "{blogger}",
         "region": {"x_norm": 0.68, "y_norm": 0.87, "w_norm": 0.28, "h_norm": 0.08}},
    ]


def main() -> int:
    from playwright.sync_api import sync_playwright

    flow.FAILS = []
    runtime = flow.RunContext(flow.REPO, "v02-real-cos", flow.BACKEND_PORT, flow.FRONTEND_PORT)
    env = dict(os.environ, YY_DB_URL=f"sqlite:///{flow.DB}", YY_STORAGE_ROOT=flow.STORAGE,
               YY_E2E_BACKEND_PYTHON=str(flow.VENV_PY), YY_AUTH_STRICT="1")
    backend_pythonpath = os.environ.get("YY_E2E_BACKEND_PYTHONPATH")
    if backend_pythonpath:
        env["PYTHONPATH"] = backend_pythonpath
    benchmark_path, real_cos = eligible_real_cos()
    output_path = runtime.run_dir / "v02-real-cos-result.json"
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
    output_started_at: float | None = None
    output_records: list[dict] = []
    zip_members: list[str] = []
    cn_mapping: list[dict] = []
    result: dict = {"schema": 1, "result": "IN_PROGRESS", "scope": "B9 development real Cos only"}
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

            blessing_texts = ["天天开心", "顺顺利利"]
            for blessing in blessing_texts:
                st, blessing_row = flow.api("POST", "/api/v1/phrase-templates",
                                            json_body={"text": blessing, "enabled": True})
                flow.check(f"create-blessing-template:{blessing}", st == 201,
                           f"status={st}; {str(blessing_row)[:160]}")

            fixture_dir = runtime.run_dir / "real-cos-sources"
            fixture_dir.mkdir(parents=True, exist_ok=False)
            fixture_paths = []
            for source in real_cos:
                path = fixture_dir / f"{source['id']}{source['path'].suffix.lower()}"
                path.write_bytes(source["path"].read_bytes())
                if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                    raise RuntimeError(f"upload fixture changed source bytes: {source['id']}")
                source["test_copy_path"] = str(path)
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
            pending_by_source = {}
            for candidate_asset in pending:
                file_status, file_bytes = flow.api("GET", f"/api/v1/assets/{candidate_asset['asset_id']}/file")
                uploaded_hash = hashlib.sha256(file_bytes).hexdigest() if isinstance(file_bytes, bytes) else None
                match = next((source for source in real_cos if source["sha256"] == uploaded_hash), None)
                if file_status == 200 and match:
                    pending_by_source[match["id"]] = candidate_asset
            assets = [pending_by_source[cos["id"]] for cos in real_cos if cos["id"] in pending_by_source]
            flow.check("upload-10-assets", st == 200 and len(assets) == 10, f"status={st}, n={len(assets)}")
            if len(assets) != 10:
                raise RuntimeError(f"expected 10 uploaded assets, got {len(assets)}")

            for index, asset in enumerate(assets):
                asset_id = asset["asset_id"]
                source = real_cos[index]
                st, server_source = flow.api("GET", f"/api/v1/assets/{asset_id}/file")
                source_hash_matches = st == 200 and hashlib.sha256(server_source).hexdigest() == source["sha256"] if isinstance(server_source, bytes) else False
                flow.check(f"uploaded-real-source-hash-{source['id']}", source_hash_matches,
                           f"status={st}; expected={source['sha256']}")
                st, result = flow.api("POST", f"/api/v1/assets/{asset_id}/approve")
                flow.check(f"approve-{asset_id[:8]}", st == 200 and result.get("data", {}).get("status") == "approved",
                           f"status={st}")
                st, result = flow.api("POST", "/api/v1/layout-templates", json_body={
                    "name": f"V02 real Cos {source['id']}", "base_asset_id": asset_id,
                    "canvas_width": source["width"], "canvas_height": source["height"],
                    "slots": template_slots(index),
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
                cn = CN_TEXTS[index]
                cn_mapping.append({"row": row, "asset_id": asset_id,
                                   "source_cos_id": real_cos[index % 10]["id"], "cn": cn})
                page.fill(f'input[aria-label="第 {row} 行粉丝昵称"]', cn)
                page.select_option(f"#asset-{index}", asset_id)
                page.select_option(f"#template-{index}", template_id)
                blessing_select = page.locator(f"#blessing-{index}")
                if blessing_select.count():
                    values = blessing_select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                    if values:
                        blessing_select.select_option(values[index % len(values)])
            flow.check("20-row-form-filled", page.locator('input[aria-label$="行粉丝昵称"]').count() == 20)
            page.screenshot(path=str(runtime.run_dir / "v02-batch-20-rows.png"), full_page=True)
            output_started_at = time.monotonic()
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

            out_dir = runtime.run_dir / "server-png-outputs"
            out_dir.mkdir()
            for index, item in enumerate(item_rows):
                rel = item.get("output_relative_path")
                file_path = Path(flow.STORAGE) / rel if rel else None
                exists = bool(file_path and file_path.is_file())
                output_bytes = file_path.read_bytes() if exists else b""
                copied_file = out_dir / f"item-{index + 1:02}.png" if exists else None
                if copied_file:
                    copied_file.write_bytes(output_bytes)
                try:
                    with Image.open(__import__("io").BytesIO(output_bytes)) as opened:
                        dimensions = list(opened.size)
                        opened.verify()
                except Exception:
                    dimensions = None
                original = real_cos[index % 10]
                output_records.append({"item_id": item.get("item_id"), "status": item.get("status"),
                                      "asset_id": item.get("asset_id"), "template_id": item.get("template_id"),
                                      "base_asset_id_via_template": assets_by_template.get(item.get("template_id")),
                                      "path": rel, "exists": exists,
                                      "local_evidence_path": str(copied_file) if copied_file else None,
                                      "sha256": hashlib.sha256(output_bytes).hexdigest() if exists else None,
                                      "dimensions": dimensions,
                                      "expected_source_dimensions": [original["width"], original["height"]],
                                      "dimension_match": dimensions == [original["width"], original["height"]],
                                      "fallback_used": item.get("fallback_used"),
                                      "fallback_map": item.get("fallback_map"),
                                      "cn": item.get("cn"), "blessing": item.get("blessing")})
            flow.check("20-server-output-files", sum(1 for record in output_records if record["exists"]) == 20,
                       f"exists={sum(1 for record in output_records if record['exists'])}")
            flow.check("all-outputs-preserve-source-dimensions",
                       all(record.get("dimension_match") for record in output_records))
            flow.check("all-outputs-render-blessing-text",
                       all(isinstance(record.get("blessing"), str) and record["blessing"].strip()
                           for record in output_records))
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
            if export_path.is_file():
                with zipfile.ZipFile(export_path) as archive:
                    zip_members = archive.namelist()
                    png_members = [name for name in zip_members if name.lower().endswith(".png")]
                    flow.check("export-contains-20-png", len(png_members) == 20, f"pngs={len(png_members)}")
            result = {
                "run_id": runtime.run_id, "scope": "B9 development real Cos only; product output/layout acceptance; not a model benchmark",
                "release_gate_sha256": _sha256_file(flow.REPO / ".ai-hub" / "parallel-lines" / "line-b" / "gates" / "B-GATE-RELEASE.json"),
                "frontend_source_sha256": frontend_source_sha256(flow.REPO / "frontend"),
                "source_manifest_sha256": _sha256_file(benchmark_path),
                "real_cos_sources": [{k: (str(v) if isinstance(v, Path) else v) for k,v in source.items()} for source in real_cos],
                "cn_mapping": cn_mapping,
                "blessing_texts": blessing_texts,
                "elapsed_seconds": time.monotonic() - output_started_at if output_started_at else None,
                "job_id": job_id, "job_status": job.get("status"), "item_count": len(item_rows),
                "status_counts": {status: sum(1 for item in item_rows if item.get("status") == status)
                                  for status in sorted({str(item.get("status")) for item in item_rows})},
                "asset_count": len(assets), "template_count": len(templates_by_asset), "outputs": output_records,
                "export_path": str(export_path) if export_path else None,
                "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest() if export_path and export_path.is_file() else None,
                "zip_members": zip_members, "console_errors": console_errors, "request_errors": request_errors,
                "success_count": sum(1 for item in item_rows if item.get("status") in {"completed", "completed_with_warning", "approved"}),
                "failed_count": sum(1 for item in item_rows if item.get("status") in {"failed", "cancelled", "partial"}),
                "fallback_count": sum(1 for item in item_rows if item.get("fallback_used")),
                "dimension_mismatches": sum(1 for record in output_records if not record.get("dimension_match")),
                "blessing_count": sum(1 for record in output_records if record.get("blessing")),
                "result": "PASS" if not flow.FAILS else "FAIL",
            }
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
    result.update({"runner_exit_code": exit_code, "result": "PASS" if exit_code == 0 else "FAIL",
                   "checks": {"failed": list(flow.FAILS)},
                   "runtime": runtime.fingerprints("stopped", exit_code),
                   "run_dir": str(runtime.run_dir)})
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt = ["# V02 real-Cos product acceptance receipt", "",
               f"- Result: **{result['result']}**; runner exit `{exit_code}`.",
               f"- Release Gate SHA-256: `{result.get('release_gate_sha256')}`.",
               f"- Frontend source fingerprint: `{result.get('frontend_source_sha256')}`.",
               f"- B9 benchmark/role manifest SHA-256: `{_sha256_file(benchmark_path)}`; only role=`development` images selected.",
               f"- Assets `{len(real_cos)}`; CN rows `{len(cn_mapping)}`; job `{result.get('job_id')}` status `{result.get('job_status')}`.",
               f"- success `{result.get('success_count')}`; failed `{result.get('failed_count')}`; fallback `{result.get('fallback_count')}`; elapsed `{result.get('elapsed_seconds')}` seconds.",
               f"- PNG outputs copied from server: `{sum(1 for r in output_records if r.get('exists'))}/20`; dimensions mismatches `{result.get('dimension_mismatches')}`.",
               f"- Export ZIP: `{result.get('export_path')}`; SHA-256 `{result.get('export_sha256')}`.",
               f"- Run/evidence directory: `{runtime.run_dir}`.",
               "- Scope is product layout and output only; this does not assess personal handwriting quality or model benchmarks.", ""]
    (runtime.run_dir / "v02-real-cos-receipt.md").write_text("\n".join(receipt), encoding="utf-8")
    print(f"# summary v02_real_cos: {len(flow.FAILS)} failed {flow.FAILS}")
    print(f"V02_REAL_RESULT={result['result']} RUNNER_EXIT={exit_code} RUN_DIR={runtime.run_dir}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
