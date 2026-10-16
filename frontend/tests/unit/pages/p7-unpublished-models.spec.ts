import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import UnpublishedModels from '@/components/UnpublishedModels.vue'
import P7Training from '@/pages/P7_Training.vue'
import { useAuthStore } from '@/stores/auth'
import { __resetHttpGatesForTest } from '@/api/http'
import type { User } from '@/types/domain'
import { TEST_USER, envelope, mountPage } from './_harness'

const wrappers: VueWrapper[] = []
const OLD = '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81'
const MANIFEST = '9bbefed5c2c845720a11423e0be241a5b30702a89818c67c1f1c4ffd474e40f2'
const acceptedV3 = () => ({ id: 'user-accepted-0178-v3-20261007-r2', text_lines: ['To：shitT', '拉shit永远通畅'],
  latin_font_sha256: '0bdb6b660482d31531b3945849fba5916b3ef8695da7024a9e6b9ee3c4157988', latin_weight: 600,
  punctuation_font_sha256: 'b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a', punctuation_face_index: 2,
  color: '#8C3044', layout_seed: 2026100401, model_quality_approval: false, publication_approval: false })
const model = (digest = OLD) => ({ import_id: '1'.repeat(32), adapter_sha256: digest, manifest_sha256: '9'.repeat(64),
  input_fingerprint_sha256: '8'.repeat(64), created_at: '2026-10-07T00:00:00+00:00',
  source_summary: '官方离线导入（模型包和资料包）', publication_status: 'UNPUBLISHED', runtime_status: 'NOT_REGISTERED',
  inference_ready: false, publish_ready: false, model_quality_approval: false, verification_scope: 'IMPORT_RECORD_ONLY' })
const list = (items = [model()], page = 1, total = items.length) => ({ items, total, page, page_size: 20, registry_status: 'ready' })
const readyModel = () => ({ ...model(), manifest_sha256: MANIFEST, runtime_status: 'INFERENCE_READY', inference_ready: true,
  inference_selection_id: 7, verification_scope: 'CURRENT_OWNED_QUALIFIED_V3_SELECTION', quality_status: 'V3_LIMITED_USER_ACCEPTED',
  inference_only: true, acceptance_scope: acceptedV3() })
const blockedModel = () => ({ ...model(), manifest_sha256: MANIFEST, runtime_status: 'SELECTED_BLOCKED',
  inference_selection_id: 7, verification_scope: 'CURRENT_SELECTION_RECHECK_BLOCKED' })
