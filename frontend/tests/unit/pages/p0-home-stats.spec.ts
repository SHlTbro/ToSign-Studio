import { afterEach, describe, expect, it, vi } from 'vitest'
import P0_Home from '@/pages/P0_Home.vue'
import { flushPromises, mountPage, problem, type FetchPlan } from './_harness'

function response(data: unknown, total?: number) {
  return {
    status: 200,
    json: { data, meta: { api_version: 'v1', schema_version: '0.4', ...(total === undefined ? {} : { total }) }, warnings: [] },
  }
}

function plan(overrides: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/assets': response({ items: [] }, 18),
    '/api/v1/styles': response({ items: [] }, 3),
    '/api/v1/render-jobs': response({ items: [] }, 2),
    '/api/v1/training-sets': response({ items: [], total: 24 }),
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('P0 首页统计消费者', () => {
  it('生成底图计数排除训练参考图', async () => {
    const { wrapper, logs } = mountPage(P0_Home, { plan: plan({
      '/api/v1/assets': response({ items: [] }, 12),
      '/api/v1/assets?type=cos&page=1&page_size=1': response({ items: [] }, 2),
    }) })
    await flushPromises()
    expect(logs.some(call => call.url === '/api/v1/assets?type=cos&page=1&page_size=1')).toBe(true)
    expect(wrapper.text()).toContain('2 raw')
    expect(wrapper.text()).not.toContain('12 raw')
  })
  it('读列表 meta.total 与训练集 data.total，保持各自真实计数', async () => {
    const renderPlan: FetchPlan = {
      '/api/v1/render-jobs?status=succeeded&page=1&page_size=100': response({ items: [{ job_id: 'job-a' }, { job_id: 'job-b' }] }, 2),
      '/api/v1/render-jobs?status=succeeded&page=2&page_size=100': response({ items: [] }, 2),
      '/api/v1/render-jobs?status=partial&page=1&page_size=100': response({ items: [{ job_id: 'job-c' }] }, 1),
      '/api/v1/render-jobs?status=partial&page=2&page_size=100': response({ items: [] }, 1),
      '/api/v1/render-jobs/job-a': response({ job: { status: 'succeeded' }, items: [{ status: 'completed' }, { status: 'approved' }] }),
      '/api/v1/render-jobs/job-b': response({ job: { status: 'failed' }, items: [{ status: 'completed' }] }),
      '/api/v1/render-jobs/job-c': response({ job: { status: 'partial' }, items: [{ status: 'completed_with_warning' }, { status: 'rejected' }] }),
    }
    const { wrapper, logs } = mountPage(P0_Home, { plan: plan(renderPlan) })
    await flushPromises()
    const getUrls = logs.filter((call) => call.method === 'GET').map((call) => call.url)
    expect(getUrls).toContain('/api/v1/render-jobs?status=succeeded&page=1&page_size=100')
    expect(getUrls).toContain('/api/v1/render-jobs?status=partial&page=1&page_size=100')
    expect(getUrls).toContain('/api/v1/render-jobs/job-a')
    expect(getUrls).toContain('/api/v1/render-jobs/job-b')
    expect(getUrls).toContain('/api/v1/render-jobs/job-c')
    expect(getUrls.some((url) => url.includes('pending_review'))).toBe(false)
    expect(wrapper.text()).toContain('18 raw')
    expect(wrapper.text()).toContain('3 个启用')
    expect(wrapper.text()).toContain('2 待审')
    expect(wrapper.text()).toContain('24')
    expect(wrapper.find('a[href="/batch"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/batch/job-0001/review"]').exists()).toBe(false)
  })

  it('统计 API 失败时显示未知，不伪报 0', async () => {
    const failed = problem(400, 'BAD_REQUEST', 'unavailable')
    const { wrapper } = mountPage(P0_Home, {
      plan: plan({
        '/api/v1/assets': failed,
        '/api/v1/styles': failed,
        '/api/v1/render-jobs?status=succeeded&page=1&page_size=100': failed,
        '/api/v1/render-jobs?status=partial&page=1&page_size=100': failed,
        '/api/v1/training-sets': failed,
      }),
    })
    await flushPromises()
    expect(wrapper.text()).toContain('-- raw')
    expect(wrapper.text()).toContain('-- 个启用')
    expect(wrapper.text()).toContain('待审--件')
    expect(wrapper.text()).toContain('--')
  })
})
