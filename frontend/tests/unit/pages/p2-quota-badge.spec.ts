/**
 * B1-07：P2_Gallery 头部配额徽章接真 usage。
 * - 挂载时 GET /api/v1/usage → total_bytes/limit_bytes 经 formatBytes 展示（内容正确性：逐字节断言）；
 * - usage 失败降级 `-- / --`（不阻塞资产列表渲染）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P2_Gallery from '@/pages/P2_Gallery.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const ASSET = {
  asset_id: 'ast-1', type: 'cos', status: 'pending', sha256: 'a'.repeat(64),
  bytes: 1024, width: 2048, height: 2048, thumbnail_path: 't.png',
  created_at: '2026-09-21T00:00:00Z',
}

function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/assets': envelope({ items: [ASSET] }, { total: 1, page: 1, page_size: 60 }),
    ...extra,
  }
}

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('P2_Gallery 配额徽章（B1-07）', () => {
  it('原图资产台仅显示生成底图，不混入训练参考图', async () => {
    const { wrapper, logs } = mountPage(P2_Gallery, { asAdmin: false, plan: {
      '/api/v1/assets': envelope({ items: [ASSET, { ...ASSET, asset_id: 'reference-1', type: 'sample_source' }] }, { total: 2, page: 1, page_size: 60 }),
      '/api/v1/usage': envelope({ total_bytes: 2048, limit_bytes: 5368709120, pools: [] }),
    } })
    await flushPromises()
    expect(logs.some(log => log.url.includes('/api/v1/assets?type=cos&'))).toBe(true)
    expect(wrapper.find('button[aria-label*="reference-1"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('生成底图 · 需无 To签')
    expect(wrapper.text()).not.toContain('Cos Raw / No Text')
    expect(wrapper.text()).toContain('训练参考图请到训练页查看')
  })
  it('挂载时 GET /api/v1/usage 并渲染真数据 1.2 / 5.0 GB（内容正确性）', async () => {
    const usagePlan = plan({
      '/api/v1/usage': envelope({
        total_bytes: 1288490189, // 1.2 GiB
        limit_bytes: 5368709120, // 5.0 GiB
        pools: [],
      }),
    })
    const { wrapper, logs } = mountPage(P2_Gallery, { asAdmin: false, plan: usagePlan })
    await flushPromises()
    const usageCall = logs.find((l) => l.method === 'GET' && l.url === '/api/v1/usage')
    expect(usageCall).toBeTruthy()
    // 逐字节内容正确性：不是任意文本，而是 formatBytes(total)/formatBytes(limit) 的精确产物
    expect(wrapper.text()).toContain('1.2 GB / 5.0 GB')
  })

  it('usage 失败降级 -- / --，且不阻塞资产列表渲染', async () => {
    const usageFailPlan = plan({
      '/api/v1/usage': problem(500, 'INTERNAL', 'usage boom'),
    })
    const { wrapper } = mountPage(P2_Gallery, { asAdmin: false, plan: usageFailPlan })
    await flushPromises()
    expect(wrapper.text()).toContain('-- / --')
    // 列表未被 usage 失败阻塞：资产卡片仍在（状态徽章 + FAB 上传入口）
    expect(wrapper.text()).toContain('待审核')
    expect(wrapper.find('button[aria-label="上传生成底图"]').exists()).toBe(true)
  })
})
