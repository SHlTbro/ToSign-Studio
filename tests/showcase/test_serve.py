from pathlib import Path
import importlib.util
import pytest

SPEC = importlib.util.spec_from_file_location('showcase_serve', Path(__file__).resolve().parents[2] / 'showcase' / 'serve.py')
serve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serve)


def test_refuses_unknown_data_before_import(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'existing.db').write_bytes(b'private')
    monkeypatch.setenv('YY_DB_URL', 'sqlite:///' + (data / 'existing.db').as_posix())
    monkeypatch.setenv('YY_STORAGE_ROOT', str(data / 'storage'))
    with pytest.raises(ValueError, match='unknown'):
        serve.prepare_environment()
    assert (data / 'existing.db').read_bytes() == b'private'


def test_explicit_paths_and_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('YY_DB_URL', raising=False)
    monkeypatch.delenv('YY_STORAGE_ROOT', raising=False)
    with pytest.raises(ValueError, match='explicit'):
        serve.prepare_environment()
    monkeypatch.setenv('YY_DB_URL', 'sqlite:///' + (tmp_path / 'new' / 'app.db').as_posix())
    monkeypatch.setenv('YY_STORAGE_ROOT', str(tmp_path / 'new' / 'storage'))
    serve.prepare_environment()
    assert __import__('os').environ['YY_AUTH_STRICT'] == '1'
    serve.prepare_environment()
    monkeypatch.setenv('YY_DB_URL', 'sqlite:///' + (tmp_path / 'other.db').as_posix())
    with pytest.raises(ValueError, match='unknown|binding'):
        serve.prepare_environment()


def test_missing_frontend_fails_before_app_import(tmp_path, monkeypatch):
    import sys
    monkeypatch.setenv('YY_DB_URL', 'sqlite:///' + (tmp_path / 'app.db').as_posix())
    monkeypatch.setenv('YY_STORAGE_ROOT', str(tmp_path / 'storage'))
    with pytest.raises(ValueError, match='Frontend build missing'):
        serve.build_app(tmp_path / 'no-dist')
    assert 'app.main' not in sys.modules
    assert not (tmp_path / 'app.db').exists()


def test_spa_annotation_and_static_namespace(tmp_path, monkeypatch):
    import asyncio
    import sys
    import types
    import httpx
    from fastapi import FastAPI
    monkeypatch.setenv('YY_DB_URL', 'sqlite:///' + (tmp_path / 'app.db').as_posix())
    monkeypatch.setenv('YY_STORAGE_ROOT', str(tmp_path / 'storage'))
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<html>frontend</html>')
    (dist / 'assets' / 'build.js').write_text('window.test=true;')
    module = types.ModuleType('app.main')
    module.app = FastAPI()
    monkeypatch.setitem(sys.modules, 'app.main', module)
    app = serve.build_app(dist)

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for path in ('/login', '/assets/example/annotate', '/batch/example/review'):
                response = await client.get(path)
                assert response.status_code == 200 and response.text == '<html>frontend</html>'
            assert (await client.get('/assets/build.js')).text == 'window.test=true;'
            assert (await client.get('/assets/missing.js')).status_code == 404
            response = await client.get('/api/v1/no-such-api')
            assert response.status_code == 404 and response.headers['content-type'] == 'application/json'
    asyncio.run(verify())
