import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import RenderStyleControls from '@/components/RenderStyleControls.vue'
import FrozenInkSummary from '@/components/FrozenInkSummary.vue'
import P5Batch from '@/pages/P5_Batch.vue'
import P5Review from '@/pages/P5_Review.vue'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { FROZEN_INK, INK_BINDING, INK_RENDER_CAPABILITIES } from './ink-capability-fixtures'
import { envelope, mountPage, problem, type FetchPlan } from './_harness'
import { inkBindingIdentity } from '@/api/inkColor'
import { listPendingRenders, pendingRenderKey, persistPendingRender, type PendingRenderOperation } from '@/api/pendingRender'
import { __resetHttpGatesForTest } from '@/api/http'
import type { RenderCapabilities, RenderSelection } from '@/types/renderControls'

const STYLES = [{ style_id: 7, name: 'Synthetic adapter', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }]
const PREF = 'draft:batch-render-preferences:2'
const PENDING = 'yy:p5:pending-render:2'
const CN = '  \u3000小雨  星河\u3000\n\n  春风\u3000 \n '
const wrappers: VueWrapper[] = []
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => envelope(value).json }) as Response
const last = (wrapper: VueWrapper) => wrapper.emitted('ready')?.at(-1)?.[0] as RenderSelection | null
const selected = { styleId: '7', sourcePolicy: 'm2_required', layout: 'natural-layout-v1', seed: 123,
  inkMode: 'source-solid-srgb-v1', inkPaletteId: 'train-magenta-v1', inkPaletteIdentity: inkBindingIdentity(INK_BINDING) }
async function click(wrapper: VueWrapper, text: string) {
  const node = wrapper.findAll('button').find((button) => button.text() === text)
  if (!node) throw new Error(`Missing button ${text}`)
  await node.trigger('click'); await flushPromises()
}
function controls(read: (url: string) => Promise<RenderCapabilities> = async (url) => clone(url.includes('style_id=7') ? INK_RENDER_CAPABILITIES : SYSTEM_CAPABILITIES)) {
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => response(String(url).startsWith('/api/v1/styles') ? { items: STYLES } : await read(String(url)))))
  const wrapper = mount(RenderStyleControls, { props: { disabled: false, scopeKey: '2' } }); wrappers.push(wrapper); return wrapper
}
async function natural(wrapper: VueWrapper) {
  await wrapper.get('#render-style').setValue('7'); await flushPromises()
  await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required')
  await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
  await wrapper.get('[aria-label="自然排版重复种子"]').setValue('123')
}
async function ink(wrapper: VueWrapper, palette = 'train-magenta-v1') {
  await natural(wrapper)
  await wrapper.get('[aria-label="墨色方式"]').setValue('source-solid-srgb-v1')
  await wrapper.get('[aria-label="来源墨色"]').setValue(palette)
}
beforeEach(() => { localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear(); document.body.innerHTML = '' })

