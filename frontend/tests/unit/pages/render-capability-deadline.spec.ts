import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import RenderStyleControls from '@/components/RenderStyleControls.vue'
import { __resetHttpGatesForTest, request } from '@/api/http'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const wrappers: VueWrapper[] = []
const json = (status: number, data: unknown) => new Response(JSON.stringify({ data }), { status, headers: { 'content-type': 'application/json' } })
beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); __resetHttpGatesForTest(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear() })
function controls(capability: (signal: AbortSignal) => Promise<Response>) {
  const fetch = vi.fn((path: RequestInfo | URL, options?: RequestInit) => String(path).startsWith('/api/v1/styles')
    ? Promise.resolve(json(200, { items: [] })) : capability(options!.signal as AbortSignal))
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(RenderStyleControls, { props: { disabled: false, scopeKey: 'deadline-test' } })
  wrappers.push(wrapper)
  return { wrapper, fetch }
}
describe('bounded capabilities reader', () => {
  it('accepts a 20 second capability response through the real HTTP reader', async () => {
    let signal!: AbortSignal
    const { wrapper } = controls(actual => { signal = actual; return new Promise(resolve => window.setTimeout(() => resolve(json(200, SYSTEM_CAPABILITIES)), 20_000)) })
    await vi.advanceTimersByTimeAsync(15_001)
    expect(signal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(5_000); await flushPromises()
    expect(wrapper.emitted('ready')?.at(-1)?.[0]).toMatchObject({ style_mode: 'system', glyph_source_policy: 'fallback_allowed' })
  })
  it('aborts once at 60 seconds and does not retry the expensive read', async () => {
    const { wrapper, fetch } = controls(signal => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('deadline', 'AbortError')))))
    await vi.advanceTimersByTimeAsync(59_999)
    expect(wrapper.text()).not.toContain('请求超时')
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(wrapper.text()).toContain('请求超时')
    await vi.advanceTimersByTimeAsync(10_000); await flushPromises()
    expect(fetch.mock.calls.filter(([path]) => String(path).includes('/render-jobs/capabilities'))).toHaveLength(1)
  })
  it('does not retry a 503 capability failure', async () => {
    const { wrapper, fetch } = controls(async () => new Response(JSON.stringify({ code: 'WORKER_UNAVAILABLE' }), { status: 503 }))
    await flushPromises(); await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.text()).toContain('处理服务暂不可用，请稍后刷新核对。')
    expect(fetch.mock.calls.filter(([path]) => String(path).includes('/render-jobs/capabilities'))).toHaveLength(1)
  })
  it('keeps the ordinary HTTP default at 15 seconds', async () => {
    vi.stubGlobal('fetch', vi.fn((_: unknown, options: RequestInit) => new Promise((_, reject) => options.signal!.addEventListener('abort', () => reject(new DOMException('deadline', 'AbortError'))))))
    const response = request('/api/v1/ordinary-reader', { retries: 0 }).catch(error => error)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(await response).toMatchObject({ code: 'TIMEOUT' })
  })
})
