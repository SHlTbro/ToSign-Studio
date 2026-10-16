import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { listPendingRenders, persistPendingRender } from '@/api/pendingRender'
import { envelope, flushPromises, mountPage, problem } from './_harness'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const JOB_ID = 'draft-original-owned'
const PATH = `/api/v1/render-jobs/${JOB_ID}`
const result = (status = 'running', id = JOB_ID) => ({ job: { job_id: id, status }, items: [{ item_id: 'item-a', status: 'pending', cn: 'Alice', output_relative_path: null, warnings: [] }] })
const response = (value: unknown) => new Response(JSON.stringify(envelope(value).json), { status: 200 })
const PLAN = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset-a', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template-a', base_asset_id: 'asset-a', status: 'approved' }] }),
  '/api/v1/phrase-templates': envelope({ items: [] }), '/api/v1/blogger-name-presets': envelope({ items: [] }),
  '/api/v1/styles': envelope({ items: [] }), '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
}
interface Call { path: string; method: string; body?: unknown; key: string | null; signal?: AbortSignal }
const wrappers: VueWrapper[] = []
beforeEach(() => { vi.useFakeTimers(); localStorage.clear() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })
function delayed(value: Response, signal?: AbortSignal, delay = 55000) {
  return new Promise<Response>((resolve, reject) => {
    const timer = window.setTimeout(() => resolve(value), delay)
    signal?.addEventListener('abort', () => { window.clearTimeout(timer); reject(new DOMException('deadline', 'AbortError')) }, { once: true })
  })
}
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text() === text)
  if (!button) throw new Error(`Missing visible button ${text}`)
  await button.trigger('click'); await flushPromises()
}
async function open(read?: (call: Call, calls: Call[]) => Response | Promise<Response>) {
  const mounted = mountPage(P5Batch, { plan: PLAN }); wrappers.push(mounted.wrapper)
  await flushPromises()
  await mounted.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
  await mounted.wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
  const ordinaryFetch = globalThis.fetch; const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (!path.startsWith('/api/v1/render-jobs') || path.includes('/capabilities')) return ordinaryFetch(input, init)
    const call = { path, method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      key: new Headers(init?.headers).get('Idempotency-Key'), signal: init?.signal as AbortSignal | undefined }
    calls.push(call)
    return Promise.resolve(read?.(call, calls) ?? response(call.path.endsWith('/cancel') ? { job: { job_id: JOB_ID, status: 'cancelled' } } : result()))
  }))
  return { ...mounted, calls }
}
const creates = (calls: Call[]) => calls.filter((call) => call.path === '/api/v1/render-jobs' && call.method === 'POST')
const cancels = (calls: Call[]) => calls.filter((call) => call.path.endsWith('/cancel'))
const confirmed = (wrapper: VueWrapper) => wrapper.text().includes('草稿生成已取消')

