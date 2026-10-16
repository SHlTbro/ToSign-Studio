/**
 * G58B-02：P2_Gallery 审批入口 admin 可见、非 admin 不可见；审批调用真端点
 * POST /assets/{id}/approve|reject 并刷新列表（D-T12-3）。
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

async function openDetail(wrapper: ReturnType<typeof mountPage>['wrapper']): Promise<void> {
  await wrapper.find('button[aria-label^="资产 ast-1"]').trigger('click')
  await flushPromises()
}

describe('P2_Gallery 审批入口（G58B-02）', () => {
  it('资产台页头提供返回首页入口', async () => {
    const { wrapper } = mountPage(P2_Gallery, { plan: plan() })
    await flushPromises()
    const homeLink = wrapper.find('a[aria-label="返回首页"]')
    expect(homeLink.exists()).toBe(true)
    expect(homeLink.attributes('href')).toBe('/')
    expect(homeLink.text()).toContain('首页')
  })

  it('缩略图不可用时回退到原图文件预览并说明最终失败', async () => {
    const { wrapper } = mountPage(P2_Gallery, { plan: plan() })
    await flushPromises()
    const preview = wrapper.find('img[alt^="原图预览"]')
    expect(preview.attributes('src')).toBe('/api/v1/assets/ast-1/thumbnail')

    await preview.trigger('error')
    expect(wrapper.find('img[alt^="原图预览"]').attributes('src')).toBe('/api/v1/assets/ast-1/file')

    await wrapper.find('img[alt^="原图预览"]').trigger('error')
    expect(wrapper.find('[role="img"][aria-label="原图预览加载失败"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('原图预览暂不可用')
  })

  it('从批量生成跳转时选中待审批筛选并提示待审核状态', async () => {
    const { wrapper, router } = mountPage(P2_Gallery, { asAdmin: true, plan: plan() })
    await router.push({ path: '/assets', query: { filter: 'pending_review', from: 'batch-upload' } })
    await flushPromises()
    expect(wrapper.text()).toContain('新上传底图已列在「待审批」中')
    expect(wrapper.findAll('button').find((b) => b.text().includes('待审批'))?.attributes('aria-pressed')).toBe('true')
    expect(wrapper.find('button[aria-label^="资产 ast-1"]').exists()).toBe(true)
  })

  it('admin 可见审批双决策按钮', async () => {
    const { wrapper } = mountPage(P2_Gallery, { asAdmin: true, plan: plan() })
    await flushPromises()
    await openDetail(wrapper)
    expect(wrapper.text()).toContain('通过入库')
    expect(wrapper.text()).toContain('驳回资产')
  })

  it('非 admin 不可见审批按钮，仅只读视图提示', async () => {
    const { wrapper } = mountPage(P2_Gallery, { asAdmin: false, plan: plan() })
    await flushPromises()
    await openDetail(wrapper)
    expect(wrapper.text()).not.toContain('通过入库')
    expect(wrapper.text()).not.toContain('驳回资产')
    expect(wrapper.text()).toContain('审批由管理员执行')
  })

  it('admin 点通过入库调用 POST /assets/{id}/approve 并刷新列表', async () => {
    const approvePlan = plan({
      '/api/v1/assets/ast-1/approve': envelope({ asset_id: 'ast-1', status: 'approved' }),
    })
    const { wrapper, logs } = mountPage(P2_Gallery, { asAdmin: true, plan: approvePlan })
    await flushPromises()
    await openDetail(wrapper)
    const approveBtn = wrapper.findAll('button').find((b) => b.text().includes('通过入库'))!
    await approveBtn.trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/approve'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    // 审批后重新拉取列表（状态刷新）
    expect(logs.filter((l) => l.method === 'GET' && l.url.startsWith('/api/v1/assets')).length).toBeGreaterThanOrEqual(2)
  })

  it('确认删除调用真实 DELETE 端点并从当前资产台移除', async () => {
    const { wrapper, logs } = mountPage(P2_Gallery, {
      plan: plan({ '/api/v1/assets/ast-1': envelope({ asset_id: 'ast-1', deleted: 'soft' }) }),
    })
    await flushPromises()
    await openDetail(wrapper)
    await wrapper.find('button[aria-label="删除资产"]').trigger('click')
    expect(wrapper.find('[role="alertdialog"][aria-label="确认删除资产"]').exists()).toBe(true)
    await wrapper.find('button[aria-label="确认删除资产"]').trigger('click')
    await flushPromises()
    const deletion = logs.find((log) => log.url === '/api/v1/assets/ast-1' && log.method === 'DELETE')
    expect(deletion).toBeTruthy()
    expect(wrapper.find('button[aria-label^="资产 ast-1"]').exists()).toBe(false)
    expect(wrapper.find('[role="dialog"][aria-label="资产详情与审核"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('原图已从资产库移除')
  })

  it('审批冲突 409 时提示错误信息（RFC 9457 解析）', async () => {
    const { wrapper } = mountPage(P2_Gallery, {
      asAdmin: true,
      plan: plan({ '/api/v1/assets/ast-1/approve': problem(409, 'INVALID_REQUEST', 'cannot approve from approved') }),
    })
    await flushPromises()
    await openDetail(wrapper)
    const approveBtn = wrapper.findAll('button').find((b) => b.text().includes('通过入库'))!
    await approveBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('审批失败')
  })
})