const response = (data: unknown) => new Response(JSON.stringify({ data, meta: {}, warnings: [] }), { status: 200 })
function setup(fetcher: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>, account: User | null = TEST_USER) {
  const fetch = vi.fn(fetcher)
  vi.stubGlobal('fetch', fetch)
  const pinia = createPinia(); setActivePinia(pinia)
  const auth = useAuthStore(); auth.__setUserForTest(account)
  const wrapper = mount(UnpublishedModels, { global: { plugins: [pinia] }, attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, auth, fetch }
}
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach(w => { if (w.exists()) w.unmount() }); vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('unpublished models in the existing training page', () => {
  it('keeps the owned model list loading beyond the general fifteen-second budget and accepts the verified result', async () => {
    vi.useFakeTimers()
    let finish!: (result: Response) => void
    const { wrapper, fetch } = setup(async (_input, init) => new Promise<Response>((resolve, reject) => {
      finish = resolve
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    await vi.advanceTimersByTimeAsync(30000); await flushPromises()
    expect(wrapper.text()).toContain('正在读取')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(fetch).toHaveBeenCalledTimes(1)
    finish(response(list([readyModel()]))); await flushPromises()
    expect(wrapper.text()).toContain('推理资格已通过')
    expect(wrapper.text()).toContain('可生成：已认可的 V3 组合')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('ends the model-list wait at six hundred seconds without an automatic retry', async () => {
    vi.useFakeTimers()
    const { wrapper, fetch } = setup(async (_input, init) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    await vi.advanceTimersByTimeAsync(599999); await flushPromises()
    expect(wrapper.text()).toContain('正在读取')
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('请求超时')
    await vi.advanceTimersByTimeAsync(600000); await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('[data-testid="unpublished-model"]')).toHaveLength(0)
  })
  it('shows loading then frozen identity/status and refreshes with GET only', async () => {
    let finish: (result: Response) => void = () => undefined
    const first = new Promise<Response>(resolve => { finish = resolve })
    let calls = 0
    const { wrapper, fetch } = setup(async () => ++calls === 1 ? first : response(list()))
    expect(wrapper.text()).toContain('正在读取')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('button').trigger('click'); await wrapper.get('button').trigger('click')
    expect(fetch).toHaveBeenCalledTimes(1)
    finish(response(list())); await flushPromises()
    expect(wrapper.text()).toContain(OLD)
    expect(wrapper.text()).toContain('未发布'); expect(wrapper.text()).toContain('未注册推理资格')
    expect(wrapper.text()).toContain('不可生成'); expect(wrapper.text()).toContain('模型包和资料包')
    await wrapper.get('button').trigger('click'); await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(2)
    for (const [url, init] of fetch.mock.calls) {
      expect(String(url)).toBe('/api/v1/styles/unpublished-models?page=1&page_size=20')
      expect(init?.method).toBe('GET')
      expect(String(url)).not.toContain('user_id')
    }
    expect(wrapper.findAll('button')).toHaveLength(1)
  })
  it('keeps load failure visible and can refresh an empty list without creating a task', async () => {
    let calls = 0
    const { wrapper, fetch } = setup(async () => ++calls === 1
      ? new Response(JSON.stringify({ code: 'UNPUBLISHED_MODELS_UNAVAILABLE', detail: '导入记录读取失败', retryable: false }), { status: 503 })
      : response(list([])))
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('导入记录读取失败')
    await wrapper.get('button').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('本账户还没有已导入')
    expect(wrapper.findAll('[data-testid="unpublished-model"]')).toHaveLength(0)
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('shows qualified V3 generation separately from unpublished/model quality status', async () => {
    const { wrapper, fetch } = setup(async () => response(list([readyModel()])))
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('推理资格已通过')
    expect(wrapper.text()).toContain('推理资格已通过')
    expect(wrapper.text()).toContain('可生成：已认可的 V3 组合')
    expect(wrapper.get('h3').text()).toBe('经典手写 V3')
    expect(wrapper.get('[data-testid="model-runtime-status"]').text()).toBe('推理资格已通过')
    expect(wrapper.text()).not.toContain('UNPUBLISHED')
    expect(wrapper.find('li > p.select-text').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('未注册推理资格')
    expect(wrapper.text()).not.toContain('尚未完成发布与推理资格验证')
    expect(wrapper.text()).toContain('To：shitT')
    expect(wrapper.text()).toContain('拉shit永远通畅')
    expect(wrapper.text()).not.toContain('artifact_id')
    expect(fetch.mock.calls.every(([, init]) => init?.method === 'GET')).toBe(true)
  })
  it('shows a selected model whose current inference recheck failed as unavailable', async () => {
    const { wrapper } = setup(async () => response(list([blockedModel()])))
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前推理资格未通过')
    expect(wrapper.text()).toContain('当前推理资格未通过')
    expect(wrapper.text()).toContain('不可生成')
    expect(wrapper.text()).not.toContain('可生成：已认可')
    expect(wrapper.text()).toContain('未发布')
  })
  it.each([
    { inference_selection_id: 0 }, { inference_selection_id: -7 }, { inference_selection_id: 7.5 }, { inference_selection_id: '7' },
    { inference_ready: false }, { publish_ready: true }, { model_quality_approval: true },
    { verification_scope: 'IMPORT_RECORD_ONLY' }, { quality_status: 'AI_ACCEPTED_USER_AUTHORIZED' }, { inference_only: false },
    { adapter_sha256: '2'.repeat(64) }, { manifest_sha256: '9'.repeat(64) }, { acceptance_scope: undefined },
    { acceptance_scope: { ...acceptedV3(), text_lines: ['To：other', '其它文案'] } },
    { acceptance_scope: { ...acceptedV3(), latin_weight: 400 } },
    { acceptance_scope: { ...acceptedV3(), punctuation_face_index: 1 } },
    { acceptance_scope: { ...acceptedV3(), latin_font_sha256: '3'.repeat(64) } },
    { acceptance_scope: { ...acceptedV3(), layout_seed: 1 } },
    { acceptance_scope: { ...acceptedV3(), publication_approval: true } },
  ])('rejects a contradictory or expanded inference-only authority: %j', async changed => {
    const { wrapper } = setup(async () => response({ ...list(), items: [{ ...readyModel(), ...changed }] }))
    await flushPromises()
    expect(wrapper.findAll('[data-testid="unpublished-model"]')).toHaveLength(0)
    expect(wrapper.get('[role="alert"]').text()).toContain('无法核验')
  })
  it.each([
    { ...blockedModel(), inference_ready: true }, { ...blockedModel(), inference_selection_id: 0 },
    { ...blockedModel(), verification_scope: 'CURRENT_OWNED_QUALIFIED_V3_SELECTION' },
    { ...model(), inference_selection_id: 7 }, { ...model(), inference_only: true },
  ])('rejects inconsistent blocked or unregistered selection states: %j', async row => {
    const { wrapper } = setup(async () => response({ ...list(), items: [row] }))
    await flushPromises()
    expect(wrapper.findAll('[data-testid="unpublished-model"]')).toHaveLength(0)
    expect(wrapper.get('[role="alert"]').text()).toContain('无法核验')
  })
  it('clears prior data on refresh failure', async () => {
    let calls = 0
    const { wrapper } = setup(async () => ++calls === 1 ? response(list()) : response({ invalid: true }))
    await flushPromises(); expect(wrapper.text()).toContain(OLD)
    await wrapper.get('button').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain(OLD)
    expect(wrapper.get('[role="alert"]').text()).toContain('无法核验')
  })
  it('discards late results after an account switch and removes all data on logout', async () => {
    let finish: (result: Response) => void = () => undefined
    const first = new Promise<Response>(resolve => { finish = resolve })
    let calls = 0
    const { wrapper, auth, fetch } = setup(async () => ++calls === 1 ? first : response(list([model('2'.repeat(64))])))
    auth.__setUserForTest({ ...TEST_USER, user_id: '99' }); await flushPromises()
    expect(wrapper.text()).toContain('2'.repeat(64))
    finish(response(list())); await flushPromises()
    expect(wrapper.text()).not.toContain(OLD)
    auth.__setUserForTest(null); await flushPromises()
    expect(wrapper.text()).not.toContain('2'.repeat(64))
    expect(wrapper.text()).toContain('请先登录')
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('does not fetch for an unauthenticated account', async () => {
    const { wrapper, fetch } = setup(async () => response(list()), null)
    await flushPromises()
    expect(fetch).not.toHaveBeenCalled(); expect(wrapper.text()).toContain('请先登录')
  })
  it.each([
    { inference_ready: true }, { publish_ready: true }, { model_quality_approval: true },
    { publication_status: 'PUBLISHED' }, { runtime_status: 'REGISTERED' },
  ])('never displays a changed authority contract as a usable model: %j', async changed => {
    const { wrapper } = setup(async () => response({ ...list(), items: [{ ...model(), ...changed }] }))
    await flushPromises()
    expect(wrapper.findAll('[data-testid="unpublished-model"]')).toHaveLength(0)
    expect(wrapper.get('[role="alert"]').text()).toContain('无法核验')
  })
  it('reloads from the API on remount and pages within the current session', async () => {
    const ctx = setup(async input => response(list([model()], String(input).includes('page=2') ? 2 : 1, 21)))
    await flushPromises()
    const next = ctx.wrapper.findAll('button').find(b => b.text() === '下一页')!
    await next.trigger('click'); await flushPromises()
    expect(ctx.fetch.mock.calls[1]?.[0]).toContain('page=2')
    ctx.wrapper.unmount()
    const restored = setup(async () => response(list()))
    await flushPromises(); expect(restored.fetch).toHaveBeenCalledTimes(1)
    expect(restored.wrapper.text()).toContain(OLD)
  })
  it('is visible on the actual P7 page across all four steps without publish/start calls', async () => {
    const ctx = mountPage(P7Training, { plan: {
      '/api/v1/styles/unpublished-models': envelope(list()),
      '/api/v1/styles': envelope({ items: [] }),
      '/api/v1/training-sets': envelope({ items: [], page: 1, page_size: 24, total: 0 }),
      '/api/v1/training-samples': envelope({ items: [], page: 1, page_size: 100, total: 0 }),
    } }); wrappers.push(ctx.wrapper)
    await ctx.router.replace('/training'); await flushPromises()
    for (let step = 1; step <= 4; step++) {
      await ctx.wrapper.get(`[data-testid="choose-step-${step}"]`).trigger('click')
      expect(ctx.wrapper.get('[data-testid="unpublished-model-list"]').isVisible()).toBe(true)
      expect(ctx.wrapper.get('[data-testid="unpublished-model-list"]').text()).toContain(OLD)
    }
    expect(ctx.logs.filter(call => call.method !== 'GET')).toHaveLength(0)
  })
  it('returns to a valid page when imported records disappear during a refresh', async () => {
    let calls = 0
    const { wrapper, fetch } = setup(async input => {
      calls += 1
      return response(String(input).includes('page=2')
        ? list([], 2, 1) : list([model()], 1, calls === 1 ? 21 : 1))
    })
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '下一页')!.trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain(OLD)
    expect(wrapper.text()).not.toContain('本账户还没有已导入')
  })
})