describe('P5 explicit ink controls', () => {
  it('keeps the exact legacy request by default even when qualified palettes exist', async () => {
    const wrapper = controls(); await flushPromises(); await natural(wrapper)
    expect(last(wrapper)).toEqual({ style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 123 })
    expect((wrapper.get('[aria-label="墨色方式"]').element as HTMLSelectElement).value).toBe('auto-contrast-v1')
    expect(wrapper.find('[aria-label="来源墨色"]').exists()).toBe(false)
  })
  it('never selects palette/layout/policy automatically and submits no binding or RGB', async () => {
    const wrapper = controls(); await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    await wrapper.get('[aria-label="墨色方式"]').setValue('source-solid-srgb-v1')
    expect(last(wrapper)).toBeNull(); expect(wrapper.text()).toContain('不会自动更换')
    expect((wrapper.get('[aria-label="排版方式"]').element as HTMLSelectElement).value).toBe('baseline-v1')
    expect((wrapper.get('[aria-label="字形来源要求"]').element as HTMLSelectElement).value).toBe('fallback_allowed')
    await natural(wrapper)
    expect(last(wrapper)).toBeNull(); expect(wrapper.text()).toContain('请明确选择一份')
    await wrapper.get('[aria-label="来源墨色"]').setValue('train-magenta-v1')
    expect(last(wrapper)).toEqual({ style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 123,
      ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1' })
    expect(wrapper.get('[data-testid="selected-ink-identity"]').text()).toContain('#C41AB4')
    expect(wrapper.text()).toContain('按 sRGB 假设处理'); expect(wrapper.text()).toContain('实际照片预览')
    expect(wrapper.text()).toContain('不改变透明度、笔画、排版或种子')
  })
  it('returns to byte-compatible default fields only when explicitly switched off, keeping all other choices', async () => {
    const wrapper = controls(); await flushPromises(); await ink(wrapper)
    const colored = last(wrapper)!
    await wrapper.get('[aria-label="墨色方式"]').setValue('auto-contrast-v1')
    const { ink_color_mode: _mode, ink_palette_id: _palette, ...prior } = colored
    expect(last(wrapper)).toEqual(prior)
    await wrapper.get('[aria-label="墨色方式"]').setValue('source-solid-srgb-v1')
    expect(last(wrapper)).toEqual(colored)
  })
  it('requires fresh server revalidation after restoring the exact explicit choice', async () => {
    const one = controls(); await flushPromises(); await ink(one, 'train-violet-v1'); one.unmount()
    const two = controls(); expect(last(two)).toBeNull(); await flushPromises()
    expect(last(two)?.ink_palette_id).toBe('train-violet-v1')
    expect(last(two)?.layout_seed).toBe(123)
  })
  it('keeps a restored source selection blocked when the catalog is unqualified, without switching to monochrome', async () => {
    localStorage.setItem(PREF, JSON.stringify(selected))
    const unavailable = clone(INK_RENDER_CAPABILITIES); unavailable.ink_colors!.source_solid = { available: false, reason: 'Independent photo QA incomplete', palettes: [] }
    const wrapper = controls(async () => unavailable); await flushPromises()
    expect(last(wrapper)).toBeNull(); expect(wrapper.text()).toContain('训练原图尚未完成独立质量审核')
    expect((wrapper.get('[aria-label="墨色方式"]').element as HTMLSelectElement).value).toBe('source-solid-srgb-v1')
    expect((wrapper.get('[aria-label="来源墨色"]').element as HTMLSelectElement).value).toBe('train-magenta-v1')
  })
  it('does not infer availability from an M2 label when ink capability is missing or malformed', async () => {
    const bad = clone(INK_RENDER_CAPABILITIES); bad.ink_colors!.source_solid.palettes[0]!.binding.artifact_id = 99
    const wrapper = controls(async (url) => url.includes('style_id=7') ? bad : clone(SYSTEM_CAPABILITIES)); await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
    expect(wrapper.get('option[value="source-solid-srgb-v1"]').attributes('disabled')).toBeDefined()
    expect(last(wrapper)?.ink_color_mode).toBeUndefined()
    expect(wrapper.text()).toContain('身份不完整或不匹配')
  })
  it('preserves source mode and palette across an incompatible style switch without generating', async () => {
    const wrapper = controls(); await flushPromises(); await ink(wrapper)
    await wrapper.get('#render-style').setValue(''); await flushPromises()
    expect(last(wrapper)).toBeNull()
    expect((wrapper.get('[aria-label="墨色方式"]').element as HTMLSelectElement).value).toBe('source-solid-srgb-v1')
    expect((wrapper.get('[aria-label="来源墨色"]').element as HTMLSelectElement).value).toBe('train-magenta-v1')
  })
  it('requires explicit reselection if the same palette ID returns a changed frozen identity', async () => {
    let caps = clone(INK_RENDER_CAPABILITIES)
    const wrapper = controls(async (url) => url.includes('style_id=7') ? caps : clone(SYSTEM_CAPABILITIES)); await flushPromises(); await ink(wrapper)
    caps = clone(caps); caps.ink_colors!.source_solid.palettes[0]!.binding.catalog_sha256 = '9'.repeat(64)
    await click(wrapper, '重新核验所选风格')
    expect(last(wrapper)).toBeNull(); expect(wrapper.text()).toContain('身份尚未确认或已改变')
    await wrapper.get('[aria-label="来源墨色"]').setValue('')
    await wrapper.get('[aria-label="来源墨色"]').setValue('train-magenta-v1')
    expect(last(wrapper)?.ink_palette_id).toBe('train-magenta-v1')
  })
  it('ignores a late ink-capability response after source changes', async () => {
    let finish!: (value: RenderCapabilities) => void
    const wrapper = controls(async (url) => url.includes('style_id=7') ? new Promise((resolve) => { finish = resolve }) : clone(SYSTEM_CAPABILITIES))
    await flushPromises(); await wrapper.get('#render-style').setValue('7'); await wrapper.get('#render-style').setValue(''); await flushPromises()
    finish(clone(INK_RENDER_CAPABILITIES)); await flushPromises()
    expect(last(wrapper)?.style_mode).toBe('system'); expect(wrapper.get('option[value="source-solid-srgb-v1"]').attributes('disabled')).toBeDefined()
  })
  it.each([{ inkMode: 'automatic-random' }, { inkPaletteId: 'heldout-green' }, { inkPaletteIdentity: 123 }])('requires explicit reset for malformed saved color preferences %j', async (change) => {
    localStorage.setItem(PREF, JSON.stringify({ ...selected, ...change }))
    const wrapper = controls(); await flushPromises(); expect(last(wrapper)).toBeNull()
    expect(wrapper.text()).toContain('请明确重新选择')
  })
})

const PLAN: FetchPlan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset1', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset1', name: 'Test', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template1', base_asset_id: 'asset1', status: 'approved', created_at: '2026-10-02' }] }),
  '/api/v1/styles': envelope({ items: STYLES }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope(INK_RENDER_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { ...FROZEN_INK, job_id: 'fixture-render', status: 'succeeded' }, items: [] }),
}
async function batch(plan = PLAN) { const value = mountPage(P5Batch, { plan }); wrappers.push(value.wrapper); await flushPromises(); return value }

describe('P5 ink payload, replay and frozen readback', () => {
  it.each(['train-magenta-v1', 'train-violet-v1'])('preserves exact %s color and authored whitespace across preview/formal requests', async (palette) => {
    const { wrapper, logs } = await batch(); await ink(wrapper, palette)
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN)
    await click(wrapper, '预览草稿')
    // Returned frozen settings are displayed independently of the currently selected palette.
    expect(wrapper.get('[data-testid="frozen-ink-settings"]').text()).toContain('冻结调色板：train-magenta-v1')
    expect(wrapper.get('[data-testid="frozen-ink-settings"]').text()).toContain(INK_BINDING.source_image_sha256)
    await click(wrapper, '继续编辑'); await click(wrapper, '正式生成')
    const posts = logs.filter(({ method }) => method === 'POST').map(({ body }) => body as Record<string, unknown>)
    expect(posts).toHaveLength(2); expect(posts[0]).toEqual({ ...posts[1], draft: true })
    for (const body of posts) {
      expect(body).toMatchObject({ ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: palette, layout_seed: 123, items: [{ cn: CN }] })
      expect(body).not.toHaveProperty('ink_color_binding'); expect(body).not.toHaveProperty('ink_color_binding_sha256'); expect(body).not.toHaveProperty('rgb')
    }
  })
  it('invalidates the prior preview immediately on a color choice change', async () => {
    const { wrapper } = await batch(); await ink(wrapper); await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN)
    await click(wrapper, '预览草稿'); expect(wrapper.find('[aria-label="批量生成草稿预览"]').exists()).toBe(true)
    await wrapper.get('[aria-label="来源墨色"]').setValue('train-violet-v1')
    expect(wrapper.find('[aria-label="批量生成草稿预览"]').exists()).toBe(false)
  })
  it('replays the exact color body/key after response loss, reload and later preference edits', async () => {
    const first = await batch(); await ink(first.wrapper); await first.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN)
    const original = globalThis.fetch; let body = ''; let key: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') { body = String(init.body); key = new Headers(init.headers).get('Idempotency-Key'); throw new TypeError('lost') }
      return original(url, init)
    }))
    await click(first.wrapper, '正式生成'); first.wrapper.unmount()
    localStorage.setItem(PREF, JSON.stringify({ ...selected, inkMode: 'auto-contrast-v1' }))
    const second = await batch(); expect(second.logs.filter(({ method }) => method === 'POST')).toHaveLength(0)
    const next = globalThis.fetch; let replay = ''; let replayKey: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') { replay = String(init.body); replayKey = new Headers(init.headers).get('Idempotency-Key') }
      return next(url, init)
    }))
    await click(second.wrapper, '重试原提交')
    expect(replay).toBe(body); expect(replayKey).toBe(key); expect(localStorage.getItem(PENDING)).toBeNull()
  })
  it.each([['INK_COLOR_UNAVAILABLE', 409], ['INK_COLOR_UNSUPPORTED', 400]] as const)('unlocks edits after definite pre-create %s rejection', async (code, status) => {
    const { wrapper } = await batch({ ...PLAN, '/api/v1/render-jobs': problem(status, code, 'Ink selection rejected before creation') })
    await ink(wrapper); await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN); await click(wrapper, '正式生成')
    expect(localStorage.getItem(PENDING)).toBeNull(); expect(wrapper.get('[aria-label="墨色方式"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('Ink selection rejected before creation')
  })
  it('blocks a forged server-only color binding in saved pending state instead of submitting it', async () => {
    localStorage.setItem(PENDING, JSON.stringify({ key: 'frozen', body: { template_id: 'template1', items: [{ cn: CN }], ...FROZEN_INK } }))
    const { wrapper, logs } = await batch(); await click(wrapper, '正式生成')
    expect(logs.filter(({ method }) => method === 'POST')).toHaveLength(0); expect(wrapper.text()).toContain('未确认提交的记录不完整')
  })
  it('reads review color only from the frozen job, independent of changed local preferences', async () => {
    localStorage.setItem(PREF, JSON.stringify({ ...selected, inkMode: 'auto-contrast-v1', inkPaletteId: 'train-violet-v1' }))
    const { wrapper, router } = mountPage(P5Review, { plan: { '/api/v1/render-jobs/job-a': envelope({ job: { ...FROZEN_INK, status: 'succeeded' }, items: [] }) } }); wrappers.push(wrapper)
    await router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
    const summary = wrapper.get('[data-testid="frozen-ink-settings"]')
    expect(summary.text()).toContain('冻结调色板：train-magenta-v1'); expect(summary.text()).toContain('#C41AB4')
    expect(summary.text()).toContain(INK_BINDING.implementation_sha256); expect(summary.text()).toContain('服务端完整绑定校验：' + '8'.repeat(64)); expect(summary.text()).toContain('没有自动可读性通过结论')
    expect(summary.text()).not.toContain('train-violet-v1')
  })
  it.each([undefined, null, { ...INK_BINDING, artifact_id: '12' }, { ...INK_BINDING, profile_id: 99 }])('does not present malformed frozen binding as confirmed %j', (binding) => {
    const wrapper = mount(FrozenInkSummary, { props: { settings: { ...FROZEN_INK, ink_color_binding: binding } as never } }); wrappers.push(wrapper)
    expect(wrapper.text()).toContain('不能确认本次墨色来源'); expect(wrapper.text()).not.toContain('#C41AB4')
  })
  it('labels absent historical color and explicit auto mode honestly', () => {
    const one = mount(FrozenInkSummary, { props: { settings: {} } }); wrappers.push(one)
    expect(one.text()).toContain('历史任务未记录墨色方式')
    const two = mount(FrozenInkSummary, { props: { settings: { ink_color_mode: 'auto-contrast-v1', ink_palette_id: null, ink_color_binding: null } } }); wrappers.push(two)
    expect(two.text()).toContain('原有自动对比单色'); expect(two.text()).not.toContain('训练原图取色')
  })
})


