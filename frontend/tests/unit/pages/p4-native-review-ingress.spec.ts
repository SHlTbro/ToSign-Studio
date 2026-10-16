import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import NativeReviewIngressPanel from '@/components/NativeReviewIngress.vue'
import NativeIntakePanel from '@/components/NativeIntakePanel.vue'
import { assertReviewIngress } from '@/api/nativeReviewIngress'
import { useAuthStore } from '@/stores/auth'
import type { NativeReviewIngress, NativeReviewJob } from '@/types/nativeIntake'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
import { ASSET, DRAFT, PROFILE, JOB, OTHER_JOB, HASH, IMPORT, review, plan, applied } from './native-intake-fixtures'

const DETAIL = `/api/v1/candidate-review-jobs/${JOB}`
const INGRESS = `${DETAIL}/review-ingress`
const REQUEST = 'a'.repeat(32)
const PENDING = `yy:p4:review-ingress:pending:2:9:${JOB}:${review().package_sha256}`
const wrappers: VueWrapper[] = []
function ingress(status: NativeReviewIngress['status'] = 'not_requested', extra: Partial<NativeReviewIngress> = {}): NativeReviewIngress {
  return { job_id: JOB, package_sha256: review().package_sha256, request: status === 'not_requested' || status === 'blocked' ? null : { request_id: REQUEST, request_sha256: 'e'.repeat(64), nonce: 'f'.repeat(32), created_at: '2026-10-02T09:00:00+00:00' }, status,
    reason: status === 'blocked' ? { code: 'REVIEW_INGRESS_CHANGED', message: 'Current evidence cannot be verified' } : null,
    stage: ['staged', 'imported'].includes(status) ? { bundle_sha256: IMPORT, reviewer: { provider: 'OpenAI', model_id: 'gpt-6-astra', reviewer_kind: 'vision_model' }, source_review_sha256: '6'.repeat(64), reviewed_at: '2026-10-02T09:30:00Z', staged_at: '2026-10-02T09:40:00+00:00' } : null,
    capabilities: { visible_request: true, http_raw_review_import: false, trusted_import: 'operator_staged_hash_pinned_selection', online_provider_status: 'NOT_CONFIGURED', automatic_review_dispatch: false }, canonical_samples_created: false, ...extra }
}
function mount(value = ingress(), overrides: FetchPlan = {}, job = review()) {
  const routes = plan({ [INGRESS]: envelope(value), [DETAIL]: envelope(job), ...overrides })
  const props = reactive<{ job: NativeReviewJob; fresh: boolean; disabled: boolean }>({ job, fresh: true, disabled: false })
  const ctx = mountPage(defineComponent({ setup: () => () => h(NativeReviewIngressPanel, props) }), { plan: routes, asAdmin: job.package.user_id === 1 })
  wrappers.push(ctx.wrapper)
  return { ...ctx, props, routes }
}
function button(wrapper: VueWrapper, label: string) {
  const node = wrapper.findAll('button').find((item) => item.text().includes(label))
  if (!node) throw new Error(`Missing button ${label}`)
  return node
}
const selected = (wrapper: VueWrapper) => wrapper.findComponent(NativeReviewIngressPanel)
const localReviewer = { provider: 'CodexLocal', model_id: 'current-session', reviewer_kind: 'vision_model' }
function localJob(): NativeReviewJob {
  const job = review()
  return { ...job, package: { ...job.package, user_id: 1,
    items: job.package.items.map(item => ({ ...item, draft_alpha_path: 'users/1/styles/drafts/0123456789abcdef.png' })) } }
}
function localIngress(status: 'staged' | 'imported' = 'staged'): NativeReviewIngress {
  const value = ingress(status)
  return { ...value, stage: { ...value.stage!, reviewer: localReviewer } }
}
function mutation(routes: FetchPlan, destination: string, value: NativeReviewIngress, lost = false) {
  const previous = globalThis.fetch
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === destination && init?.method === 'POST') {
      routes[INGRESS] = envelope(value)
      if (lost) throw new TypeError('response lost')
    }
    return previous(input, init)
  })
  vi.stubGlobal('fetch', spy)
  return spy
}
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('trusted ingress response validation', () => {
  it('accepts the actual scoped local reviewer in the owned user1 server record', () => {
    expect(() => assertReviewIngress(localIngress(), localJob())).not.toThrow()
  })
  it('rejects local reviewer evidence for another account', () => {
    expect(() => assertReviewIngress(localIngress(), review())).toThrow('已阻止操作')
  })
  it.each([
    { ...localReviewer, model_id: 'different-session' },
    { ...localReviewer, provider: 'OpenAI' },
    { ...localReviewer, model_id: 'gpt-6-astra' },
    { ...localReviewer, reviewer_kind: 'human' },
  ])('rejects changed local reviewer identity %j', reviewer => {
    const value = localIngress()
    expect(() => assertReviewIngress({ ...value, stage: { ...value.stage!, reviewer } }, localJob())).toThrow('已阻止操作')
  })
  it.each(['not_requested', 'requested', 'staged', 'imported', 'cancelled', 'blocked'] as const)('accepts the bound %s state', (status) => {
    expect(() => assertReviewIngress(ingress(status), review())).not.toThrow()
  })
  it.each([
    ['another job', { job_id: OTHER_JOB }], ['another package', { package_sha256: HASH }],
    ['unrecognized state', { status: 'processing' }], ['implicit acceptance', { canonical_samples_created: true }],
    ['missing request', { request: null }], ['missing stage', { stage: null }],
    ['raw ingress', { capabilities: { ...ingress().capabilities, http_raw_review_import: true } }],
    ['automatic provider', { capabilities: { ...ingress().capabilities, automatic_review_dispatch: true } }],
    ['forged reviewer', { stage: { ...ingress('staged').stage!, reviewer: { provider: 'OpenAI', model_id: 'browser-authored', reviewer_kind: 'human' } } }],
    ['unbound request nonce', { request: { ...ingress('staged').request!, nonce: 'invalid' } }],
    ['untyped date', { stage: { ...ingress('staged').stage!, reviewed_at: '2026-10-02T09:30:00' } }],
  ])('rejects %s authority', (_name, changes) => {
    expect(() => assertReviewIngress(ingress('staged', changes as Partial<NativeReviewIngress>), review())).toThrow('已阻止操作')
  })
})

