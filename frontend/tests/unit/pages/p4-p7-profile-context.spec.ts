import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P4 from '@/pages/P4_Extract.vue'
import P7 from '@/pages/P7_Training.vue'
import { UploadSession } from '@/api/xhrUpload'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const styles = [
  { style_id: 99, name: '其他来源', source_type: 'HANDWRITING_ASSET', status: 'approved' },
  { style_id: 42, name: '指定来源', source_type: 'HANDWRITING_ASSET', status: 'approved' },
]
const wrappers: VueWrapper[] = []
function mount(component: unknown, extra: FetchPlan = {}) {
  const result = mountPage(component, { plan: {
    '/api/v1/styles': envelope({ items: styles }),
    '/api/v1/styles/42/samples': envelope({ items: [] }),
    '/api/v1/styles/99/samples': envelope({ items: [] }),
    '/api/v1/styles/42/m2-runtime': envelope({ ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'SNAPSHOT_UNAVAILABLE', detail: '指定来源无快照' }], capabilities: {}, acceptance_policy: { mode: 'OWNER_REQUIRED', human_blind_review: 'REQUIRED', candidate_confirmation: 'OWNER_REQUIRED' } }),
    '/api/v1/styles/99/m2-runtime': envelope({ ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'SNAPSHOT_UNAVAILABLE', detail: '其他来源无快照' }], capabilities: {}, acceptance_policy: { mode: 'OWNER_REQUIRED', human_blind_review: 'REQUIRED', candidate_confirmation: 'OWNER_REQUIRED' } }),
    '/api/v1/training-sets': envelope({ items: [], total: 0, page: 1, page_size: 24 }),
    '/api/v1/training-samples': envelope({ items: [], total: 0 }),
    '/api/v1/assets/uploaded': envelope({ width: 128, height: 128 }),
    '/api/v1/detect-text': envelope({ items: [] }),
    '/api/v1/style-runs/8': envelope({ run_id: 8, status: 'cancelled', config: { engine: 'm2', style_id: 99 }, sample_ids: [] }),
    '/api/v1/style-runs/8/readiness': envelope({ evaluation_state: 'not_started', publication: null, allowed_actions: {}, blockers: {} }),
    '/api/v1/style-runs/8/progress': envelope({ status: 'cancelled' }),
    '/api/v1/style-runs/8/metrics': envelope({ state: 'pending' }),
    '/api/v1/style-runs/8/evaluate': envelope({ state: 'unavailable' }),
    '/api/v1/style-runs/8/samples': envelope({ items: [] }),
    ...extra,
  } })
  wrappers.push(result.wrapper)
  return result
}
beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => { if (wrapper.exists()) wrapper.unmount() }); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('Profile-scoped onboarding continuity', () => {
  it('P4 upload preserves the profile, and reload with two profiles does not select the first one', async () => {
    vi.spyOn(UploadSession.prototype, 'start').mockResolvedValue({ data: { items: [{ asset_id: 'uploaded' }] } })
    const ctx = mount(P4)
    await ctx.router.replace('/extract?style_id=42'); await flushPromises()
    expect(ctx.wrapper.get('[data-testid="p4-profile-context"]').text()).toContain('指定来源')
    const file = ctx.wrapper.get('input[type="file"]')
    Object.defineProperty(file.element, 'files', { value: [new File(['qa'], 'qa.png', { type: 'image/png' })] })
    await file.trigger('change'); await flushPromises()
    expect(ctx.router.currentRoute.value.fullPath).toBe('/extract/uploaded?style_id=42')
    ctx.wrapper.unmount()
    const reload = mount(P4)
    await reload.router.replace('/extract/uploaded?style_id=42'); await flushPromises()
    expect(reload.wrapper.get('[data-testid="p4-profile-context"]').text()).toContain('指定来源')
    await reload.router.push('/extract?style_id=42'); await flushPromises()
    expect(reload.wrapper.get('[data-testid="p4-asset-id"]').text()).toBe('未选择训练参考图')
    expect(reload.wrapper.find('[data-testid="p4-stage"]').exists()).toBe(false)
  })

  it('P4 explicit old profile is fetched by owned ID; unavailable profile never defaults to another', async () => {
    const ctx = mount(P4, {
      '/api/v1/styles': envelope({ items: [styles[0]] }), '/api/v1/styles/42': envelope(styles[1]),
      '/api/v1/styles/43': problem(404, 'STYLE_NOT_FOUND', 'Style not found'),
    })
    await ctx.router.replace('/extract?style_id=42'); await flushPromises()
    expect(ctx.wrapper.get('[data-testid="p4-profile-context"]').text()).toContain('指定来源')
    await ctx.router.replace('/extract?style_id=43'); await flushPromises()
    expect(ctx.wrapper.get('[data-testid="p4-profile-context"]').text()).toContain('指定档案不可用')
  })

  it('late upload completion cannot reopen an abandoned P4 route', async () => {
    let finish!: (value: unknown) => void
    vi.spyOn(UploadSession.prototype, 'start').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const ctx = mount(P4)
    await ctx.router.replace('/extract?style_id=42'); await flushPromises()
    const file = ctx.wrapper.get('input[type="file"]')
    Object.defineProperty(file.element, 'files', { value: [new File(['qa'], 'qa.png')] })
    void file.trigger('change'); await flushPromises()
    ctx.wrapper.unmount(); await ctx.router.replace('/styles')
    finish({ data: { items: [{ asset_id: 'uploaded' }] } }); await flushPromises()
    expect(ctx.router.currentRoute.value.path).toBe('/styles')
  })

  it('P7 requested profile takes precedence over a different saved run without writes, generic recovery still works', async () => {
    localStorage.setItem('yy:p7:last-style-run-id', '8')
    const ctx = mount(P7)
    await ctx.router.replace('/training?style_id=42'); await flushPromises()
    expect((ctx.wrapper.get('select[aria-label="风格档案"]').element as HTMLSelectElement).value).toBe('42')
    expect(ctx.wrapper.text()).toContain('已保留其他档案的上次运行')
    expect(ctx.wrapper.text()).toContain('指定来源无快照')
    expect(ctx.logs.filter((call) => call.method !== 'GET')).toHaveLength(0)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('8')
    await ctx.router.push('/training'); await flushPromises()
    expect((ctx.wrapper.get('select[aria-label="风格档案"]').element as HTMLSelectElement).value).toBe('99')
    ctx.router.back(); await flushPromises(); await new Promise(resolve => setTimeout(resolve, 0)); await flushPromises()
    expect((ctx.wrapper.get('select[aria-label="风格档案"]').element as HTMLSelectElement).value).toBe('42')
  })

  it('P7 preserves a different profile pending create and does not allow overwriting it', async () => {
    const pending = JSON.stringify({ key: 'other-profile-request', body: { sample_ids: [], config: { engine: 'm2', style_id: 99 } } })
    localStorage.setItem('yy:p7:pending-style-run-create', pending)
    const ctx = mount(P7)
    await ctx.router.replace('/training?style_id=42'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('另一个档案的创建结果尚未确认')
    expect(ctx.wrapper.findAll('button').find(button => button.text() === '创建新运行')!.attributes('disabled')).toBeDefined()
    expect(localStorage.getItem('yy:p7:pending-style-run-create')).toBe(pending)
    expect(ctx.logs.every((call) => call.method === 'GET')).toBe(true)
  })

  it('P7 ignores a late runtime from a previously selected profile', async () => {
    const ctx = mount(P7); await flushPromises()
    let finish!: (value: Response) => void
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/styles/42/m2-runtime'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await ctx.router.replace('/training?style_id=42'); await flushPromises()
    await ctx.router.replace('/training?style_id=99'); await flushPromises()
    finish(new Response(JSON.stringify(envelope({ ready: false, status: 'BLOCKED', config: null, capabilities: {}, acceptance_policy: { mode: 'OWNER_REQUIRED', human_blind_review: 'REQUIRED', candidate_confirmation: 'OWNER_REQUIRED' }, blockers: [{ code: 'STALE', detail: 'obsolete response' }] }).json)))
    await flushPromises()
    expect((ctx.wrapper.get('select[aria-label="风格档案"]').element as HTMLSelectElement).value).toBe('99')
    expect(ctx.wrapper.text()).not.toContain('obsolete response')
    expect(ctx.wrapper.text()).toContain('其他来源无快照')
  })
})
