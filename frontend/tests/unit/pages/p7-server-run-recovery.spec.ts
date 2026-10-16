import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P7 from '@/pages/P7_Training.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const wrappers: VueWrapper[] = []
const RUN = { run_id: 2, status: 'succeeded', config: { engine: 'm2', style_id: 1 }, sample_ids: [], created_at: '2026-10-08T04:19:37Z', updated_at: '2026-10-08T06:05:12Z' }
function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/styles': envelope({ items: [{ style_id: 1, name: 'Owned training fixture', source_type: 'HANDWRITING_ASSET', status: 'approved' }] }),
    '/api/v1/styles/1/samples': envelope({ items: [] }),
    '/api/v1/styles/1/m2-runtime': envelope({ ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'NEED_NEW_INPUTS', detail: 'Need 20 chars for new training' }], capabilities: {} }),
    '/api/v1/training-sets': envelope({ items: [], total: 0, page: 1, page_size: 24 }),
    '/api/v1/training-samples': envelope({ items: [], total: 0 }),
    '/api/v1/style-runs/2': envelope(RUN),
    '/api/v1/style-runs/2/progress': envelope({ run_id: 2, status: 'succeeded', stage: 'admitted', failure: null, units_done: 8, units_total: 8, unit: 'epochs', admission: { status: 'PASSED', model_sha256: 'b'.repeat(64), receipt_sha256: 'a'.repeat(64) } }),
    '/api/v1/style-runs/2/readiness': envelope({ status: 'BLOCKED', blockers: { quality: 'PENDING' }, allowed_actions: { evaluate: true, publish: false }, evaluation_state: 'not_started', publication: null }),
    '/api/v1/style-runs/2/inference-deployment': envelope({ run_id: 2, supports_preserved_training: true, state: 'registered', available: [], runtime_binding: { registration_id: 'qualified-current-source' }, training_runtime_binding: { registration_id: 'original-training' } }),
    '/api/v1/style-runs/2/metrics': envelope({ state: 'available' }),
    '/api/v1/style-runs/2/evaluate': envelope({ state: 'unavailable' }),
    '/api/v1/style-runs/2/samples': envelope({ items: [] }),
    ...extra,
  }
}
async function open(url: string, extra: FetchPlan = {}) {
  const ctx = mountPage(P7, { plan: plan(extra) }); wrappers.push(ctx.wrapper)
  await ctx.router.replace(url); await flushPromises(); await flushPromises()
  return ctx
}
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => { if (wrapper.exists()) wrapper.unmount() }); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('Owned run recovery through the existing detail API', () => {
  it('uses the exact imported model identity for the training profile label without renaming unrelated profiles', async () => {
    const ctx = await open('/training?style_id=1', { '/api/v1/styles': envelope({ items: [
      { style_id: 1, name: 'Owned training fixture', source_type: 'HANDWRITING_ASSET', status: 'approved' },
      { style_id: 2, name: '0178 · 已认可 V3', source_type: 'M2_ADAPTER', status: 'approved', model_version: 'adapter@017881442b7f8b90', receipt_id: 'imported_inference/16ec01496e0b472b9f561ce383a7ed33' },
      { style_id: 3, name: 'My model', source_type: 'M2_ADAPTER', status: 'approved', model_version: 'different', receipt_id: 'imported_inference/other' },
    ] }) })
    expect(ctx.wrapper.get('option[value="2"]').text()).toContain('经典手写 V3')
    expect(ctx.wrapper.get('option[value="3"]').text()).toContain('My model')
  })
  it('allows evaluation of an admitted run while unrelated new-training preflight is still pending', async () => {
    const ctx = mountPage(P7, { plan: plan() }); wrappers.push(ctx.wrapper)
    const original = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).includes('/m2-runtime')
      ? new Promise<Response>(() => {}) : original(input, init))
    await ctx.router.replace('/training?style_id=1&run_id=2'); await flushPromises(); await flushPromises()
    expect(ctx.wrapper.text()).toContain('补验入库通过')
    expect(ctx.wrapper.text()).toContain('完成 8 / 8 轮')
    expect(ctx.wrapper.findAll('button').find(button => button.text() === '运行评估')?.attributes('disabled')).toBeUndefined()
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it('restores the successful pointer on a fresh page and clears prior state when the next explicit ID is invalid', async () => {
    const first = await open('/training?style_id=1&run_id=2'); first.wrapper.unmount()
    const restored = await open('/training?style_id=1')
    expect(restored.wrapper.text()).toContain('补验入库通过')
    await restored.router.replace('/training?style_id=1&run_id=invalid'); await flushPromises()
    expect(restored.wrapper.text()).not.toContain('补验入库通过')
    expect(restored.wrapper.text()).toContain('运行编号必须是正整数')
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('rejects a detail response whose identity differs from the requested run', async () => {
    const ctx = await open('/training?style_id=1&run_id=2', { '/api/v1/style-runs/2': envelope({ ...RUN, run_id: 7 }) })
    expect(ctx.wrapper.text()).toContain('服务器运行身份无效')
    expect(ctx.wrapper.text()).not.toContain('补验入库通过')
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('recovers an admitted run from an explicit deep link with no local pointer and leaves new training gates intact', async () => {
    const ctx = await open('/training?style_id=1&run_id=2')
    expect(ctx.wrapper.text()).toContain('补验入库通过')
    expect(ctx.wrapper.get('[data-testid="choose-step-4"]').attributes('aria-current')).toBe('step')
    expect(ctx.wrapper.findAll('button').find(button => button.text() === '运行评估')?.attributes('disabled')).toBeUndefined()
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('2')
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
    expect(ctx.logs.some(call => call.url === '/api/v1/style-runs')).toBe(false)
    await ctx.wrapper.get('[data-testid="choose-step-2"]').trigger('click')
    expect(ctx.wrapper.find('[data-testid="training-blockers"]').exists()).toBe(false)
    expect(ctx.wrapper.text()).toContain('新训练准备提醒')
  })
  it('offers explicit positive run number recovery without creating training', async () => {
    const ctx = await open('/training?style_id=1')
    await ctx.wrapper.get('[aria-label="已有训练运行编号"]').setValue('2')
    await ctx.wrapper.findAll('button').find(button => button.text() === '读取已有训练')!.trigger('click'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('补验入库通过')
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it.each(['abc', '0', '-1', '2.5', '9007199254740992'])('rejects illegal deep link %s without reading any run', async id => {
    const ctx = await open(`/training?style_id=1&run_id=${id}`)
    expect(ctx.wrapper.text()).toContain('运行编号必须是正整数')
    expect(ctx.logs.some(call => /^\/api\/v1\/style-runs\//.test(call.url))).toBe(false)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('rejects a different profile detail rather than saving or showing it', async () => {
    const ctx = await open('/training?style_id=1&run_id=2', { '/api/v1/style-runs/2': envelope({ ...RUN, config: { engine: 'm2', style_id: 9 } }) })
    expect(ctx.wrapper.text()).toContain('档案不匹配')
    expect(ctx.wrapper.text()).not.toContain('补验入库通过')
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('respects an unknown or another owner run refusal and keeps no recovered state', async () => {
    const ctx = await open('/training?style_id=1&run_id=2', { '/api/v1/style-runs/2': problem(403, 'FORBIDDEN', 'Owned run unavailable') })
    expect(ctx.wrapper.text()).not.toContain('补验入库通过')
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('does not replace an unresolved idempotent pending create with a deep link', async () => {
    const pending = { key: 'preserved-create-key', body: { sample_ids: [], config: { engine: 'm2', style_id: 1 } } }
    localStorage.setItem('yy:p7:pending-style-run-create', JSON.stringify(pending))
    const ctx = await open('/training?style_id=1&run_id=2')
    expect(JSON.parse(localStorage.getItem('yy:p7:pending-style-run-create')!)).toEqual(pending)
    expect(ctx.logs.some(call => call.url === '/api/v1/style-runs/2')).toBe(false)
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
})