describe('P4 visible request and server-staged import', () => {
  it('displays the actual CodexLocal identity and imports only opaque server IDs without renaming or applying', async () => {
    const { wrapper, routes, logs } = mount(localIngress(), {}, localJob()); await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="native-ingress-stage"]').text()).toContain('CodexLocal / current-session / vision_model')
    expect(wrapper.text()).not.toContain('gpt-6-astra')
    mutation(routes, `${INGRESS}/import`, localIngress('imported'))
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    expect(logs.find(row => row.method === 'POST')?.body).toEqual({ request_id: REQUEST, bundle_sha256: IMPORT })
    expect(wrapper.get('[data-testid="native-ingress-stage"]').text()).toContain('CodexLocal / current-session / vision_model')
    expect(wrapper.text()).toContain('仍需单独应用')
    expect(logs.filter(row => row.method === 'POST')).toHaveLength(1)
    expect(logs.some(row => row.url.endsWith('/apply'))).toBe(false)
  })
  it('loads by authenticated GET, states provider/operator boundaries, and never offers editable scores or reviewer identity', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    expect(wrapper.text()).toContain('在线视觉服务尚未配置')
    expect(wrapper.text()).toContain('不会自动启动 自动视觉 处理')
    expect(wrapper.find('input, textarea, select').exists()).toBe(false)
    expect(logs).toEqual([{ method: 'GET', url: INGRESS, body: undefined }])
    expect(button(wrapper, '提交本任务').attributes('disabled')).toBeUndefined()
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it('posts only the package digest after fresh owner/job and ingress reads, then shows durable REQUESTED without auto-applying', async () => {
    const { wrapper, routes, logs } = mount(); await flushPromises()
    mutation(routes, INGRESS, ingress('requested'))
    await button(wrapper, '提交本任务').trigger('click'); await flushPromises()
    expect(logs.map((row) => [row.method, row.url])).toEqual([['GET', INGRESS], ['GET', DETAIL], ['GET', INGRESS], ['POST', INGRESS], ['GET', INGRESS]])
    expect(logs.find((row) => row.method === 'POST')?.body).toEqual({ package_sha256: review().package_sha256 })
    expect(wrapper.get('[data-testid="native-ingress-status"]').text()).toContain('等待授权审核者与操作员')
    expect(wrapper.get('[data-testid="native-ingress-request"]').text()).toContain(REQUEST)
    expect(localStorage.getItem(PENDING)).toBeNull()
    expect(logs.some((row) => row.url.endsWith('/apply'))).toBe(false)
    expect(selected(wrapper).emitted('changed')).toHaveLength(1)
  })
  it('imports only the authoritative request ID and staged hash and retains separate Apply', async () => {
    const { wrapper, routes, logs } = mount(ingress('staged')); await flushPromises()
    expect(wrapper.get('[data-testid="native-ingress-stage"]').text()).toContain('OpenAI / gpt-6-astra / vision_model')
    mutation(routes, `${INGRESS}/import`, ingress('imported'))
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    expect(logs.find((row) => row.method === 'POST')?.body).toEqual({ request_id: REQUEST, bundle_sha256: IMPORT })
    expect(wrapper.text()).toContain('仍需单独应用')
    expect(wrapper.text()).toContain('不是人工确认')
    expect(logs.filter((row) => row.method === 'POST')).toHaveLength(1)
    expect(logs.some((row) => row.url.endsWith('/apply'))).toBe(false)
  })
  it('cancels by request ID and truthfully identifies whole-job cancellation', async () => {
    const { wrapper, routes, logs } = mount(ingress('staged')); await flushPromises()
    mutation(routes, `${INGRESS}/cancel`, ingress('cancelled'))
    await button(wrapper, '取消请求及整个').trigger('click'); await flushPromises()
    expect(logs.find((row) => row.method === 'POST')?.body).toEqual({ request_id: REQUEST })
    expect(wrapper.get('[data-testid="native-ingress-status"]').text()).toContain('审核任务已取消')
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it.each(['request', 'import', 'cancel'] as const)('reconciles a lost %s response by GET, never reposting', async (action) => {
    const { wrapper, routes } = mount(ingress(action === 'request' ? 'not_requested' : 'staged')); await flushPromises()
    const destination = action === 'request' ? INGRESS : `${INGRESS}/${action}`
    const spy = mutation(routes, destination, ingress(action === 'request' ? 'requested' : action === 'import' ? 'imported' : 'cancelled'), true)
    await button(wrapper, action === 'request' ? '提交本任务' : action === 'import' ? '导入这份' : '取消请求及整个').trigger('click'); await flushPromises()
    expect(spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(false)
    expect(localStorage.getItem(PENDING)).toBeNull()
    expect(selected(wrapper).emitted('changed')).toHaveLength(1)
  })
  it('blocks duplicate clicks while a POST is pending and persists uncertainty if readback cannot establish an outcome', async () => {
    const { wrapper, routes } = mount(); await flushPromises()
    const previous = globalThis.fetch; let fail!: (e: Error) => void
    const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === INGRESS && init?.method === 'POST'
      ? new Promise<Response>((_resolve, reject) => { fail = reject }) : previous(input, init))
    vi.stubGlobal('fetch', spy)
    await button(wrapper, '提交本任务').trigger('click'); await flushPromises(); await button(wrapper, '提交本任务').trigger('click')
    expect(spy.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toMatchObject({ action: 'request', request_id: null, bundle_sha256: null })
    routes[INGRESS] = problem(503, 'READ_FAILED', 'Cannot establish outcome'); fail(new TypeError('lost')); await flushPromises()
    expect(wrapper.get('[data-testid="native-ingress-uncertain"]').text()).toContain('停止再次提交')
    expect(button(wrapper, '提交本任务').attributes('disabled')).toBeDefined()
  })
  it('recovers a persisted pending operation only from the authenticated server, without POST on reload', async () => {
    localStorage.setItem(PENDING, JSON.stringify({ action: 'import', request_id: REQUEST, bundle_sha256: IMPORT }))
    const { wrapper, logs } = mount(ingress('staged')); await flushPromises()
    expect(wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(true)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    expect(logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it('requires explicit acknowledgement before releasing an unchanged uncertain state and sends no write for acknowledgement', async () => {
    localStorage.setItem(PENDING, JSON.stringify({ action: 'import', request_id: REQUEST, bundle_sha256: IMPORT }))
    const { wrapper, logs } = mount(ingress('staged')); await flushPromises()
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    await button(wrapper, '确认按当前服务端状态继续').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(false)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeUndefined()
    expect(logs.every((row) => row.method === 'GET')).toBe(true)
    expect(wrapper.text()).toContain('没有重发、导入或应用')
  })
  it('keeps uncertain state locked if the server read failed or the selected hash changed', async () => {
    localStorage.setItem(PENDING, JSON.stringify({ action: 'import', request_id: REQUEST, bundle_sha256: IMPORT }))
    const newer = ingress('staged'); newer.stage!.bundle_sha256 = HASH
    const { wrapper, routes } = mount(newer); await flushPromises()
    expect(button(wrapper, '确认按当前服务端状态继续').attributes('disabled')).toBeDefined()
    routes[INGRESS] = problem(503, 'READ_FAILED', 'unavailable')
    await button(wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    expect(button(wrapper, '确认按当前服务端状态继续').attributes('disabled')).toBeDefined()
  })
  it('resolves an uncertain import as terminal if another tab cancelled the whole job', async () => {
    localStorage.setItem(PENDING, JSON.stringify({ action: 'import', request_id: REQUEST, bundle_sha256: IMPORT }))
    const { wrapper, logs } = mount(ingress('cancelled')); await flushPromises()
    expect(wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(false)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    expect(logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it('does not retry a rejected POST and displays the server rejection', async () => {
    const { wrapper, routes, logs } = mount(); await flushPromises()
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === INGRESS && init?.method === 'POST') routes[INGRESS] = problem(409, 'REVIEW_INGRESS_BUSY', 'refresh first')
      return previous(input, init)
    })
    await button(wrapper, '提交本任务').trigger('click'); await flushPromises()
    expect(logs.filter((row) => row.method === 'POST')).toHaveLength(1)
    expect(wrapper.text()).toContain('审核导入正在处理，请等待完成后刷新。')
    expect(localStorage.getItem(PENDING)).toBeNull()
  })
  it.each(['job', 'package', 'code', 'import'])('stops if fresh %s authority differs from the selected job', async (kind) => {
    const { wrapper, routes, logs } = mount(ingress('staged')); await flushPromises()
    routes[DETAIL] = envelope(review(kind === 'job' ? OTHER_JOB : JOB, { package_sha256: kind === 'package' ? HASH : review().package_sha256, code_version_matches: kind !== 'code', imported_bundle_sha256: kind === 'import' ? IMPORT : null }))
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    expect(logs.some((row) => row.method === 'POST')).toBe(false)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it.each(['hash', 'request', 'blocked', 'cancelled'])('stops for %s changed by another tab before import', async (change) => {
    const { wrapper, routes, logs } = mount(ingress('staged')); await flushPromises()
    const newer = change === 'blocked' ? ingress('blocked') : change === 'cancelled' ? ingress('cancelled') : ingress('staged')
    if (change === 'hash') newer.stage!.bundle_sha256 = HASH
    if (change === 'request') newer.request!.request_id = 'b'.repeat(32)
    routes[INGRESS] = envelope(newer)
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    expect(logs.some((row) => row.method === 'POST')).toBe(false)
    expect(wrapper.text()).toContain('暂存证据已变化')
  })
  it('invalidates cached state after cross-tab activity and requires an explicit refresh', async () => {
    const { wrapper } = mount(ingress('staged')); await flushPromises()
    window.dispatchEvent(new StorageEvent('storage', { key: PENDING }))
    await flushPromises()
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('其他标签页')
    await button(wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeUndefined()
  })
  it('fails closed on refresh failure without presenting old stage as current', async () => {
    const { wrapper, routes } = mount(ingress('staged')); await flushPromises()
    routes[INGRESS] = problem(503, 'READ_FAILED', 'unavailable')
    await button(wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-testid="native-ingress-status"]').text()).toContain('尚未重新核验')
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('在线视觉服务尚未配置')
  })
  it('does not accept a late status result after owner/job scope changes', async () => {
    const { wrapper, props } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (r: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === INGRESS
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    props.job = review(OTHER_JOB)
    await flushPromises()
    finish({ ok: true, status: 200, json: async () => envelope(ingress('staged')).json } as Response)
    await flushPromises()
    expect(wrapper.find('[data-testid="native-ingress-stage"]').exists()).toBe(false)
  })
  it('discards late mutation completion after account switch and never attaches its evidence to the new owner', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (r: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === INGRESS && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '提交本任务').trigger('click'); await flushPromises()
    useAuthStore().__setUserForTest({ user_id: '3', username: 'other', display_name: 'Other', role: 'user', must_change_password: false }); await flushPromises()
    finish({ ok: true, status: 200, json: async () => envelope(ingress('requested')).json } as Response); await flushPromises()
    expect(wrapper.find('[data-testid="native-ingress-request"]').exists()).toBe(false)
    expect(logs.some((row) => row.url.endsWith('/apply'))).toBe(false)
  })
  it('does not transmit if browser recovery persistence fails', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('no space') })
    await button(wrapper, '提交本任务').trigger('click'); await flushPromises()
    expect(logs.some((row) => row.method === 'POST')).toBe(false)
    expect(wrapper.text()).toContain('无法保存操作恢复记录')
  })
  it('disables mutations on stale parent state, source activity, or old code', async () => {
    const { wrapper, props } = mount(ingress('staged')); await flushPromises()
    props.fresh = false; await flushPromises(); expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    props.fresh = true; props.disabled = true; await flushPromises(); expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
    props.disabled = false; props.job.code_version_matches = false; await flushPromises(); expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it('shows blocked diagnostic without exposing historical stage or enabling import', async () => {
    const { wrapper } = mount(ingress('blocked')); await flushPromises()
    expect(wrapper.get('[data-testid="native-ingress-reason"]').text()).toContain('审核导入记录已变化')
    expect(wrapper.find('[data-testid="native-ingress-stage"]').exists()).toBe(false)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it('blocks parent Apply while cancellation is unresolved, including after reload', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    localStorage.setItem(PENDING, JSON.stringify({ action: 'cancel', request_id: REQUEST, bundle_sha256: IMPORT }))
    const routes = plan({ [INGRESS]: envelope(ingress('imported')), [DETAIL]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(ctx.wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(true)
    expect(ctx.logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it('resolves a prior uncertain cancellation against authoritative terminal Apply without claiming cancellation succeeded', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    localStorage.setItem(PENDING, JSON.stringify({ action: 'cancel', request_id: REQUEST, bundle_sha256: IMPORT }))
    const routes = plan({ [INGRESS]: envelope(ingress('imported')), [DETAIL]: envelope(applied()) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(ctx.wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(false)
    expect(localStorage.getItem(PENDING)).toBeNull()
    expect(ctx.wrapper.text()).toContain('先前取消未生效')
    expect(ctx.wrapper.text()).not.toContain('审核任务已取消')
    expect(button(ctx.wrapper, '准备新的审核任务').attributes('disabled')).toBeUndefined()
    expect(button(ctx.wrapper, '打开/刷新所选任务').attributes('disabled')).toBeUndefined()
    expect(button(ctx.wrapper, '刷新服务端任务列表').attributes('disabled')).toBeUndefined()
    expect(ctx.logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it.each([true, false])('never overwrites a cross-tab cancel observed during import preflight (storage event: %s)', async (sendEvent) => {
    const { wrapper } = mount(ingress('staged')); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    const posts: RequestInit[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') posts.push(init)
      if (String(input) === DETAIL && (!init?.method || init.method === 'GET')) return new Promise<Response>((resolve) => { finish = resolve })
      return previous(input, init)
    })
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    const marker = { action: 'cancel', request_id: REQUEST, bundle_sha256: IMPORT }
    localStorage.setItem(PENDING, JSON.stringify(marker))
    if (sendEvent) window.dispatchEvent(new StorageEvent('storage', { key: PENDING }))
    await flushPromises()
    finish({ ok: true, status: 200, json: async () => envelope(review()).json } as Response); await flushPromises()
    expect(posts).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(marker)
    expect(wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(true)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it('invalidates an import even if a newly observed cross-tab marker is cleared before preflight returns', async () => {
    const { wrapper } = mount(ingress('staged')); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    const posts: RequestInit[] = []
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') posts.push(init)
      if (String(input) === DETAIL) return new Promise<Response>((resolve) => { finish = resolve })
      return previous(input, init)
    })
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    localStorage.setItem(PENDING, JSON.stringify({ action: 'cancel', request_id: REQUEST, bundle_sha256: IMPORT }))
    window.dispatchEvent(new StorageEvent('storage', { key: PENDING }))
    localStorage.removeItem(PENDING); window.dispatchEvent(new StorageEvent('storage', { key: PENDING }))
    finish({ ok: true, status: 200, json: async () => envelope(review()).json } as Response); await flushPromises()
    expect(posts).toHaveLength(0)
    expect(button(wrapper, '导入这份').attributes('disabled')).toBeDefined()
  })
  it.each([409, 200])('does not erase a newer cross-tab cancel marker after a late HTTP %s from this tab', async (status) => {
    const { wrapper, routes } = mount(ingress('staged')); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === `${INGRESS}/import` && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    vi.stubGlobal('fetch', spy)
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    const marker = { action: 'cancel', request_id: REQUEST, bundle_sha256: IMPORT, operation_id: 'another-tab-operation' }
    localStorage.setItem(PENDING, JSON.stringify(marker)); window.dispatchEvent(new StorageEvent('storage', { key: PENDING }))
    routes[INGRESS] = envelope(ingress('imported'))
    const count = spy.mock.calls.length
    finish({ ok: status === 200, status, json: async () => status === 200 ? envelope(ingress('imported')).json : problem(409, 'REVIEW_INGRESS_CHANGED', 'late rejection').json } as Response)
    await flushPromises()
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(marker)
    expect(wrapper.get('[data-testid="native-ingress-uncertain"]').text()).toContain('取消审核')
    expect(spy.mock.calls.length).toBe(count)
  })
  it('protects a newer same-action marker even if its storage event has not arrived', async () => {
    const { wrapper } = mount(ingress('staged')); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === `${INGRESS}/import` && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '导入这份').trigger('click'); await flushPromises()
    const own = JSON.parse(localStorage.getItem(PENDING)!)
    expect(own.operation_id).toBeTruthy()
    const newer = { ...own, operation_id: 'newer-import-selection' }
    localStorage.setItem(PENDING, JSON.stringify(newer))
    finish({ ok: false, status: 409, json: async () => problem(409, 'REVIEW_INGRESS_CHANGED', 'late rejection').json } as Response)
    await flushPromises()
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(newer)
  })
  it('blocks parent Apply when ingress is blocked despite a historical imported digest', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const routes = plan({ [INGRESS]: envelope(ingress('blocked')), [DETAIL]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    await button(ctx.wrapper, '应用服务端').trigger('click'); await flushPromises()
    expect(ctx.logs.some((row) => row.method === 'POST')).toBe(false)
    expect(button(ctx.wrapper, '取消此审核任务').attributes('disabled')).toBeUndefined()
  })
  it.each(['requested', 'staged', 'cancelled'] as const)('blocks stale parent imported authority when ingress has moved to %s', async (status) => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const routes = plan({ [INGRESS]: envelope(ingress(status)), [DETAIL]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    await button(ctx.wrapper, '应用服务端').trigger('click'); await flushPromises()
    expect(ctx.logs.some((row) => row.method === 'POST')).toBe(false)
  })
  it('requires the imported ingress bundle to match the parent job digest before Apply', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const routes = plan({ [INGRESS]: envelope(ingress('imported')), [DETAIL]: envelope(review(JOB, { imported_bundle_sha256: HASH })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(ctx.logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it('requires fresh ingress verification before parent Apply, including after a failed read', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const routes = plan({ [INGRESS]: envelope(ingress('imported')), [DETAIL]: envelope(review(JOB, { imported_bundle_sha256: IMPORT })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
    routes[INGRESS] = problem(503, 'READ_FAILED', 'cannot verify')
    await button(ctx.wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    routes[INGRESS] = envelope(ingress('imported'))
    await button(ctx.wrapper, '刷新审核请求').trigger('click'); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
  })
  it('allows read-only leaving, listing and switching a blocked uncertain job while retaining its lock on return', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const marker = { action: 'import', request_id: REQUEST, bundle_sha256: IMPORT, operation_id: 'persisted-old-job-import' }
    localStorage.setItem(PENDING, JSON.stringify(marker))
    const list = '/api/v1/candidate-review-jobs?style_id=9&limit=50'
    const routes = plan({ [INGRESS]: envelope(ingress('blocked')), [DETAIL]: envelope(review(JOB, { code_version_matches: false })),
      [list]: envelope({ items: [{ job_id: JOB, style_id: 9, status: 'pending', code_version_matches: false }, { job_id: OTHER_JOB, style_id: 9, status: 'pending', code_version_matches: true }], next_after_job_id: null }) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(ctx.wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(true)
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(button(ctx.wrapper, '取消此审核任务').attributes('disabled')).toBeDefined()
    expect(button(ctx.wrapper, '离开当前审核任务').attributes('disabled')).toBeUndefined()
    expect(button(ctx.wrapper, '刷新服务端任务列表').attributes('disabled')).toBeUndefined()
    expect(button(ctx.wrapper, '打开/刷新所选任务').attributes('disabled')).toBeUndefined()
    await button(ctx.wrapper, '刷新服务端任务列表').trigger('click'); await flushPromises()
    await button(ctx.wrapper, '离开当前审核任务').trigger('click'); await flushPromises()
    expect(ctx.wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(marker)
    await ctx.wrapper.get('[aria-label="服务端审核任务"]').setValue(OTHER_JOB)
    await button(ctx.wrapper, '打开/刷新所选任务').trigger('click'); await flushPromises()
    expect(ctx.wrapper.get('[data-testid="native-current-job"]').text()).toContain(OTHER_JOB)
    expect(ctx.wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(false)
    await ctx.wrapper.get('[aria-label="服务端审核任务"]').setValue(JOB)
    await button(ctx.wrapper, '打开/刷新所选任务').trigger('click'); await flushPromises()
    expect(ctx.wrapper.find('[data-testid="native-ingress-uncertain"]').exists()).toBe(true)
    expect(ctx.wrapper.get('[data-testid="native-ingress-status"]').text()).toContain('当前请求已阻止')
    expect(button(ctx.wrapper, '导入这份').attributes('disabled')).toBeDefined()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(button(ctx.wrapper, '确认按当前服务端状态继续').attributes('disabled')).toBeDefined()
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(marker)
    expect(ctx.logs.every((row) => row.method === 'GET')).toBe(true)
    // The occurrence inventory also reads job details; at least the initial and returning owner reads must occur.
    expect(ctx.logs.filter((row) => row.url === DETAIL).length).toBeGreaterThanOrEqual(2)
    expect(ctx.logs.filter((row) => row.url === INGRESS)).toHaveLength(2)
  })
  it('invalidates an outstanding selected-job read on explicit leave without deleting recovery state', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const marker = { action: 'import', request_id: REQUEST, bundle_sha256: IMPORT }
    localStorage.setItem(PENDING, JSON.stringify(marker))
    const routes = plan({ [INGRESS]: envelope(ingress('blocked')), [DETAIL]: envelope(review(JOB, { code_version_matches: false })) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === DETAIL
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(ctx.wrapper, '打开/刷新所选任务').trigger('click'); await flushPromises()
    expect(button(ctx.wrapper, '离开当前审核任务').attributes('disabled')).toBeUndefined()
    await button(ctx.wrapper, '离开当前审核任务').trigger('click'); await flushPromises()
    finish({ ok: true, status: 200, json: async () => envelope(review(JOB, { code_version_matches: false })).json } as Response)
    await flushPromises()
    expect(ctx.wrapper.find('[data-testid="native-current-job"]').exists()).toBe(false)
    expect(JSON.parse(localStorage.getItem(PENDING)!)).toEqual(marker)
    expect(ctx.logs.every((row) => row.method === 'GET')).toBe(true)
  })
  it('refreshes parent imported authority after ingress import, while requiring a separate Apply click', async () => {
    localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB))
    const routes = plan({ [INGRESS]: envelope(ingress('staged')) })
    const ctx = mountPage(defineComponent({ setup: () => () => h(NativeIntakePanel, { job: undefined, asset: ASSET, profile: PROFILE, drafts: [DRAFT] }) }), { plan: routes })
    wrappers.push(ctx.wrapper); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === `${INGRESS}/import` && init?.method === 'POST') {
        routes[INGRESS] = envelope(ingress('imported')); routes[DETAIL] = envelope(review(JOB, { imported_bundle_sha256: IMPORT }))
      }
      return previous(input, init)
    })
    await button(ctx.wrapper, '导入这份').trigger('click'); await flushPromises()
    expect(button(ctx.wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
    expect(ctx.logs.some((row) => row.url.endsWith('/apply'))).toBe(false)
    expect(ctx.wrapper.get('[data-testid="native-import-state"]').text()).toContain('已导入，仍需单独应用')
  })
})
