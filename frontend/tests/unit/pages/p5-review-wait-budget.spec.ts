import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Review from '@/pages/P5_Review.vue'
import { envelope, flushPromises, mountPage, problem } from './_harness'

// Exercise real http deadlines at the fetch seam, without live APIs, DB or model execution.
const PATH = '/api/v1/render-jobs/review-slow'
const completeItem = { item_id: 'item-a', status: 'completed', cn: '小美', output_relative_path: 'output/item-a.png',
  fallback_used: false, fallback_map: {}, warnings: [], retryable: false, render_version: 1 }
const result = (status = 'completed', jobStatus = 'succeeded') => ({ job: { status: jobStatus }, items: [{ ...completeItem, status }] })
const json = (data: unknown) => new Response(JSON.stringify(envelope(data).json), { status: 200, headers: { 'Content-Type': 'application/json' } })
const failure = () => new Response(JSON.stringify(problem(500, 'CURRENT_PROOF_UNAVAILABLE', 'current proof failed').json), { status: 500 })
interface Call { path: string; method: string; signal?: AbortSignal; body?: unknown }
const wrappers: VueWrapper[] = []
beforeEach(() => {
  vi.useFakeTimers(); localStorage.clear(); sessionStorage.clear()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:fixture-review'), revokeObjectURL: vi.fn() }))
})
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); document.body.innerHTML = '' })
function delayed(response: Response, signal?: AbortSignal, ms = 55000): Promise<Response> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => resolve(response), ms)
    signal?.addEventListener('abort', () => { window.clearTimeout(timer); reject(new DOMException('deadline', 'AbortError')) }, { once: true })
  })
}
async function openReview(read: (call: Call, calls: Call[]) => Response | Promise<Response>) {
  const mounted = mountPage(P5Review, { plan: {} }); wrappers.push(mounted.wrapper)
  const ordinaryFetch = globalThis.fetch; const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (!path.startsWith(PATH)) return ordinaryFetch(input, init)
    const call = { path, method: init?.method ?? 'GET', signal: init?.signal as AbortSignal | undefined,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined }
    calls.push(call); if (path.endsWith('/outputs')) return Promise.resolve(json({ job_id: 'review-slow', export_artifacts: [] })); return Promise.resolve(read(call, calls))
  }))
  await mounted.router.push('/batch/review-slow/review'); await flushPromises()
  return { ...mounted, calls }
}
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text().includes(text))
  if (!button) throw new Error(`Missing button ${text}`)
  await button.trigger('click'); await flushPromises()
}
async function passOldDeadline(signal?: AbortSignal) {
  await vi.advanceTimersByTimeAsync(15001); await flushPromises()
  expect(signal?.aborted).toBe(false)
}
async function finish55Seconds() { await vi.advanceTimersByTimeAsync(39999); await flushPromises() }

