import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P3 from '@/pages/P3_Annotate.vue'
import P5 from '@/pages/P5_Batch.vue'
import { envelope, flushPromises, mountPage, type FetchPlan } from './_harness'

const draft = { template_id: 't1', name: '我的布局', version: 1, status: 'draft', base_asset_id: 'base', style_id: null, canvas_width: 600, canvas_height: 400, slots: [{ key: 'to_cn', text_template: 'To {cn}', region: { x_norm: .1, y_norm: .1, w_norm: .6, h_norm: .2 } }] }
const approved = { ...draft, status: 'approved' }
const wrappers: VueWrapper[] = []
function plan(extra: FetchPlan = {}): FetchPlan { return {
  '/api/v1/assets/base': envelope({ asset_id: 'base', width: 600, height: 400, status: 'approved' }),
  '/api/v1/layout-templates': envelope(draft),
  '/api/v1/layout-templates?page_size=100': envelope({ items: [] }),
  '/api/v1/layout-templates/t1': envelope(draft),
  '/api/v1/layout-templates/t1/approve': envelope(approved),
  ...extra,
} }
function mount(component: unknown, responses: FetchPlan) { const ctx = mountPage(component, { plan: responses }); wrappers.push(ctx.wrapper); return ctx }
function button(wrapper: VueWrapper, text: string) { return wrapper.findAll('button').find(node => node.text() === text)! }
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => { if (wrapper.exists()) wrapper.unmount() }); vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('P3 layout admission restores the ordinary M0 user path', () => {
  it('Save creates one unbound draft, a separate explicit action admits it, then P5 can submit M0', async () => {
    const ctx = mount(P3, plan())
    await ctx.router.replace('/assets/base/annotate'); await flushPromises()
    await button(ctx.wrapper, '保存模板').trigger('click')
    void button(ctx.wrapper, '确认保存').trigger('click'); void button(ctx.wrapper, '确认保存').trigger('click')
    await flushPromises()
    expect(ctx.logs.filter(call => call.method === 'POST')).toHaveLength(1)
    expect(ctx.logs.find(call => call.method === 'POST')?.body).not.toHaveProperty('style_id')
    expect(ctx.router.currentRoute.value.query.template_id).toBe('t1')
    expect(ctx.wrapper.text()).toContain('草稿')
    expect(button(ctx.wrapper, '确认准入此布局模板').attributes('disabled')).toBeDefined()
    await ctx.wrapper.get('input[aria-label="确认已保存布局模板准入"]').setValue(true)
    void button(ctx.wrapper, '确认准入此布局模板').trigger('click'); void button(ctx.wrapper, '确认准入此布局模板').trigger('click'); await flushPromises()
    expect(ctx.logs.filter(call => call.url.endsWith('/approve') && call.method === 'POST')).toHaveLength(1)
    expect(ctx.wrapper.text()).toContain('布局模板已准入')
    expect(ctx.logs.some(call => /style-runs|training-samples/.test(call.url))).toBe(false)
    ctx.wrapper.unmount()
    const batch = mount(P5, {
      '/api/v1/assets': envelope({ items: [{ asset_id: 'base', width: 600, height: 400, status: 'approved' }] }),
      '/api/v1/phrase-templates': envelope({ items: [] }), '/api/v1/blogger-name-presets': envelope({ items: [] }),
      '/api/v1/styles': envelope({ items: [] }), '/api/v1/layout-templates': envelope({ items: [approved] }),
      '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
      '/api/v1/render-jobs': envelope({ job: { job_id: 'qa', status: 'succeeded' }, items: [] }),
    }); await flushPromises()
    await batch.wrapper.get('input[aria-label="第 1 行粉丝昵称"]').setValue('Audit')
    await batch.wrapper.get('button[aria-label="第 1 行图上署名开关"]').trigger('click')
    await batch.wrapper.findAll('button').find(node => node.text().includes('正式生成'))!.trigger('click'); await flushPromises()
    const render = batch.logs.find(call => call.method === 'POST' && call.url === '/api/v1/render-jobs')
    expect(render?.body).toMatchObject({ template_id: 't1' })
    expect(render?.body).not.toHaveProperty('style_id')
  })

  it('refresh recovers the exact owned draft without approving or creating it', async () => {
    const ctx = mount(P3, plan())
    await ctx.router.replace('/assets/base/annotate?template_id=t1'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('我的布局')
    expect(ctx.wrapper.get('input[aria-label="确认已保存布局模板准入"]').element).toHaveProperty('checked', false)
    expect(ctx.logs.every(call => call.method === 'GET')).toBe(true)
  })

  it('ambiguous approval requires authoritative re-read; approved state never posts twice', async () => {
    const responses = plan({ '/api/v1/layout-templates/t1/approve': { status: 503, json: { title: 'Unavailable', detail: '', code: 'UNAVAILABLE' } } })
    const ctx = mount(P3, responses)
    await ctx.router.replace('/assets/base/annotate?template_id=t1'); await flushPromises()
    await ctx.wrapper.get('input[aria-label="确认已保存布局模板准入"]').setValue(true)
    await button(ctx.wrapper, '确认准入此布局模板').trigger('click'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('操作暂未完成')
    expect(button(ctx.wrapper, '确认准入此布局模板')).toBeUndefined()
    responses['/api/v1/layout-templates/t1'] = envelope(approved)
    await button(ctx.wrapper, '重新读取模板状态').trigger('click'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('布局模板已准入')
    expect(ctx.logs.filter(call => call.method === 'POST')).toHaveLength(1)
  })

  it('foreign template 404 is visible and a template for another asset cannot be approved', async () => {
    const ctx = mount(P3, plan({
      '/api/v1/layout-templates/foreign': { status: 404, json: { title: 'Template not found', detail: '', code: 'INVALID_REQUEST' } },
      '/api/v1/layout-templates/other': envelope({ ...draft, template_id: 'other', base_asset_id: 'other-base' }),
    }))
    await ctx.router.replace('/assets/base/annotate?template_id=foreign'); await flushPromises()
    expect(ctx.wrapper.get('[role="alert"]').text()).toContain('找不到此布局模板')
    await ctx.router.replace('/assets/base/annotate?template_id=other'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('不属于当前源图')
    expect(ctx.wrapper.find('input[aria-label="确认已保存布局模板准入"]').exists()).toBe(false)
    expect(ctx.logs.every(call => call.method === 'GET')).toBe(true)
  })

  it('late save response cannot navigate back into an abandoned asset route', async () => {
    const ctx = mount(P3, plan()); await ctx.router.replace('/assets/base/annotate'); await flushPromises()
    const previous = globalThis.fetch
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/layout-templates' && init?.method === 'POST'
      ? new Promise<Response>(resolve => { finish = resolve }) : previous(input, init))
    await button(ctx.wrapper, '保存模板').trigger('click'); void button(ctx.wrapper, '确认保存').trigger('click'); await flushPromises()
    ctx.wrapper.unmount(); await ctx.router.replace('/assets')
    finish(new Response(JSON.stringify(envelope(draft).json), { status: 201 })); await flushPromises()
    expect(ctx.router.currentRoute.value.path).toBe('/assets')
    expect(sessionStorage.getItem('yy:p3:pending-template:2:base')).toBeNull()
  })

  it('late save response cannot replace a newer template selection on the same asset', async () => {
    const ctx = mount(P3, plan({ '/api/v1/layout-templates/t2': envelope({ ...draft, template_id: 't2', name: '另一个模板' }) }))
    await ctx.router.replace('/assets/base/annotate'); await flushPromises()
    const previous = globalThis.fetch
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/layout-templates' && init?.method === 'POST'
      ? new Promise<Response>(resolve => { finish = resolve }) : previous(input, init))
    await button(ctx.wrapper, '保存模板').trigger('click'); void button(ctx.wrapper, '确认保存').trigger('click'); await flushPromises()
    await ctx.router.replace('/assets/base/annotate?template_id=t2'); await flushPromises()
    finish(new Response(JSON.stringify(envelope(draft).json), { status: 201 })); await flushPromises()
    expect(ctx.router.currentRoute.value.query.template_id).toBe('t2')
    expect(ctx.wrapper.text()).toContain('另一个模板')
    expect(sessionStorage.getItem('yy:p3:pending-template:2:base')).toBeNull()
  })

  it('uncertain save survives reload and does not automatically create another template', async () => {
    const ctx = mount(P3, plan({ '/api/v1/layout-templates': { status: 503, json: { title: 'Unavailable', detail: '', code: 'UNAVAILABLE' } } }))
    await ctx.router.replace('/assets/base/annotate'); await flushPromises()
    await button(ctx.wrapper, '保存模板').trigger('click'); await button(ctx.wrapper, '确认保存').trigger('click'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('保存结果尚未确认')
    expect(ctx.logs.filter(call => call.method === 'POST')).toHaveLength(1)
    ctx.wrapper.unmount()
    const reload = mount(P3, plan()); await reload.router.replace('/assets/base/annotate'); await flushPromises()
    expect(reload.wrapper.text()).toContain('保存结果尚未确认')
    await button(reload.wrapper, '保存模板').trigger('click')
    expect(button(reload.wrapper, '确认保存').attributes('disabled')).toBeDefined()
    expect(reload.logs.every(call => call.method === 'GET')).toBe(true)
  })
})
