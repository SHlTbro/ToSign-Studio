/**
 * B5-02 · P4_Extract 解灰接线行为测试（PRD-v0.7 §5.4 + patch-D §4 四要素）。
 *
 * 覆盖（manifest GWT 口径：挂载 / 候选渲染 / 框选 / 四要素存在 / touchup_confirmed 携带）：
 * - 路由带入 assetId → POST /api/v1/detect-text → 候选框虚线 SVG 叠加渲染；
 * - 快划选中（pointer 路径命中候选）；
 * - 手动框选兜底（橡皮筋出框 + 过小提示，无候选时全流程可用）；
 * - 提取 → POST /api/v1/extract {asset_id, box_norm} → 黑底白字 alpha 预览切换；
 * - patch-D §4 四要素：修正前后对比视图 / 强制查看后才激活确认按钮 /
 *   独立确认入库按钮 + 二次弹窗（明示入库、5GB 配额、训练影响）/ 空操作 warning；
 * - 登记请求携带 touchup_confirmed:true（缺省 = 400 TOUCHUP_REQUIRED 的前端契约位）。
 * 新文件；既有 100 条测试零改动（endpoints-frozen.spec 的机械修订单独登记）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P4_Extract from '@/pages/P4_Extract.vue'
import { envelope, flushPromises, mountPage, type FetchPlan } from './_harness'

const DETECT_ITEM = { box_norm: { x_norm: 0.1, y_norm: 0.1, w_norm: 0.3, h_norm: 0.3 }, score: 0.87 }
const EXTRACT_RESP = { stroke_path: 'users/u1/styles/9/drafts/d1.png', needs_touchup: true }
const STYLES = {
  items: [{ style_id: 9, name: '手写风格A', source_type: 'HANDWRITING_ASSET', status: 'approved' }],
}
let canvasPngDataUrl = 'data:image/png;base64,AQID'
let uploadedForms: FormData[] = []
let strokePngDataUrls: string[] = []
let strokePngIndex = 0
let drawResultQueue: string[] = []

function basePlan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/assets/asset-0003': envelope({ width: 1080, height: 1920 }),
    '/api/v1/styles/9/samples': envelope({ items: [] }),
    '/api/v1/extract': envelope(EXTRACT_RESP),
    '/api/v1/detect-text': envelope({ items: [DETECT_ITEM] }),
    '/api/v1/styles': envelope(STYLES),
    ...extra,
  }
}

/** 挂载后推入 /extract/:assetId 路由，触发 watch → 自动 detect */
async function mountOnAsset(plan: FetchPlan): Promise<ReturnType<typeof mountPage>> {
  const ctx = mountPage(P4_Extract, { plan })
  await ctx.router.push('/extract/asset-0003')
  await flushPromises()
  return ctx
}

it('uses Chinese extraction labels and keeps source identity in collapsed advanced information', async () => {
  const { wrapper } = await mountOnAsset(basePlan())
  expect(wrapper.find('h1').text()).toBe('字形提取')
  expect(wrapper.get('[data-testid="p4-source"]').text()).toContain('训练参考图')
  const ordinaryText = wrapper.get('header').text()
  expect(ordinaryText).not.toMatch(/beta|patch-D|已接线/i)
  expect(wrapper.get('[data-testid="p4-stage-card"]').text()).not.toMatch(/Detect Layer|SWIPE|boxes/)
  const identity = wrapper.get('[data-testid="p4-asset-id"]')
  expect(identity.element.closest('details')?.hasAttribute('open')).toBe(false)
  expect(identity.text()).toContain('asset-0003')
})

/** jsdom getBoundingClientRect 全零 → 手工给定 stage 几何（400×300） */
function stubRect(el: Element, width: number, height: number): void {
  ;(el as unknown as { getBoundingClientRect: () => DOMRect }).getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: height, width, height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
}

