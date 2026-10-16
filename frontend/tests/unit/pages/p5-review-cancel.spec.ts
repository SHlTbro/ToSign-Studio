import { afterEach, describe, expect, it, vi } from 'vitest'
import P5_Review from '@/pages/P5_Review.vue'
import { useAuthStore } from '@/stores/auth'
import { envelope, flushPromises, mountPage, problem, TEST_ADMIN, type FetchPlan } from './_harness'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  sessionStorage.clear()
  localStorage.clear()
  document.body.innerHTML = ''
})

function result(status: string, id = 'job-a') {
  return envelope({ job: { job_id: id, status }, items: [{ item_id: 'one', status: status === 'cancelled' ? 'cancelled' : 'queued', warnings: [], render_version: 1 }] })
}

async function open(plan: FetchPlan) {
  const page = mountPage(P5_Review, { plan })
  await page.router.push('/batch/job-a/review')
  await flushPromises()
  await flushPromises()
  return page
}

describe('formal render cancellation through the review page', () => {
  it('blocks submission when the saved cancellation marker cannot be read', async () => {
    const getItem = Storage.prototype.getItem
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key) {
      if (key.startsWith('yy:render-cancel:')) throw new DOMException('denied', 'SecurityError')
      return getItem.call(this, key)
    })
    const page = await open({ '/api/v1/render-jobs/job-a': result('running') })
    expect(page.wrapper.get('[aria-label="取消当前生成任务"]').attributes('disabled')).toBeDefined()
    expect(page.wrapper.text()).toContain('无法读取取消请求标记')
    await page.wrapper.get('[aria-label="读取原任务取消状态"]').trigger('click')
    await flushPromises()
    expect(page.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    page.wrapper.unmount()
  })

  it('sends no POST when the cancellation marker cannot be saved', async () => {
    const page = await open({ '/api/v1/render-jobs/job-a': result('running') })
    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith('yy:render-cancel:')) throw new DOMException('full', 'QuotaExceededError')
      return setItem.call(this, key, value)
    })
    await page.wrapper.get('[aria-label="取消当前生成任务"]').trigger('click')
    await flushPromises()
    expect(page.wrapper.text()).toContain('无法保存取消请求标记，尚未提交')
    expect(page.wrapper.get('[aria-label="取消当前生成任务"]').attributes('disabled')).toBeDefined()
    expect(page.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    page.wrapper.unmount()
  })

  it('ignores a late cancellation response after switching accounts', async () => {
    const plan: FetchPlan = { '/api/v1/render-jobs/job-a': result('running') }
    const page = await open(plan)
    const originalFetch = globalThis.fetch
    let release!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST' && String(input).endsWith('/cancel')) return new Promise<Response>(resolve => { release = resolve })
      return originalFetch(input, init)
    }))
    await page.wrapper.get('[aria-label="取消当前生成任务"]').trigger('click')
    const readsBeforeSwitch = page.logs.filter(call => call.method === 'GET').length
    plan['/api/v1/render-jobs/job-a'] = problem(404, 'NOT_FOUND', 'not owned')
    useAuthStore(page.pinia).__setUserForTest(TEST_ADMIN)
    await flushPromises()
    expect(page.wrapper.find('[aria-label="取消当前生成任务"]').exists()).toBe(false)
    expect(page.logs.filter(call => call.method === 'GET').length).toBeGreaterThan(readsBeforeSwitch)
    expect(page.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    release(new Response(JSON.stringify(envelope({ job: { job_id: 'job-a', status: 'cancelled' } }).json), { status: 200 }))
    await flushPromises()
    expect(page.wrapper.text()).not.toContain('取消请求已提交')
    expect(page.wrapper.text()).not.toContain('任务已取消')
    page.wrapper.unmount()
  })

  it('submits only once while pending and reads the same original task', async () => {
    const plan = { '/api/v1/render-jobs/job-a': result('running') }
    const page = await open(plan)
    const originalFetch = globalThis.fetch
    let release!: (response: Response) => void
    const posts: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST' && String(input).endsWith('/cancel')) {
        posts.push(String(input))
        return new Promise<Response>(resolve => { release = resolve })
      }
      return originalFetch(input, init)
    }))
    const button = page.wrapper.get('[aria-label="取消当前生成任务"]')
    await button.trigger('click')
    await button.trigger('click')
    expect(posts).toEqual(['/api/v1/render-jobs/job-a/cancel'])
    expect(button.attributes('disabled')).toBeDefined()
    plan['/api/v1/render-jobs/job-a'] = result('cancelled')
    release(new Response(JSON.stringify(envelope({ job: { job_id: 'job-a', status: 'cancelled' } }).json), { status: 200 }))
    await flushPromises()
    await flushPromises()
    expect(page.wrapper.text()).toContain('已取消')
    expect(page.logs.filter(call => call.method === 'GET').every(call => call.url === '/api/v1/render-jobs/job-a')).toBe(true)
    page.wrapper.unmount()
  })

  it('recovers unknown delivery by GET after remount, without automatic POST replay', async () => {
    const plan = { '/api/v1/render-jobs/job-a': result('running') }
    const first = await open(plan)
    const originalFetch = globalThis.fetch
    let posts = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST' && String(input).endsWith('/cancel')) { posts += 1; return Promise.reject(new TypeError('lost reply')) }
      return originalFetch(input, init)
    }))
    await first.wrapper.get('[aria-label="取消当前生成任务"]').trigger('click')
    await flushPromises()
    expect(posts).toBe(1)
    expect(first.wrapper.text()).toContain('取消结果尚未确认')
    first.wrapper.unmount()
    const restored = await open(plan)
    expect(restored.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    expect(restored.wrapper.get('[aria-label="取消当前生成任务"]').attributes('disabled')).toBeDefined()
    plan['/api/v1/render-jobs/job-a'] = result('cancelled')
    await restored.wrapper.get('[aria-label="读取原任务取消状态"]').trigger('click')
    await flushPromises()
    await flushPromises()
    expect(restored.wrapper.text()).toContain('已取消')
    expect(restored.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    restored.wrapper.unmount()
  })

  it('ignores a late cancellation response after moving to another task', async () => {
    const page = await open({ '/api/v1/render-jobs/job-a': result('running'), '/api/v1/render-jobs/job-b': result('running', 'job-b') })
    const originalFetch = globalThis.fetch
    let release!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST' && String(input).endsWith('/cancel')) return new Promise<Response>(resolve => { release = resolve })
      return originalFetch(input, init)
    }))
    await page.wrapper.get('[aria-label="取消当前生成任务"]').trigger('click')
    await page.router.push('/batch/job-b/review')
    await flushPromises()
    release(new Response(JSON.stringify(envelope({ job: { job_id: 'job-a', status: 'cancelled' } }).json), { status: 200 }))
    await flushPromises()
    expect(page.wrapper.text()).not.toContain('任务已取消')
    expect(page.wrapper.get('[aria-label="取消当前生成任务"]').attributes('disabled')).toBeUndefined()
    page.wrapper.unmount()
  })

  it('does not offer cancellation for a terminal task', async () => {
    const page = await open({ '/api/v1/render-jobs/job-a': result('failed') })
    expect(page.wrapper.find('[aria-label="取消当前生成任务"]').exists()).toBe(false)
    expect(page.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    page.wrapper.unmount()
  })
})
