import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
/**
 * G58B-03：P5_Batch 草稿预览走真渲染 draft 车道——提交 draft job → 轮询 →
 * 展示真渲染图；draft 失败时 UI 降级为文本预览（C-27）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P5_Batch from '@/pages/P5_Batch.vue'
import { __resetXhrFactoryForTest, __setXhrFactoryForTest, type MinimalXhr } from '@/api/xhrUpload'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const ASSETS = {
  items: [{ asset_id: 'ast-1', status: 'approved', width: 2048, height: 2048 }],
}
const BLESSINGS = { items: [{ id: 'b-1', text: '天天开心', enabled: true }] }
const PRESETS = { items: [{ preset_id: 'p-1', name: '星河', is_default: true }] }
const STYLES = { items: [{ style_id: 7, name: 'QA system font', source_type: 'SYSTEM_FONT' }] }
const TEMPLATES = {
  items: [{ template_id: 't-1', base_asset_id: 'ast-1', status: 'approved', created_at: '2026-09-21T00:00:00Z' }],
}
const DRAFT_JOB_QUEUED = {
  job: { job_id: 'job-d1', status: 'queued' },
  items: [{ item_id: 'it-1', status: 'pending', cn: '小美', output_relative_path: null, warnings: [] }],
}
const DRAFT_JOB_DONE = {
  job: { job_id: 'job-d1', status: 'succeeded' },
  items: [{ item_id: 'it-1', status: 'completed', cn: '小美', output_relative_path: 'x.png', warnings: [] }],
}

function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/assets': envelope(ASSETS),
    '/api/v1/phrase-templates': envelope(BLESSINGS),
    '/api/v1/blogger-name-presets': envelope(PRESETS),
    '/api/v1/layout-templates': envelope(TEMPLATES),
    '/api/v1/styles': envelope(STYLES),
    '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
    ...extra,
  }
}

beforeEach(() => {
  localStorage.clear()
  __resetXhrFactoryForTest()
})
afterEach(() => {
  __resetXhrFactoryForTest()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

async function openPreview(wrapper: ReturnType<typeof mountPage>['wrapper']): Promise<void> {
  const input = wrapper.find('input[aria-label="第 1 行粉丝昵称"]')
  await input.setValue('小美')
  const btn = wrapper.findAll('button').find((b) => b.text().includes('预览草稿'))!
  await btn.trigger('click')
  await flushPromises()
  await flushPromises()
}

describe('P5_Batch 草稿预览真渲染（G58B-03）', () => {
  it('新底图上传后跳到待审批入口', async () => {
    const { wrapper, router } = mountPage(P5_Batch, { plan: plan() })
    const xhr: MinimalXhr = {
      upload: { onprogress: null },
      responseType: '',
      status: 201,
      response: null,
      onabort: null,
      onload: null,
      onerror: null,
      open: vi.fn(),
      setRequestHeader: vi.fn(),
      send() {
        this.response = { data: { items: [{ asset_id: 'ast-new', reused: false }] } }
        this.onload?.()
      },
      abort: vi.fn(),
    }
    __setXhrFactoryForTest(() => xhr)
    await flushPromises()
    const input = wrapper.find('input[type="file"]')
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [new File(['image'], 'new-cos.png', { type: 'image/png' })] as unknown as FileList,
    })
    await input.trigger('change')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/assets')
    expect(router.currentRoute.value.query.filter).toBe('pending_review')
    expect(router.currentRoute.value.query.from).toBe('batch-upload')
  })

  it('打开预览即提交 draft:true 渲染任务并轮询到真渲染图', async () => {
    const { wrapper, logs } = mountPage(P5_Batch, {
      asAdmin: false,
      plan: plan({
        '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs': envelope(DRAFT_JOB_QUEUED),
        '/api/v1/render-jobs/job-d1': envelope(DRAFT_JOB_DONE),
      }),
    })
    await flushPromises()
    await openPreview(wrapper)
    const call = logs.find((l) => l.method === 'POST' && l.url.endsWith('/render-jobs'))
    expect(call).toBeTruthy()
    expect((call!.body as Record<string, unknown>).draft).toBe(true)
    expect((call!.body as Record<string, unknown>).style_id).toBeUndefined()
    expect((call!.body as Record<string, unknown>).style_mode).toBe('system')
    // 轮询到终态后行缩略图指向真渲染图端点
    const img = wrapper.find('img[alt="小美 草稿渲染图"]')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toBe('/api/v1/render-jobs/job-d1/items/it-1/image')
    // 文案区分草稿语义，不冒充正式结果
    expect(wrapper.text()).toContain('非正式结果')
  })

  it('无已审批模板时降级为文本预览，不发无效 draft 请求', async () => {
    const { wrapper, logs } = mountPage(P5_Batch, {
      asAdmin: false,
      plan: plan({ '/api/v1/layout-templates': envelope({ items: [] }) }),
    })
    await flushPromises()
    await openPreview(wrapper)
    expect(logs.find((l) => l.method === 'POST' && l.url.endsWith('/render-jobs'))).toBeUndefined()
    expect(wrapper.text()).toContain('草稿渲染不可用，已降级为文本预览')
  })

  it('draft 任务创建失败（409）时显示失败态与重试入口', async () => {
    const { wrapper } = mountPage(P5_Batch, {
      asAdmin: false,
      plan: plan({ '/api/v1/render-jobs': problem(409, 'TEMPLATE_NOT_APPROVED', 'Template not available') }),
    })
    await flushPromises()
    await openPreview(wrapper)
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).toContain('重试草稿渲染')
  })
})