/** 选中首个候选框：pointerdown 落在候选框内（bubbles 到 stage）→ pointerup 结选 */
async function swipeSelectFirst(wrapper: VueWrapper): Promise<void> {
  const stage = wrapper.find('[data-testid="p4-stage"]')
  stubRect(stage.element, 400, 300)
  const box = wrapper.find('[data-testid="detect-box"]')
  // 候选框 norm(0.1..0.4)² → stage 400×300 上取 (100,60) = norm(0.25, 0.2)，命中
  await box.trigger('pointerdown', { clientX: 100, clientY: 60 })
  await stage.trigger('pointerup')
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  canvasPngDataUrl = 'data:image/png;base64,AQID'
  uploadedForms = []
  strokePngDataUrls = []
  strokePngIndex = 0
  drawResultQueue = []
  class MockImage {
    naturalWidth = 128
    naturalHeight = 128
    complete = false
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    private currentSrc = ''
    set src(value: string) {
      this.currentSrc = value
      this.complete = true
      queueMicrotask(() => this.onload?.())
    }
    get src(): string { return this.currentSrc }
  }
  vi.stubGlobal('Image', MockImage as unknown as typeof Image)
  class MockXhr {
    upload = { onprogress: null as null | ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) }
    responseType = ''
    status = 200
    response: unknown = { data: { items: [{ image_id: 17 }] } }
    onabort: (() => void) | null = null
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    open(): void {}
    setRequestHeader(): void {}
    send(body?: FormData): void {
      if (body) uploadedForms.push(body)
      this.onload?.()
    }
    abort(): void {}
  }
  vi.stubGlobal('XMLHttpRequest', MockXhr as unknown as typeof XMLHttpRequest)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ({
    clearRect() {},
    drawImage(image: { src?: string }) {
      canvasPngDataUrl = drawResultQueue.shift()
        ?? (image.src?.startsWith('data:image/png') ? image.src : 'data:image/png;base64,AQID')
    },
    beginPath() {}, moveTo() {}, lineTo() {},
    stroke() {
      canvasPngDataUrl = strokePngDataUrls[strokePngIndex++] ?? 'data:image/png;base64,BAUG'
    },
    arc() {}, fill() {},
  } as unknown as CanvasRenderingContext2D))
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(() => canvasPngDataUrl)
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('P4_Extract 解灰接线（B5-02）', () => {
  it('挂载：路由带入 assetId → POST detect-text → 资产号展示', async () => {
    const { wrapper, logs } = await mountOnAsset(basePlan())
    expect(wrapper.find('[data-testid="p4-asset-id"]').text()).toContain('asset-0003')
    const call = logs.find((l) => l.url === '/api/v1/detect-text')
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    expect(call!.body).toEqual({ asset_id: 'asset-0003' })
  })

  it('候选渲染：SVG 候选框以虚线叠加呈现，初始未选中', async () => {
    const { wrapper } = await mountOnAsset(basePlan())
    const boxes = wrapper.findAll('[data-testid="detect-box"]')
    expect(boxes).toHaveLength(1)
    expect(boxes[0]!.attributes('stroke-dasharray')).toBe('1.6 1.1')
    expect(boxes[0]!.attributes('data-selected')).toBe('false')
    expect(wrapper.find('[data-testid="p4-candidate-count"]').text()).toContain('1 个框')
  })

  it('快划选中：pointer 路径命中候选框 → data-selected 翻转', async () => {
    const { wrapper } = await mountOnAsset(basePlan())
    await swipeSelectFirst(wrapper)
    expect(wrapper.find('[data-testid="detect-box"]').attributes('data-selected')).toBe('true')
    expect(wrapper.find('[data-testid="p4-selected-count"]').text()).toContain('1')
  })

  it('手动框选兜底：过小提示 + 橡皮筋出框生成手动框（无候选时全流程可用）', async () => {
    const { wrapper } = await mountOnAsset(basePlan({
      '/api/v1/detect-text': envelope({ items: [] }),
    }))
    expect(wrapper.text()).toContain('未检测到候选框')
    const stage = wrapper.find('[data-testid="p4-stage"]')
    stubRect(stage.element, 400, 300)
    await wrapper.find('[data-testid="p4-manual-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="p4-manual-toggle"]').attributes('aria-pressed')).toBe('true')
    // 过小框 → 提示且不生成
    await stage.trigger('pointerdown', { clientX: 80, clientY: 60 })
    await stage.trigger('pointerup')
    expect(wrapper.text()).toContain('框选范围过小')
    // 有效框 → 手动框入列，可提取
    await stage.trigger('pointerdown', { clientX: 80, clientY: 60 })
    await stage.trigger('pointermove', { clientX: 280, clientY: 210 })
    await stage.trigger('pointerup')
    expect(wrapper.find('[data-testid="p4-rubber"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="p4-selected-count"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="p4-extract"]').attributes('disabled')).toBeUndefined()
  })

  it('提取：POST extract {asset_id, box_norm} → 黑底白字 alpha 预览可切换', async () => {
    const { wrapper, logs } = await mountOnAsset(basePlan())
    await swipeSelectFirst(wrapper)
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url === '/api/v1/extract')
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    expect(call!.body).toEqual({ asset_id: 'asset-0003', box_norm: DETECT_ITEM.box_norm })
    // 半成品语义：一律 needs_touchup，无直通分支
    expect(wrapper.find('[data-testid="p4-draft-section"]').exists()).toBe(true)
    const alphaImg = wrapper.find('[data-testid="p4-alpha-before"] img')
    expect(alphaImg.exists()).toBe(true)
    expect(alphaImg!.attributes('src')).toBe('/api/v1/extract/drafts/d1.png')
    // 预览切换
    const toggle = wrapper.find('[data-testid="p4-alpha-toggle"]')
    expect(toggle.attributes('aria-pressed')).toBe('true')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-pressed')).toBe('false')
    expect(wrapper.find('[data-testid="p4-alpha-before"] img').exists()).toBe(false)
  })

  it('多 draft 切换后分别恢复各自的 canvas PNG 数据', async () => {
    const secondDetect = {
      box_norm: { x_norm: 0.55, y_norm: 0.55, w_norm: 0.25, h_norm: 0.25 },
      score: 0.78,
    }
    const { wrapper } = await mountOnAsset(basePlan({
      '/api/v1/detect-text': envelope({ items: [DETECT_ITEM, secondDetect] }),
    }))
    const stage = wrapper.find('[data-testid="p4-stage"]')
    stubRect(stage.element, 400, 300)
    for (const [candidateIndex, point] of [[0, [100, 60]], [1, [270, 210]]] as const) {
      await wrapper.findAll('[data-testid="detect-box"]')[candidateIndex]!.trigger('pointerdown', {
        clientX: point[0], clientY: point[1],
      })
      await stage.trigger('pointerup')
    }
    expect(wrapper.find('[data-testid="p4-selected-count"]').text()).toContain('2')
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-testid="p4-compare-after"]').trigger('click')
    await flushPromises()
    const canvas = wrapper.find('[data-testid="p4-canvas"]')
    stubRect(canvas.element, 320, 160)
    strokePngDataUrls = [
      'data:image/png;base64,BAUG',
      'data:image/png;base64,BwgJ',
      'data:image/png;base64,CgsM',
      'data:image/png;base64,DQ4P',
    ]

    await canvas.trigger('pointerdown', { clientX: 10, clientY: 10 })
    await canvas.trigger('pointermove', { clientX: 40, clientY: 30 })
    await canvas.trigger('pointerup')
    const firstPng = (canvas.element as HTMLCanvasElement).toDataURL()

    await wrapper.findAll('button').find((button) => button.text() === '框 d1')!.trigger('click')
    await flushPromises()
    expect((canvas.element as HTMLCanvasElement).toDataURL()).toBe('data:image/png;base64,AQID')
    await canvas.trigger('pointerdown', { clientX: 10, clientY: 10 })
    await canvas.trigger('pointermove', { clientX: 40, clientY: 30 })
    await canvas.trigger('pointerup')
    const secondPng = (canvas.element as HTMLCanvasElement).toDataURL()
    expect(secondPng).not.toBe(firstPng)

    await wrapper.findAll('button').find((button) => button.text() === '框 d0')!.trigger('click')
    await flushPromises()
    expect((canvas.element as HTMLCanvasElement).toDataURL()).toBe(firstPng)
    await wrapper.findAll('button').find((button) => button.text() === '框 d1')!.trigger('click')
    await flushPromises()
    expect((canvas.element as HTMLCanvasElement).toDataURL()).toBe(secondPng)
  })

  it('旧 draft 图片晚解码时不会覆盖当前 draft 的 canvas', async () => {
    const secondDetect = {
      box_norm: { x_norm: 0.55, y_norm: 0.55, w_norm: 0.25, h_norm: 0.25 },
      score: 0.78,
    }
    const { wrapper } = await mountOnAsset(basePlan({
      '/api/v1/detect-text': envelope({ items: [DETECT_ITEM, secondDetect] }),
    }))
    const stage = wrapper.find('[data-testid="p4-stage"]')
    stubRect(stage.element, 400, 300)
    for (const [candidateIndex, point] of [[0, [100, 60]], [1, [270, 210]]] as const) {
      await wrapper.findAll('[data-testid="detect-box"]')[candidateIndex]!.trigger('pointerdown', {
        clientX: point[0], clientY: point[1],
      })
      await stage.trigger('pointerup')
    }
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()

    const pendingImages: Array<{ src: string; onload: (() => void) | null; resolve(): void }> = []
    class DeferredImage {
      naturalWidth = 128
      naturalHeight = 128
      complete = false
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      private currentSrc = ''
      set src(value: string) {
        this.currentSrc = value
        pendingImages.push(this)
      }
      get src(): string { return this.currentSrc }
      resolve(): void { this.complete = true; this.onload?.() }
    }
    vi.stubGlobal('Image', DeferredImage as unknown as typeof Image)

    await wrapper.find('[data-testid="p4-compare-after"]').trigger('click')
    await flushPromises()
    expect(pendingImages).toHaveLength(1)
    await wrapper.findAll('button').find((button) => button.text() === '框 d1')!.trigger('click')
    await flushPromises()
    expect(pendingImages).toHaveLength(2)

    drawResultQueue = ['data:image/png;base64,BwgJ', 'data:image/png;base64,BAUG']
    pendingImages[1]!.resolve()
    await flushPromises()
    const canvas = wrapper.find('[data-testid="p4-canvas"]')
    expect(canvas.attributes('data-ready')).toBe('true')
    expect((canvas.element as HTMLCanvasElement).toDataURL()).toBe('data:image/png;base64,BwgJ')

    pendingImages[0]!.resolve()
    await flushPromises()
    expect((canvas.element as HTMLCanvasElement).toDataURL()).toBe('data:image/png;base64,BwgJ')
  })

  it('touchup 四要素：对比视图 → 强制查看激活确认 → 空操作 warning → 二次弹窗明示后果 → 携带 touchup_confirmed:true 登记', async () => {
    const persistedSample = {
      sample_id: 81,
      style_id: 9,
      source_training_image_id: 17,
      text_label: '福',
      sha256: 'f'.repeat(64),
    }
    const { wrapper, logs } = await mountOnAsset(basePlan({
      '/api/v1/styles/9/samples': envelope({ items: [persistedSample] }),
    }))
    await swipeSelectFirst(wrapper)
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()

    // 要素 2：未查看对比 → 确认按钮禁用 + 引导提示
    const confirmBtn = wrapper.find('[data-testid="p4-confirm-register"]')
    expect(confirmBtn.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="p4-view-hint"]').text()).toContain('请先查看')

    // 要素 1：对比视图（修正前/后切换）→ 查看过后按钮激活
    await wrapper.find('[data-testid="p4-compare-before"]').trigger('click')
    await wrapper.find('[data-testid="p4-compare-after"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="p4-compare-after"]').attributes('class')).toContain('!bg-ink')
    expect(wrapper.find('[data-testid="p4-confirm-register"]').attributes('disabled')).toBeUndefined()

    // 要素 4：修正操作次数为 0 → 二次弹窗出现空操作 warning（默认放行留痕）
    await confirmBtn.trigger('click')
    const modal = wrapper.find('[data-testid="p4-register-modal"]')
    expect(modal.exists()).toBe(true)
    expect(wrapper.find('[data-testid="p4-noop-warning"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="p4-noop-warning"]').text()).toContain('空操作')
    // 明示后果（要素 3）：创建候选不等于字符真值确认，也不会直接进入 S03 快照。
    expect(modal.text()).toContain('待本人确认的字符候选')
    expect(modal.text()).toContain('字符真值已确认')
    expect(modal.text()).toContain('不会进入训练快照')
    await wrapper.find('[data-testid="p4-register-cancel"]').trigger('click')
    expect(wrapper.find('[data-testid="p4-register-modal"]').exists()).toBe(false)

    // 修正一次（画布手势计数）→ 再开弹窗无空操作 warning
    const canvas = wrapper.find('[data-testid="p4-canvas"]')
    stubRect(canvas.element, 320, 160)
    await canvas.trigger('pointerdown', { clientX: 10, clientY: 10 })
    await canvas.trigger('pointermove', { clientX: 40, clientY: 30 })
    expect(wrapper.find('[data-testid="p4-edit-count"]').text()).toBe('0')
    await wrapper.find('[data-testid="p4-confirm-register"]').trigger('click')
    expect(wrapper.find('[data-testid="p4-edit-count"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="p4-noop-warning"]').exists()).toBe(false)

    // 要素 3：独立确认入库按钮 → 二次弹窗终确认 → POST 登记
    await wrapper.find('[data-testid="p4-style-select"]').setValue('9')
    await wrapper.find('[data-testid="p4-text-label"]').setValue('福')
    await wrapper.find('[data-testid="p4-register-final"]').trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url === '/api/v1/styles/9/samples' && l.method === 'POST')
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    expect(call!.body).toEqual({
      text_label: '福',
      source_asset_id: 'asset-0003',
      source_bbox_norm: { x_norm: 0.1, y_norm: 0.1, w_norm: 0.3, h_norm: 0.3 },
      draft_alpha_path: 'users/u1/styles/9/drafts/d1.png',
      source_training_image_id: 17,
      touchup_edit_count: 1,
      touchup_confirmed: true,
    })
    const upload = uploadedForms.find((form) => form.has('files'))
    expect(upload).toBeTruthy()
    const savedFile = upload!.get('files') as File
    expect(savedFile.type).toBe('image/png')
    const uploadedBytes = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as ArrayBuffer)
      reader.onerror = () => reject(reader.error)
      reader.readAsArrayBuffer(savedFile)
    })
    expect([...new Uint8Array(uploadedBytes)]).toEqual([4, 5, 6])
    expect(wrapper.find('[data-testid="p4-register-done"]').exists()).toBe(true)
    const savedImage = wrapper.find('[data-testid="p4-saved-sample-image"]')
    expect(savedImage.attributes('src')).toBe('/api/v1/training-sets/images/17/file')
  })

  it('登记响应与回读都丢失时保留已上传图，重试回读命中后不重复上传或删除', async () => {
    const { wrapper, logs } = await mountOnAsset(basePlan())
    await swipeSelectFirst(wrapper)
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="p4-compare-before"]').trigger('click')
    await wrapper.find('[data-testid="p4-compare-after"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="p4-confirm-register"]').trigger('click')
    await wrapper.find('[data-testid="p4-style-select"]').setValue('9')
    await wrapper.find('[data-testid="p4-text-label"]').setValue('福')

    const fallbackFetch = globalThis.fetch
    let readbackAvailable = false
    const intercepted: string[] = []
    const samplePath = '/api/v1/styles/9/samples'
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input).split('?')[0]
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url === samplePath) intercepted.push(method)
      if (url === samplePath && method === 'POST') {
        return { ok: false, status: 504, json: async () => ({ status: 504, code: 'TIMEOUT', title: 'TIMEOUT', detail: 'response lost' }) } as Response
      }
      if (url === samplePath && method === 'GET') {
        if (!readbackAvailable) {
          return { ok: false, status: 404, json: async () => ({ status: 404, code: 'NOT_FOUND', title: 'NOT_FOUND', detail: 'readback unavailable' }) } as Response
        }
        return { ok: true, status: 200, json: async () => envelope({ items: [{ source_training_image_id: 17 }] }).json } as Response
      }
      return fallbackFetch(input, init)
    })

    await wrapper.find('[data-testid="p4-register-final"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="p4-register-done"]').exists()).toBe(false)
    expect(uploadedForms.filter((form) => form.has('files'))).toHaveLength(1)
    expect(logs.some((call) => call.method === 'DELETE' && call.url.includes('/training-sets/images/17'))).toBe(false)

    readbackAvailable = true
    await wrapper.find('[data-testid="p4-register-final"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="p4-register-done"]').exists(), `${intercepted.join(',')} :: ${wrapper.text()}`).toBe(true)
    expect(uploadedForms.filter((form) => form.has('files'))).toHaveLength(1)
    expect(logs.some((call) => call.method === 'DELETE' && call.url.includes('/training-sets/images/17'))).toBe(false)
  })

  it('提取失败逐框标记可重试：500 EXTRACT_FAILED → 行内错误 + 重试按钮', async () => {
    const { wrapper } = await mountOnAsset(basePlan({
      '/api/v1/extract': { status: 500, json: { type: 'about:blank', title: 'EXTRACT_FAILED', status: 500, detail: 'worker 异常', instance: '/', code: 'EXTRACT_FAILED', retryable: true, trace_id: 'b5-test' } },
    }))
    await swipeSelectFirst(wrapper)
    await wrapper.find('[data-testid="p4-extract"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[role="alert"]').some(alert => alert.text().includes('字样提取失败，请核对图片后重试。'))).toBe(true)
    expect(wrapper.find('[data-testid="p4-retry"]').exists()).toBe(true)
  })
})
