import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P7 from '@/pages/P7_Training.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const styles = [1, 2].map(style_id => ({ style_id, name: `本人风格${style_id}`, source_type: 'HANDWRITING_ASSET', status: 'approved' }))
const candidate = (sample_id = 'a', style_id = 1, overrides = {}) => ({ sample_id, style_id, text_label: '永', source_asset_id: 'source', source_family_id: 'family', source_training_image_id: 1, final_alpha_sha256: 'a'.repeat(64), provenance_complete: true, review_status: 'candidate', ...overrides })
const planFor = (candidates = [candidate()]): FetchPlan => ({
  '/api/v1/styles?page=1&page_size=100': envelope({ items: styles }),
  '/api/v1/training-sets': envelope({ items: [{ image_id: 1, file_name: 'engineering.png', width: 256, height: 256, size_bytes: 1200 }], page: 1, total: 1 }),
  '/api/v1/styles/1/samples': envelope({ items: [{ sample_id: 11, style_id: 1, text_label: '永', source_training_image_id: 1 }] }),
  '/api/v1/styles/2/samples': envelope({ items: [] }),
  '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=candidate': envelope({ items: candidates, total: candidates.length }),
  '/api/v1/training-samples?style_id=2&page=1&page_size=100&review_status=candidate': envelope({ items: [candidate('b', 2)], total: 1 }),
  '/api/v1/training-samples': envelope({ items: [], total: 0 }),
})
let wrappers: VueWrapper[] = []
function page(plan = planFor()) {
  const mounted = mountPage(P7, { plan })
  wrappers.push(mounted.wrapper)
  return mounted
}
function button(wrapper: VueWrapper, text: string) { return wrapper.findAll('button').find(b => b.text() === text)! }
async function enableManual(wrapper: VueWrapper) { const label = wrapper.findAll('label').find(l => l.text().includes('本人手动复核（')); if (label) { const input = label.find('input'); if (!(input.element as HTMLInputElement).checked) await input.setValue(true) } }
function response(data: unknown, status = 200) { return new Response(JSON.stringify(envelope(data).json), { status }) }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', '') } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open') } })
})
afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers = []; vi.restoreAllMocks(); vi.unstubAllGlobals(); Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal'); Reflect.deleteProperty(HTMLDialogElement.prototype, 'close'); document.body.innerHTML = '' })

