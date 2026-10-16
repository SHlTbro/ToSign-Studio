"""Local showcase launcher; refuses unclaimed data before importing the application."""
import argparse
import json
import os
import secrets
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = '.yy-showcase.json'


def prepare_environment():
    """Claim fresh explicit paths, or reopen only this launcher's recorded pair."""
    url = os.environ.get('YY_DB_URL', '')
    raw = os.environ.get('YY_STORAGE_ROOT', '')
    if not url.startswith('sqlite:///') or not raw:
        raise ValueError('Set explicit YY_DB_URL=sqlite:///ABSOLUTE_PATH and YY_STORAGE_ROOT')
    db = Path(url[len('sqlite:///'):])
    storage = Path(raw)
    if not db.is_absolute() or not storage.is_absolute():
        raise ValueError('Both explicit data paths must be absolute')
    db, storage = db.resolve(), storage.resolve()
    if db == storage or storage in db.parents:
        raise ValueError('Database must be outside the storage directory')
    marker = storage / MARKER
    binding = {'schema': 'yy-showcase/1', 'database': str(db), 'storage': str(storage)}
    if marker.is_file():
        if json.loads(marker.read_text(encoding='utf-8')) != binding:
            raise ValueError('Showcase data binding differs; refusing unknown database')
    else:
        if db.exists() or (storage.exists() and any(storage.iterdir())):
            raise ValueError('Refusing unknown nonempty data; choose fresh database and storage paths')
        db.parent.mkdir(parents=True, exist_ok=True)
        storage.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(binding), encoding='utf-8')
    os.environ['YY_DB_URL'] = 'sqlite:///' + db.as_posix()
    os.environ['YY_STORAGE_ROOT'] = str(storage)
    os.environ['YY_AUTH_STRICT'] = '1'
    os.environ['YY_STYLE_WORKER'] = '0'
    return db, storage


def build_app(dist=None):
    prepare_environment()
    dist = Path(dist or ROOT / 'frontend' / 'dist').resolve()
    if not (dist / 'index.html').is_file():
        raise ValueError('Frontend build missing; run npm ci then npm run build in frontend')
    sys.path.insert(0, str(ROOT))
    # app.main creates its global app once at import; do not invoke its factory twice.
    from app.main import app
    from fastapi import HTTPException, Request, Response
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    @app.middleware('http')
    async def local_process_identity(request, call_next):
        response = await call_next(request)
        if request.url.path == '/api/v1/health' and os.environ.get('YY_SHOWCASE_INSTANCE'):
            response.headers['X-YY-Showcase-Instance'] = os.environ['YY_SHOWCASE_INSTANCE']
            response.headers['X-YY-Showcase-Process'] = str(os.getpid())
        return response

    if os.environ.get('YY_SHOWCASE_STOP_TOKEN'):
        @app.post('/_showcase/stop', include_in_schema=False)
        def stop_demo(request: Request):
            if not secrets.compare_digest(request.headers.get('X-YY-Showcase-Stop', ''), os.environ['YY_SHOWCASE_STOP_TOKEN']):
                raise HTTPException(status_code=403, detail='Forbidden')
            app.state.showcase_server.should_exit = True
            return Response(status_code=204)

    # The frontend also owns /assets/:id/annotate. Register its refresh route
    # before the build's /assets static mount, which otherwise consumes it.
    @app.get('/assets/{asset_id}/annotate', include_in_schema=False)
    def annotate_spa(asset_id: str):
        return FileResponse(dist / 'index.html')

    app.mount('/assets', StaticFiles(directory=str(dist / 'assets')), name='showcase-assets')

    @app.get('/{path:path}', include_in_schema=False)
    def spa(path: str):
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(status_code=404, detail='Not Found')
        target = (dist / path).resolve()
        if not target.is_relative_to(dist):
            raise HTTPException(status_code=404, detail='Not Found')
        return FileResponse(target if target.is_file() else dist / 'index.html')
    return app


def main():
    from processes import record_identity
    record_identity()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18916)
    parser.add_argument('--dist', type=Path, default=None)
    parser.add_argument('--init-data', action='store_true', help='claim fresh explicit paths before first_invite.py')
    args = parser.parse_args()
    if args.init_data:
        prepare_environment()
        print('Showcase data paths initialized; next run installer/first_invite.py')
        return
    import uvicorn
    app = build_app(args.dist)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port))
    app.state.showcase_server = server
    server.run()


if __name__ == '__main__':
    main()

