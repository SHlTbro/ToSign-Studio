import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import NativeIntakePanel from '@/components/NativeIntakePanel.vue'
import P4 from '@/pages/P4_Extract.vue'
import { UploadSession } from '@/api/xhrUpload'
import { useAuthStore } from '@/stores/auth'
import { assertJob } from '@/api/nativeIntake'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
import { ASSET, DRAFT, PROFILE, JOB, OTHER_JOB, HASH, IMPORT, review, applied, result, plan } from './native-intake-fixtures'
import type { IntakeAsset, IntakeStyle, NativeDraftInput } from '@/types/nativeIntake'
const wrappers: VueWrapper[] = []
function mount(routes: FetchPlan = plan()) {
  const state = reactive<{ asset: IntakeAsset | null; profile: IntakeStyle | null; drafts: NativeDraftInput[]; sourceBusy: boolean }>({ asset: { ...ASSET }, profile: { ...PROFILE }, drafts: [{ ...DRAFT }], sourceBusy: false })
  const component = defineComponent({ setup: () => () => h(NativeIntakePanel, state) })
  const ctx = mountPage(component, { plan: routes }); wrappers.push(ctx.wrapper); return { ...ctx, state }
}
function button(wrapper: VueWrapper, label: string) { const found = wrapper.findAll('button').find((node) => node.text().includes(label)); if (!found) throw new Error(`Missing button ${label}`); return found }
function response(data: unknown): Response { return { ok: true, status: 200, json: async () => envelope(data).json } as Response }
function pointer(id = JOB) { localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(id)) }
async function selectDraft(wrapper: VueWrapper) { await wrapper.get(`input[aria-label="选择原生提取稿 1"]`).setValue(true) }
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })
describe('P4 native source and trusted-review workflow', () => {
  it('requires explicit source scope/reason, submits server hash and retains UNKNOWN/AI identity', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    expect(button(wrapper, '登记来源准入').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="来源准入理由"]').setValue('当前上传原图，仅用于此个人手写档案')
    await wrapper.get('[aria-label="确认来源范围与未知历史"]').setValue(true)
    await button(wrapper, '登记来源准入').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST')?.body).toEqual({ style_id: 9, source_sha256: HASH, origin: 'user_supplied_attachment', history_status: 'UNKNOWN', allow_unknown_history: true, reason: '当前上传原图，仅用于此个人手写档案', actor_kind: 'delegated_ai' })
    expect(wrapper.get('[data-testid="native-admission"]').text()).toContain('使用历史未知')
    expect(wrapper.get('[data-testid="native-admission"]').text()).toContain('未批准字形')
  })
  it('creates the exact source-box draft job without labels/scores and shows missing trusted import honestly', async () => {
    const { wrapper, logs } = mount(); await flushPromises(); await selectDraft(wrapper)
    await button(wrapper, '创建当前像素').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST')?.body).toEqual({ style_id: 9, items: [DRAFT] })
    expect(wrapper.get('[data-testid="native-import-state"]').text()).toContain('未导入')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('不会把任意文件当作可信审核')
  })
  it('prevents double create and persists uncertain creation without automatic replay on reload', async () => {
    const { wrapper } = mount(); await flushPromises(); await selectDraft(wrapper)
    const previous = globalThis.fetch; let fail!: (error: Error) => void
    const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/candidate-review-jobs' && init?.method === 'POST' ? new Promise<Response>((_, reject) => { fail = reject }) : previous(input, init))
    vi.stubGlobal('fetch', spy)
    const create = button(wrapper, '创建当前像素'); await create.trigger('click'); await create.trigger('click')
    expect(spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    fail(new TypeError('network')); await flushPromises(); wrapper.unmount(); wrappers.splice(wrappers.indexOf(wrapper), 1)
    const reload = mount(); await flushPromises(); await selectDraft(reload.wrapper)
    expect(reload.wrapper.get('[data-testid="native-uncertain-create"]').text()).toContain('已停止再次创建')
    expect(button(reload.wrapper, '创建当前像素').attributes('disabled')).toBeDefined()
    expect(reload.logs.every((entry) => entry.method === 'GET')).toBe(true)
  })
  it('uses paged server recovery and clears uncertain creation only for exact owned native items', async () => {
    localStorage.setItem(`yy:p4:native:uncertain-create:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify({ style_id: 9, items: [DRAFT] }))
    const rows = [{ job_id: JOB, style_id: 9, status: 'pending', code_version_matches: true }]
    const { wrapper, logs } = mount(plan({ '/api/v1/candidate-review-jobs?style_id=9&limit=50': envelope({ items: rows, next_after_job_id: JOB }), [`/api/v1/candidate-review-jobs?style_id=9&limit=50&after_job_id=${JOB}`]: envelope({ items: [{ ...rows[0], job_id: OTHER_JOB }], next_after_job_id: null }) })); await flushPromises()
    await button(wrapper, '加载更多任务').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('已读取 2 条')
    await wrapper.get('[aria-label="服务端审核任务"]').setValue(JOB)
    await button(wrapper, '打开/刷新').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="native-uncertain-create"]').exists()).toBe(true)
    await button(wrapper, '确认用此匹配任务继续').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="native-uncertain-create"]').exists()).toBe(false)
    expect(logs.some((entry) => entry.url.includes(`after_job_id=${JOB}`))).toBe(true)
  })
  it('requires all recovery pages and explicit adoption before clearing an uncertain create', async () => {
    localStorage.setItem(`yy:p4:native:uncertain-create:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify({ style_id: 9, items: [DRAFT] }))
    const { wrapper } = mount(plan({ '/api/v1/candidate-review-jobs?style_id=9&limit=50': envelope({ items: [{ job_id: JOB, style_id: 9, status: 'pending', code_version_matches: true }], next_after_job_id: JOB }) })); await flushPromises()
    await wrapper.get('[aria-label="服务端审核任务"]').setValue(JOB); await button(wrapper, '打开/刷新').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="native-uncertain-create"]').exists()).toBe(true)
    expect(button(wrapper, '确认用此匹配任务继续').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('相同稿件可能有多个历史任务')
  })

  it('does not reuse pre-create completed pages or a historical job read to resolve a later uncertain request', async () => {
    pointer(); const { wrapper } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(applied()), '/api/v1/candidate-review-jobs?style_id=9&limit=50': envelope({ items: [{ job_id: JOB, style_id: 9, status: 'applied', code_version_matches: true }], next_after_job_id: null }) })); await flushPromises(); await selectDraft(wrapper)
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/candidate-review-jobs' && init?.method === 'POST' ? Promise.reject(new TypeError('lost create')) : previous(input, init))
    await button(wrapper, '创建当前像素').trigger('click'); await flushPromises()
    expect(button(wrapper, '确认用此匹配任务继续').attributes('disabled')).toBeDefined()
    await button(wrapper, '打开/刷新').trigger('click'); await flushPromises()
    expect(button(wrapper, '确认用此匹配任务继续').attributes('disabled')).toBeDefined()
    await button(wrapper, '刷新服务端任务列表').trigger('click'); await flushPromises()
    expect(button(wrapper, '确认用此匹配任务继续').attributes('disabled')).toBeUndefined()
  })

  it('restores the pointer via authenticated GET, requires a fresh code-matched imported hash, and applies only that hash', async () => {
    pointer(); const routes = plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const { wrapper, logs } = mount(routes); await flushPromises()
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith(`/${JOB}/apply`) && init?.method === 'POST') { routes[`/api/v1/candidate-review-jobs/${JOB}`] = envelope(applied()); return previous(input, init) }
      return previous(input, init)
    })
    await button(wrapper, '应用服务端').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.url.endsWith('/apply'))?.body).toEqual({ bundle_sha256: IMPORT })
    expect(wrapper.get('[data-testid="native-decision"]').text()).toContain('自动视觉授权确认')
    expect(wrapper.get('[data-testid="native-decision"]').text()).toContain('不是人工确认')
    expect(wrapper.text()).toContain('未训练、未发布模型')
  })
  it('never sends apply after the server import digest changes during preflight', async () => {
    pointer(); const routes = plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const { wrapper, logs } = mount(routes); await flushPromises()
    routes[`/api/v1/candidate-review-jobs/${JOB}`] = envelope(review(JOB, { imported_bundle_sha256: '9'.repeat(64) }))
    await button(wrapper, '应用服务端').trigger('click'); await flushPromises()
    expect(logs.some((entry) => entry.url.endsWith('/apply'))).toBe(false)
    expect(wrapper.text()).toContain('证据已变化')
  })
  it('disables Apply after an authoritative job refresh fails', async () => {
    pointer(); const routes = plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const { wrapper } = mount(routes); await flushPromises()
    routes[`/api/v1/candidate-review-jobs/${JOB}`] = problem(503, 'REVIEW_READ_FAILED', 'Read unavailable')
    await button(wrapper, '打开/刷新').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('审核记录读取失败，请刷新核对。')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
  })
  it('reconciles a lost Apply response through GET instead of resubmitting the bundle', async () => {
    pointer(); const routes = plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const { wrapper } = mount(routes); await flushPromises()
    const previous = globalThis.fetch; let posts = 0
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/apply') && init?.method === 'POST') { posts++; routes[`/api/v1/candidate-review-jobs/${JOB}`] = envelope(applied()); return Promise.reject(new TypeError('response lost')) }
      return previous(input, init)
    })
    await button(wrapper, '应用服务端').trigger('click'); await flushPromises()
    expect(posts).toBe(1)
    expect(wrapper.text()).toContain('接受 1，隔离 0')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
  })
  it('does not attach a late Apply completion to a different source context', async () => {
    pointer(); const { wrapper, state } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/apply') && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '应用服务端').trigger('click'); await flushPromises()
    state.asset = { ...ASSET, asset_id: 'next-source' }; await flushPromises()
    finish(response(result())); await flushPromises()
    expect(wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('接受 1，隔离 0')
  })
  it('does not treat a failed/partial list as proof an uncertain creation is absent', async () => {
    localStorage.setItem(`yy:p4:native:uncertain-create:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify({ style_id: 9, items: [DRAFT] }))
    const { wrapper } = mount(plan({ '/api/v1/candidate-review-jobs?style_id=9&limit=50': problem(503, 'LIST_UNAVAILABLE', 'try later') })); await flushPromises(); await selectDraft(wrapper)
    expect(wrapper.text()).toContain('任务列表读取失败，请刷新核对已有任务。')
    expect(wrapper.find('[data-testid="native-uncertain-create"]').exists()).toBe(true)
    expect(button(wrapper, '创建当前像素').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('当前列表已读完')
  })

  it('shows rejected/uncertain decisions as isolated without canonical samples or human confirmation', async () => {
    pointer(); const isolated = result(); isolated.accepted = 0; isolated.isolated = 1; isolated.items[0] = { ...isolated.items[0]!, canonical_sample_id: null, review_status: 'candidate', recommendation: 'AI_NEEDS_HUMAN', reason: 'Uncertain crop', isolation_reasons: ['CROP_UNCERTAIN'], reviewed_label: null }
    const { wrapper } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { status: 'applied', imported_bundle_sha256: IMPORT, result: isolated })) })); await flushPromises()
    expect(wrapper.get('[data-testid="native-decision"]').text()).toContain('隔离，不进入训练样本')
    expect(wrapper.text()).toContain('接受 0，隔离 1')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
  })
  it.each([['profile', { ...PROFILE, status: 'draft' }], ['code', null]])('keeps %s prerequisites blocked rather than inventing readiness', async (kind, value) => {
    pointer(); const { wrapper, state } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT, code_version_matches: kind === 'code' ? false : true })) }))
    if (kind === 'profile') state.profile = value as IntakeStyle
    await flushPromises(); expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
  })
  it('rejects cross-user/cross-style packages and does not use cached import authority', async () => {
    pointer(); const wrong = review(); wrong.package.user_id = 99; wrong.imported_bundle_sha256 = IMPORT
    const { wrapper } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(wrong) })); await flushPromises()
    expect(wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('身份、来源或版本不匹配')
    expect(() => assertJob(review(), '2', 10, JOB)).toThrow()
  })
  it('ignores a late old-source task read after source/profile reset', async () => {
    const { wrapper, state } = mount(plan({ '/api/v1/candidate-review-jobs?style_id=9&limit=50': envelope({ items: [{ job_id: JOB, style_id: 9, status: 'pending', code_version_matches: true }], next_after_job_id: null }) })); await flushPromises()
    const previous = globalThis.fetch; let finish!: (r: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === `/api/v1/candidate-review-jobs/${JOB}` ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await wrapper.get('[aria-label="服务端审核任务"]').setValue(JOB); await button(wrapper, '打开/刷新').trigger('click')
    state.asset = { ...ASSET, asset_id: 'other-source', sha256: '8'.repeat(64) }; state.profile = { ...PROFILE, style_id: 10 }; await flushPromises()
    finish(response(review())); await flushPromises()
    expect(wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
  })
  it('does not leak a previous owner pointer after auth changes', async () => {
    pointer(); const { wrapper } = mount(); await flushPromises()
    const auth = useAuthStore(); auth.__setUserForTest({ user_id: '3', username: 'other', display_name: 'Other', role: 'user', must_change_password: false }); await flushPromises()
    expect(wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
  })
  it('requires real image reload after a failed proof image, not just a successful metadata refresh', async () => {
    pointer(); const { wrapper } = mount(plan({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })); await flushPromises()
    const before = wrapper.get('[data-testid="native-current-job"] img')
    await before.trigger('error')
    await button(wrapper, '打开/刷新').trigger('click'); await flushPromises()
    const images = wrapper.findAll('[data-testid="native-current-job"] img')
    expect(images[0]!.element).not.toBe(before.element)
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    for (const image of images) await image.trigger('load')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
  })
  it('clears old-source drafts on uploaded replacement and binds every native draft to its original source', async () => {
    const routes = plan({ '/api/v1/styles': envelope({ items: [PROFILE] }), '/api/v1/styles/9/samples': envelope({ items: [] }), '/api/v1/assets/source-a': envelope(ASSET), '/api/v1/assets/source-b': envelope({ ...ASSET, asset_id: 'source-b', sha256: '8'.repeat(64) }), '/api/v1/detect-text': envelope({ items: [{ box_norm: DRAFT.source_bbox_norm, score: .9 }] }), '/api/v1/extract': envelope({ stroke_path: DRAFT.draft_alpha_path, needs_touchup: true }) })
    const ctx = mountPage(P4, { plan: routes }); wrappers.push(ctx.wrapper)
    await ctx.router.replace('/extract/source-a?style_id=9'); await flushPromises()
    const stage = ctx.wrapper.get('[data-testid="p4-stage"]')
    stage.element.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 300 }) as DOMRect
    await stage.trigger('pointerdown', { clientX: 100, clientY: 100 }); await stage.trigger('pointerup')
    await ctx.wrapper.get('[data-testid="p4-extract"]').trigger('click'); await flushPromises()
    expect(ctx.wrapper.findComponent(NativeIntakePanel).props('drafts')).toEqual([DRAFT])
    vi.spyOn(UploadSession.prototype, 'start').mockResolvedValue({ data: { items: [{ asset_id: 'source-b' }] } })
    const file = ctx.wrapper.get('input[type="file"]')
    Object.defineProperty(file.element, 'files', { value: [new File(['test'], 'next.png', { type: 'image/png' })], configurable: true })
    await file.trigger('change'); await flushPromises()
    expect(ctx.wrapper.findComponent(NativeIntakePanel).props('asset')?.asset_id).toBe('source-b')
    expect(ctx.wrapper.findComponent(NativeIntakePanel).props('drafts')).toEqual([])
  })
  it('preserves the explicitly chosen native profile when uploading a replacement', async () => {
    const second = { ...PROFILE, style_id: 10, name: 'Second native profile' }
    const ctx = mountPage(P4, { plan: plan({ '/api/v1/styles': envelope({ items: [PROFILE, second] }), '/api/v1/styles/9/samples': envelope({ items: [] }), '/api/v1/styles/10/samples': envelope({ items: [] }), '/api/v1/assets/source-a': envelope(ASSET), '/api/v1/assets/source-b': envelope({ ...ASSET, asset_id: 'source-b', sha256: '8'.repeat(64) }), '/api/v1/detect-text': envelope({ items: [] }), '/api/v1/styles/10/occurrence-selection': envelope({ native_intake_sha256: null, versions: [], selection_required: false }), '/api/v1/candidate-review-jobs?style_id=10&limit=50': envelope({ items: [], next_after_job_id: null }) }) }); wrappers.push(ctx.wrapper)
    await ctx.router.replace('/extract/source-a'); await flushPromises()
    await ctx.wrapper.get('[aria-label="原生审核目标档案"]').setValue('10'); await flushPromises()
    vi.spyOn(UploadSession.prototype, 'start').mockResolvedValue({ data: { items: [{ asset_id: 'source-b' }] } })
    const file = ctx.wrapper.get('input[type="file"]'); Object.defineProperty(file.element, 'files', { value: [new File(['test'], 'next.png')], configurable: true })
    await file.trigger('change'); await flushPromises()
    expect(ctx.router.currentRoute.value.query.style_id).toBe('10')
    expect(ctx.wrapper.findComponent(NativeIntakePanel).props('profile')?.style_id).toBe(10)
  })

  it('renders the actual P4 native affordance from source read and existing explicit profile', async () => {
    const ctx = mountPage(P4, { plan: plan({ '/api/v1/styles': envelope({ items: [PROFILE] }), '/api/v1/styles/9/samples': envelope({ items: [] }), '/api/v1/assets/source-a': envelope(ASSET), '/api/v1/detect-text': envelope({ items: [] }) }) }); wrappers.push(ctx.wrapper)
    await ctx.router.replace('/extract/source-a?style_id=9'); await flushPromises()
    expect(ctx.wrapper.get('[data-testid="native-intake-panel"]').text()).toContain('自己的手写')
    expect(ctx.wrapper.get('[data-testid="native-intake-panel"]').text()).toContain(HASH)
    expect(ctx.wrapper.get('[aria-label="原生审核目标档案"]').attributes('disabled')).toBeDefined()
  })
})
