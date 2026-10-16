import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P7 from '@/pages/P7_Training.vue'
import { envelope, flushPromises, mountPage } from './_harness'

// Transport fixtures only; no model, training process, quality or owner approval.
const wrappers: VueWrapper[] = []
function mountFailure(failure: { code?: string; message?: string }, admitted = false) {
  const ctx = mountPage(P7, { plan: {
    '/api/v1/styles': envelope({ items: [{ style_id: 99, name: 'Failure transport fixture', source_type: 'HANDWRITING_ASSET', status: 'approved' }] }),
    '/api/v1/styles/99/samples': envelope({ items: [] }),
    '/api/v1/styles/99/m2-runtime': envelope({ ready: false, status: 'BLOCKED', config: null,
      blockers: [{ code: 'TEST_RUNTIME_BLOCKED', detail: 'Transport fixture has no runtime' }], capabilities: {},
      acceptance_policy: { mode: 'OWNER_REQUIRED', human_blind_review: 'REQUIRED', candidate_confirmation: 'OWNER_REQUIRED' } }),
    '/api/v1/training-sets': envelope({ items: [], total: 0, page: 1, page_size: 24 }),
    '/api/v1/training-samples': envelope({ items: [], total: 0 }),
    '/api/v1/style-runs/8': envelope({ run_id: 8, status: admitted ? 'succeeded' : 'failed', config: { engine: 'm2', style_id: 99 }, sample_ids: [] }),
    '/api/v1/style-runs/8/readiness': envelope({ evaluation_state: 'not_started', publication: null, allowed_actions: {}, blockers: {} }),
    '/api/v1/style-runs/8/progress': envelope(admitted ? { status: 'succeeded', stage: 'admitted', failure: null, admission: { status: 'PASSED', receipt_sha256: 'a'.repeat(64), model_sha256: 'b'.repeat(64) }, historical_execution: { stage: 'failed', failure } } : { status: 'failed', failure }),
    '/api/v1/style-runs/8/metrics': envelope({ state: 'pending' }),
    '/api/v1/style-runs/8/evaluate': envelope({ state: 'unavailable' }),
    '/api/v1/style-runs/8/samples': envelope({ items: [] }),
  } })
  wrappers.push(ctx.wrapper)
  return ctx
}
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); localStorage.setItem('yy:p7:last-style-run-id', '8') })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => { if (wrapper.exists()) wrapper.unmount() })
  vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = ''
})

describe('Persisted failure remains in the always-visible summary', () => {
  it('separates formally admitted state from preserved historical failure without making it a current blocker', async () => {
    const ctx = mountFailure({ code: 'E_HISTORICAL', message: 'Original admission failure' }, true)
    await ctx.router.replace('/training'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('补验入库通过，质量待评估')
    expect(ctx.wrapper.get('[data-testid="historical-execution"]').text()).toContain('E_HISTORICAL')
    expect(ctx.wrapper.find('[data-testid="training-blockers"]').exists() ? ctx.wrapper.find('[data-testid="training-blockers"]').text() : '').not.toContain('E_HISTORICAL')
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it('keeps producer code and message when navigating away from the training step', async () => {
    const failure = { code: 'E_TEST_RECORDED_FAILURE', message: 'Recorded transport failure' }
    const ctx = mountFailure(failure); await ctx.router.replace('/training'); await flushPromises()
    expect(ctx.wrapper.text()).toContain('网络通信失败') // Existing progress pane received the transport failure.
    const steps = ctx.wrapper.findAll('nav button')
    expect(steps).toHaveLength(4)
    for (const index of [0, 1, 3]) {
      await steps[index]!.trigger('click')
      await flushPromises()
      expect(ctx.wrapper.get(`[data-testid="training-step-${index + 1}"]`).isVisible()).toBe(true)
      const summary = ctx.wrapper.get('[data-testid="training-blockers"]')
      expect(summary.isVisible()).toBe(true)
      expect(summary.text()).toContain('网络通信失败')
      expect(summary.text()).toContain('网络通信失败')
    }
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it('re-fetches the persisted failure after a page remount without writing or starting anything', async () => {
    const failure = { code: 'E_TEST_PERSISTED', message: 'Persisted transport message' }
    const first = mountFailure(failure); await first.router.replace('/training'); await flushPromises()
    expect(first.wrapper.text()).toContain('网络通信失败')
    first.wrapper.unmount()
    const restored = mountFailure(failure); await restored.router.replace('/training'); await flushPromises()
    expect(restored.wrapper.get('[data-testid="training-blockers"]').text()).toContain('网络通信失败')
    expect(restored.wrapper.get('[data-testid="training-blockers"]').text()).toContain('网络通信失败')
    expect(restored.logs.some(call => call.url === '/api/v1/style-runs/8/progress')).toBe(true)
    expect(restored.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it.each([{ code: 'E_TEST_CODE_ONLY' }, { message: 'Message only transport failure' }])(
    'retains available fields without displaying missing values: %j', async failure => {
      const ctx = mountFailure(failure); await ctx.router.replace('/training'); await flushPromises()
      expect(ctx.wrapper.text()).toContain(failure.code ? '操作暂未完成' : '网络通信失败')
      const text = ctx.wrapper.get('[data-testid="training-blockers"]').text()
      expect(text).toContain(failure.code ? '操作暂未完成' : '网络通信失败')
      expect(text).not.toContain('undefined')
      expect(text).not.toContain('null')
    },
  )
})
