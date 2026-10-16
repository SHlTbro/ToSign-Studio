import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, flushPromises, mountPage } from './_harness'
import { M2_CAPABILITIES, SYSTEM_CAPABILITIES } from './render-capability-fixtures'

// Current UI choices are synthetic API fixtures here. No qualified API, live DB or model is used.
const STYLE = '2'
const CAP_PATH = '/api/v1/render-jobs/capabilities'
const preferences = { styleId: STYLE, sourcePolicy: 'm2_cjk_required', layout: 'baseline-v1', seed: 2026100401,
  mixedFontsEnabled: true, latinFontId: 'preset-mengshen', otherFontId: 'preset-noto-cjk-sc-face2',
  inkMode: 'custom-solid-srgb-v1', inkHex: '#8C3044', inkPaletteId: '', inkPaletteIdentity: '' }
const capabilities = {
  ...M2_CAPABILITIES, style_id: 2, source_policies: ['fallback_allowed', 'm2_required', 'm2_cjk_required'],
  accepted_v3: { available: true, fonts: { latin: 'preset-caveat-600', other: 'preset-noto-cjk-sc-face2' },
    binding: { schema: 'accepted-v3-layout-binding/1', profile_sha256: 'a'.repeat(64), implementation_sha256: 'b'.repeat(64),
      adapter_sha256: '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81' }, seed: 2026100401, rgb: [140, 48, 68] },
  ink_colors: { default_mode: 'auto-contrast-v1', custom_solid: { available: true }, source_solid: { available: false, reason: 'fixture', palettes: [] },
    supported_layouts: ['natural-layout-v1'], supported_source_policies: ['m2_required'], automatic_recoloring: false },
  non_cjk_fonts: { schema: 'non-cjk-font-catalog/1', missing_requested_fonts: [], fonts: [
    { id: 'preset-mengshen', name: 'MengShen', roles: ['latin'], available: true },
    { id: 'preset-caveat-600', name: 'Caveat 600 (V3)', roles: ['latin'], available: true },
    { id: 'preset-noto-cjk-sc-face2', name: 'Noto face 2', roles: ['other'], available: true },
  ] },
}
const plan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset-a', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template-a', base_asset_id: 'asset-a', status: 'approved' }] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'first', text: '拉shit开心', enabled: true }, { id: 'second', text: '美貌永驻6百6十6', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 2, name: '0178认可V3', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
  '/api/v1/render-jobs': envelope({ job: { job_id: 'public-fixture-only', status: 'queued' }, items: [] }),
}
const wrappers: VueWrapper[] = []
interface Read { path: string; signal: AbortSignal }
beforeEach(() => {
  vi.useFakeTimers(); localStorage.clear()
  localStorage.setItem('draft:batch-render-preferences:2', JSON.stringify(preferences))
  localStorage.setItem('draft:batch-typography:2', JSON.stringify({ ordinaryScale: 1.3, sizeVariation: 2, tiltVariation: 0, seed: 2026100401 }))
  localStorage.setItem('draft:batch-generation-rows', JSON.stringify([{ cn: 'Alice', blessing_id: 'first', sign_blogger: false }, { cn: 'Bob', blessing_id: 'second', sign_blogger: false }]))
})
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })
const response = (data: unknown) => new Response(JSON.stringify(envelope(data).json), { status: 200 })
function delayed(data: unknown, signal: AbortSignal, ms = 90000): Promise<Response> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => resolve(response(data)), ms)
    signal.addEventListener('abort', () => { window.clearTimeout(timer); reject(new DOMException('deadline', 'AbortError')) }, { once: true })
  })
}
async function open(read: (call: Read, reads: Read[]) => Response | Promise<Response>) {
  const mounted = mountPage(P5Batch, { plan }); wrappers.push(mounted.wrapper)
  const ordinaryFetch = globalThis.fetch; const reads: Read[] = []
  // The immediate component reader has already started; install this before remounting its owner key.
  mounted.wrapper.unmount(); wrappers.pop()
  vi.stubGlobal('fetch', vi.fn((path: RequestInfo | URL, init?: RequestInit) => {
    if (!String(path).startsWith(CAP_PATH)) return ordinaryFetch(path, init)
    const call = { path: String(path), signal: init!.signal as AbortSignal }; reads.push(call)
    return Promise.resolve(read(call, reads))
  }))
  // Mount with the existing Pinia/router without replacing fetch, exercising the same public page.
  const { mount } = await import('@vue/test-utils')
  const wrapper = mount(P5Batch, { global: { plugins: [mounted.pinia, mounted.router] }, attachTo: document.body })
  wrappers.push(wrapper); await flushPromises()
  return { wrapper, reads, logs: mounted.logs }
}
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text() === text)
  if (!button) throw new Error(`Missing button ${text}`)
  await button.trigger('click'); await flushPromises()
}
function expectAuthoredChoices(wrapper: VueWrapper) {
  expect(wrapper.get('#render-style').element).toHaveProperty('value', STYLE)
  expect(wrapper.get('[aria-label="英文手写字体"]').element).toHaveProperty('value', 'preset-mengshen')
  expect(wrapper.get('[aria-label="标点及其他字体"]').element).toHaveProperty('value', 'preset-noto-cjk-sc-face2')
  expect(wrapper.get('[aria-label="逐字大小不规则程度（%）"]').element).toHaveProperty('value', '2')
  expect(wrapper.get('#blessing-1').element).toHaveProperty('value', 'second')
  expect(wrapper.text()).toContain('美貌永驻6百6十6')
}

