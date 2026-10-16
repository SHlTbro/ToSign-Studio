import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P4 from '@/pages/P4_Extract.vue'
import { envelope, flushPromises, mountPage, type FetchPlan } from './_harness'

let wrappers: VueWrapper[] = []
beforeEach(() => localStorage.clear())
afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers = []; vi.unstubAllGlobals(); document.body.innerHTML = '' })
async function setup(asAdmin: boolean) {
  const plan: FetchPlan = {
    '/api/v1/assets/source': envelope({ width: 400, height: 500, type: 'sample_source', status: 'pending' }),
    '/api/v1/styles': envelope({ items: [] }),
    '/api/v1/detect-text': envelope({ items: [] }),
    '/api/v1/assets/source/approve': envelope({ status: 'approved' }),
  }
  const ctx = mountPage(P4, { plan, asAdmin })
  wrappers.push(ctx.wrapper)
  await ctx.router.push('/extract/source'); await flushPromises()
  return { ...ctx, plan }
}
describe('P4 explicit source approval', () => {
  it('creates a handwriting profile as draft without silently approving it', async () => {
    const { wrapper, logs, plan } = await setup(false)
    plan['/api/v1/styles'] = envelope({ style_id: 8, name: '我的字形', source_type: 'HANDWRITING_ASSET', status: 'draft' })
    const underlying = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method !== 'POST' && String(input).startsWith('/api/v1/styles')
      ? Promise.resolve(new Response(JSON.stringify(envelope({ items: [{ style_id: 8, name: '我的字形', source_type: 'HANDWRITING_ASSET', status: 'draft' }] }).json)))
      : underlying(input, init))
    await wrapper.find('[aria-label="本人字形档案名称"]').setValue('我的字形')
    await wrapper.findAll('button').find(b => b.text() === '创建字形档案草稿')!.trigger('click'); await flushPromises()
    expect(logs.find(l => l.url === '/api/v1/styles' && l.method === 'POST')?.body).toEqual({ name: '我的字形', source_type: 'HANDWRITING_ASSET' })
    expect(logs.filter(l => l.url.includes('/approve'))).toHaveLength(0)
    expect(wrapper.findAll('button').find(b => b.text() === '确认档案归属')!.attributes('disabled')).toBeDefined()
  })
  it('approves only a draft handwriting profile after a separate explicit acknowledgement', async () => {
    const { wrapper, logs, plan } = await setup(false)
    plan['/api/v1/styles'] = envelope({ items: [{ style_id: 8, name: '我的字形', source_type: 'HANDWRITING_ASSET', status: 'draft' }] })
    plan['/api/v1/styles/8/approve'] = envelope({ style_id: 8, status: 'approved' })
    await wrapper.find('[aria-label="本人字形档案名称"]').setValue('我的字形')
    await wrapper.findAll('button').find(b => b.text() === '创建字形档案草稿')!.trigger('click'); await flushPromises()
    await wrapper.find('[aria-label="确认档案归属 8"]').setValue(true)
    await wrapper.findAll('button').find(b => b.text() === '确认档案归属')!.trigger('click'); await flushPromises()
    expect(logs.filter(l => l.url === '/api/v1/styles/8/approve' && l.method === 'POST')).toHaveLength(1)
    expect(wrapper.text()).toContain('模型质量和发布仍须各自通过')
  })
  it('allows the authenticated empty extraction route for starting a real upload', async () => {
    const { wrapper, router, logs } = await setup(false)
    await router.push('/extract'); await flushPromises()
    expect(router.currentRoute.value.matched[0]?.meta.requiresAuth).toBe(true)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(wrapper.find('#source-review-heading').exists()).toBe(false)
    expect(wrapper.find('[data-testid="p4-stage-card"]').exists()).toBe(false)
    expect(logs.some(l => l.url.endsWith('/approve'))).toBe(false)
  })
  it('keeps pending uploads unapproved and offers admin approval only after acknowledgement', async () => {
    const { wrapper, logs, plan } = await setup(true)
    const button = wrapper.findAll('button').find(b => b.text() === '批准本张原图')!
    expect(button.attributes('disabled')).toBeDefined()
    expect(logs.some(l => l.url.endsWith('/approve'))).toBe(false)
    await wrapper.find('[aria-label="确认原图使用权"]').setValue(true)
    plan['/api/v1/assets/source'] = envelope({ width: 400, height: 500, type: 'sample_source', status: 'approved' })
    await button.trigger('click'); await flushPromises()
    expect(logs.filter(l => l.url.endsWith('/approve') && l.method === 'POST')).toHaveLength(1)
    expect(wrapper.find('#source-review-heading').exists()).toBe(false)
  })
  it('ordinary users see an actionable admin requirement with no approve control', async () => {
    const { wrapper, logs } = await setup(false)
    expect(wrapper.text()).toContain('请由管理员审核')
    expect(wrapper.find('[aria-label="确认原图使用权"]').exists()).toBe(false)
    expect(logs.some(l => l.url.endsWith('/approve'))).toBe(false)
  })
})
