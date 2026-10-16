/**
 * B1-06：P11 Worker 三卡接 GET /workers/status 真数据。
 * - 真数据渲染：三卡 + 顶栏显示端点返回的实数；
 * - 失败降级：遥测请求失败时三卡退回 -- 占位，且不阻塞备份列表加载；
 * - 调用形状：GET /api/v1/workers/status（admin 页面挂载时发起）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P11_Backup from '@/pages/P11_Backup.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

const WORKERS_STATUS = {
  worker_id: 'render-worker-01',
  queue_depth: 2,
  running_tasks: 1,
  max_concurrency: 1,
  worker_count: 2,
  gpu_available: false,
  last_heartbeat: '2026-09-22T01:00:00+00:00',
  workers: [
    { worker_id: 'render-worker-01', alive: true, queue_depth: 2, running_tasks: 1, max_concurrency: 1 },
    { worker_id: 'backup-worker-01', alive: true, queue_depth: 0, running_tasks: 0, max_concurrency: 1 },
  ],
}

const backups = {
  items: [
    { backup_id: 'bk-1', status: 'succeeded', created_at: '2026-09-21T03:00:00Z', size_bytes: 1024 * 1024 * 12, includes: ['assets'], checksum: 'd3f1a7c2e9b44012abcd', path_relative: '_backups/bk-1' },
  ],
}

function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/backups': envelope(backups),
    '/api/v1/workers/status': envelope(WORKERS_STATUS),
    ...extra,
  }
}

describe('P11 Worker 三卡接 /workers/status 真数据（B1-06）', () => {
  it('挂载时请求 GET /api/v1/workers/status 并把实数渲染进三卡与顶栏', async () => {
    const { wrapper, logs } = mountPage(P11_Backup, { asAdmin: true, plan: plan() })
    await flushPromises()
    expect(logs.some((l) => l.method === 'GET' && l.url.endsWith('/api/v1/workers/status'))).toBe(true)
    const running = wrapper.find('[data-test="card-running"]')
    const queue = wrapper.find('[data-test="card-queue"]')
    const concurrency = wrapper.find('[data-test="card-concurrency"]')
    expect(running.text()).toContain('1')
    expect(queue.text()).toContain('2')
    expect(concurrency.text()).toContain('1')
    // 顶栏 worker 标识来自端点实数（旧占位是 worker-01）
    expect(wrapper.text()).toContain('render-worker-01 · queue 2')
    // 不应残留 -- 占位
    expect(wrapper.text()).not.toContain('--')
  })

  it('遥测失败时三卡降级 -- 占位，且备份列表照常加载（不阻塞）', async () => {
    const { wrapper } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: plan({ '/api/v1/workers/status': problem(403, 'FORBIDDEN', 'admin role required') }),
    })
    await flushPromises()
    expect(wrapper.find('[data-test="card-running"]').text()).toContain('--')
    expect(wrapper.find('[data-test="card-queue"]').text()).toContain('--')
    expect(wrapper.find('[data-test="card-concurrency"]').text()).toContain('--')
    expect(wrapper.text()).toContain('bk-1')
    expect(wrapper.text()).toContain('12.0 MB')
  })

  it('非 admin 用户不渲染遥测页（守卫 404 重定向语义保持）', async () => {
    const { wrapper, router } = mountPage(P11_Backup, { asAdmin: false, plan: plan() })
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/404')
    expect(wrapper.find('[data-test="worker-cards"]').exists()).toBe(false)
  })
})
