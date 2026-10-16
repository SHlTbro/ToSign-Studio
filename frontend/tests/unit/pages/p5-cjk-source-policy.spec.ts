import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import RenderStyleControls from '@/components/RenderStyleControls.vue'
import P5Batch from '@/pages/P5_Batch.vue'
import P5Review from '@/pages/P5_Review.vue'
import { decodePending, pendingRenderKey } from '@/api/pendingRender'
import { __resetHttpGatesForTest } from '@/api/http'
import { envelope, mountPage, problem, type FetchPlan } from './_harness'
import { M2_CAPABILITIES, SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { INK_RENDER_CAPABILITIES } from './ink-capability-fixtures'
import type { RenderCapabilities, RenderSelection } from '@/types/renderControls'

// Synthetic capabilities test UI wiring; they do not qualify a model or source policy.
const CJK_CAPABILITIES: RenderCapabilities = { ...M2_CAPABILITIES, source_policies: ['fallback_allowed', 'm2_required', 'm2_cjk_required'] }
const CJK_SELECTION: RenderSelection = { style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_cjk_required', layout_profile: 'baseline-v1' }
const TEXT = 'To：shitT / 拉shit永远通畅  〇豈𠀀'
const STYLES = [{ style_id: 7, name: 'Synthetic adapter', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }]
const PREFERENCES_KEY = 'draft:batch-render-preferences:2'
const PENDING_KEY = pendingRenderKey('2')
const wrappers: VueWrapper[] = []
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => envelope(value).json }) as Response
const lastSelection = (wrapper: VueWrapper) => wrapper.emitted('ready')?.at(-1)?.[0] as RenderSelection | null
function controls(read: (id: string) => Promise<RenderCapabilities> = async (id) => copy(id ? CJK_CAPABILITIES : SYSTEM_CAPABILITIES)) {
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url)
    return response(path.startsWith('/api/v1/styles') ? { items: STYLES } : await read(new URL(path, 'http://fixture').searchParams.get('style_id') ?? ''))
  }))
  const wrapper = mount(RenderStyleControls, { props: { disabled: false, scopeKey: '2' } }); wrappers.push(wrapper); return wrapper
}
const selectPolicy = async (wrapper: VueWrapper) => {
  await wrapper.get('#render-style').setValue('7'); await flushPromises()
  await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_cjk_required')
}
const PLAN: FetchPlan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset1', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'bless1', text: '拉shit永远通畅', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset1', name: 'Synthetic', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template1', base_asset_id: 'asset1', status: 'approved', created_at: '2026-10-03' }] }),
  '/api/v1/styles': envelope({ items: STYLES }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope(CJK_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { job_id: 'fixture-render', status: 'succeeded' }, items: [] }),
}
async function openBatch(plan = PLAN) { const result = mountPage(P5Batch, { plan }); wrappers.push(result.wrapper); await flushPromises(); return result }
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text() === text)
  if (!button) throw new Error(`Missing button ${text}`)
  await button.trigger('click'); await flushPromises()
}
const operation = () => ({ key: 'original-cjk-request', body: { ...CJK_SELECTION, template_id: 'template1', items: [{ cn: TEXT, sign_blogger: false }] } })
beforeEach(() => { localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })

