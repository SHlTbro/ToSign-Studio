"""Reproducible CPU-only API demonstration. Uses only a fresh isolated data directory.

python showcase/demo.py --output ABSOLUTE_EMPTY_DIRECTORY [--port 18916]
The API approvals below are test actions, not human or independent acceptance.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'showcase'))
from serve import prepare_environment
from processes import terminate_owned


def run(output, port, dist=None, *, startup_timeout=20):
    import httpx
    from PIL import Image, ImageChops, ImageDraw
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Demo output must be new or empty; existing data is never reused')
    output.mkdir(parents=True, exist_ok=True)
    os.environ['YY_DB_URL'] = 'sqlite:///' + (output / 'showcase.db').as_posix()
    os.environ['YY_STORAGE_ROOT'] = str(output / 'storage')
    prepare_environment()
    invite = 'DEMO-' + secrets.token_hex(4).upper()
    subprocess.run([sys.executable, str(ROOT / 'installer' / 'first_invite.py'), invite, '--uses', '1', '--yes'], check=True)
    username = 'demo_' + secrets.token_hex(5)
    password, changed = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS ' + name, flush=True)
    base = f'http://127.0.0.1:{port}'
    client = httpx.Client(base_url=base, timeout=45, trust_env=False, headers={'X-Requested-With': 'XMLHttpRequest'})
    def req(method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code != expected:
            raise AssertionError(f'{method} {path}: {response.status_code} {response.text[:600]}')
        # httpx does not implement the browser's trusted-loopback exception for
        # Secure cookies. Forward only this server-issued session to our fixed
        # loopback origin; the API's Secure/HttpOnly cookie policy stays intact.
        if response.cookies.get('session'):
            client.headers['Cookie'] = 'session=' + response.cookies['session']
        return response
    def launch():
        # Never send test credentials or write requests to a preexisting listener.
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('Demo port already has a listener; choose another port')
        log = (output / 'server.log').open('ab')
        instance = secrets.token_hex(24)
        stop_token = secrets.token_hex(32)
        process_record = output / ('process-' + instance + '.json')
        child_env = dict(os.environ, YY_SHOWCASE_INSTANCE=instance, YY_SHOWCASE_STOP_TOKEN=stop_token, YY_SHOWCASE_PROCESS_RECORD=str(process_record))
        command = [sys.executable, str(ROOT / 'showcase' / 'serve.py'), '--port', str(port)]
        if dist is not None:
            command.extend(['--dist', str(Path(dist).resolve())])
        process = subprocess.Popen(command, cwd=output, stdout=log, stderr=subprocess.STDOUT, env=child_env)
        log.close()
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Server exited; inspect server.log')
            try:
                ready = client.get('/api/v1/health', timeout=1)
                if ready.status_code == 200 and ready.headers.get('X-YY-Showcase-Instance') == instance:
                    process.server_pid = int(ready.headers['X-YY-Showcase-Process'])
                    process.stop_token = stop_token
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        # A timed-out server may already be listening. Try its private token
        # first, then use its recorded OS identity if startup is still stuck.
        try:
            client.post('/_showcase/stop', headers={'X-YY-Showcase-Stop': stop_token}, timeout=2)
            process.wait(timeout=2)
        except (httpx.HTTPError, subprocess.TimeoutExpired):
            pass
        # Even a successful HTTP response + exited redirector must be checked
        # against the recorded real child's process handle.
        terminate_owned(process, process_record, instance)
        raise RuntimeError('Server did not become healthy')
    def stop(process):
        # Graceful local shutdown also closes a Windows venv redirector's child.
        # This endpoint exists only for a demo child given a random secret token.
        req('POST', '/_showcase/stop', 204, headers={'X-YY-Showcase-Stop': process.stop_token})
        process.wait(timeout=20)
    process = launch()
    try:
        check('demo shutdown rejects invalid token', req('POST', '/_showcase/stop', 403, headers={'X-YY-Showcase-Stop': 'invalid'}).status_code == 403)
        check('SPA index and client-route fallback', '<html' in req('GET', '/').text.lower() and '<html' in req('GET', '/login').text.lower())
        check('annotation route survives refresh beside static assets', '<html' in req('GET', '/assets/example/annotate').text.lower() and req('GET', '/assets/no-such-build.js', 404).status_code == 404)
        check('unknown API remains JSON 404', 'application/json' in req('GET', '/api/v1/no-such-api', 404).headers['content-type'])
        check('strict auth refuses development header', client.get('/api/v1/auth/me', headers={'X-User-Id': '1'}).status_code == 401)
        req('POST', '/api/v1/auth/register', 201, json={'username': username, 'password': password, 'display_name': 'API demo test account', 'invite_code': invite})
        login = req('POST', '/api/v1/auth/login', json={'username': username, 'password': password})
        check('server preserves Secure HttpOnly session cookie', 'secure' in login.headers['set-cookie'].lower() and 'httponly' in login.headers['set-cookie'].lower())
        req('POST', '/api/v1/auth/change-password', json={'old_password': password, 'new_password': changed})
        check('first invite registration and password rotation', req('GET', '/api/v1/auth/me').json()['data']['username'] == username)
        image = Image.new('RGB', (1200, 900), '#f7f3e8')
        draw = ImageDraw.Draw(image)
        draw.rectangle((45,45,1155,855), outline='#a5b5aa', width=3)
        draw.rectangle((65,65,1135,835), outline='#d0d9ce', width=2)
        draw.line((150,295,1050,295), fill='#a5b5aa', width=2)
        draw.line((150,665,1050,665), fill='#a5b5aa', width=2)
        for x,y,r in [(130,130,24),(1070,770,24),(1070,130,12),(130,770,12)]:
            draw.ellipse((x-r,y-r,x+r,y+r), fill='#a5b5aa')
        png = io.BytesIO(); image.save(png, 'PNG')
        (output / 'base-original.png').write_bytes(png.getvalue())
        asset = req('POST', '/api/v1/assets', 201, files=[('files', ('original-demo.png', png.getvalue(), 'image/png'))]).json()['data']['items'][0]['asset_id']
        req('POST', f'/api/v1/assets/{asset}/approve')
        template = {'name': 'Original two-slot M0 demo', 'base_asset_id': asset, 'canvas_width': 1200, 'canvas_height': 900, 'slots': [
            {'key': 'to_cn', 'text_template': 'To: {cn}', 'region': {'x_norm': .15, 'y_norm': .18, 'w_norm': .7, 'h_norm': .12}},
            {'key': 'blessing', 'text_template': '{blessing}', 'region': {'x_norm': .15, 'y_norm': .43, 'w_norm': .7, 'h_norm': .18}}]}
        tid = req('POST', '/api/v1/layout-templates', 201, json=template).json()['data']['template_id']
        req('POST', f'/api/v1/layout-templates/{tid}/approve')
        body = {'template_id': tid, 'style_mode': 'system', 'items': [{'cn': '小满', 'blessing': '愿你日日有欢喜'}, {'cn': '清和', 'blessing': '平安喜乐 自在生长'}]}
        key = 'showcase-' + secrets.token_hex(16)
        response = req('POST', '/api/v1/render-jobs', json=body, headers={'Idempotency-Key': key}).json()
        jid = response['data']['job']['job_id']
        for _ in range(100):
            job = req('GET', f'/api/v1/render-jobs/{jid}').json()['data']['job']
            if job['status'] in ('succeeded','failed','partial','cancelled'): break
            time.sleep(.2)
        check('real CPU M0 generation completes', job['status'] == 'succeeded' and job['style_mode'] == 'system')
        replay = req('POST', '/api/v1/render-jobs', json=body, headers={'Idempotency-Key': key}).json()
        check('idempotent render creates no duplicate', replay['data']['job']['job_id'] == jid and replay['meta']['idempotent_replay'])
        items = req('GET', f'/api/v1/render-jobs/{jid}/items').json()['data']['items']
        denied = req('POST', f'/api/v1/render-jobs/{jid}/export', 409).json()
        check('unreviewed export is denied', denied['code'] == 'ITEM_NOT_APPROVED')
        original_bytes = {}
        for index, item in enumerate(items, 1):
            data = req('GET', f'/api/v1/render-jobs/{jid}/items/{item["item_id"]}/image').content
            rendered = Image.open(io.BytesIO(data)).convert('RGB')
            check(f'PNG {index} retains full canvas and text in both separated slots', rendered.size == image.size and all(ImageChops.difference(rendered, image).crop(box).getbbox() for box in [(180,162,1020,270),(180,387,1020,549)]))
            original_bytes[item['item_id']+'.png'] = data
            (output / f'render-{index}.png').write_bytes(data)
        req('POST', f'/api/v1/render-jobs/{jid}/review', json={'item_ids': [i['item_id'] for i in items], 'decision':'approved', 'comment':'Automated API smoke action only; not independent or Owner acceptance'})
        exported = req('POST', f'/api/v1/render-jobs/{jid}/export').json()['data']
        check('approved export succeeds', exported['status'] == 'succeeded' and len(exported['artifacts']) == 3)
        hashes = {}
        for artifact in exported['artifacts']:
            data = req('GET', f'/api/v1/render-jobs/{jid}/exports/{artifact["artifact_id"]}/file').content
            filename = Path(artifact['relative_path']).name
            if filename.endswith('.zip'):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    check('ZIP members equal rendered PNG bytes', set(archive.namelist()) == set(original_bytes) and all(archive.read(n) == original_bytes[n] for n in archive.namelist()))
                (output / 'outputs.zip').write_bytes(data)
            else:
                check('exported PNG equals rendered bytes', data == original_bytes[filename])
            hashes[filename] = hashlib.sha256(data).hexdigest()
        stop(process)
        process = launch()
        client.cookies.clear()
        client.headers.pop('Cookie', None)
        req('POST', '/api/v1/auth/login', json={'username': username, 'password': changed})
        persisted = req('GET', f'/api/v1/render-jobs/{jid}/items').json()['data']['items']
        check('restart retains authenticated data and review decisions', len(persisted)==2 and all(i.get('status') == 'approved' for i in persisted))
        for artifact in exported['artifacts']:
            data = req('GET', f'/api/v1/render-jobs/{jid}/exports/{artifact["artifact_id"]}/file').content
            check('restart artifact readback preserves bytes', hashlib.sha256(data).hexdigest() == hashes[Path(artifact['relative_path']).name])
        (output / 'result.json').write_text(json.dumps({'checks': checks, 'job_id':jid, 'font':'Windows simkai (system font, not redistributed)', 'source':'M0_SYSTEM', 'approvals':'automated API test actions; not human or independent acceptance', 'artifacts_sha256':hashes}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'{len(checks)} checks passed; original background and results saved to {output}')
    finally:
        if process.poll() is None:
            stop(process)
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18916)
    parser.add_argument('--dist', type=Path, default=None, help='optional frontend build outside the source checkout')
    args = parser.parse_args()
    run(args.output, args.port, args.dist)
