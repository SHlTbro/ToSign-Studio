import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { listPendingRenders } from '@/api/pendingRender'
import { __resetHttpGatesForTest } from '@/api/http'
import { envelope, mountPage } from './_harness'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const wrappers: VueWrapper[] = []
const plan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'a', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 't', base_asset_id: 'a', status: 'approved' }] }),
  '/api/v1/phrase-templates': envelope({ items: [] }), '/api/v1/blogger-name-presets': envelope({ items: [] }),
  '/api/v1/styles': envelope({ items: [] }), '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
}
beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); __resetHttpGatesForTest(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })
async function click(wrapper: VueWrapper, text: string) {
  await wrapper.findAll('button').find(button => button.text() === text)!.trigger('click')
  await flushPromises()
}
describe('P5 request-specific render budget', () => {
  it('explains formal request waiting without claiming rendering started and opens the returned existing job once confirmed', async () => {
    const ctx = mountPage(P5Batch, { plan }); wrappers.push(ctx.wrapper); await flushPromises()
    await ctx.wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
    await ctx.wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    const ordinaryFetch = globalThis.fetch; let confirm!: (response: Response) => void; let posts = 0
    vi.stubGlobal('fetch', vi.fn((path: RequestInfo | URL, options?: RequestInit) => {
      if (String(path) === '/api/v1/render-jobs' && options?.method === 'POST') {
        posts += 1; return new Promise<Response>(resolve => { confirm = resolve })
      }
      return ordinaryFetch(path, options)
    }))
    await click(ctx.wrapper, '正式生成')
    const waiting = ctx.wrapper.get('[data-testid="formal-submission-status"]')
    expect(waiting.attributes('role')).toBe('status')
    expect(waiting.text()).toContain('正在核验并提交')
    expect(waiting.text()).toContain('不表示图片已开始生成')
    expect(waiting.text()).toContain('不要重复提交')
    await vi.advanceTimersByTimeAsync(15001)
    expect(ctx.wrapper.find('[data-testid="formal-submission-status"]').exists()).toBe(true)
    expect(listPendingRenders('2')).toHaveLength(1)
    await click(ctx.wrapper, '创建任务中…'); expect(posts).toBe(1)
    confirm(new Response(JSON.stringify(envelope({ job: { job_id: 'confirmed-existing-job' }, items: [] }).json), { status: 200 }))
    await flushPromises()
    expect(ctx.wrapper.find('[data-testid="formal-submission-status"]').exists()).toBe(false)
    expect(ctx.router.currentRoute.value.path).toBe('/batch/confirmed-existing-job/review')
    expect(listPendingRenders('2')).toHaveLength(0)
    expect(posts).toBe(1)
  })
  it.each(['draft', 'formal'] as const)('keeps the %s POST alive until 60 seconds without automatic replay', async lane => {
    const { wrapper } = mountPage(P5Batch, { plan }); wrappers.push(wrapper)
    await flushPromises()
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
    await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    const ordinaryFetch = globalThis.fetch
    const posts: { body: Record<string, unknown>; key: string; signal: AbortSignal }[] = []
    vi.stubGlobal('fetch', vi.fn((path: RequestInfo | URL, options?: RequestInit) => {
      if (String(path) !== '/api/v1/render-jobs' || options?.method !== 'POST') return ordinaryFetch(path, options)
      posts.push({ body: JSON.parse(String(options.body)), key: new Headers(options.headers).get('Idempotency-Key')!, signal: options.signal as AbortSignal })
      if (posts.length > 1) return Promise.resolve(new Response(JSON.stringify({ data: { job: { job_id: 'explicit-retry', status: 'succeeded' }, items: [] } }), { status: 200 }))
      return new Promise((_, reject) => options.signal!.addEventListener('abort', () => reject(new DOMException('deadline', 'AbortError'))))
    }))
    await click(wrapper, lane === 'draft' ? '预览草稿' : '正式生成')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.key).toBeTruthy()
    expect((posts[0]!.body.items as { cn: string }[])[0]!.cn).toBe('Alice')
    expect(posts[0]!.body.draft === true).toBe(lane === 'draft')
    const original = { key: posts[0]!.key, body: JSON.parse(JSON.stringify(posts[0]!.body)) }
    await vi.advanceTimersByTimeAsync(15_001)
    expect(posts[0]!.signal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(44_998)
    expect(posts[0]!.signal.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1); await flushPromises()
    expect(posts[0]!.signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(10_000); await flushPromises()
    expect(posts).toHaveLength(1)
    if (lane === 'formal') {
      expect(listPendingRenders('2')).toEqual([original])
      await click(wrapper, '重试原提交')
      expect(posts).toHaveLength(2)
      expect({ key: posts[1]!.key, body: posts[1]!.body }).toEqual(original)
      expect(listPendingRenders('2')).toEqual([])
    }
  })
})
