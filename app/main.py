"""App factory: ApiError handler + all nine domain routers + health (auth-exempt per v0.2.1)."""

import uuid

from app.core.font_shaping import bootstrap as _font_shaping_bootstrap
_font_shaping_bootstrap()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import audit as _audit_models  # noqa: F401  (registers audit_logs table)
from app.core.db import Base, get_engine
from app.core.migration import ensure_schema
from app.core.envelope import ok
from app.core.errors import ApiError, api_error_handler
from app.domains.assets_upload import models as _assets_models  # noqa: F401  (registers assets)
from app.domains.assets_upload.router import router as assets_router
from app.domains.auth import models as _auth_models  # noqa: F401  (registers users/invites)
from app.domains.auth.router import router as auth_router
from app.domains.export_guard.router import router as export_router
from app.domains.phrases import models as _phrases_models  # noqa: F401  (registers blessing_templates)
from app.domains.phrases.router import router as phrases_router
from app.domains.presets import models as _presets_models  # noqa: F401  (registers blogger_name_presets)
from app.domains.presets.router import router as presets_router
from app.domains.usage.router import router as usage_router
from app.domains.training_sets import models as _training_models  # noqa: F401  (registers training_images)
from app.domains.training_sets.router import router as training_router
from app.domains.training_sets import occurrence_models as _occurrence_models  # noqa: F401
from app.domains.training_sets.occurrence_router import router as occurrence_router
from app.domains.styles import models as _styles_models  # noqa: F401  (registers style tables)
from app.domains.styles.router import router as styles_router
from app.domains.font_presets.router import router as font_presets_router
from app.domains.backups import models as _backups_models  # noqa: F401  (registers backups table)
from app.domains.backups.router import router as backups_router
from app.domains.render import models as _render_models  # noqa: F401  (registers render tables)
from app.domains.render.router import router as render_router
from app.domains.render.router import router_workers as workers_router
from app.domains.render.router import router_coverage as coverage_router
from app.domains.templates import models as _templates_models  # noqa: F401  (registers layout_templates)
from app.domains.templates.router import router as templates_router
from app.domains.auditlog.router import router as auditlog_router
from app.domains.user_export.router import router as user_export_router
from app.domains.extraction.router import router as extraction_router


def create_app() -> FastAPI:
    app = FastAPI(title="YY Backend", version="0.3")
    app.add_exception_handler(ApiError, api_error_handler)
    # DF-P0-01 (b-migration-api 2026-09-29, Plan A): central migrations run on
    # EVERY startup path (repo uvicorn / installer / package launchers / tests
    # all enter through create_app) BEFORE any ORM access. On a fresh DB both
    # steps no-op ("table absent") and create_all builds the complete model;
    # on a legacy DB pending ALTERs land first, so the ORM never reads a
    # stale schema. ensure_schema keeps the D06 semantics: per-step
    # transactional DDL + ledger row, pre-migration backup, fail-stop on any
    # failure - a failed migration aborts startup instead of serving the new
    # code against an old schema.
    ensure_schema(get_engine())
    Base.metadata.create_all(bind=get_engine())
    # T75 (D-T65-6): resolve the font signing key at startup; without the env
    # a per-process random key is generated and a WARNING is logged.
    from app.domains.font_presets import service as _font_service

    _font_service.ensure_signing_secret()
    # P01-BE (E18): the X-User-Id dev shim stays open unless YY_AUTH_STRICT=1;
    # make that loudly visible at every startup instead of silently insecure.
    from app.core import identity as _identity

    _identity.warn_if_shim_enabled()
    app.include_router(auth_router)
    app.include_router(presets_router)
    app.include_router(phrases_router)
    app.include_router(assets_router)
    app.include_router(export_router)
    app.include_router(usage_router)
    app.include_router(training_router)
    app.include_router(occurrence_router)
    app.include_router(styles_router)
    app.include_router(font_presets_router)
    app.include_router(render_router)
    app.include_router(workers_router)
    app.include_router(coverage_router)  # B10-REALGT 后续 FB-C 薄预检（coverage/check）
    app.include_router(templates_router)
    app.include_router(backups_router)
    app.include_router(auditlog_router)
    app.include_router(user_export_router)
    app.include_router(extraction_router)  # T59-R1 (batch B5-01): detect-text / extract

    # GWT-G7-csrf (v0.2.1 section 11): cookie-authenticated write requests must
    # carry X-Requested-With: XMLHttpRequest. Requests authenticated by the
    # X-User-Id dev shim carry no session cookie, so existing test flows are
    # unaffected (D-T10-4).
    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.cookies.get("session") and request.headers.get("x-requested-with") != "XMLHttpRequest":
                return JSONResponse(
                    status_code=403,
                    media_type="application/problem+json",
                    content={
                        "type": "about:blank",
                        "title": "Forbidden",
                        "status": 403,
                        "detail": "missing X-Requested-With header for cookie-authenticated write",
                        "instance": request.url.path,
                        "code": "FORBIDDEN",
                        "retryable": False,
                        "trace_id": uuid.uuid4().hex,
                    },
                )
        return await call_next(request)

    # Section 7 worker recovery + section 1.6 draft TTL cleanup at startup.
    from sqlalchemy.orm import sessionmaker as _sm

    from app.core.db import get_engine as _get_engine
    from app.domains.render import service as _render_service

    _bootstrap = _sm(bind=_get_engine())()
    try:
        _render_service.recover_stale_running(_bootstrap)
        _render_service.cleanup_expired_drafts(_bootstrap)
    finally:
        _bootstrap.close()

    # T75 (D-T10R-1/D-T70-1): resident daemon workers for the render queue and
    # the backup builds; they die with the process, nothing hangs on exit.
    _render_service.start_worker()
    from app.domains.backups import service as _backups_service

    _backups_service.start_worker()

    # S06 (B-DATA-STYLES 2026-09-27, the ONE authorized main.py change of the
    # batch): resident style-run worker - restart recovery + queue claiming +
    # engine subprocesses (dummy test engine default; real m1 via config/env).
    # YY_STYLE_WORKER=0 disables it (probes drive worker.execute_run directly).
    from app.domains.styles import worker as _styles_worker

    _styles_worker.start_worker()

    @app.get("/api/v1/health")
    def health():
        return ok({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8101)
