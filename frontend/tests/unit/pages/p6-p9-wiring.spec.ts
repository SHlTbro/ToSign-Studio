/**
 * G58B-04：P6_Styles / P6_NewStyle 接 styles 域真端点；G58B-05（P7）：
 * training_sets 数据面 + 引擎前置灰。G58B-06/07 的 P8/P9 也在本文件。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P6_Styles from '@/pages/P6_Styles.vue'
import P6_NewStyle from '@/pages/P6_NewStyle.vue'
import P7_Training from '@/pages/P7_Training.vue'
import P8_Blessings from '@/pages/P8_Blessings.vue'
import P9_UserCenter from '@/pages/P9_UserCenter.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('P6_Styles 接真端点（G58B-04）', () => {
  const styles = {
    items: [
      { style_id: 1, name: '我的手写风格', source_type: 'HANDWRITING_ASSET', status: 'approved', coverage: 0.92, model_version: 'hw-1', receipt_id: 'r1', font_url: null, enabled: true, reject_reason: null, created_at: '2026-09-21T00:00:00Z' },
      { style_id: 2, name: 'LoRA Beta', source_type: 'M2_ADAPTER', status: 'pending_review', coverage: 0.6, model_version: 'lora', receipt_id: 'r2', font_url: null, enabled: false, reject_reason: null, created_at: '2026-09-21T00:00:00Z' },
    ],
  }

  it('从 GET /styles 渲染真列表与审批门状态', async () => {
    const { wrapper } = mountPage(P6_Styles, { plan: { '/api/v1/styles': envelope(styles) } })
    await flushPromises()
    expect(wrapper.text()).toContain('我的手写风格')
    expect(wrapper.text()).toContain('已准入')
    expect(wrapper.text()).toContain('待审')
  })

  it('加载失败显示错误态与重试', async () => {
    const { wrapper } = mountPage(P6_Styles, { plan: { '/api/v1/styles': problem(403, 'FORBIDDEN', 'boom') } })
    await flushPromises()
    expect(wrapper.text()).toContain('风格列表加载失败')
    expect(wrapper.text()).toContain('重试')
  })

  // 原断言「启停 Switch 只读（D-T69-2 端点延后）」固化的是 D-T69-2 旧态；
  // B1-03 的交付物即关闭 D-T69-2（编排者 2026-09-22 批准仅改写本用例，
  // 断言更强：可点击 + 调 POST 真端点 + 状态翻转；diff 见 b1-styles-toggle discrepancy）。
  it('启停 Switch 可点击并调 POST /styles/{id}/disable 真端点（B1-03 解灰）', async () => {
    const { wrapper, logs } = mountPage(P6_Styles, {
      plan: {
        '/api/v1/styles': envelope(styles),
        '/api/v1/styles/1/disable': envelope({ ...styles.items[0], enabled: false }),
      },
    })
    await flushPromises()
    const sw = wrapper.find('button[role="switch"]')
    expect(sw.attributes('disabled')).toBeUndefined()
    await sw.trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/api/v1/styles/1/disable'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
    expect(sw.attributes('aria-checked')).toBe('false')
  })
})

describe('P6_NewStyle 接真端点（G58B-04）', () => {
  const presets = {
    items: [
      { preset_id: 'preset-yozai', name: '悠哉字体', description: '自然手写', font_url: '/api/v1/style-presets/preset-yozai/download', file_size_bytes: 15605374, version: 'v0.868', activated_style_id: null },
    ],
  }

  it('从 GET /style-presets 渲染预设字库并支持签发票据下载', async () => {
    const { wrapper, logs } = mountPage(P6_NewStyle, {
      plan: {
        '/api/v1/style-presets/preset-yozai/download': envelope({ url: '/api/v1/font-downloads/tok-1', ttl_seconds: 900 }),
        '/api/v1/style-presets/preset-yozai/activate': envelope({ preset_id: 'preset-yozai', activated_style_id: 8 }),
        '/api/v1/style-presets': envelope(presets),
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('悠哉字体')
    const btn = wrapper.findAll('button').find((b) => b.text().includes('下载启用'))!
    await btn.trigger('click')
    await flushPromises()
    expect(logs.find((l) => l.url.includes('/style-presets/preset-yozai/download'))).toBeTruthy()
    expect(logs.find((l) => l.url.includes('/style-presets/preset-yozai/activate') && l.method === 'POST')).toBeTruthy()
    expect(wrapper.text()).toContain('已加入资产')
  })

  it('加载失败显示错误态与重试', async () => {
    const { wrapper } = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': problem(404, 'PRESET_NOT_FOUND', 'manifest unreadable') } })
    await flushPromises()
    expect(wrapper.text()).toContain('预设字库加载失败')
  })
})

describe('P7_Training 接 training_sets 数据面（G58B-05）', () => {
  const images = {
    items: [
      { image_id: 1, file_name: 'IMG_2031.jpg', width: 1080, height: 1920, size_bytes: 2840 * 1024, status: 'ready', uploaded_at: '2026-09-21T00:00:00Z' },
    ],
    page: 1,
    page_size: 24,
    total: 1,
  }
  const styles = { items: [{ style_id: 7, name: 'M1冻结参考风格', source_type: 'M1_FROZEN', status: 'approved', quality_status: 'BLOCKED_OWNER', enabled: true, coverage: null, model_version: 'fontdiffuser-frozen-v1', receipt_id: null }] }
  const samples = { items: [{ sample_id: 12, style_id: 7, text_label: '永', source_training_image_id: 1, uploaded_at: '2026-09-21T00:00:00Z' }] }

  it('从 GET /training-sets 渲染真训练图列表', async () => {
    const { wrapper } = mountPage(P7_Training, { plan: {
      '/api/v1/training-sets': envelope(images),
      '/api/v1/styles?page=1&page_size=100': envelope(styles),
      '/api/v1/styles/7/samples': envelope(samples),
    } })
    await flushPromises()
    await wrapper.get('[aria-label="运行引擎"]').setValue('m1') // M2 is the new default; this fixture explicitly tests M1.
    expect(wrapper.text()).toContain('IMG_2031.jpg')
    expect(wrapper.text()).toContain('永')
    expect(wrapper.text()).toContain('1 个可用参考样本')
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label*="支持多选"]').exists()).toBe(false)
    const history = wrapper.get('[data-testid="historical-training-materials"]')
    expect(history.attributes('open')).toBeUndefined()
    expect(history.text()).toContain('登记不表示可训练')
    expect(history.find('img[alt="历史字图素材预览"]').attributes('src')).toBe('/api/v1/training-sets/images/1/file')
    expect(wrapper.find('a[href="/extract?style_id=7"]').exists()).toBe(true)
  })

  it('选择 ready 的 StyleSample 后创建并启动真实 M1_FROZEN run', async () => {
    const { wrapper, logs } = mountPage(P7_Training, { plan: {
      '/api/v1/training-sets': envelope(images),
      '/api/v1/styles?page=1&page_size=100': envelope(styles),
      '/api/v1/styles/7/samples': envelope(samples),
      '/api/v1/style-runs': envelope({ run_id: 23, status: 'created', sample_ids: [12], config: { engine: 'm1', target_char: '永', seed: 20260928, style_id: 7 }, created_at: '2026-09-28', updated_at: '2026-09-28' }),
      '/api/v1/style-runs/23': envelope({ run_id: 23, status: 'created', sample_ids: [12], config: { engine: 'm1', target_char: '永', seed: 20260928, style_id: 7 }, created_at: '2026-09-28', updated_at: '2026-09-28' }),
      '/api/v1/style-runs/23/progress': envelope({ run_id: 23, status: 'created', stage: 'created', engine_mode: null, epoch: null, total_epochs: null, loss: null, eta_s: null, percent: null, samples_seen: null, units_done: null, units_total: null, unit: null, log_tail: [], updated_at: '2026-09-28' }),
      '/api/v1/style-runs/23/metrics': envelope({ run_id: 23, state: 'pending', reason: 'RUN_CREATED', metrics: null, receipt: null }),
      '/api/v1/style-runs/23/samples': envelope({ items: samples.items }),
      '/api/v1/style-runs/23/start': envelope({ run_id: 23, status: 'queued', sample_ids: [12], config: { engine: 'm1', target_char: '永', seed: 20260928, style_id: 7 }, created_at: '2026-09-28', updated_at: '2026-09-28' }),
    } })
    await flushPromises()
    await wrapper.get('[aria-label="运行引擎"]').setValue('m1') // Preserve the explicit M1 regression flow.
    const create = wrapper.findAll('button').find((button) => button.text().includes('创建新运行'))!
    expect(create.attributes('disabled'), wrapper.text()).toBeUndefined()
    await create.trigger('click')
    await flushPromises()
    expect(logs.find((call) => call.url === '/api/v1/style-runs' && call.method === 'POST')?.body, JSON.stringify(logs)).toMatchObject({ sample_ids: [12], config: { engine: 'm1', target_char: '永', style_id: 7 } })
    const start = wrapper.findAll('button').find((button) => button.text().includes('启动真实运行'))!
    await start.trigger('click')
    await flushPromises()
    expect(logs.some((call) => call.url === '/api/v1/style-runs/23/start' && call.method === 'POST')).toBe(true)
    expect(wrapper.text()).toContain('已创建')
  })
})

describe('P8_Blessings 接 phrases 真端点（G58B-06）', () => {
  const blessings = { items: [{ id: 'b-1', text: '天天开心', enabled: true, created_by: '2', updated_at: '2026-09-21T00:00:00Z' }] }
  const presets = { items: [{ preset_id: 'bn-1', name: '星河', is_default: true }] }

  function plan(extra: FetchPlan = {}): FetchPlan {
    return {
      '/api/v1/phrase-templates': envelope(blessings),
      '/api/v1/blogger-name-presets': envelope(presets),
      ...extra,
    }
  }

  it('从真端点渲染祝福语与署名预设', async () => {
    const { wrapper } = mountPage(P8_Blessings, { plan: plan() })
    await flushPromises()
    expect(wrapper.find('textarea[aria-label="祝福语第 1 条"]').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="署名预设 1"]').exists()).toBe(true)
  })

  it('启停调用 POST enable/disable 真端点', async () => {
    const { wrapper, logs } = mountPage(P8_Blessings, {
      plan: plan({ '/api/v1/phrase-templates/b-1/disable': envelope({ id: 'b-1', text: '天天开心', enabled: false, updated_at: '2026-09-21T01:00:00Z' }) }),
    })
    await flushPromises()
    await wrapper.find('button[aria-label="第 1 条启停"]').trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/phrase-templates/b-1/disable'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('POST')
  })

  it('删除署名预设调用 DELETE /blogger-name-presets/{id}', async () => {
    const { wrapper, logs } = mountPage(P8_Blessings, {
      plan: plan({ '/api/v1/blogger-name-presets/bn-1': envelope({ deleted: true }) }),
    })
    await flushPromises()
    await wrapper.find('button[aria-label="删除署名预设 1"]').trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/blogger-name-presets/bn-1'))
    expect(call).toBeTruthy()
    expect(call!.method).toBe('DELETE')
    expect(wrapper.find('input[aria-label="署名预设 1"]').exists()).toBe(false)
  })

  it('空白祝福语保持本地草稿，只有输入有效内容才 POST 创建', async () => {
    const { wrapper } = mountPage(P8_Blessings, {
      plan: {
        '/api/v1/phrase-templates': envelope({ items: [] }),
        '/api/v1/blogger-name-presets': envelope({ items: [] }),
      },
    })
    const harnessFetch = globalThis.fetch
    const postCalls: { method: string; body?: unknown }[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/api/v1/phrase-templates') && init?.method === 'POST') {
        const body = typeof init.body === 'string' ? JSON.parse(init.body) as unknown : init.body
        postCalls.push({ method: 'POST', body })
        return new Response(JSON.stringify(envelope({ id: 'b-created', text: '天天开心', enabled: true, created_by: '2', updated_at: '2026-09-27T00:00:00Z' }).json), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        })
      }
      return harnessFetch(input, init)
    }))
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text().includes('新增一条祝福语'))!.trigger('click')
    await flushPromises()
    const area = wrapper.find('textarea[aria-label="祝福语第 1 条"]')
    await area.trigger('blur')
    expect(postCalls).toHaveLength(0)
    await area.setValue('天天开心')
    await area.trigger('blur')
    await flushPromises()
    expect(postCalls).toHaveLength(1)
    expect(postCalls[0]?.body).toEqual({ text: '天天开心', enabled: true })
    expect((wrapper.find('textarea[aria-label="祝福语第 1 条"]').element as HTMLTextAreaElement).value).toBe('天天开心')
  })

  it('删除默认署名后不擅自将其它署名设为默认', async () => {
    const { wrapper } = mountPage(P8_Blessings, {
      plan: {
        '/api/v1/phrase-templates': envelope({ items: [] }),
        '/api/v1/blogger-name-presets': envelope({ items: [
          { preset_id: 'bn-default', name: '默认名', is_default: true },
          { preset_id: 'bn-other', name: '其它名', is_default: false },
        ] }),
        '/api/v1/blogger-name-presets/bn-default': envelope({ deleted: true }),
      },
    })
    await flushPromises()
    await wrapper.find('button[aria-label="删除署名预设 1"]').trigger('click')
    await flushPromises()
    const remaining = wrapper.find('button[aria-label="将 其它名 设为默认署名"]')
    expect(remaining.attributes('aria-checked')).toBe('false')
  })

  it('创建署名后以服务端 preset_id 执行 PATCH，不使用旧 id 字段', async () => {
    const { wrapper } = mountPage(P8_Blessings, { plan: plan({ '/api/v1/blogger-name-presets': envelope({ items: [] }) }) })
    const harnessFetch = globalThis.fetch
    const presetCalls: { url: string; method: string }[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/blogger-name-presets')) presetCalls.push({ url, method: init?.method ?? 'GET' })
      if (url.endsWith('/blogger-name-presets') && init?.method === 'POST') {
        return new Response(JSON.stringify(envelope({ preset_id: 'bn-created', name: '新署名', is_default: false }).json), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/blogger-name-presets/bn-created') && init?.method === 'PATCH') {
        return new Response(JSON.stringify(envelope({ preset_id: 'bn-created', name: '新署名', is_default: true }).json), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return harnessFetch(input, init)
    }))
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text().includes('添加署名预设'))!.trigger('click')
    await flushPromises()
    const input = wrapper.find('input[aria-label="署名预设 1"]')
    await input.setValue('新署名')
    await input.trigger('blur')
    await flushPromises()
    await wrapper.find('button[aria-label="将 新署名 设为默认署名"]').trigger('click')
    await flushPromises()
    expect(presetCalls.some((call) => call.method === 'POST' && call.url.endsWith('/blogger-name-presets'))).toBe(true)
    expect(presetCalls.some((call) => call.method === 'PATCH' && call.url.endsWith('/blogger-name-presets/bn-created'))).toBe(true)
    expect(wrapper.find('button[aria-label="将 新署名 设为默认署名"]').attributes('aria-checked')).toBe('true')
  })
})

describe('P9_UserCenter 接 usage 六池 + auth（G58B-07）', () => {
  const usage = {
    total_bytes: 1024 * 1024 * 100,
    limit_bytes: 1024 * 1024 * 1024 * 5,
    pools: [
      { pool: 'assets', bytes: 1024 * 1024 * 60, file_count: 3 },
      { pool: 'training_images', bytes: 1024 * 1024 * 10, file_count: 2 },
      { pool: 'style_samples', bytes: 1024 * 1024 * 10, file_count: 1 },
      { pool: 'outputs', bytes: 1024 * 1024 * 10, file_count: 4 },
      { pool: 'backups', bytes: 1024 * 1024 * 10, file_count: 1 },
      { pool: 'models', bytes: 0, file_count: 0 },
    ],
  }

  it('渲染 usage 六池分池用量', async () => {
    const { wrapper } = mountPage(P9_UserCenter, { plan: { '/api/v1/usage': envelope(usage) } })
    await flushPromises()
    expect(wrapper.text()).toContain('存储用量')
    expect(wrapper.text()).toContain('原图资产')
    expect(wrapper.text()).toContain('训练图集')
    expect(wrapper.text()).toContain('3 文件')
  })

  // B1-04（D-T10-6a 关闭）改写用例（编排者 2026-09-22 批准，仅此一例，详见
  // .ai-hub/handoffs/b1-user-export-discrepancy.md）：旧用例「导出前置灰（D-T10-6
  // 端点未上线）」固化 D-T10-6a 的旧状态——解灰后必然失败。替换断言更强：
  // (a) 导出按钮可点击且发起 POST /api/v1/users/me/export 真调用；
  // (b) 请求期间防连击（按钮 disabled + 文案切换）；
  // (c) 失败时错误提示可见（mock 500 → 提示文案）。
  // 完整行为测试见 B1-04 新文件 frontend/tests/unit/pages/p9-user-export.spec.ts。
  it('导出解灰：真调用 POST /users/me/export + 防连击 + 失败提示（B1-04）', async () => {
    const { wrapper } = mountPage(P9_UserCenter, { plan: { '/api/v1/usage': envelope(usage) } })
    // pending 请求门：保持真实请求进行中状态
    let release!: (r: Response) => void
    const gate = new Promise<Response>((resolve) => {
      release = resolve
    })
    const harnessFetch = globalThis.fetch
    const postSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/users/me/export')) return gate
      return harnessFetch(input, init)
    })
    vi.stubGlobal('fetch', postSpy)
    await flushPromises()
    // (a) 按钮可点且发起真调用
    const btn = wrapper.findAll('button').find((b) => b.text() === '导出')!
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    await flushPromises()
    const calls = postSpy.mock.calls.filter((c) => String(c[0]).includes('/users/me/export'))
    expect(calls.length).toBe(1)
    expect(String(calls[0]![1]?.method ?? 'GET')).toBe('POST')
    // (b) 请求进行中防连击
    expect(btn.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').some((b) => b.text().includes('打包中'))).toBe(true)
    release(
      new Response(
        JSON.stringify({ type: 'about:blank', title: 'WORKER_UNAVAILABLE', status: 500, detail: '导出服务暂不可用', instance: '/', code: 'WORKER_UNAVAILABLE', retryable: false, trace_id: 'b104' }),
        { status: 500, headers: { 'content-type': 'application/problem+json' } },
      ),
    )
    await flushPromises()
    // (c) 失败提示可见（RFC 9457 code:detail），按钮恢复可点
    expect(wrapper.find('p[role="alert"]').text()).toContain('处理服务暂不可用，请稍后刷新核对。')
    expect(btn.attributes('disabled')).toBeUndefined()
  })
})
