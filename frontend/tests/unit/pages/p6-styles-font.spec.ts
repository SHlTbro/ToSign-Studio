/**
 * B1-08（D-T65-1 链路落地）：P6_Styles @font-face 动态字体行为测试。
 * - 成功：有 font_url 的风格（HANDWRITING_ASSET/M2 类）→ 鉴权请求票据签发入口
 *   → 以签发的免鉴权签名 URL 构造 FontFace 注入 document.fonts → 预览用真字体族；
 * - 失败：票据请求失败 / 响应缺 url → 静默回退系统字体栈，不阻塞列表渲染；
 * - 缓存：同一 font_url 已成功加载后不再重复签发票据。
 * 新文件（既有测试零改动）。jsdom 无 FontFace，用 stub 驱动注入路径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type VueWrapper } from '@vue/test-utils'
import P6_Styles from '@/pages/P6_Styles.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

beforeEach(() => {
  localStorage.clear()
})

const realFonts = Object.getOwnPropertyDescriptor(document, 'fonts')
afterEach(() => {
  vi.unstubAllGlobals()
  if (realFonts) Object.defineProperty(document, 'fonts', realFonts)
  else delete (document as unknown as Record<string, unknown>).fonts
  document.body.innerHTML = ''
})

const constructedFaces: { family: string; source: string }[] = []

class FakeFontFace {
  family: string
  source: string
  constructor(family: string, source: string) {
    constructedFaces.push({ family, source })
    this.family = family
    this.source = source
  }
  load(): Promise<unknown> {
    return Promise.resolve()
  }
}

function stubFonts(): ReturnType<typeof vi.fn> {
  const fontsAdd = vi.fn()
  Object.defineProperty(document, 'fonts', { value: { add: fontsAdd }, configurable: true })
  vi.stubGlobal('FontFace', FakeFontFace)
  return fontsAdd
}

function styleRow(styleId: number, name: string, fontUrl: string | null) {
  return {
    style_id: styleId, name, source_type: 'HANDWRITING_ASSET', status: 'approved',
    coverage: 0.9, model_version: 'hw', receipt_id: 'r', font_url: fontUrl,
    enabled: true, reject_reason: null, created_at: '2026-09-21T00:00:00Z',
  }
}

function ticketPlan(fontUrl: string, hit: { status: number; json: unknown }, extra: FetchPlan = {}): FetchPlan {
  return { '/api/v1/styles': envelope({ items: [styleRow(7, '手写体甲', fontUrl)] }), [fontUrl]: hit, ...extra }
}

function previewFamily(wrapper: VueWrapper, which: 'h3' | 'p'): string {
  const el = which === 'h3'
    ? wrapper.findAll('h3').find((e) => e.text() === '手写体甲')
    : wrapper.findAll('p[aria-hidden="true"]')[0]
  return el!.attributes('style') ?? ''
}

describe('P6_Styles @font-face 动态字体（B1-08）', () => {
  it('票据获取+注入：鉴权请求 font_url 签发票据，FontFace 以签名 URL 注入，预览用真字体族', async () => {
    const fontsAdd = stubFonts()
    constructedFaces.length = 0
    const fontUrl = '/api/v1/style-presets/preset-yozai/download'
    const { wrapper, logs } = mountPage(P6_Styles, {
      plan: ticketPlan(fontUrl, envelope({ url: '/api/v1/font-downloads/tok-abc123', expires_at: '2026-09-22T00:15:00Z', ttl_seconds: 900 })),
    })
    await flushPromises()
    // 1) 票据签发入口被鉴权 fetch 调用（同源 fetch，session cookie 即鉴权）
    const ticketCall = logs.find((l) => l.url === fontUrl)
    expect(ticketCall).toBeTruthy()
    expect(ticketCall!.method).toBe('GET')
    // 2) FontFace 以预期字体族构造，src 为签发的免鉴权签名 URL
    expect(constructedFaces).toHaveLength(1)
    expect(constructedFaces[0].family).toBe('yy-preset-preset-yozai')
    expect(constructedFaces[0].source).toBe('url("/api/v1/font-downloads/tok-abc123")')
    // 3) 注入 document.fonts
    expect(fontsAdd).toHaveBeenCalledTimes(1)
    // 4) 标题与字样预览 fontFamily = 真字体族在前 + 系统栈回退在后（内容正确性）
    expect(previewFamily(wrapper, 'h3')).toContain('yy-preset-preset-yozai')
    expect(previewFamily(wrapper, 'p')).toContain('yy-preset-preset-yozai')
    expect(previewFamily(wrapper, 'p')).toContain('Segoe Print')
    // 列表未被字体加载阻塞：卡片正常渲染
    expect(wrapper.text()).toContain('手写体甲')
  })

  it('票据签发失败（404）回退系统字体栈，不阻塞列表', async () => {
    const fontsAdd = stubFonts()
    constructedFaces.length = 0
    const fontUrl = '/api/v1/style-presets/preset-ma-shan-zheng/download'
    const { wrapper } = mountPage(P6_Styles, {
      plan: ticketPlan(fontUrl, problem(404, 'PRESET_NOT_FOUND', 'Preset not found')),
    })
    await flushPromises()
    // 不构造 FontFace、不注入；预览 fontFamily = 系统字体栈（HANDWRITING_ASSET 栈）
    expect(constructedFaces).toHaveLength(0)
    expect(fontsAdd).not.toHaveBeenCalled()
    const h3Style = previewFamily(wrapper, 'h3')
    expect(h3Style).toContain('Segoe Print')
    expect(h3Style).not.toContain('yy-preset')
    // 列表照常渲染
    expect(wrapper.text()).toContain('手写体甲')
  })

  it('票据响应缺 url 字段回退系统字体栈', async () => {
    stubFonts()
    constructedFaces.length = 0
    const fontUrl = '/api/v1/style-presets/preset-no-url/download'
    const { wrapper } = mountPage(P6_Styles, {
      plan: ticketPlan(fontUrl, envelope({ ttl_seconds: 900 })),
    })
    await flushPromises()
    expect(constructedFaces).toHaveLength(0)
    expect(previewFamily(wrapper, 'p')).not.toContain('yy-preset')
    expect(wrapper.text()).toContain('手写体甲')
  })

  it('无 font_url 的风格不发起票据请求（font_url=null 直走系统栈）', async () => {
    stubFonts()
    const { logs } = mountPage(P6_Styles, {
      plan: { '/api/v1/styles': envelope({ items: [styleRow(9, '手写体乙', null)] }) },
    })
    await flushPromises()
    expect(logs.filter((l) => l.url.includes('/api/v1/style-presets'))).toHaveLength(0)
    expect(logs.filter((l) => l.url.includes('/api/v1/font-downloads'))).toHaveLength(0)
  })

  it('已成功加载的字体按 font_url 缓存：同 font_url 再次进入页面不重复签发票据', async () => {
    const fontsAdd = stubFonts()
    constructedFaces.length = 0
    const fontUrl = '/api/v1/style-presets/preset-cachecheck/download'
    const plan = ticketPlan(fontUrl, envelope({ url: '/api/v1/font-downloads/tok-cache', ttl_seconds: 900 }))
    const first = mountPage(P6_Styles, { plan })
    await flushPromises()
    expect(first.logs.filter((l) => l.url === fontUrl)).toHaveLength(1)
    expect(fontsAdd).toHaveBeenCalledTimes(1)
    // 第二次挂载：模块级缓存命中，零票据请求、零重复注入
    constructedFaces.length = 0
    const second = mountPage(P6_Styles, { plan })
    await flushPromises()
    expect(second.logs.filter((l) => l.url === fontUrl)).toHaveLength(0)
    expect(constructedFaces).toHaveLength(0)
    expect(fontsAdd).toHaveBeenCalledTimes(1)
    // 但预览仍用已缓存真字体族
    expect(previewFamily(second.wrapper, 'p')).toContain('yy-preset-preset-cachecheck')
  })
})