describe('P5 cross-instance recovery chooser', () => {
  const first: PendingRenderOperation = { key: 'lost-magenta', body: { template_id: 'template1', items: [{ cn: CN, sign_blogger: false }],
    style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 123,
    ink_color_mode: 'source-solid-srgb-v1', ink_palette_id: 'train-magenta-v1' } }
  const second: PendingRenderOperation = { key: 'lost-violet', body: { ...first.body, ink_palette_id: 'train-violet-v1', items: [{ cn: '  别的窗口\n 内容 ', sign_blogger: false }] } }

  it('blocks an already-open tab before POST without depending on a storage event, and never silently adopts the other palette', async () => {
    const current = await batch(); await ink(current.wrapper, 'train-violet-v1'); await current.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('当前内容')
    persistPendingRender('2', first)
    await click(current.wrapper, '正式生成')
    expect(current.logs.filter(({ method }) => method === 'POST')).toHaveLength(0)
    expect((current.wrapper.get('[aria-label="来源墨色"]').element as HTMLSelectElement).value).toBe('train-violet-v1')
    expect(current.wrapper.text()).not.toContain('重试原提交')
    expect(current.wrapper.text()).toContain('不会覆盖或自动重放它')
    await current.wrapper.get('[aria-label="恢复未确认提交 lost-magenta"]').trigger('click'); await flushPromises()
    await click(current.wrapper, '重试原提交')
    expect(current.logs.find(({ method }) => method === 'POST')?.body).toEqual(first.body)
    expect(listPendingRenders('2')).toEqual([])
  })

  it('requires an explicit selection among multiple durable operations and clears only the chosen one', async () => {
    persistPendingRender('2', first); persistPendingRender('2', second)
    const current = await batch()
    expect(current.wrapper.get('[data-testid="pending-render-recovery"]').text()).toContain('未确认的正式提交（2）')
    expect(current.wrapper.text()).not.toContain('重试原提交')
    await click(current.wrapper, '正式生成')
    expect(current.logs.filter(({ method }) => method === 'POST')).toHaveLength(0)
    await current.wrapper.get('[aria-label="恢复未确认提交 lost-violet"]').trigger('click'); await flushPromises()
    await click(current.wrapper, '重试原提交')
    expect(current.logs.find(({ method }) => method === 'POST')?.body).toEqual(second.body)
    expect(listPendingRenders('2')).toEqual([first])
  })

  it('keeps a newer unrelated pending operation after the original request finally returns', async () => {
    const current = await batch(); await ink(current.wrapper); await current.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN)
    const normal = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => String(url) === '/api/v1/render-jobs' && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : normal(url, init)))
    await click(current.wrapper, '正式生成')
    persistPendingRender('2', second)
    localStorage.setItem(pendingRenderKey('2'), JSON.stringify(second))
    finish(response({ job: { ...FROZEN_INK, job_id: 'old-success', status: 'succeeded' }, items: [] })); await flushPromises()
    expect(listPendingRenders('2')).toEqual([second])
    expect(localStorage.getItem(pendingRenderKey('2'))).toBe(JSON.stringify(second))
  })

  it('arbitrates two immediately submitted same-owner pages before a second POST and retains the in-flight operation', async () => {
    const a = await batch(); await ink(a.wrapper); await a.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue(CN)
    const b = await batch(); await ink(b.wrapper, 'train-violet-v1'); await b.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('第二份')
    const normal = globalThis.fetch; let posts = 0; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') { ++posts; return new Promise<Response>((resolve) => { finish = resolve }) }
      return normal(url, init)
    }))
    await Promise.all([click(a.wrapper, '正式生成'), click(b.wrapper, '正式生成')])
    expect(posts).toBe(1); expect(listPendingRenders('2')).toHaveLength(1)
    expect(listPendingRenders('2')[0]!.body.ink_palette_id).toBe('train-magenta-v1')
    expect((b.wrapper.get('[aria-label="来源墨色"]').element as HTMLSelectElement).value).toBe('train-violet-v1')
    finish(response({ job: { ...FROZEN_INK, job_id: 'success', status: 'succeeded' }, items: [] })); await flushPromises()
  })
})

describe('frozen ink proof consistency', () => {
  it.each([
    { ink_color_mode: 'unknown' }, { ink_color_mode: null }, { ink_color_mode: undefined }, { ink_palette_id: null },
    { ink_palette_id: null, ink_color_binding: null, ink_color_binding_sha256: null },
    { ink_color_mode: 'auto-contrast-v1', ink_palette_id: 'train-magenta-v1' },
    { ink_color_mode: 'auto-contrast-v1', ink_color_binding_sha256: '8'.repeat(64) },
    { ink_color_binding_sha256: '8'.repeat(64) }, { ...FROZEN_INK, ink_color_binding_sha256: 'bad' },
  ])('alerts for partial or contradictory modern metadata %j', (settings) => {
    const wrapper = mount(FrozenInkSummary, { props: { settings: settings as never } }); wrappers.push(wrapper)
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('历史任务未记录墨色方式')
    expect(wrapper.text()).not.toContain('原有自动对比单色')
    expect(wrapper.text()).not.toContain('#C41AB4')
  })
})
