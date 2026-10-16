/**
 * B1-03（D-T69-2 关闭）：P6_Styles 启停 Switch 接真端点的行为测试。
 * - 成功：POST /api/v1/styles/{id}/enable|disable，状态以服务端响应为准；
 * - 失败：乐观翻转回滚 + 行内错误提示（RFC 9457 code:detail）；
 * - 非 owner：404 STYLE_NOT_FOUND 错误态展示且回滚。
 * 新文件（既有测试零改动；p6-p9-wiring.spec.ts 的只读断言改写系编排者 2026-09-22 单独授权）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P6_Styles from '@/pages/P6_Styles.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

const styles = {
  items: [
    { style_id: 1, name: '我的手写风格', source_type: 'HANDWRITING_ASSET', status: 'approved', coverage: 0.92, model_version: 'hw-1', receipt_id: 'r1', font_url: null, enabled: true, reject_reason: null, created_at: '2026-09-21T00:00:00Z' },
    { style_id: 2, name: '停用手写风格', source_type: 'HANDWRITING_ASSET', status: 'approved', coverage: 0.8, model_version: 'hw-2', receipt_id: 'r2', font_url: null, enabled: false, reject_reason: null, created_at: '2026-09-21T00:00:00Z' },
  ],
}

function plan(extra: FetchPlan = {}): FetchPlan {
  return { '/api/v1/styles': envelope(styles), ...extra }
}

describe('P6_Styles 启停 Switch（B1-03）', () => {
  it('点击停用状态的 Switch 调 POST enable 并以服务端响应翻转状态', async () => {
    const { wrapper, logs } = mountPage(P6_Styles, {
      plan: plan({ '/api/v1/styles/2/enable': envelope({ ...styles.items[1], enabled: true }) }),
    })
    await flushPromises()
    const sws = wrapper.findAll('button[role="switch"]')
    const sw = sws.find((b) => b.attributes('aria-label') === '停用手写风格 启停')!
    expect(sw.attributes('aria-checked')).toBe('false')
    await sw.trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/api/v1/styles/2/enable'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    // 服务端响应 enabled=true → UI 翻转且无行内错误
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).not.toContain('启停失败')
  })

  it('端点失败（500）时乐观翻转回滚并显示行内错误', async () => {
    const { wrapper } = mountPage(P6_Styles, {
      plan: plan({ '/api/v1/styles/1/disable': problem(500, 'WORKER_UNAVAILABLE', '服务异常') }),
    })
    await flushPromises()
    const sw = wrapper.findAll('button[role="switch"]').find((b) => b.attributes('aria-label') === '我的手写风格 启停')!
    expect(sw.attributes('aria-checked')).toBe('true')
    await sw.trigger('click')
    await flushPromises()
    // 回滚：仍为 true（未翻转）
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).toContain('启停失败')
    expect(wrapper.text()).toContain('处理服务暂不可用，请稍后刷新核对。')
  })

  it('非 owner 404 STYLE_NOT_FOUND 错误态展示且回滚', async () => {
    const { wrapper } = mountPage(P6_Styles, {
      plan: plan({ '/api/v1/styles/1/disable': problem(404, 'STYLE_NOT_FOUND', 'Style not found') }),
    })
    await flushPromises()
    const sw = wrapper.findAll('button[role="switch"]').find((b) => b.attributes('aria-label') === '我的手写风格 启停')!
    await sw.trigger('click')
    await flushPromises()
    expect(sw.attributes('aria-checked')).toBe('true') // 回滚
    expect(wrapper.find('p[role="alert"]').text()).toContain('找不到此字体')
    expect(wrapper.find('p[role="alert"]').text()).toContain('找不到此字体')
  })

  it('点击启用的 Switch 调 POST disable 真端点', async () => {
    const { wrapper, logs } = mountPage(P6_Styles, {
      plan: plan({ '/api/v1/styles/1/disable': envelope({ ...styles.items[0], enabled: false }) }),
    })
    await flushPromises()
    const sw = wrapper.findAll('button[role="switch"]').find((b) => b.attributes('aria-label') === '我的手写风格 启停')!
    await sw.trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/api/v1/styles/1/disable'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    expect(sw.attributes('aria-checked')).toBe('false')
  })
})