describe('P7 explicit Owner review (engineering fixtures, no M2/Owner quality evidence)', () => {
  it('captures corrected labels, allows cancel/ESC without writing, and requires explicit acknowledgement', async () => {
    const { wrapper, logs } = page()
    await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="修正候选字符 a"]').setValue('安')
    await wrapper.find('[aria-label="选择候选 a"]').setValue(true)
    await button(wrapper, '确认选中的 1 个字符').trigger('click')
    await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('dialog').text()).toContain('安 · a')
    expect(button(wrapper, '提交本人复核').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[aria-label="风格档案"]').attributes('disabled')).toBeDefined()
    await wrapper.find('dialog').trigger('cancel')
    expect(wrapper.find('dialog').attributes('open')).toBeUndefined()
    expect(logs.filter(l => l.method === 'POST')).toHaveLength(0)
    expect((wrapper.find('[aria-label="修正候选字符 a"]').element as HTMLInputElement).value).toBe('安')
  })

  it('submits a single fixed batch despite duplicate clicks and clears reviewed candidates by rereading', async () => {
    const plan = planFor()
    const { wrapper } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    const gate = deferred<Response>()
    const underlying = globalThis.fetch
    const writes: unknown[] = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/batch-review')) { writes.push(JSON.parse(String(init?.body))); return gate.promise }
      return underlying(input, init)
    })
    await button(wrapper, '选择前 1 条').trigger('click')
    await wrapper.find('[aria-label="修正候选字符 a"]').setValue('安')
    await button(wrapper, '确认选中的 1 个字符').trigger('click')
    await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="本人已逐项复核"]').setValue(true)
    const submit = button(wrapper, '提交本人复核')
    await submit.trigger('click'); await submit.trigger('click')
    expect(writes).toEqual([{ style_id: 1, items: [{ sample_id: 'a', decision: 'confirm', text_label: '安' }] }])
    expect(button(wrapper, '继续编辑').attributes('disabled')).toBeDefined()
    plan['/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=candidate'] = envelope({ items: [], total: 0 })
    gate.resolve(response({ confirmed: 1, rejected: 0 }))
    await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('dialog').attributes('open')).toBeUndefined()
    expect(wrapper.text()).toContain('暂无候选')
  })

  it('requires a real rejection reason while allowing incomplete provenance to be excluded', async () => {
    const plan = planFor([candidate('a', 1, { provenance_complete: false })])
    plan['/api/v1/training-samples/batch-review'] = envelope({ confirmed: 0, rejected: 1 })
    const { wrapper, logs } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="选择候选 a"]').setValue(true)
    expect(button(wrapper, '确认选中的 1 个字符').attributes('disabled')).toBeDefined()
    await button(wrapper, '排除选中的 1 个候选').trigger('click')
    await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="本人已逐项复核"]').setValue(true)
    expect(button(wrapper, '提交本人复核').attributes('disabled')).toBeDefined()
    await wrapper.find('[aria-label="排除理由"]').setValue('  裁剪包含背景  ')
    await button(wrapper, '提交本人复核').trigger('click')
    await flushPromises(); await enableManual(wrapper)
    expect(logs.find(l => l.method === 'POST')?.body).toEqual({ style_id: 1, items: [{ sample_id: 'a', decision: 'reject', reason: '裁剪包含背景' }] })
  })

  it('shows stale review errors inside the dialog and blocks blind retry', async () => {
    const plan = planFor()
    plan['/api/v1/training-samples/batch-review'] = problem(409, 'SAMPLE_ALREADY_REVIEWED', 'already reviewed')
    const { wrapper, logs } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    await button(wrapper, '选择前 1 条').trigger('click')
    await button(wrapper, '确认选中的 1 个字符').trigger('click'); await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="本人已逐项复核"]').setValue(true)
    await button(wrapper, '提交本人复核').trigger('click'); await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('dialog [role="alert"]').text()).toContain('此字样已审核，请刷新读取最新审核结果。')
    expect(button(wrapper, '提交本人复核').attributes('disabled')).toBeDefined()
    expect(logs.filter(l => l.method === 'POST')).toHaveLength(1)
  })

  it('discards a delayed old style response after switching styles', async () => {
    const { wrapper } = page()
    const underlying = globalThis.fetch
    const gate = deferred<Response>()
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).includes('style_id=1') && String(input).endsWith('review_status=candidate') ? gate.promise : underlying(input, init))
    await flushPromises(); await enableManual(wrapper)
    await wrapper.find('[aria-label="风格档案"]').setValue(2)
    await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('[aria-label="选择候选 b"]').exists()).toBe(true)
    gate.resolve(response({ items: [candidate('old')], total: 1 })); await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('[aria-label="选择候选 old"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="选择候选 b"]').exists()).toBe(true)
  })

  it('shows read failure as a retryable error rather than an empty candidate pool', async () => {
    const plan = planFor()
    const key = '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=candidate'
    plan[key] = problem(403, 'FORBIDDEN', 'no access')
    const { wrapper } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    expect(wrapper.text()).toContain('候选读取失败')
    expect(wrapper.text()).not.toContain('暂无候选')
    plan[key] = envelope({ items: [candidate()], total: 1 })
    await button(wrapper, '重试读取候选').trigger('click'); await flushPromises(); await enableManual(wrapper)
    expect(wrapper.find('[aria-label="选择候选 a"]').exists()).toBe(true)
  })

  it('blocks confirmation for unapproved styles and limits bulk selection to 100', async () => {
    const plan = planFor(Array.from({ length: 101 }, (_, i) => candidate(String(i))))
    plan['/api/v1/styles?page=1&page_size=100'] = envelope({ items: [{ ...styles[0], status: 'pending_review' }] })
    const { wrapper } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    await button(wrapper, '选择前 100 条').trigger('click')
    expect(wrapper.findAll('input[aria-label^="选择候选 "]').filter(c => (c.element as HTMLInputElement).checked)).toHaveLength(100)
    expect(button(wrapper, '确认选中的 100 个字符').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '排除选中的 100 个候选').attributes('disabled')).toBeUndefined()
  })
})

