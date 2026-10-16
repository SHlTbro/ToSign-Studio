"""Windows CPU-only P4 sample_source -> P7 review browser engineering test.

All traffic is real authenticated loopback HTTP. Synthetic confirmation is
marked TEST_FIXTURE_CONFIRMATION in the isolated DB by tagging its request.
Explicit source consent and human input review are tagged synthetic engineering
actions. No GPU, AI provider, S03 freeze, M2 execution, quality approval, or release.
Outputs and profiles stay under the supplied E-drive evidence root.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import traceback
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw
from runtime_guard import RunContext, low_memory_browser_args
from runtime_guard import session_cookie_value

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ['YY_REVIEW_EVIDENCE']).resolve()
assert OUT.drive.upper() == 'E:' and OUT.is_relative_to(REPO / 'artifacts')

def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def fixture():
    image = Image.new('RGB', (900, 1200), (12, 12, 12))
    draw = ImageDraw.Draw(image)
    for ox in (0, 360):
        draw.line([(250+ox, 320), (430+ox, 320), (340+ox, 320), (340+ox, 850)], fill='white', width=32)
        draw.line([(260+ox, 580), (435+ox, 580)], fill='white', width=28)
    buf = io.BytesIO(); image.save(buf, 'PNG'); return buf.getvalue()

def main():
    global OUT
    backend_port, frontend_port = port(), port()
    runtime = RunContext(REPO, 'p7-owner-review-cpu', backend_port, frontend_port)
    OUT = OUT / runtime.run_id
    OUT.mkdir(exist_ok=False)
    (OUT.parent/'browser-latest.txt').write_text(str(OUT),encoding='utf-8')
    report = {'scope': __doc__, 'checks': [], 'api': [], 'screenshots': [], 'page_errors': [], 'request_failures': [], 'console_errors': [], 'ports': [backend_port, frontend_port], 'style_worker': 'disabled', 'run_dir': str(runtime.run_dir)}
    env = dict(os.environ, YY_DB_URL='sqlite:///' + (runtime.run_dir/'engineering.db').as_posix(), YY_STORAGE_ROOT=str(runtime.run_dir/'storage'), YY_AUTH_STRICT='1', YY_STYLE_WORKER='0', CUDA_VISIBLE_DEVICES='', PYTHONDONTWRITEBYTECODE='1')
    for name in ('TEMP','TMP','TMPDIR','HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME'):
        env[name] = str(OUT/'browser-home'/name.lower()); Path(env[name]).mkdir(parents=True, exist_ok=True)
    for name in ('HF_HOME','TORCH_HOME','CUDA_CACHE_PATH','MPLCONFIGDIR','PIP_CACHE_DIR','UV_CACHE_DIR'):
        env[name] = str(OUT/'browser-home'/name.lower())
    os.environ.update(env)
    backend_venv = REPO/'_w4/.venv-app'
    home = next(line.split('=',1)[1].strip() for line in (backend_venv/'pyvenv.cfg').read_text().splitlines() if line.startswith('home ='))
    backend_python = Path(home)/'python.exe'
    env['PYTHONPATH'] = os.pathsep.join([str(REPO), str(backend_venv/'Lib/site-packages')])
    config = OUT/'browser-vite.config.mjs'
    config.write_text(f"import config from {json.dumps((REPO/'frontend/vite.config.ts').as_uri())};\nexport default {{...config, server: {{...config.server, proxy: {{'/api': {{target: 'http://127.0.0.1:{backend_port}', changeOrigin: true}}}}}}}};\n", encoding='utf-8')
    base = f'http://127.0.0.1:{frontend_port}'
    def check(name, condition):
        report['checks'].append({'name':name, 'passed':bool(condition)})
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        assert condition, name
    browser = None; rc = 1
    try:
        runtime.acquire()
        runtime.start('backend', [str(backend_python), '-B', '-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(backend_port)], REPO, env, backend_port)
        runtime.start('frontend', ['Z:/node/node.exe', str(REPO/'frontend/node_modules/vite/bin/vite.js'), '--config',str(config),'--configLoader','runner','--host','127.0.0.1','--port',str(frontend_port),'--strictPort'], REPO/'frontend', env, frontend_port)
        check('owned backend listener', runtime.wait_owned_listener('backend', timeout=30))
        check('owned frontend listener', runtime.wait_owned_listener('frontend', timeout=30))
        invite = subprocess.run([str(backend_python),'-B',str(REPO/'installer/first_invite.py'),'CPU-REVIEW-FIXTURE','--yes'], cwd=REPO, env=env, capture_output=True,text=True,timeout=30)
        (OUT/'browser-invite.log').write_text(invite.stdout+invite.stderr,encoding='utf-8')
        check('official first-run invite bootstrap', invite.returncode == 0)
        with sync_playwright() as pw:
            # Read-only reuse of existing browser binary; runtime profiles/env are E-only.
            executable = os.environ.get('YY_REVIEW_CHROMIUM', 'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1187/chrome-win/chrome.exe')
            browser = pw.chromium.launch(executable_path=executable, args=low_memory_browser_args(), env=env)
            context = browser.new_context(viewport={'width':1280,'height':900})
            context.set_default_timeout(60000)
            # No external requests are permitted from this engineering page.
            def local_only(route):
                from urllib.parse import urlsplit
                if urlsplit(route.request.url).hostname == '127.0.0.1': route.continue_()
                else: route.abort()
            context.route('**/*', local_only)
            page = context.new_page()
            page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
            page.on('requestfailed', lambda req: report['request_failures'].append({'url':req.url,'failure':req.failure}))
            page.on('console', lambda msg: report['console_errors'].append(msg.text) if msg.type == 'error' else None)
            page.on('response', lambda response: report['api'].append({'method':response.request.method,'url':response.url,'status':response.status}) if '/api/' in response.url else None)
            username = 'cpureview'+secrets.token_hex(3)
            password = 'CPU-'+secrets.token_urlsafe(12)+'!7Aa'
            registered = context.request.post(base+'/api/v1/auth/register', data={'username':username,'password':password,'display_name':'SYNTHETIC engineering test','invite_code':'CPU-REVIEW-FIXTURE'})
            check('real register', registered.status == 201)
            page.goto(base+'/login'); page.fill('#login-username',username); page.fill('#login-password',password); page.locator('button[type=submit]').click()
            page.wait_for_url('**/change-password')
            new_password = 'CPU-New-'+secrets.token_urlsafe(12)+'!8Bb'
            page.fill('#old-password',password); page.fill('#new-password',new_password); page.fill('#confirm-password',new_password); page.locator('button[type=submit]').click()
            page.wait_for_url(base+'/')
            cookie = session_cookie_value(context.cookies())
            def api(method, path, data=None):
                # APIRequestContext does not send Secure cookies over plain HTTP.
                # Replay the actual browser-issued session, never fabricate one.
                response = context.request.fetch(base+path,method=method,data=data,headers={'X-Requested-With':'XMLHttpRequest','Cookie':'session='+cookie})
                if response.status >= 300: print('API failure',response.status,response.text()[:300],flush=True)
                check(f'{method} {path}', response.status < 300)
                return response.json()['data']
            page.goto(base+'/extract')
            page.get_by_label('本人字形档案名称').fill('SYNTHETIC CPU REVIEW ONLY')
            page.get_by_role('button',name='创建字形档案草稿',exact=True).click()
            page.get_by_label(re.compile('^确认档案归属 ')).wait_for()
            style=api('GET','/api/v1/styles')['items'][0]
            style_id=style['style_id']
            check('UI creates handwriting profile as draft, no automatic approval',style['source_type']=='HANDWRITING_ASSET' and style['status']=='draft')
            page.screenshot(path=str(OUT/'p4-profile-draft.png')); report['screenshots'].append('p4-profile-draft.png')
            page.get_by_label(f'确认档案归属 {style_id}').check(); page.get_by_role('button',name=f'确认档案归属 #{style_id}',exact=True).click()
            page.get_by_label(f'确认档案归属 {style_id}').wait_for(state='detached')
            check('explicit UI profile approval persists',api('GET',f'/api/v1/styles/{style_id}')['status']=='approved')
            source_file = OUT/'synthetic-source.png'; source_file.write_bytes(fixture())
            page.goto(base+'/extract'); page.set_input_files('input[type=file]',str(source_file))
            page.get_by_label('确认原图使用权').wait_for(timeout=30000)
            check('upload remains pending until explicit approval', page.get_by_role('button',name='批准本张原图').is_disabled())
            page.screenshot(path=str(OUT/'p4-source-pending.png')); report['screenshots'].append('p4-source-pending.png')
            page.get_by_label('确认原图使用权').check(); page.get_by_role('button',name='批准本张原图').click()
            page.locator('#source-review-heading').wait_for(state='detached')
            asset_id = page.url.rsplit('/',1)[-1]
            check('P4 really uploaded sample_source', api('GET',f'/api/v1/assets/{asset_id}')['type'] == 'sample_source')
            submit_source=page.get_by_role('button',name='本人提交本张来源声明',exact=True)
            submit_source.wait_for()
            check('source declaration requires unchecked explicit permissions',submit_source.is_disabled())
            page.get_by_label('本人确认本张原图使用权').check()
            page.get_by_role('button',name='取消来源声明',exact=True).click()
            check('source cancellation clears permission without writes',not page.get_by_label('本人确认本张原图使用权').is_checked() and not any(row['method']=='POST' and '/training-sources/' in row['url'] for row in report['api']))
            for label in ('本人确认本张原图使用权','本人同意当前档案个人训练用途','确认来源历史仍为UNKNOWN'):page.get_by_label(label).check()
            page.get_by_label('来源与用途说明').fill('TEST_FIXTURE_HUMAN_BROWSER: synthetic engineering source consent only')
            submit_source.click();page.get_by_text(re.compile('^已有来源声明')).wait_for()
            admitted=api('GET',f'/api/v1/training-sources/{asset_id}/admission?style_id={style_id}')
            check('human source receipt is authenticated and preserves UNKNOWN',admitted['status']=='ADMITTED' and admitted['receipt']['actor_kind']=='authenticated_user' and admitted['history_status']=='UNKNOWN')
            page.reload();page.get_by_text(re.compile('^已有来源声明')).wait_for()
            page.screenshot(path=str(OUT/'p4-human-source-consent.png'));report['screenshots'].append('p4-human-source-consent.png')
            page.get_by_test_id('p4-stage').locator('img').wait_for()
            stage = page.get_by_test_id('p4-stage')
            page.get_by_test_id('p4-manual-toggle').click()
            stage.scroll_into_view_if_needed(); box = stage.bounding_box()
            for x0,x1 in ((.20,.48),(.54,.83)):
                page.mouse.move(box['x']+box['width']*x0,box['y']+box['height']*.20); page.mouse.down()
                page.mouse.move(box['x']+box['width']*x1,box['y']+box['height']*.80,steps=8); page.mouse.up()
            page.get_by_test_id('p4-extract').click()
            drafts = page.get_by_role('button',name=re.compile('^框 ')); drafts.nth(1).wait_for(timeout=30000)
            for index,label in enumerate(('永','山')):
                drafts.nth(index).click(); page.get_by_test_id('p4-compare-before').click(); page.get_by_test_id('p4-compare-after').click()
                page.wait_for_function("document.querySelector('[data-testid=p4-canvas]')?.dataset.ready === 'true'")
                page.get_by_test_id('p4-confirm-register').click(); page.get_by_test_id('p4-style-select').select_option(str(style_id)); page.get_by_test_id('p4-text-label').fill(label)
                page.get_by_test_id('p4-register-final').click(); page.get_by_test_id('p4-register-done').wait_for(); page.get_by_role('button',name='继续',exact=True).click()
            candidates = api('GET',f'/api/v1/training-samples?style_id={style_id}&review_status=candidate')['items']
            check('two provenance-bound candidates, no auto confirmation', len(candidates)==2 and all(row['owner_confirmed_by'] is None and row['provenance_complete'] for row in candidates))
            page.goto(base+'/training'); page.get_by_label('选择候选 '+candidates[0]['sample_id']).wait_for()
            def focus_review():
                page.locator('section[aria-labelledby="owner-confirm-heading"]').evaluate("el => { const scroller = el.parentElement; scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top }")
                page.wait_for_timeout(100)
            for width in (375,768,1024,1280,1440):
                page.set_viewport_size({'width':width,'height':900}); focus_review()
                check(f'no horizontal overflow {width}', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                filename=f'p7-{width}.png'; page.screenshot(path=str(OUT/filename)); report['screenshots'].append(filename)
            page.set_viewport_size({'width':375,'height':900})
            focus_review()
            page.screenshot(path=str(OUT/'p7-candidates-375.png'))
            check('white alpha has contrasting preview background', page.locator('img[alt="候选字形 PNG"]').first.evaluate("img => getComputedStyle(img).backgroundImage.includes('conic-gradient')"))
            page.get_by_label('选择候选 '+candidates[0]['sample_id']).check(); page.get_by_label('修正候选字符 '+candidates[0]['sample_id']).fill('安')
            page.get_by_role('button',name='确认选中的 1 个字符',exact=True).click(); page.screenshot(path=str(OUT/'p7-confirm-dialog-375.png'))
            check('review requires acknowledgement', page.get_by_role('button',name='提交本人复核').is_disabled())
            before = sum(row['method']=='POST' and row['url'].endswith('/batch-review') for row in report['api'])
            page.keyboard.press('Escape'); check('ESC closes review without writing', not page.locator('dialog').is_visible() and before == sum(row['method']=='POST' and row['url'].endswith('/batch-review') for row in report['api']))
            page.get_by_role('button',name='排除选中的 1 个候选',exact=True).click(); page.get_by_label('本人已逐项复核').check()
            check('rejection requires reason', page.get_by_role('button',name='提交本人复核').is_disabled())
            page.get_by_label('排除理由').fill('TEST_FIXTURE_REJECTION: synthetic CPU review test'); page.get_by_role('button',name='提交本人复核').click(); page.locator('dialog').wait_for(state='hidden')
            second = candidates[1]['sample_id']; page.get_by_label('选择候选 '+second).check(); page.get_by_label('修正候选字符 '+second).fill('林')
            def tag_test(route):
                payload=route.request.post_data_json
                for item in payload['items']: item['reason']='TEST_FIXTURE_CONFIRMATION'
                route.continue_(post_data=json.dumps(payload,ensure_ascii=False))
            page.route('**/api/v1/training-samples/batch-review',tag_test)
            page.get_by_role('button',name='确认选中的 1 个字符',exact=True).click(); page.get_by_label('本人已逐项复核').check(); page.get_by_role('button',name='提交本人复核').click(); page.locator('dialog').wait_for(state='hidden')
            page.unroute('**/api/v1/training-samples/batch-review',tag_test)
            confirmed=api('GET',f'/api/v1/training-samples?style_id={style_id}&review_status=owner_confirmed')['items']
            check('corrected label/reviewer/test marker persisted',len(confirmed)==1 and confirmed[0]['text_label']=='林' and confirmed[0]['review_reason']=='TEST_FIXTURE_CONFIRMATION' and confirmed[0]['owner_confirmed_at'])
            page.reload(); page.wait_for_function("document.querySelector('[aria-labelledby=owner-confirm-heading]')?.innerText.includes('候选 0 · 已确认字 1 · 已排除 1')")
            check('refresh restores reviewed counts', True)
            visual=page.locator('section[aria-labelledby="human-input-heading"]')
            visual.get_by_role('button',name='查看并审核字 林',exact=True).click()
            visual.get_by_role('img',name='待审核字的真实原图',exact=True).wait_for()
            page.wait_for_function("Array.from(document.querySelectorAll('[aria-labelledby=human-input-heading] img')).every(img => img.complete && img.naturalWidth > 0)")
            check('human visual acceptance requires fresh unchecked checklist',visual.get_by_role('button',name='本人接受当前输入',exact=True).is_disabled())
            before_human=sum(row['method']=='POST' and '/human-visual-review' in row['url'] for row in report['api'])
            visual.get_by_role('button',name='取消输入审核',exact=True).click()
            check('human pixel-review cancellation makes no mutation',before_human==sum(row['method']=='POST' and '/human-visual-review' in row['url'] for row in report['api']))
            visual.get_by_role('button',name='查看并审核字 林',exact=True).click()
            page.wait_for_function("Array.from(document.querySelectorAll('[aria-labelledby=human-input-heading] img')).every(img => img.complete && img.naturalWidth > 0)")
            for label in ('本人已核对原图与标签一致','本人已核对笔画完整','本人已核对透明字形可用','本人已逐项核对结构疑点'):visual.get_by_label(label).check()
            visual.get_by_label('人工输入审核说明').fill('TEST_FIXTURE_HUMAN_BROWSER: synthetic API transport and image viewing only; no owner/model acceptance')
            for width in (375,768,1024,1280,1440):
                page.set_viewport_size({'width':width,'height':900})
                check(f'human input review has no horizontal overflow {width}',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.set_viewport_size({'width':375,'height':900})
            visual.evaluate("el => el.scrollIntoView({block:'center'})")
            page.screenshot(path=str(OUT/'p7-human-input-review-375.png'));report['screenshots'].append('p7-human-input-review-375.png')
            visual.get_by_role('button',name='本人接受当前输入',exact=True).click()
            visual.get_by_text(re.compile('人工意见 accept')).wait_for()
            human_receipt=api('GET',f'/api/v1/training-samples/human-visual-review?style_id={style_id}')['items'][0]['receipt']
            check('explicit browser input audit is human, separate from AI/model approval',human_receipt['actor_kind']=='authenticated_user' and human_receipt['owner_confirmation']=='UNCHANGED' and human_receipt['model_quality_approved'] is False)
            page.reload();visual.get_by_text(re.compile('人工意见 accept')).wait_for()
            check('reload restores actual human input receipt',True)
            # Real S03/M2 capability read: this synthetic source has no AI
            # model/20-character evidence. Never make a fake READY fixture.
            s03 = page.locator('section[aria-labelledby="s03-heading"]')
            s03.get_by_role('button',name='创建真实 M2 草稿运行',exact=True).wait_for()
            check('S03 UI refuses incomplete source/data freeze',s03.get_by_role('button',name='预览并冻结 S03',exact=True).is_disabled())
            check('S03 UI refuses uninstalled real M2',s03.get_by_role('button',name='创建真实 M2 草稿运行',exact=True).is_disabled())
            s03.get_by_role('button',name='刷新冻结预检',exact=True).click()
            s03.get_by_text('M2 runtime 与快照能力',exact=True).wait_for()
            check('real canonical preflight remains BLOCKED',api('GET',f'/api/v1/training-samples/snapshot-preflight?style_id={style_id}')['snapshot_ready'] is False)
            check('real runtime remains BLOCKED without a model registration',api('GET',f'/api/v1/styles/{style_id}/m2-runtime')['ready'] is False)
            s03.evaluate("el => { const scroller = el.parentElement; scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top }")
            page.screenshot(path=str(OUT/'s03-runtime-blocked-375.png')); report['screenshots'].append('s03-runtime-blocked-375.png')
            # Exercise actual API lifecycle with the execution worker disabled;
            # queuing here cannot invoke M1, M2, or any optimizer/GPU work.
            page.locator('section[aria-labelledby="run-heading"] select').nth(1).select_option('林')
            page.get_by_role('button',name='创建新运行',exact=True).click()
            page.wait_for_function("document.querySelector('[aria-labelledby=run-heading]')?.innerText.includes('已创建 · #')")
            run_id = page.evaluate("Number(localStorage.getItem('yy:p7:last-style-run-id'))")
            page.reload(); page.get_by_role('button',name='启动真实运行',exact=True).wait_for()
            check('created run restored from server on reload', api('GET',f'/api/v1/style-runs/{run_id}')['status']=='created')
            page.get_by_role('button',name='启动真实运行',exact=True).click()
            page.get_by_role('button',name='取消运行',exact=True).wait_for()
            check('queue only with execution disabled', api('GET',f'/api/v1/style-runs/{run_id}')['status']=='queued' and env['YY_STYLE_WORKER']=='0')
            page.get_by_role('button',name='取消运行',exact=True).click()
            page.wait_for_function("document.querySelector('[aria-labelledby=run-heading]')?.innerText.includes('已取消 · #')")
            page.reload(); page.wait_for_function("document.querySelector('[aria-labelledby=run-heading]')?.innerText.includes('已取消 · #')")
            check('cancelled run restored after reload', api('GET',f'/api/v1/style-runs/{run_id}')['status']=='cancelled')
            for name,options in (('dark',{'color_scheme':'dark'}),('hc',{'forced_colors':'active'})):
                page.emulate_media(**options); focus_review(); page.screenshot(path=str(OUT/f'p7-{name}.png'))
            page.emulate_media(color_scheme='light',forced_colors='none'); page.evaluate("document.documentElement.style.zoom='200%'"); page.screenshot(path=str(OUT/'p7-200pct.png'))
            check('200 percent header controls do not overlap', page.locator('header').evaluate("header => { const [a,b,c] = Array.from(header.children).map(el => el.getBoundingClientRect()); return a.right <= b.left && b.right <= c.left }"))
            check('no uncaught page errors',not report['page_errors'])
            rc=0; browser.close(); browser=None
    except Exception:
        report['error']=traceback.format_exc(); print(report['error'],flush=True)
        try:
            page.screenshot(path=str(OUT/'failure-page.png'),timeout=10000)
            (OUT/'failure-body.txt').write_text(page.locator('body').inner_text(timeout=10000),encoding='utf-8')
        except Exception:report['failure_capture_error']=traceback.format_exc()
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
        try:
            runtime.stop_all(rc)
            report['runtime']=runtime.fingerprints('stopped',rc)
        except Exception:
            report['cleanup_error']=traceback.format_exc()
        report['exit_code']=rc
        report['source_sha256']={str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (REPO/'frontend/src/pages/P4_Extract.vue',REPO/'frontend/src/pages/P7_Training.vue',REPO/'frontend/src/components/S03Training.vue',REPO/'frontend/src/components/SourceAdmission.vue',REPO/'frontend/src/components/HumanDatasetReview.vue',REPO/'app/domains/styles/service.py',REPO/'app/domains/styles/m2_runtime_api.py',REPO/'app/domains/training_sets/service.py',REPO/'app/domains/training_sets/source_admission.py',REPO/'app/domains/training_sets/human_review.py',REPO/'app/domains/training_sets/split_authorization.py',REPO/'scripts/export_personal_style_dataset.py',REPO/'engine/style/dataset/snapshot.py',REPO/'app/domains/styles/worker.py',Path(__file__))}
        (OUT/'browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return rc

if __name__=='__main__': raise SystemExit(main())
