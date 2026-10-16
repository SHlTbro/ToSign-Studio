import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import RenderStyleControls from '@/components/RenderStyleControls.vue'
import P5Batch from '@/pages/P5_Batch.vue'
import { SYSTEM_CAPABILITIES, M2_CAPABILITIES } from './render-capability-fixtures'
import { envelope, mountPage, problem } from './_harness'
import { useAuthStore } from '@/stores/auth'
import { __resetHttpGatesForTest } from '@/api/http'
import type { RenderCapabilities, RenderSelection } from '@/types/renderControls'

const STYLES = [
  { style_id: 7, name: 'Synthetic adapter', source_type: 'M2_ADAPTER', status: 'approved', enabled: true },
  { style_id: 8, name: 'Disabled model', source_type: 'M2_ADAPTER', status: 'approved', enabled: false },
  { style_id: 9, name: 'Source only', source_type: 'HANDWRITING_ASSET', status: 'approved', enabled: true },
  { style_id: 10, name: 'Unsupported frozen-base row', source_type: 'M1_FROZEN', status: 'approved', enabled: true },
]
const wrappers: VueWrapper[] = []
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => envelope(value).json }) as Response
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
function lastSelection(wrapper: VueWrapper): RenderSelection | null { return wrapper.emitted('ready')?.at(-1)?.[0] as RenderSelection | null }
function controls(read: (url: string) => Promise<RenderCapabilities> = async (url) => url.includes('style_id=7') ? clone(M2_CAPABILITIES) : clone(SYSTEM_CAPABILITIES)) {
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => response(String(url).startsWith('/api/v1/styles') ? { items: STYLES } : await read(String(url)))))
  const wrapper = mount(RenderStyleControls, { props: { disabled: false } }); wrappers.push(wrapper); return wrapper
}
beforeEach(() => { localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('explicit source and deterministic layout controls', () => {
  it('labels only the known imported 0178 identity as classic handwriting without altering model identity', async () => {
    const ordinary = controls(); await flushPromises(); ordinary.unmount()
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => response(String(url).startsWith('/api/v1/styles') ? { items: [
      { ...STYLES[0], style_id: 2, name: '0178 · 已认可 V3', model_version: 'adapter@017881442b7f8b90', receipt_id: 'imported_inference/16ec01496e0b472b9f561ce383a7ed33' },
      { ...STYLES[0], style_id: 3, name: 'Other handwritten model', model_version: 'adapter@different', receipt_id: 'imported_inference/other' },
    ] } : clone(SYSTEM_CAPABILITIES))))
    const wrapper = mount(RenderStyleControls, { props: { disabled: false } }); wrappers.push(wrapper); await flushPromises()
    expect(wrapper.get('option[value="2"]').text()).toBe('经典手写 V3')
    expect(wrapper.get('option[value="3"]').text()).toContain('Other handwritten model')
  })
  it('keeps explicit system default after approved styles load and hides disabled/input-only rows', async () => {
    const wrapper = controls(); await flushPromises()
    expect((wrapper.get('#render-style').element as HTMLSelectElement).value).toBe('')
    expect(lastSelection(wrapper)).toEqual({ style_mode: 'system', glyph_source_policy: 'fallback_allowed', layout_profile: 'baseline-v1' })
    expect(wrapper.text()).not.toContain('Disabled model')
    expect(wrapper.text()).not.toContain('Source only')
    expect(wrapper.text()).not.toContain('Unsupported frozen-base row')
    expect(wrapper.text()).toContain('自然排版尚未完成资格核验')
  })
  it('requires a qualified M2-only choice before natural layout and submits explicit stable seed', async () => {
    const wrapper = controls(); await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('需要明确选择')
    await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required')
    await wrapper.get('[aria-label="自然排版重复种子"]').setValue('314159')
    expect(lastSelection(wrapper)).toEqual({ style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 314159 })
  })
  it.each(['-1', '1.5', '4294967296', ''])('rejects invalid natural seed %s', async (seed) => {
    const wrapper = controls(); await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required'); await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    await wrapper.get('[aria-label="自然排版重复种子"]').setValue(seed)
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('须为 0 到 4294967295 的整数')
  })
  it('restores preference and seed after reload, then revalidates server availability', async () => {
    const one = controls(); await flushPromises(); await one.get('#render-style').setValue('7'); await flushPromises()
    await one.get('[aria-label="字形来源要求"]').setValue('m2_required'); await one.get('[aria-label="排版方式"]').setValue('natural-layout-v1'); await one.get('[aria-label="自然排版重复种子"]').setValue('99'); one.unmount()
    const two = controls(); expect(lastSelection(two)).toBeNull(); await flushPromises()
    expect(lastSelection(two)?.layout_seed).toBe(99)
    expect(lastSelection(two)?.style_id).toBe(7)
  })
  it('does not silently weaken M2-required or natural-layout choices when switching to M0', async () => {
    const wrapper = controls(); await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required'); await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    await wrapper.get('#render-style').setValue(''); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull()
    expect((wrapper.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('m2_required')
  })
  it('ignores a late profile response after the user switches back to system', async () => {
    let finish!: (value: RenderCapabilities) => void
    const wrapper = controls(async (url) => url.includes('style_id=7') ? new Promise((resolve) => { finish = resolve }) : clone(SYSTEM_CAPABILITIES))
    await flushPromises(); await wrapper.get('#render-style').setValue('7'); await wrapper.get('#render-style').setValue(''); await flushPromises()
    finish(clone(M2_CAPABILITIES)); await flushPromises()
    expect(lastSelection(wrapper)?.style_mode).toBe('system')
    expect(lastSelection(wrapper)?.style_id).toBeUndefined()
  })
  it('fails closed on wrong capability identity and never falls back on API error', async () => {
    let fail = false
    const wrapper = controls(async (url) => { if (fail) throw new Error('runtime unavailable'); return url.includes('style_id=7') ? clone(SYSTEM_CAPABILITIES) : clone(SYSTEM_CAPABILITIES) })
    await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull(); expect(wrapper.text()).toContain('能力与所选风格不匹配')
    expect(wrapper.text()).not.toContain('已核验所选来源与排版能力')
    fail = true; await wrapper.findAll('button').find((button) => button.text().includes('重新核验'))!.trigger('click'); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull(); expect((wrapper.get('#render-style').element as HTMLSelectElement).value).toBe('7')
    expect(wrapper.text()).toContain('生成运行环境当前不可用')
    expect(wrapper.text()).not.toContain('已核验所选来源与排版能力')
  })
  it('blocks natural layout if its profile hash is absent despite an available flag', async () => {
    const bad = clone(M2_CAPABILITIES); bad.layouts[1]!.profile_sha256 = null
    const wrapper = controls(async (url) => url.includes('style_id=7') ? bad : clone(SYSTEM_CAPABILITIES)); await flushPromises()
    await wrapper.get('#render-style').setValue('7'); await flushPromises(); await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required'); await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    expect(lastSelection(wrapper)).toBeNull(); expect(wrapper.text()).toContain('配置尚未冻结')
    expect(wrapper.text()).not.toContain('已核验所选来源与排版能力')
  })
  it('requires explicit correction instead of silently using M0 for a malformed saved preference', async () => {
    localStorage.setItem('draft:batch-render-preferences:', JSON.stringify({ styleId: '7', sourcePolicy: 'unrecognized', layout: 'natural-layout-v1', seed: 4 }))
    const wrapper = controls(); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('本机保存的字形或排版选择无法识别')
    await wrapper.findAll('button').find((button) => button.text() === '重新选择系统字体与原有排版')!.trigger('click')
    expect(lastSelection(wrapper)?.style_mode).toBe('system')
  })
  it.each(['{broken-json', 'null'])('keeps unreadable saved preference %s blocked across reload until an explicit reset', async (raw) => {
    localStorage.setItem('draft:batch-render-preferences:', raw)
    const one = controls(); await flushPromises(); expect(lastSelection(one)).toBeNull(); one.unmount()
    const two = controls(); await flushPromises(); expect(lastSelection(two)).toBeNull()
    expect(two.text()).toContain('请明确重新选择')
  })
})

const PAGE_PLAN = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset1', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset1', name: 'Test', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template1', base_asset_id: 'asset1', status: 'approved', created_at: '2026-10-02', style_id: 7 }] }),
  '/api/v1/styles': envelope({ items: STYLES }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope(M2_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { job_id: 'fixture-render', status: 'succeeded' }, items: [] }),
}
const click = async (wrapper: VueWrapper, text: string) => { await wrapper.findAll('button').find((button) => button.text() === text)!.trigger('click'); await flushPromises() }
describe('P5 render payload contract', () => {
  it('sends explicit M0 over a personal-bound template in preview and formal lanes', async () => {
    const { wrapper, logs } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '预览草稿'); await click(wrapper, '继续编辑'); await click(wrapper, '正式生成')
    const posts = logs.filter((entry) => entry.method === 'POST')
    expect(posts).toHaveLength(2)
    for (const post of posts) { expect(post.body).toMatchObject({ style_mode: 'system', glyph_source_policy: 'fallback_allowed', layout_profile: 'baseline-v1' }); expect(post.body).not.toHaveProperty('style_id') }
  })
  it('keeps source policy, natural layout and seed identical across both render lanes', async () => {
    const { wrapper, logs } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('#render-style').setValue('7'); await flushPromises(); await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required'); await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1'); await wrapper.get('[aria-label="自然排版重复种子"]').setValue('12345')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '预览草稿'); await click(wrapper, '继续编辑'); await click(wrapper, '正式生成')
    const posts = logs.filter((entry) => entry.method === 'POST')
    expect(posts).toHaveLength(2)
    for (const post of posts) expect(post.body).toMatchObject({ style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 12345 })
  })
  it('does not render while selected capability is blocked', async () => {
    const { wrapper, logs } = mountPage(P5Batch, { plan: { ...PAGE_PLAN, '/api/v1/render-jobs/capabilities': envelope({ ...SYSTEM_CAPABILITIES, renderable: false, reason: 'font missing' }) } }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    expect(wrapper.findAll('button').find(button => button.text() === '预览草稿')?.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find(button => button.text() === '正式生成')?.attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })
  it('replays the immutable formal body after response loss, despite later text edits', async () => {
    const { wrapper } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    const original = globalThis.fetch; const submitted: { body: string; key: string | null }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') {
        submitted.push({ body: String(init.body), key: new Headers(init.headers).get('Idempotency-Key') })
        if (submitted.length === 1) throw new TypeError('response lost')
      }
      return original(url, init)
    }))
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('原内容'); await click(wrapper, '正式生成')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('新内容'); await click(wrapper, '重试原提交')
    expect(submitted).toHaveLength(2); expect(submitted[1]).toEqual(submitted[0])
  })
  it.each([401, 403, 408, 409, 429])('preserves the uncertain original body/key after a later HTTP %s response', async (status) => {
    const { wrapper } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    const original = globalThis.fetch; const sent: string[] = []; let count = 0
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') {
        sent.push(String(init.body)); ++count
        if (count === 1) throw new TypeError('lost response')
        return { ok: false, status, json: async () => problem(status, 'RETRY_OR_AUTH_REQUIRED', 'Try again with original key').json } as Response
      }
      return original(url, init)
    }))
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('原内容'); await click(wrapper, '正式生成')
    const saved = localStorage.getItem('yy:p5:pending-render:2')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('新内容'); await click(wrapper, '重试原提交')
    expect(sent[1]).toBe(sent[0]); expect(localStorage.getItem('yy:p5:pending-render:2')).toBe(saved)
    expect(wrapper.text()).toContain('重试原提交')
  })
  it('recovers exactly one saved pending formal request after reload without auto-submit', async () => {
    const one = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(one.wrapper); await flushPromises()
    const original = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') throw new TypeError('response lost')
      return original(url, init)
    }))
    await one.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('原内容'); await click(one.wrapper, '正式生成')
    const saved = JSON.parse(localStorage.getItem('yy:p5:pending-render:2')!); one.wrapper.unmount()
    const two = mountPage(P5Batch, { plan: { ...PAGE_PLAN, '/api/v1/render-jobs/capabilities': envelope({ ...SYSTEM_CAPABILITIES, renderable: false, reason: 'font missing' }) } }); wrappers.push(two.wrapper); await flushPromises()
    expect(two.logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
    expect(two.wrapper.text()).toContain('重试原提交')
    expect(two.wrapper.findAll('button').find(button => button.text() === '重试原提交')?.attributes('disabled')).toBeUndefined()
    expect(two.wrapper.findAll('button').find(button => button.text() === '预览草稿')?.attributes('disabled')).toBeDefined()
    const restoredFetch = globalThis.fetch; let repeatedKey: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') repeatedKey = new Headers(init.headers).get('Idempotency-Key')
      return restoredFetch(url, init)
    }))
    await two.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('后来编辑'); await click(two.wrapper, '重试原提交')
    expect(two.logs.find((entry) => entry.method === 'POST')?.body).toEqual(saved.body)
    expect(repeatedKey).toBe(saved.key)
    expect(localStorage.getItem('yy:p5:pending-render:2')).toBeNull()
  })
  it.each(['STYLE_DISABLED', 'STYLE_NOT_APPROVED', 'TEMPLATE_NOT_APPROVED', 'NON_CJK_FONT_UNAVAILABLE'])('releases a definite pre-create %s rejection so the user can correct selection', async (code) => {
    const { wrapper, logs } = mountPage(P5Batch, { plan: { ...PAGE_PLAN, '/api/v1/render-jobs': problem(409, code, 'Selection rejected before job creation') } }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '正式生成')
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    expect(localStorage.getItem('yy:p5:pending-render:2')).toBeNull()
    expect(wrapper.get('#render-style').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('重试原提交')
    expect(wrapper.text()).toContain('Selection rejected before job creation')
  })
  it('blocks malformed pending recovery rather than silently starting a different request', async () => {
    localStorage.setItem('yy:p5:pending-render:2', '{"key":"prior","body":{}}')
    const { wrapper, logs } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '正式生成')
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
    expect(wrapper.text()).toContain('未确认提交的记录不完整')
  })
  it.each(['failed', 'cancelled', 'partial', 'unknown'])('does not claim a ready preview from immediate %s result', async (status) => {
    const { wrapper } = mountPage(P5Batch, { plan: { ...PAGE_PLAN, '/api/v1/render-jobs': envelope({ job: { job_id: 'blocked-preview', status }, items: [] }) } }); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '预览草稿')
    expect(wrapper.text()).toContain(`草稿渲染未完成（${status}）`)
    expect(wrapper.text()).not.toContain('草稿渲染完成 ·')
  })
  it('suppresses an old preview create response after closing and changing source', async () => {
    const { wrapper } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    const original = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => String(url) === '/api/v1/render-jobs' && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : original(url, init)))
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '预览草稿')
    await wrapper.get('[aria-label="关闭预览"]').trigger('click'); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    finish(response({ job: { job_id: 'old-preview', status: 'succeeded' }, items: [] })); await flushPromises()
    expect(wrapper.find('[aria-label="批量生成草稿预览"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('old-preview')
  })
  it('does not navigate or adopt an old account formal result after account change', async () => {
    const { wrapper, pinia, router } = mountPage(P5Batch, { plan: PAGE_PLAN }); wrappers.push(wrapper); await flushPromises()
    const original = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => String(url) === '/api/v1/render-jobs' && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : original(url, init)))
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨'); await click(wrapper, '正式生成')
    useAuthStore(pinia).__setUserForTest({ user_id: '3', username: 'other', display_name: 'Other', role: 'user', must_change_password: false })
    await flushPromises(); finish(response({ job: { job_id: 'old-account' }, items: [] })); await flushPromises()
    expect(router.currentRoute.value.path).not.toContain('old-account')
    expect(localStorage.getItem('yy:p5:pending-render:3')).toBeNull()
    expect(wrapper.text()).not.toContain('重试原提交')
  })
})