describe('P7 run refresh and mutation isolation', () => {
  const run = (status = 'queued') => ({ run_id: 23, status, config: { engine: 'm1', style_id: 1 }, sample_ids: [11] })
  const runPlan = (): FetchPlan => ({ ...planFor(), '/api/v1/style-runs/23': envelope(run()), '/api/v1/style-runs/23/progress': envelope({ stage: 'queued', units_done: 0 }), '/api/v1/style-runs/23/metrics': envelope({ state: 'pending' }), '/api/v1/style-runs/23/samples': envelope({ items: [] }), '/api/v1/style-runs/23/evaluate': envelope({ state: 'unavailable' }) })
  it('allows the Windows signed-artifact readiness check its bounded verification budget', async () => {
    localStorage.setItem('yy:p7:last-style-run-id', '23')
    const plan = runPlan()
    plan['/api/v1/style-runs/23'] = envelope({ ...run('succeeded'), config: { engine: 'm2', style_id: 1, recipe_id: 'm2-scr2n-user-windows-spatial-v1' } })
    plan['/api/v1/style-runs/23/readiness'] = envelope({ status: 'BLOCKED', gates: {}, blockers: {}, details: [], allowed_actions: { evaluate: true }, evaluation_state: 'idle' })
    const timer = vi.spyOn(window, 'setTimeout')
    const { logs } = page(plan)
    const underlying = globalThis.fetch
    let readinessTimeout: unknown
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/23/readiness')) readinessTimeout = timer.mock.calls.at(-1)?.[1]
      return underlying(input, init)
    })
    await flushPromises()
    expect(logs.some(l => l.url.includes('/23/readiness'))).toBe(true)
    expect(readinessTimeout).toBe(600000)
  })
  it('locks creation until saved run recovery completes, then cancels once and rereads terminal state', async () => {
    localStorage.setItem('yy:p7:last-style-run-id', '23')
    const plan = runPlan()
    const { wrapper } = page(plan)
    const underlying = globalThis.fetch
    const gate = deferred<Response>()
    let cancellationCount = 0
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/23/cancel')) { cancellationCount++; return gate.promise }
      return underlying(input, init)
    })
    expect(wrapper.find('[role="status"]').text()).toContain('恢复档案')
    await flushPromises(); await enableManual(wrapper)
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    const cancel = button(wrapper, '取消运行')
    await cancel.trigger('click'); await cancel.trigger('click')
    expect(cancellationCount).toBe(1)
    plan['/api/v1/style-runs/23'] = envelope(run('cancelled'))
    gate.resolve(response(run('cancelled'))); await flushPromises(); await enableManual(wrapper)
    expect(wrapper.text()).toContain('已取消 · #23')
    expect(wrapper.findAll('button').some(b => b.text() === '取消运行')).toBe(false)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('23')
  })
  it('preserves an unresolved run pointer on recovery failure and requires retry before creating', async () => {
    localStorage.setItem('yy:p7:last-style-run-id', '23')
    const plan = runPlan()
    plan['/api/v1/style-runs/23'] = problem(403, 'FORBIDDEN', 'blocked')
    const { wrapper } = page(plan)
    await flushPromises(); await enableManual(wrapper)
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="run-recovery-error"]').text()).toContain('尚未恢复')
    plan['/api/v1/style-runs/23'] = envelope(run())
    await button(wrapper, '重试恢复上次运行').trigger('click'); await flushPromises(); await enableManual(wrapper)
    expect(wrapper.text()).toContain('排队中 · #23')
    expect(wrapper.text()).not.toContain('尚未恢复')
  })
})