describe('P5 visible Han-only required-M2 policy', () => {
  it('preserves M0 default and requires explicit capability-supported selection', async () => {
    const wrapper = controls(); await flushPromises()
    expect(lastSelection(wrapper)).toEqual({ style_mode: 'system', glyph_source_policy: 'fallback_allowed', layout_profile: 'baseline-v1' })
    expect(wrapper.get('option[value="m2_cjk_required"]').attributes('disabled')).toBeDefined()
    await selectPolicy(wrapper)
    expect(wrapper.get('option[value="m2_cjk_required"]').attributes('disabled')).toBeUndefined()
    expect(lastSelection(wrapper)).toEqual(CJK_SELECTION)
    const explanation = wrapper.get('[data-testid="cjk-source-policy-help"]').text()
    expect(explanation).toContain('含〇及兼容表意文字')
    expect(explanation).toContain('全角冒号')
    expect(explanation).toContain('明确使用系统字体 M0')
    expect(explanation).toContain('汉字失败会停止该项，不回退')
    expect(explanation).toContain('自定义单色')
  })
  it('does not enable the new policy for an older capability response that omits it', async () => {
    const wrapper = controls(async (id) => copy(id ? M2_CAPABILITIES : SYSTEM_CAPABILITIES)); await flushPromises()
    await selectPolicy(wrapper)
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('所选字形来源要求当前不可用')
    expect(wrapper.get('option[value="m2_cjk_required"]').attributes('disabled')).toBeDefined()
  })
  it.each([{ artifact: null }, { source_type: 'M0_SYSTEM' }, { renderable: false, reason: 'runtime unavailable' }])('fails closed for unqualified response %j', async (change) => {
    const wrapper = controls(async (id) => id ? { ...copy(CJK_CAPABILITIES), ...change } : copy(SYSTEM_CAPABILITIES)); await flushPromises(); await selectPolicy(wrapper)
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).not.toContain('已核验所选来源与排版能力')
  })
  it('restores a per-user CJK preference only after current capability validation', async () => {
    const one = controls(); await flushPromises(); await selectPolicy(one); one.unmount()
    const two = controls(); expect(lastSelection(two)).toBeNull(); await flushPromises()
    expect(lastSelection(two)).toEqual(CJK_SELECTION)
    expect((two.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('m2_cjk_required')
  })
  it('keeps a recovered CJK preference blocked when the capability has been removed', async () => {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ styleId: '7', sourcePolicy: 'm2_cjk_required', layout: 'baseline-v1', seed: 42 }))
    const wrapper = controls(async () => copy(M2_CAPABILITIES)); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull()
    expect((wrapper.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('m2_cjk_required')
    expect(wrapper.text()).not.toContain('本机保存的字形或排版选择无法识别')
  })
  it('does not silently downgrade CJK policy after switching to M0', async () => {
    const wrapper = controls(); await flushPromises(); await selectPolicy(wrapper)
    await wrapper.get('#render-style').setValue(''); await flushPromises()
    expect(lastSelection(wrapper)).toBeNull()
    expect((wrapper.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('m2_cjk_required')
  })
  it('rejects contradictory system capabilities that advertise a personal CJK policy', async () => {
    const wrapper = controls(async () => ({ ...copy(CJK_CAPABILITIES), style_mode: 'system', style_id: null })); await flushPromises()
    await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_cjk_required')
    expect(lastSelection(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('需要已验证的个人适配器')
  })
  it('suppresses a late profile response after source change', async () => {
    let finish!: (value: RenderCapabilities) => void
    const wrapper = controls(async (id) => id ? new Promise((resolve) => { finish = resolve }) : copy(SYSTEM_CAPABILITIES)); await flushPromises()
    await wrapper.get('#render-style').setValue('7'); await wrapper.get('#render-style').setValue(''); await flushPromises()
    finish(copy(CJK_CAPABILITIES)); await flushPromises()
    expect(lastSelection(wrapper)?.style_mode).toBe('system')
    expect(wrapper.get('option[value="m2_cjk_required"]').attributes('disabled')).toBeDefined()
  })
  it('keeps natural layout and source ink restricted to old m2_required despite available old-model capabilities', async () => {
    const capabilities = { ...copy(INK_RENDER_CAPABILITIES), source_policies: CJK_CAPABILITIES.source_policies }
    const wrapper = controls(async (id) => id ? capabilities : copy(SYSTEM_CAPABILITIES)); await flushPromises(); await selectPolicy(wrapper)
    await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    expect(lastSelection(wrapper)).toBeNull(); expect(wrapper.text()).toContain('自然排版需要明确选择「仅使用当前 M2」')
    await wrapper.get('[aria-label="排版方式"]').setValue('baseline-v1')
    await wrapper.get('[aria-label="墨色方式"]').setValue('source-solid-srgb-v1')
    expect(lastSelection(wrapper)).toBeNull(); expect(wrapper.text()).toContain('来源墨色需要明确选择')
    expect((wrapper.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('m2_cjk_required')
  })
})

describe('CJK request and frozen-review binding', () => {
  it('preserves mixed text and exact policy equally in preview and formal generation', async () => {
    const { wrapper, logs } = await openBatch(); await selectPolicy(wrapper)
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(TEXT)
    await wrapper.get('#blessing-0').setValue('bless1')
    await click(wrapper, '预览草稿'); await click(wrapper, '继续编辑'); await click(wrapper, '正式生成')
    const posts = logs.filter(({ method, url }) => method === 'POST' && url === '/api/v1/render-jobs').map(({ body }) => body as Record<string, unknown>)
    expect(posts).toHaveLength(2); expect(posts[0]).toEqual({ ...posts[1], draft: true })
    for (const body of posts) {
      expect(body).toMatchObject({ ...CJK_SELECTION, items: [{ cn: TEXT, blessing: '拉shit永远通畅' }] })
      expect(body).not.toHaveProperty('layout_seed'); expect(body).not.toHaveProperty('ink_palette_id')
    }
  })
  it('retains only baseline outer trimming and never rewrites case, fullwidth colon or internal spaces', async () => {
    const { wrapper, logs } = await openBatch(); await selectPolicy(wrapper)
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(`  ${TEXT}\u3000`); await click(wrapper, '正式生成')
    const sent = logs.find(({ method }) => method === 'POST')!.body as { items: { cn: string }[] }
    expect(sent.items[0]!.cn).toBe(TEXT)
  })
  it('replays an uncertain CJK request after reload with the same body/key and without auto-submit', async () => {
    const one = await openBatch(); await selectPolicy(one.wrapper)
    await one.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(TEXT)
    const original = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') throw new TypeError('response lost')
      return original(url, init)
    }))
    await click(one.wrapper, '正式生成')
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY)!); one.wrapper.unmount()
    const two = await openBatch()
    expect(two.logs.filter(({ method }) => method === 'POST')).toHaveLength(0)
    const restoredFetch = globalThis.fetch; let key: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') key = new Headers(init.headers).get('Idempotency-Key')
      return restoredFetch(url, init)
    }))
    await two.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('不同内容')
    await click(two.wrapper, '重试原提交')
    expect(two.logs.find(({ method }) => method === 'POST')?.body).toEqual(saved.body)
    expect(key).toBe(saved.key); expect(localStorage.getItem(PENDING_KEY)).toBeNull()
  })
  it('keeps CJK pending body/key through a rejected auth retry', async () => {
    const saved = operation(); localStorage.setItem(PENDING_KEY, JSON.stringify(saved))
    const { wrapper, logs } = await openBatch({ ...PLAN, '/api/v1/render-jobs': problem(401, 'AUTH_REQUIRED', 'Session expired') })
    await click(wrapper, '重试原提交')
    expect(logs.find(({ method }) => method === 'POST')?.body).toEqual(saved.body)
    expect(JSON.parse(localStorage.getItem(PENDING_KEY)!)).toEqual(saved)
  })
  it('decodes valid prior CJK operations without changing their immutable text or policy', () => {
    const saved = operation(); expect(decodePending(JSON.stringify(saved))).toEqual(saved)
    expect(decodePending(JSON.stringify({ ...saved, body: { ...saved.body, ink_color_mode: 'auto-contrast-v1' } })).body.glyph_source_policy).toBe('m2_cjk_required')
  })
  it.each([{ style_mode: 'system', style_id: undefined }, { style_id: 0 }, { layout_profile: 'natural-layout-v1', layout_seed: 42 },
    { layout_seed: 1 }, { ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1' }])('rejects forbidden recovered CJK combination %j', (change) => {
    const saved = operation(); expect(() => decodePending(JSON.stringify({ ...saved, body: { ...saved.body, ...change } }))).toThrow()
  })
  it('shows frozen CJK policy and actual M2/M0 provenance without calling all text personal M2', async () => {
    const warning = 'GLYPH_STYLE artifact=fixture chars=拉:M2,永:M2,远:M2,T:M0,：:M0'
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ styleId: '', sourcePolicy: 'fallback_allowed', layout: 'baseline-v1', seed: 0 }))
    const { wrapper, router } = mountPage(P5Review, { plan: { '/api/v1/render-jobs/job-a': envelope({ job: { job_id: 'job-a', status: 'succeeded', ...CJK_SELECTION },
      items: [{ item_id: 'item-a', status: 'completed', cn: TEXT, fallback_used: false, fallback_map: {}, warnings: [warning], retryable: false, render_version: 1 }] }) } }); wrappers.push(wrapper)
    await router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
    expect(wrapper.get('[data-testid="frozen-render-settings"]').text()).toContain('汉字仅使用当前 M2；其他字符明确使用系统字体 M0')
    await wrapper.findAll('button').find((button) => button.text().includes('字形实际来源'))!.trigger('click')
    expect(wrapper.text()).toContain(warning)
    expect(wrapper.get('[data-testid="review-cn"]').text()).toContain(TEXT)
  })
})