describe('owned profile capability recovery preserves current authored choices', () => {
  it('shows a neutral pending state, then a real Chinese qualification failure without enabling generation', async () => {
    const { wrapper, logs } = await open(call => delayed({ ...capabilities, renderable: false, reason: 'exact positive author/profile artifact identities required' }, call.signal))
    expect(wrapper.find('[data-testid="capability-pending"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="render-style-controls"]').find('[role="alert"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(90000); await flushPromises()
    expect(wrapper.find('[data-testid="capability-pending"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="render-style-controls"]').get('[role="alert"]').text()).toContain('缺少已核验的作者、字体或模型身份')
    expectAuthoredChoices(wrapper)
    expect(wrapper.findAll('button').find(button => button.text() === '正式生成')?.attributes('disabled')).toBeDefined()
    expect(logs.filter(call => call.method === 'POST')).toHaveLength(0)
  })
  it('restores profile2 after a 90-second response and sends the current MengShen, typography and blessing choices', async () => {
    const { wrapper, reads, logs } = await open((call) => delayed(capabilities, call.signal))
    expect(reads.map((call) => call.path)).toEqual([`${CAP_PATH}?style_id=2`])
    expect(wrapper.get('[data-testid="capability-pending"]').text()).toContain('正在核验模型与字体，请稍候')
    expect(wrapper.get('[data-testid="render-style-controls"]').find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('当前不可用')
    expect(wrapper.find('[data-testid="ink-capability-warning"]').exists()).toBe(false)
    expect(wrapper.findAll('button').find(button => button.text() === '预览草稿')?.attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(60001)
    expect(reads[0]!.signal.aborted).toBe(false)
    expect(wrapper.text()).not.toContain('请求超时')
    await vi.advanceTimersByTimeAsync(29999); await flushPromises()
    expectAuthoredChoices(wrapper)
    expect(wrapper.text()).toContain('已核验所选来源与排版能力')
    await click(wrapper, '预览草稿')
    const post = logs.find((call) => call.method === 'POST' && call.url === '/api/v1/render-jobs')!
    expect(post.body).toMatchObject({ draft: true, style_mode: 'profile', style_id: 2, glyph_source_policy: 'm2_cjk_required',
      non_cjk_fonts: { latin: 'preset-mengshen', other: 'preset-noto-cjk-sc-face2' }, ink_color_mode: 'custom-solid-srgb-v1', ink_color_rgb: [140, 48, 68],
      handwriting: { size_variation_pct: 2, tilt_variation_deg: 0, seed: 2026100401 },
      items: [{ cn: 'Alice', blessing: '拉shit开心' }, { cn: 'Bob', blessing: '美貌永驻6百6十6' }] })
    expect(reads).toHaveLength(1)
  })
  it('ignores the earlier no-style M0 timeout after explicit profile selection and recovers that profile', async () => {
    localStorage.setItem('draft:batch-render-preferences:2', JSON.stringify({ ...preferences, styleId: '' }))
    const { wrapper, reads } = await open((call) => delayed(call.path.includes('style_id=2') ? capabilities : SYSTEM_CAPABILITIES, call.signal))
    await wrapper.get('#render-style').setValue(STYLE); await flushPromises()
    expect(reads.map((call) => call.path)).toEqual([CAP_PATH, `${CAP_PATH}?style_id=2`])
    await vi.advanceTimersByTimeAsync(60001)
    expect(reads[0]!.signal.aborted).toBe(true)
    expect(reads[1]!.signal.aborted).toBe(false)
    expect(wrapper.text()).not.toContain('请求超时')
    await vi.advanceTimersByTimeAsync(29999); await flushPromises()
    expectAuthoredChoices(wrapper)
    expect(wrapper.text()).toContain('已核验所选来源与排版能力')
  })
  it('bounds owned reads at 600 seconds and retries only after an explicit same-profile revalidation', async () => {
    const { wrapper, reads } = await open((call, calls) => delayed(capabilities, call.signal, calls.length === 1 ? 700000 : 90000))
    await vi.advanceTimersByTimeAsync(599999)
    expect(reads[0]!.signal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(reads[0]!.signal.aborted).toBe(true)
    expect(wrapper.text()).toContain('请求超时')
    await vi.advanceTimersByTimeAsync(10000)
    expect(reads).toHaveLength(1)
    expectAuthoredChoices(wrapper)
    await click(wrapper, '重新核验所选风格')
    expect(reads[1]!.path).toBe(`${CAP_PATH}?style_id=2`)
    await vi.advanceTimersByTimeAsync(90000); await flushPromises()
    expectAuthoredChoices(wrapper)
    expect(wrapper.text()).toContain('已核验所选来源与排版能力')
  })
  it('keeps unavailable user-selected MengShen blocked after an authoritative response rather than choosing V3 Caveat', async () => {
    const unavailable = { ...capabilities, non_cjk_fonts: { ...capabilities.non_cjk_fonts, fonts: capabilities.non_cjk_fonts.fonts.filter((font) => font.id !== 'preset-mengshen') } }
    const { wrapper, logs } = await open((call) => delayed(unavailable, call.signal))
    await vi.advanceTimersByTimeAsync(90000); await flushPromises()
    expectAuthoredChoices(wrapper)
    expect(wrapper.text()).toContain('请明确选择可用的英文手写字体')
    await click(wrapper, '预览草稿')
    expect(logs.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
  it('keeps a wrong profile capability identity rejected after the longer wait', async () => {
    const { wrapper, logs } = await open((call) => delayed({ ...capabilities, style_id: 7 }, call.signal))
    await vi.advanceTimersByTimeAsync(90000); await flushPromises()
    expectAuthoredChoices(wrapper)
    expect(wrapper.text()).toContain('能力与所选风格不匹配')
    await click(wrapper, '预览草稿')
    expect(logs.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})