describe('P5 explicit owned draft cancel', () => {
  it.each(['succeeded', 'partial', 'failed'])('does not offer cancel for an already terminal %s draft', async (status) => {
    const terminal = { ...result(status), items: [{ item_id: 'item-a', status: status === 'failed' ? 'failed' : 'completed_with_warning', cn: 'Alice', output_relative_path: status === 'failed' ? null : 'output.png', warnings: [] }] }
    const { wrapper, calls } = await open(() => response(terminal))
    await click(wrapper, '预览草稿')
    expect(wrapper.findAll('button').some((button) => button.text() === '取消草稿生成')).toBe(false)
    expect(cancels(calls)).toHaveLength(0)
  })
  it.each(['queued', 'running'])('cancels only the existing %s draft with an empty body and never creates or retries again', async (status) => {
    const { wrapper, calls } = await open((call) => response(call.path.endsWith('/cancel') ? { job: { job_id: JOB_ID, status: 'cancelled' } } : result(status)))
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成')
    expect(cancels(calls)).toHaveLength(1)
    expect(cancels(calls)[0]).toMatchObject({ path: `${PATH}/cancel`, method: 'POST', body: undefined, key: null })
    expect(confirmed(wrapper)).toBe(true)
    expect(wrapper.text()).toContain(JOB_ID)
    expect(wrapper.text()).not.toContain('渲染中')
    await vi.advanceTimersByTimeAsync(10000)
    expect(creates(calls)).toHaveLength(1)
    expect(calls.some((call) => call.path.includes('/retry'))).toBe(false)
  })
  it('keeps the cancel pending until its real response, blocks duplicate/new submits and uses the original ID after editing', async () => {
    const { wrapper, calls } = await open((call) => call.path.endsWith('/cancel') ? delayed(response({ job: { job_id: JOB_ID, status: 'cancelled' } }), call.signal) : response(result()))
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成')
    expect(confirmed(wrapper)).toBe(false)
    await vi.advanceTimersByTimeAsync(15001)
    expect(cancels(calls)[0]!.signal?.aborted).toBe(false)
    expect(confirmed(wrapper)).toBe(false)
    const pendingButton = wrapper.findAll('button').find((button) => button.text() === '正在取消草稿…')!
    expect(pendingButton.attributes('disabled')).toBeDefined()
    await pendingButton.trigger('click')
    await click(wrapper, '继续编辑')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Bob')
    expect(wrapper.findAll('button').find((button) => button.text() === '预览草稿')?.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find((button) => button.text() === '正式生成')?.attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(39999); await flushPromises()
    expect(confirmed(wrapper)).toBe(true)
    expect(wrapper.text()).toContain(JOB_ID)
    expect(cancels(calls)).toHaveLength(1)
    expect(cancels(calls)[0]!.path).toBe(`${PATH}/cancel`)
    expect(creates(calls)).toHaveLength(1)
    expect((wrapper.get('[aria-label="第 1 行粉丝昵称"]').element as HTMLInputElement).value).toBe('Bob')
  })
  it('cannot send cancel while creation has no job ID', async () => {
    const { wrapper, calls } = await open((call) => delayed(response(result()), call.signal))
    await click(wrapper, '预览草稿')
    const button = wrapper.findAll('button').find((node) => node.text() === '取消草稿生成')!
    expect(button).toBeDefined()
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click')
    expect(cancels(calls)).toHaveLength(0)
    expect(creates(calls)).toHaveLength(1)
  })
  it('does not cancel a known old ID while a new draft creation is pending', async () => {
    const { wrapper, calls } = await open((call, seen) => call.path === '/api/v1/render-jobs' && creates(seen).length === 2
      ? delayed(response(result('running', 'new-draft')), call.signal) : response(result('failed')))
    await click(wrapper, '预览草稿'); await click(wrapper, '重试草稿渲染')
    const button = wrapper.findAll('button').find((node) => node.text() === '取消草稿生成')!
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click')
    expect(cancels(calls)).toHaveLength(0)
  })
  it.each(['unknown result', 'wrong job identity', 'HTTP failure'] as const)('retains original ID for %s and confirms only after explicit GET', async (kind) => {
    let reads = 0
    const { wrapper, calls } = await open((call) => {
      if (call.path.endsWith('/cancel')) return kind === 'HTTP failure'
        ? new Response(JSON.stringify(problem(500, 'CANCEL_UNCERTAIN', 'cancel result uncertain').json), { status: 500 })
        : response({ job: { job_id: kind === 'wrong job identity' ? 'other-owned-job' : JOB_ID, status: kind === 'unknown result' ? 'running' : 'cancelled' } })
      if (call.method === 'GET' && ++reads > 1) return delayed(response(result('cancelled')), call.signal)
      return response(result())
    })
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成')
    expect(confirmed(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('取消结果尚未确认')
    expect(wrapper.text()).toContain(JOB_ID)
    await vi.advanceTimersByTimeAsync(10000)
    expect(cancels(calls)).toHaveLength(1)
    await click(wrapper, '刷新取消状态')
    const refresh = calls.at(-1)!
    expect(refresh.path).toBe(PATH)
    expect(refresh.method).toBe('GET')
    await vi.advanceTimersByTimeAsync(15001)
    expect(refresh.signal?.aborted).toBe(false)
    expect(confirmed(wrapper)).toBe(false)
    await vi.advanceTimersByTimeAsync(39999); await flushPromises()
    expect(confirmed(wrapper)).toBe(true)
    expect(cancels(calls)).toHaveLength(1)
    expect(creates(calls)).toHaveLength(1)
  })
  it('bounds uncertain cancel at 60 seconds without automatic resubmission', async () => {
    const { wrapper, calls } = await open((call) => call.path.endsWith('/cancel') ? delayed(response({}), call.signal, 70000) : response(result()))
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成')
    const cancel = cancels(calls)[0]!
    await vi.advanceTimersByTimeAsync(59999)
    expect(cancel.signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(cancel.signal?.aborted).toBe(true)
    expect(confirmed(wrapper)).toBe(false)
    expect(wrapper.text()).toContain(JOB_ID)
    await vi.advanceTimersByTimeAsync(10000)
    expect(cancels(calls)).toHaveLength(1)
    expect(creates(calls)).toHaveLength(1)
    expect(wrapper.findAll('button').some((button) => button.text() === '刷新取消状态')).toBe(true)
  })
  it('ignores a stale pre-cancel poll response after the server confirmed cancellation', async () => {
    const { wrapper, calls } = await open((call) => call.method === 'GET' ? delayed(response(result()), call.signal)
      : response(call.path.endsWith('/cancel') ? { job: { job_id: JOB_ID, status: 'cancelled' } } : result()))
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成')
    expect(confirmed(wrapper)).toBe(true)
    await vi.advanceTimersByTimeAsync(55000); await flushPromises()
    expect(confirmed(wrapper)).toBe(true)
    expect(wrapper.findAll('button').some((button) => button.text() === '取消草稿生成')).toBe(false)
    expect(creates(calls)).toHaveLength(1)
  })
  it('keeps cancelled state unconfirmed if an explicit refresh still says running or fails', async () => {
    let reads = 0
    const { wrapper, calls } = await open((call) => {
      if (call.path.endsWith('/cancel')) return response({ job: { job_id: JOB_ID, status: 'running' } })
      if (call.method === 'GET' && ++reads > 2) return new Response(JSON.stringify(problem(500, 'PROOF_UNAVAILABLE', 'proof unavailable').json), { status: 500 })
      return response(result())
    })
    await click(wrapper, '预览草稿'); await click(wrapper, '取消草稿生成'); await click(wrapper, '刷新取消状态')
    expect(confirmed(wrapper)).toBe(false)
    await click(wrapper, '刷新取消状态')
    expect(confirmed(wrapper)).toBe(false)
    expect(wrapper.text()).toContain(JOB_ID)
    expect(wrapper.text()).toContain('proof unavailable')
    expect(cancels(calls)).toHaveLength(1)
    expect(creates(calls)).toHaveLength(1)
  })
  it.each(['关闭预览', '继续编辑'])('preserves %s as local preview dismissal without sending server cancel', async (action) => {
    const { wrapper, calls } = await open(); await click(wrapper, '预览草稿')
    if (action === '关闭预览') await wrapper.get('[aria-label="关闭预览"]').trigger('click')
    else await click(wrapper, action)
    await vi.advanceTimersByTimeAsync(10000)
    expect(cancels(calls)).toHaveLength(0)
    expect(creates(calls)).toHaveLength(1)
  })
  it('does not create or cancel a draft from an unresolved formal submission', async () => {
    const pending = { key: 'formal-original-key', body: { template_id: 'template-a', style_mode: 'system', glyph_source_policy: 'fallback_allowed', layout_profile: 'baseline-v1', ink_color_mode: 'auto-contrast-v1', items: [{ cn: 'original', sign_blogger: false }] } }
    persistPendingRender('2', pending)
    const { wrapper, calls } = await open(); await click(wrapper, '预览草稿')
    expect(creates(calls)).toHaveLength(0)
    expect(cancels(calls)).toHaveLength(0)
    expect(listPendingRenders('2')).toEqual([pending])
    expect(wrapper.text()).toContain('请先确认上次正式提交结果')
  })
})
