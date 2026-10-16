import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import type { Router } from 'vue-router'
import NativeIntakePanel from '@/components/NativeIntakePanel.vue'
import { assertJob, exportNativeReviewRequest } from '@/api/nativeIntake'
import { __resetHttpGatesForTest, request, setHttpRouter } from '@/api/http'
import type { IntakeAsset, IntakeStyle, NativeDraftInput, NativeReviewCapabilities, NativeReviewJob } from '@/types/nativeIntake'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
import { ASSET, DRAFT, PROFILE, JOB, OTHER_JOB, HASH, IMPORT, review, plan } from './native-intake-fixtures'

const CAPABILITIES: NativeReviewCapabilities = { request_export: true, http_raw_review_import: false, trusted_import: 'operator_authenticated_hash_pinned_cli', online_provider_status: 'NOT_CONFIGURED', export_is_review_acceptance: false }
const ZIP = new Uint8Array([80, 75, 5, 6, ...Array<number>(18).fill(0)])
const ZIP_HASH = createHash('sha256').update(ZIP).digest('hex')
const DETAIL = `/api/v1/candidate-review-jobs/${JOB}`
const EXPORT = `${DETAIL}/export`
const wrappers: VueWrapper[] = []
const capableJob = (overrides: Partial<NativeReviewJob> = {}) => review(JOB, { capabilities: { ...CAPABILITIES }, ...overrides })
function archive(job = capableJob(), headers: Record<string, string> = {}, bytes = ZIP): Response {
  return new Response(bytes, { status: 200, headers: { 'Content-Type': 'application/zip', 'X-Review-Job-Id': job.job_id,
    'X-Review-Package-SHA256': job.package_sha256, 'X-Review-Code-Version-Matches': String(job.code_version_matches),
    'X-Review-Archive-SHA256': ZIP_HASH, 'Content-Disposition': `attachment; filename="candidate-review-${job.job_id}.zip"`, ...headers } })
}
function pointer() { localStorage.setItem(`yy:p4:native:job:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify(JOB)) }
function mount(routes: FetchPlan = plan({ [DETAIL]: envelope(capableJob()) })) {
  const state = reactive<{ asset: IntakeAsset | null; profile: IntakeStyle | null; drafts: NativeDraftInput[]; sourceBusy: boolean }>({ asset: { ...ASSET }, profile: { ...PROFILE }, drafts: [{ ...DRAFT }], sourceBusy: false })
  const component = defineComponent({ setup: () => () => h(NativeIntakePanel, state) })
  const ctx = mountPage(component, { plan: routes }); wrappers.push(ctx.wrapper); return { ...ctx, state }
}
function button(wrapper: VueWrapper, label: string) {
  const found = wrapper.findAll('button').find((node) => node.text().includes(label))
  if (!found) throw new Error(`Missing button ${label}`)
  return found
}
function interceptExport(reply: () => Promise<Response>) {
  const previous = globalThis.fetch
  const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === EXPORT ? reply() : previous(input, init))
  vi.stubGlobal('fetch', spy)
  return spy
}
function authRouter() {
  const router = { currentRoute: { value: { fullPath: '/extract/source-a?style_id=9' } }, replace: vi.fn(), push: vi.fn() }
  setHttpRouter(router as unknown as Router)
  return router
}

beforeEach(() => {
  localStorage.clear(); vi.useFakeTimers(); __resetHttpGatesForTest(); authRouter(); vi.stubGlobal('crypto', webcrypto)
  Object.defineProperty(URL, 'createObjectURL', { value: vi.fn(() => 'blob:verified-native-review'), configurable: true, writable: true })
  Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true, writable: true })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  __resetHttpGatesForTest(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = ''
})

describe('native review export response authority', () => {
  it('uses authenticated frozen export and checks exact archive bytes against the server digest', async () => {
    const fetch = vi.fn(async () => archive()); vi.stubGlobal('fetch', fetch)
    const controller = new AbortController()
    const result = await exportNativeReviewRequest(capableJob(), controller.signal)
    expect(fetch).toHaveBeenCalledWith(EXPORT, expect.objectContaining({ method: 'GET', credentials: 'same-origin', signal: controller.signal }))
    expect(result).toMatchObject({ filename: `candidate-review-${JOB}.zip`, archive_sha256: ZIP_HASH, package_sha256: capableJob().package_sha256, code_version_matches: true })
    expect(result.blob.size).toBe(ZIP.length)
  })
  it.each([
    ['another job', { 'X-Review-Job-Id': OTHER_JOB }],
    ['another package', { 'X-Review-Package-SHA256': HASH }],
    ['changed code state', { 'X-Review-Code-Version-Matches': 'false' }],
    ['non-ZIP response', { 'Content-Type': 'application/json' }],
    ['malformed archive digest', { 'X-Review-Archive-SHA256': 'not-a-hash' }],
  ])('rejects %s before a browser download', async (_label, headers) => {
    vi.stubGlobal('fetch', vi.fn(async () => archive(capableJob(), headers)))
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('已阻止下载')
  })
  it('rejects altered archive bytes even when all identity headers match', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => archive(capableJob(), {}, new Uint8Array([1, 2, 3]))))
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('SHA256 与服务端记录不一致')
  })
  it('fails closed without a secure browser digest implementation', async () => {
    vi.stubGlobal('crypto', {}); vi.stubGlobal('fetch', vi.fn(async () => archive()))
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('浏览器无法校验')
  })
  it('does not treat unsupported capabilities or raw-score ingress as trusted', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(exportNativeReviewRequest(review(), new AbortController().signal)).rejects.toThrow('尚未确认')
    expect(fetch).not.toHaveBeenCalled()
    const forged = capableJob(); Object.assign(forged.capabilities!, { http_raw_review_import: true })
    expect(() => assertJob(forged, '2', 9, JOB)).toThrow('受信渠道契约不匹配')
  })
  it('surfaces owner authorization or stale evidence errors without retrying the export', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ code: 'REVIEW_JOB_CHANGED', detail: 'stored pixels no longer match' }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('stored pixels no longer match')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it.each([[401, 'UNAUTHORIZED', '/login'], [403, 'PASSWORD_CHANGE_REQUIRED', '/change-password']] as const)('shares the same single-flight auth gate as JSON requests for %s', async (status, code, destination) => {
    const router = authRouter()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code, detail: 'auth changed' }), { status })))
    const results = await Promise.allSettled([
      request('/api/v1/ordinary-read', { retries: 0 }),
      exportNativeReviewRequest(capableJob(), new AbortController().signal),
      exportNativeReviewRequest(capableJob(), new AbortController().signal),
    ])
    expect(results.every((value) => value.status === 'rejected')).toBe(true)
    expect(router.replace).toHaveBeenCalledTimes(1)
    expect(router.replace).toHaveBeenCalledWith(status === 401 ? { path: destination, query: { redirect: '/extract/source-a?style_id=9' } } : destination)
    expect(router.push).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1500)
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('auth changed')
    expect(router.replace).toHaveBeenCalledTimes(2)
  })
  it('does not redirect an ordinary forbidden export or a cancelled late auth response', async () => {
    const router = authRouter()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 'FORBIDDEN', detail: 'not your request' }), { status: 403 })))
    await expect(exportNativeReviewRequest(capableJob(), new AbortController().signal)).rejects.toThrow('not your request')
    const controller = new AbortController(); controller.abort()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })))
    await expect(exportNativeReviewRequest(capableJob(), controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(router.replace).not.toHaveBeenCalled()
  })
})

describe('P4 authoritative export and truthful trusted channel controls', () => {
  it('shows provider missing and operator-only import, exports after a fresh owner read, and never posts review scores', async () => {
    pointer(); const { wrapper, logs } = mount(); await flushPromises()
    const fetch = interceptExport(async () => archive())
    expect(wrapper.get('[data-testid="native-provider-status"]').text()).toContain('尚未配置')
    expect(wrapper.get('[data-testid="native-trusted-channel"]').text()).toContain('服务端操作员渠道')
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    await button(wrapper, '导出审核请求').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="native-export-result"]').exists()).toBe(true))
    expect(fetch.mock.calls.map(([input]) => String(input)).filter((url) => url.startsWith(DETAIL))).toEqual([DETAIL, EXPORT])
    expect(logs.filter((row) => row.url === DETAIL)).toHaveLength(2)
    expect(logs.every((row) => row.method === 'GET')).toBe(true)
    expect(wrapper.get('[data-testid="native-export-result"]').text()).toContain('已请求下载')
    const advancedExport = wrapper.findAll('details').find(details => details.text().includes(ZIP_HASH))!
    expect(advancedExport.exists()).toBe(true)
    expect(advancedExport.attributes('open')).toBeUndefined()
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:verified-native-review')
  })
  it('keeps missing capability state unconfirmed and disables export', async () => {
    pointer(); const { wrapper } = mount(plan({ [DETAIL]: envelope(review()) })); await flushPromises()
    expect(wrapper.get('[data-testid="native-channel-unknown"]').text()).toContain('尚未返回')
    expect(button(wrapper, '导出审核请求').attributes('disabled')).toBeDefined()
  })
  it.each([[401, 'UNAUTHORIZED', '/login'], [403, 'PASSWORD_CHANGE_REQUIRED', '/change-password']] as const)('redirects if authentication changes between the owned detail read and export: %s', async (status, code, destination) => {
    pointer(); const { wrapper, logs } = mount(); await flushPromises()
    const router = authRouter()
    interceptExport(async () => new Response(JSON.stringify({ code, detail: 'auth changed after detail' }), { status }))
    await button(wrapper, '导出审核请求').trigger('click'); await flushPromises()
    expect(logs.filter((row) => row.url === DETAIL)).toHaveLength(2)
    expect(router.replace).toHaveBeenCalledTimes(1)
    expect(router.replace).toHaveBeenCalledWith(status === 401 ? { path: destination, query: { redirect: '/extract/source-a?style_id=9' } } : destination)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="native-export-result"]').exists()).toBe(false)
  })
  it('allows verified historical ZIP export while code mismatch continues blocking apply', async () => {
    const historical = capableJob({ code_version_matches: false, imported_bundle_sha256: IMPORT })
    pointer(); const { wrapper } = mount(plan({ [DETAIL]: envelope(historical) })); await flushPromises()
    interceptExport(async () => archive(historical))
    expect(wrapper.get('[data-testid="native-export-historical"]').text()).toContain('历史证据')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    await button(wrapper, '导出审核请求').trigger('click')
    await vi.waitFor(() => expect(wrapper.get('[data-testid="native-export-result"]').text()).toContain('仅供历史证据检查'))
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
  })
  it('requires explicit matching-job adoption when a creation result is unresolved', async () => {
    pointer(); localStorage.setItem(`yy:p4:native:uncertain-create:2:9:${ASSET.asset_id}:${HASH}`, JSON.stringify({ style_id: 9, items: [DRAFT] }))
    const { wrapper } = mount(); await flushPromises()
    expect(button(wrapper, '导出审核请求').attributes('disabled')).toBeDefined()
    await button(wrapper, '确认用此匹配任务继续').trigger('click'); await flushPromises()
    expect(button(wrapper, '导出审核请求').attributes('disabled')).toBeUndefined()
  })
  it('stops before export if the authoritative package changes during preflight', async () => {
    const routes = plan({ [DETAIL]: envelope(capableJob()) }); pointer(); const { wrapper } = mount(routes); await flushPromises()
    routes[DETAIL] = envelope(capableJob({ package_sha256: HASH }))
    const fetch = interceptExport(async () => archive())
    await button(wrapper, '导出审核请求').trigger('click'); await flushPromises()
    expect(fetch.mock.calls.some(([input]) => String(input) === EXPORT)).toBe(false)
    expect(wrapper.text()).toContain('任务包已变化')
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('prevents duplicate export and discards a late response after source scope changes', async () => {
    pointer(); const { wrapper, state } = mount(); await flushPromises()
    let finish!: (value: Response) => void
    const fetch = interceptExport(() => new Promise<Response>((resolve) => { finish = resolve }))
    const exportButton = button(wrapper, '导出审核请求')
    await exportButton.trigger('click'); await flushPromises(); await exportButton.trigger('click')
    const calls = fetch.mock.calls.filter(([input]) => String(input) === EXPORT)
    expect(calls).toHaveLength(1)
    state.asset = { ...ASSET, asset_id: 'another-source' }; await flushPromises()
    expect(calls[0]?.[1]?.signal?.aborted).toBe(true)
    finish(archive()); await flushPromises()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="native-export-result"]').exists()).toBe(false)
  })
  it('aborts an in-flight export on unmount without invoking a download later', async () => {
    pointer(); const { wrapper } = mount(); await flushPromises()
    let finish!: (value: Response) => void
    const fetch = interceptExport(() => new Promise<Response>((resolve) => { finish = resolve }))
    await button(wrapper, '导出审核请求').trigger('click'); await flushPromises()
    wrapper.unmount(); wrappers.splice(wrappers.indexOf(wrapper), 1)
    expect(fetch.mock.calls.find(([input]) => String(input) === EXPORT)?.[1]?.signal?.aborted).toBe(true)
    finish(archive()); await flushPromises()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('does not clear a failed-image apply guard while exporting authoritative evidence', async () => {
    pointer(); const { wrapper } = mount(plan({ [DETAIL]: envelope(capableJob({ imported_bundle_sha256: IMPORT })) })); await flushPromises()
    await wrapper.get('img').trigger('error')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    interceptExport(async () => archive())
    await button(wrapper, '导出审核请求').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[data-testid="native-export-result"]').exists()).toBe(true))
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('审核图像无法读取')
  })
  it('reports a bounded export timeout and refuses any later download', async () => {
    pointer(); const { wrapper } = mount(); await flushPromises()
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === EXPORT
      ? new Promise<Response>((_resolve, reject) => { init?.signal?.addEventListener('abort', () => reject(new DOMException('Timed out', 'AbortError'))) })
      : previous(input, init))
    await button(wrapper, '导出审核请求').trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(60000); await flushPromises()
    expect(wrapper.text()).toContain('导出已中断或超时')
    expect(button(wrapper, '导出审核请求').attributes('disabled')).toBeUndefined()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('refreshes only authoritative import status and preserves exact-hash apply gating', async () => {
    const routes = plan({ [DETAIL]: envelope(capableJob()) }); pointer(); const { wrapper, logs } = mount(routes); await flushPromises()
    routes[DETAIL] = envelope(capableJob({ imported_bundle_sha256: IMPORT }))
    await button(wrapper, '刷新受信导入状态').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-testid="native-import-state"]').text()).toContain('已导入，仍需单独应用')
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeUndefined()
    routes[DETAIL] = problem(503, 'REVIEW_READ_FAILED', 'read unavailable')
    await button(wrapper, '刷新受信导入状态').trigger('click'); await flushPromises()
    expect(button(wrapper, '导出审核请求').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '应用服务端').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="native-provider-status"]').exists()).toBe(false)
    expect(logs.every((row) => row.method === 'GET')).toBe(true)
  })
})
