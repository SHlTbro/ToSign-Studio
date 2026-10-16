import { afterEach, expect, it, vi } from 'vitest'
import P5_Review from '@/pages/P5_Review.vue'
import { envelope, flushPromises, mountPage, problem } from './_harness'
import { useAuthStore } from '@/stores/auth'

afterEach(() => {
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks()
  localStorage.clear(); sessionStorage.clear(); document.body.innerHTML = ''
})

it('shows elapsed time and read freshness without claiming a worker heartbeat or creating a task', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-12T01:10:00Z'))
  const mounted = mountPage(P5_Review, { plan: {
    '/api/v1/render-jobs/job-a': envelope({ job: { status: 'running', created_at: '2026-10-12T01:00:00Z', updated_at: '2026-10-12T01:01:00Z' }, items: [] }),
  } })
  await mounted.router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
  expect(mounted.wrapper.text()).toContain('已等待 10分0秒')
  expect(mounted.wrapper.text()).toContain('任务记录仍为生成中')
  expect(mounted.wrapper.text()).toContain('暂无模型阶段进度')
  await vi.advanceTimersByTimeAsync(1000)
  expect(mounted.wrapper.text()).toContain('已等待 10分1秒')
  expect(mounted.logs.filter(row => row.method === 'POST')).toHaveLength(0)
  mounted.wrapper.unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it('shows the first read failure as unconfirmed and recovers to terminal state through GET only', async () => {
  vi.useFakeTimers()
  const plan: Parameters<typeof mountPage>[1]['plan'] = { '/api/v1/render-jobs/job-a': envelope({ job: { status: 'running' }, items: [] }) }
  const mounted = mountPage(P5_Review, { plan })
  await mounted.router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
  plan['/api/v1/render-jobs/job-a'] = problem(503, 'UNAVAILABLE', 'temporarily unavailable')
  await vi.advanceTimersByTimeAsync(2000); await flushPromises()
  expect(mounted.wrapper.text()).toContain('读取失败，当前进度未确认')
  plan['/api/v1/render-jobs/job-a'] = envelope({ job: { status: 'failed' }, items: [] })
  await vi.advanceTimersByTimeAsync(4000); await flushPromises()
  expect(mounted.wrapper.find('[data-testid="generation-wait-status"]').exists()).toBe(false)
  expect(mounted.logs.filter(row => row.method === 'POST')).toHaveLength(0)
  mounted.wrapper.unmount()
})

it('ignores a delayed failure from another job even when both request identities are absent', async () => {
  const mounted = mountPage(P5_Review, { plan: {} })
  const auth = useAuthStore(mounted.pinia)
  auth.__setUserForTest({ user_id: '', username: 'pending', display_name: '', role: 'user', must_change_password: false })
  const ordinaryFetch = globalThis.fetch
  let rejectOld: ((reason: Error) => void) | undefined
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/v1/render-jobs/job-a') return new Promise<Response>((_resolve, reject) => { rejectOld = reject })
    if (String(input) === '/api/v1/render-jobs/job-b') return Promise.resolve(new Response(JSON.stringify(envelope({ job: { status: 'running' }, items: [] }).json), { status: 200 }))
    return ordinaryFetch(input, init)
  }))
  await mounted.router.push('/batch/job-a/review'); await flushPromises()
  await mounted.router.push('/batch/job-b/review'); await flushPromises(); await flushPromises()
  expect(rejectOld).toBeTypeOf('function')
  rejectOld!(new Error('old job disconnected')); await flushPromises()
  expect(mounted.wrapper.text()).not.toContain('读取失败，当前进度未确认')
  expect(mounted.wrapper.text()).toContain('任务记录仍为生成中')
  mounted.wrapper.unmount()
})
