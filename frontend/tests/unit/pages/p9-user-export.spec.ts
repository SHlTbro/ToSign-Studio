/**
 * B1-04（D-T10-6a）：P9_UserCenter 导出按钮解灰后的行为测试。
 * - 成功：POST /api/v1/users/me/export（frozen 端点常量），ZIP 经 blob → a[download] 触发下载；
 * - 防连击：真实请求进行中（pending fetch）重复点击不重复发请求；
 * - 失败：RFC 9457 problem+json 解析（code + detail）展示在 role=alert，按钮恢复可点。
 * 新文件（既有测试零改动）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P9_UserCenter from '@/pages/P9_UserCenter.vue'
import { envelope, flushPromises, mountPage, problem } from './_harness'

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(window.URL, 'createObjectURL', { value: vi.fn(() => 'blob:mock-zip'), configurable: true, writable: true })
  Object.defineProperty(window.URL, 'revokeObjectURL', { value: vi.fn(), configurable: true, writable: true })
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/** 导出端点的 Response 桩：真 Response（undici），支持 blob()/headers。 */
function zipResponse(bytes: Uint8Array): Response {
  return new Response(new Blob([bytes], { type: 'application/zip' }), {
    status: 200,
    headers: { 'content-length': String(bytes.byteLength), 'content-type': 'application/zip' },
  })
}

type ExportHandler = () => Response | Promise<Response>

/**
 * 在 harness 的 usage stub 之上覆写 fetch：/users/me/export 走自定义 handler
 * 并被 spy 记录，其余请求回落 harness 的 stub（挂载时已占住 globalThis.fetch）。
 */
function overrideFetchForExport(handler: ExportHandler): ReturnType<typeof vi.fn> {
  const harnessFetch = globalThis.fetch
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/users/me/export')) return handler()
    return harnessFetch(input, init)
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

describe('P9_UserCenter 数据导出（B1-04）', () => {
  it('点击导出按钮发 POST /api/v1/users/me/export 并触发 ZIP 下载', async () => {
    const { wrapper } = mountPage(P9_UserCenter, { plan: { '/api/v1/usage': envelope({ total_bytes: 1, limit_bytes: 2, pools: [] }) } })
    const fetchSpy = overrideFetchForExport(() => zipResponse(new Uint8Array([0x50, 0x4b, 0x03, 0x04])))
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text() === '导出')!
    expect(btn).toBeTruthy()
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    await flushPromises()
    const calls = fetchSpy.mock.calls.filter((c) => String(c[0]).includes('/users/me/export'))
    expect(calls.length).toBe(1)
    expect(String(calls[0]![1]?.method ?? 'GET')).toBe('POST')
    expect((calls[0]![1]?.headers as Record<string, string>)['X-Requested-With']).toBe('XMLHttpRequest')
    // 下载已触发：object URL 创建 + 回收
    expect(window.URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-zip')
    // 无错误态；请求结束后按钮恢复可点（exporting 复位）
    expect(wrapper.text()).not.toContain('导出失败')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('真实请求进行中重复点击不重复发请求（防连击）', async () => {
    const { wrapper } = mountPage(P9_UserCenter, { plan: { '/api/v1/usage': envelope({ total_bytes: 1, limit_bytes: 2, pools: [] }) } })
    let release!: (r: Response) => void
    const gate = new Promise<Response>((resolve) => {
      release = resolve
    })
    const fetchSpy = overrideFetchForExport(() => gate)
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text() === '导出')!
    await btn.trigger('click')
    await flushPromises()
    // 进行中：文案切换 + disabled + 再点不发新请求
    expect(wrapper.findAll('button').some((b) => b.text().includes('打包中'))).toBe(true)
    expect(btn.attributes('disabled')).toBeDefined()
    await btn.trigger('click')
    await flushPromises()
    expect(fetchSpy.mock.calls.filter((c) => String(c[0]).includes('/users/me/export')).length).toBe(1)
    release(zipResponse(new Uint8Array([0x50, 0x4b])))
    await flushPromises()
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  it('导出失败：解析 RFC 9457 problem+json 展示 code+detail，按钮恢复可点', async () => {
    const { wrapper } = mountPage(P9_UserCenter, { plan: { '/api/v1/usage': envelope({ total_bytes: 1, limit_bytes: 2, pools: [] }) } })
    overrideFetchForExport(() =>
      new Response(JSON.stringify(problem(413, 'ZIP_TOO_LARGE', 'planned zip payload is 600 bytes, cap is 500').json), {
        status: 413,
        headers: { 'content-type': 'application/problem+json' },
      }),
    )
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text() === '导出')!
    await btn.trigger('click')
    await flushPromises()
    const alert = wrapper.find('p[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('打包文件过大')
    expect(alert.text()).toContain('预计 600 字节，允许上限 500 字节')
    expect(btn.attributes('disabled')).toBeUndefined()
  })
})