describe('P5 review owned request wait budget', () => {
  it('keeps reload GET pending past 15 seconds and exposes the real item only after 55-second verification', async () => {
    const { wrapper, calls } = await openReview((call) => delayed(json(result()), call.signal))
    expect(wrapper.text()).toContain('正在读取任务与成品')
    await passOldDeadline(calls[0]?.signal)
    expect(calls).toHaveLength(1)
    expect(wrapper.text()).not.toContain('已质检通过')
    await finish55Seconds()
    expect(wrapper.text()).toContain('等待人工质检')
    expect(wrapper.text()).not.toContain('任务读取失败')
    expect(wrapper.findAll('button').find((node) => node.text().includes('质检通过'))?.attributes('disabled')).toBeUndefined()
  })
  it('keeps a running-job poll GET pending past 15 seconds without spawning another request', async () => {
    const { wrapper, calls } = await openReview((call, seen) => seen.length === 1 ? json(result('running', 'running')) : delayed(json(result()), call.signal))
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(calls).toHaveLength(2)
    await passOldDeadline(calls[1]?.signal)
    expect(calls).toHaveLength(2)
    await finish55Seconds()
    expect(wrapper.text()).toContain('等待人工质检')
    await vi.advanceTimersByTimeAsync(10000)
    expect(calls).toHaveLength(2)
  })
  it.each(['approve', 'reject'] as const)('waits for %s POST and current-proof readback before marking or advancing', async (decision) => {
    const decided = decision === 'approve' ? 'approved' : 'rejected'
    let reads = 0
    const { wrapper, calls } = await openReview((call) => {
      if (call.method === 'POST') return delayed(json({ status: decided }), call.signal)
      return ++reads === 1 ? json(result()) : delayed(json(result(decided)), call.signal)
    })
    if (decision === 'approve') await click(wrapper, '质检通过')
    else { await click(wrapper, '打回重做'); await wrapper.get('[aria-label="打回理由"]').setValue('  字迹模糊  '); await click(wrapper, '确认打回') }
    const post = calls.find((call) => call.method === 'POST')!
    expect(post.path).toBe(`${PATH}/items/item-a/${decision}`)
    if (decision === 'reject') expect(post.body).toEqual({ comment: '字迹模糊' })
    await passOldDeadline(post.signal)
    expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
    expect(reads).toBe(1)
    await finish55Seconds()
    expect(reads).toBe(2)
    const readback = calls.at(-1)!
    await passOldDeadline(readback.signal)
    expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
    await finish55Seconds()
    expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(true)
    expect(wrapper.text()).toContain(decision === 'approve' ? '已质检通过' : '已打回')
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })
  it('waits for the export POST and binary PNG download, then reports successful download', async () => {
    const { wrapper, calls } = await openReview((call) => {
      if (call.path.endsWith('/export')) return delayed(json({ status: 'completed', artifacts: [{ artifact_id: 'png-a', relative_path: 'approved.png' }], failures: [] }), call.signal)
      if (call.path.endsWith('/file')) return delayed(new Response(new Blob(['png fixture'], { type: 'image/png' }), { status: 200 }), call.signal)
      return json(result('approved'))
    })
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await click(wrapper, '导出 PNG')
    const post = calls.at(-1)!
    await passOldDeadline(post.signal)
    expect(wrapper.text()).toContain('导出中')
    expect(download).not.toHaveBeenCalled()
    await finish55Seconds()
    const file = calls.at(-1)!
    expect(file.path).toBe(`${PATH}/exports/png-a/file`)
    await passOldDeadline(file.signal)
    expect(download).not.toHaveBeenCalled()
    await finish55Seconds()
    expect(download).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('再次下载已通过成品')
    expect(calls.filter((call) => call.path.endsWith('/export'))).toHaveLength(1)
  })
  it.each(['approve', 'reject'] as const)('keeps failed %s uncertain and never automatically resubmits it', async (decision) => {
    const { wrapper, calls } = await openReview((call) => call.method === 'POST' ? delayed(failure(), call.signal) : json(result()))
    if (decision === 'approve') await click(wrapper, '质检通过')
    else { await click(wrapper, '打回重做'); await wrapper.get('[aria-label="打回理由"]').setValue('字迹模糊'); await click(wrapper, '确认打回') }
    await passOldDeadline(calls.at(-1)?.signal); await finish55Seconds()
    expect(wrapper.text()).toContain('current proof failed')
    expect(wrapper.text()).not.toContain('已质检通过')
    expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10000)
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(calls.filter((call) => call.path === PATH)).toHaveLength(1)
  })
  it('does not approve optimistically or replay QC when the successful POST readback fails', async () => {
    let reads = 0
    const { wrapper, calls } = await openReview((call) => call.method === 'POST' ? json({ status: 'approved' }) : ++reads === 1 ? json(result()) : failure())
    await click(wrapper, '质检通过')
    expect(wrapper.text()).toContain('回读失败')
    expect(wrapper.text()).not.toContain('已质检通过')
    expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(reads).toBe(2)
  })
  it.each(['POST', 'file'] as const)('preserves export %s failures without a download or automatic rewrite', async (where) => {
    const { wrapper, calls } = await openReview((call) => {
      if (call.path.endsWith('/export')) return where === 'POST' ? failure() : json({ status: 'completed', artifacts: [{ artifact_id: 'png-a', relative_path: 'approved.png' }], failures: [] })
      if (call.path.endsWith('/file')) return failure()
      return json(result('approved'))
    })
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await click(wrapper, '导出 PNG')
    expect(wrapper.text()).toContain(where === 'POST' ? '导出结果尚未确认' : '文件已导出，下载未完成')
    expect(download).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('再次下载已通过成品')
    await vi.advanceTimersByTimeAsync(10000)
    expect(calls.filter((call) => call.path.endsWith('/export'))).toHaveLength(1)
    expect(calls.filter((call) => call.path.endsWith('/file'))).toHaveLength(where === 'file' ? 1 : 0)
  })
  it.each(['approve', 'reject', 'export POST', 'binary file'] as const)('bounds %s at exactly 600 seconds without optimistic success or replay', async (action) => {
    const exportAction = action === 'export POST' || action === 'binary file'
    const { wrapper, calls } = await openReview((call) => {
      if (call.path.endsWith('/file')) return delayed(new Response('png fixture'), call.signal, 700000)
      if (call.method === 'POST') return action === 'binary file'
        ? json({ status: 'completed', artifacts: [{ artifact_id: 'png-a', relative_path: 'approved.png' }], failures: [] })
        : delayed(json({}), call.signal, 700000)
      return json(result(exportAction ? 'approved' : 'completed'))
    })
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    if (exportAction) await click(wrapper, '导出 PNG')
    else if (action === 'approve') await click(wrapper, '质检通过')
    else { await click(wrapper, '打回重做'); await wrapper.get('[aria-label="打回理由"]').setValue('字迹模糊'); await click(wrapper, '确认打回') }
    const pending = calls.at(-1)!
    expect(pending.signal).toBeDefined()
    await vi.advanceTimersByTimeAsync(599999)
    expect(pending.signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(pending.signal?.aborted).toBe(true)
    if (!exportAction) {
      expect(wrapper.text()).not.toContain('已质检通过')
      expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
    }
    expect(download).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('再次下载已通过成品')
    await vi.advanceTimersByTimeAsync(10000)
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(calls.filter((call) => call.path.endsWith('/file'))).toHaveLength(action === 'binary file' ? 1 : 0)
  })
  it('bounds GET at 600 seconds with no HTTP retry while preserving ordinary job polling', async () => {
    const { wrapper, calls } = await openReview((call) => delayed(json(result()), call.signal, 700000))
    await vi.advanceTimersByTimeAsync(599999)
    expect(calls[0]!.signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(calls[0]!.signal?.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toHaveLength(1)
    expect(wrapper.text()).not.toContain('已质检通过')
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toHaveLength(2)
    expect(calls.every((call) => call.method === 'GET')).toBe(true)
  })
})
