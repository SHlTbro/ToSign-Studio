import { afterEach, describe, expect, it, vi } from 'vitest'
import P5_Review from '@/pages/P5_Review.vue'
import { envelope, flushPromises, mountPage } from './_harness'
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  localStorage.clear(); sessionStorage.clear()
  HTMLElement.prototype.scrollIntoView = originalScrollIntoView
})

async function openReview(plan: Parameters<typeof mountPage>[1]['plan']) {
  const mounted = mountPage(P5_Review, { plan })
  await mounted.router.push('/batch/job-a/review')
  await flushPromises()
  await flushPromises()
  return mounted
}

describe('P5_Review state handling', () => {
  it.each([false, true])('advances only when readback confirms the original item approved: %s', async approved => {
    const scroll = vi.fn(); HTMLElement.prototype.scrollIntoView = scroll
    const items = [
      { item_id: 'first', status: 'completed_with_warning', output_relative_path: 'first.png', warnings: [], render_version: 1 },
      { item_id: 'second', status: 'completed', output_relative_path: 'second.png', warnings: [], render_version: 1 },
    ]
    const { wrapper, logs } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'succeeded' }, items }),
      '/api/v1/render-jobs/job-a/review': envelope({ items: [] }),
    })
    const ordinaryFetch = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/render-jobs/job-a' && (!init?.method || init.method === 'GET')) return Promise.resolve(new Response(JSON.stringify(envelope({ job: { status: 'succeeded' }, items: [{ ...items[0], status: approved ? 'approved' : 'completed_with_warning' }, items[1]] }).json), { status: 200 }))
      return ordinaryFetch(input, init)
    }))
    await wrapper.get('[aria-label="质检说明"]').setValue('仅本张真实输出审核')
    await wrapper.findAll('button').find(button => button.text().includes('质检通过'))!.trigger('click'); await flushPromises()
    expect(scroll).toHaveBeenCalledTimes(approved ? 1 : 0)
    if (approved) expect(wrapper.text()).toContain('已质检通过')
    else expect(wrapper.text()).toContain('1 / 2')
    if (!approved) expect(wrapper.text()).toContain('尚未确认本项审核通过')
    expect(logs.filter(call => call.method === 'POST')).toHaveLength(1)
    wrapper.unmount()
  })
  it('records an explicit review note through the existing scoped review API without changing attribution', async () => {
    const { wrapper, logs } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'succeeded' }, items: [{ item_id: 'output', status: 'completed', output_relative_path: 'out.png', warnings: [], render_version: 1 }] }),
      '/api/v1/render-jobs/job-a/review': envelope({ items: [] }),
    })
    await wrapper.get('[aria-label="质检说明"]').setValue('授权 AI 已查看本张真实输出；仅本张成品质检。')
    await wrapper.findAll('button').find(button => button.text().includes('质检通过'))!.trigger('click'); await flushPromises()
    expect(logs.filter(call => call.method === 'POST')).toEqual([{ method: 'POST', url: '/api/v1/render-jobs/job-a/review', body: { item_ids: ['output'], decision: 'approved', comment: '授权 AI 已查看本张真实输出；仅本张成品质检。' } }])
    wrapper.unmount()
  })

  it('uses the explicit linked export endpoint and downloads its ZIP even with one original approved item', async () => {
    const { wrapper, logs } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [
        { item_id: 'good', status: 'approved', warnings: [], render_version: 1 },
        { item_id: 'bad', status: 'failed', retryable: true, warnings: [], render_version: 1 },
      ] }),
      '/api/v1/render-jobs/job-a/outputs': envelope({ job_id: 'job-a', export_artifacts: [] }),
      '/api/v1/render-jobs/job-a/export-linked': envelope({ artifacts: [
        { artifact_id: 'png-good', relative_path: 'good.png' }, { artifact_id: 'png-retry', relative_path: 'retry.png' },
        { artifact_id: 'zip-both', relative_path: 'bundle.zip' },
      ], failures: [] }),
    })
    const originalFetch = globalThis.fetch
    const files: string[] = []
    vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => {
      if (String(args[0]).includes('/exports/')) { files.push(String(args[0])); return Promise.resolve(new Response(new Blob(['ZIP fixture']), { status: 200 })) }
      return originalFetch(...args)
    }))
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:linked-test'), revokeObjectURL: vi.fn() }))
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await wrapper.findAll('button').find(button => button.text().includes('导出原批次关联成品 ZIP'))!.trigger('click'); await flushPromises()
    expect(logs.filter(call => call.method === 'POST').map(call => call.url)).toEqual(['/api/v1/render-jobs/job-a/export-linked'])
    expect(files).toEqual(['/api/v1/render-jobs/job-a/exports/zip-both/file'])
  })
  it('recovers an uncertain retry after remount under the persisted same identity without automatic POST', async () => {
    const plan = { '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [
      { item_id: 'bad', status: 'failed', retryable: true, warnings: [], render_version: 1 },
    ] }) }
    const first = await openReview(plan)
    const originalFetch = globalThis.fetch
    let key = ''
    vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => {
      if (String(args[0]).includes('/items/bad/retry')) {
        key = (args[1]?.headers as Record<string, string>)['Idempotency-Key']!
        return Promise.reject(new TypeError('Unknown result'))
      }
      return originalFetch(...args)
    }))
    await first.wrapper.findAll('button').find(button => button.text() === '重试此项')!.trigger('click'); await flushPromises()
    expect(key).toBeTruthy(); first.wrapper.unmount()
    const restored = await openReview({ ...plan,
      '/api/v1/render-jobs/job-a/items/bad/retry': envelope({ job: { job_id: 'same-retry' }, retried_item_ids: ['bad'], items: [] }),
    })
    expect(restored.logs.filter(call => call.method === 'POST')).toHaveLength(0)
    expect(restored.wrapper.findAll('button').find(button => button.text() === '重试此项')!.attributes('disabled')).toBeDefined()
    const restoredFetch = globalThis.fetch
    let reusedKey = ''
    vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => {
      if (String(args[0]).includes('/items/bad/retry')) reusedKey = (args[1]?.headers as Record<string, string>)['Idempotency-Key']!
      return restoredFetch(...args)
    }))
    await restored.wrapper.findAll('button').find(button => button.text().includes('确认并恢复原重试'))!.trigger('click'); await flushPromises()
    expect(reusedKey).toBe(key)
    expect(restored.router.currentRoute.value.path).toBe('/batch/same-retry/review')
  })
  it('locks a targeted retry while pending and never replays an uncertain transport result', async () => {
    const { wrapper, logs } = await openReview({ '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [
      { item_id: 'bad', status: 'failed', retryable: true, warnings: [], render_version: 1 },
    ] }) })
    const originalFetch = globalThis.fetch
    let rejectRetry!: (reason: unknown) => void
    const fetchSpy = vi.fn((...args: Parameters<typeof fetch>) => String(args[0]).includes('/items/bad/retry')
      ? new Promise<Response>((_resolve, reject) => { rejectRetry = reject }) : originalFetch(...args))
    vi.stubGlobal('fetch', fetchSpy)
    const retry = wrapper.findAll('button').find(button => button.text() === '重试此项')!
    await retry.trigger('click'); await retry.trigger('click')
    expect(fetchSpy.mock.calls.filter(call => String(call[0]).includes('/items/bad/retry'))).toHaveLength(1)
    expect(retry.attributes('disabled')).toBeDefined()
    rejectRetry(new TypeError('Network lost')); await flushPromises()
    expect(wrapper.text()).toContain('不要重复提交')
    await retry.trigger('click'); await flushPromises()
    expect(fetchSpy.mock.calls.filter(call => String(call[0]).includes('/items/bad/retry'))).toHaveLength(1)
    expect(logs.filter(call => call.method === 'POST')).toHaveLength(0)
  })
  it.each(['failed', 'cancelled'])('labels %s output as not generated and offers a targeted retry', async status => {
    const { wrapper, logs, router } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [
        { item_id: 'bad', status, retryable: true, last_error: 'V3_RENDER_UNAVAILABLE', output_relative_path: null, warnings: [], render_version: 1 },
      ] }),
      '/api/v1/render-jobs/job-a/items/bad/retry': envelope({ job: { job_id: 'retry-job' }, retried_item_ids: ['bad'], items: [] }),
    })
    expect(wrapper.text()).toContain(status === 'failed' ? '生成失败，未生成成品' : '已取消，未生成成品')
    const retry = wrapper.findAll('button').find(button => button.text() === '重试此项')!
    expect(retry).toBeDefined()
    await retry.trigger('click'); await flushPromises()
    expect(logs.filter(call => call.method === 'POST').map(call => call.url)).toEqual(['/api/v1/render-jobs/job-a/items/bad/retry'])
    expect(router.currentRoute.value.path).toBe('/batch/retry-job/review')
  })
  it('offers export for an approved PNG in a terminal partial batch without approving the failed item', async () => {
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [
        { item_id: 'good', status: 'approved', output_relative_path: 'good.png', warnings: [], render_version: 1 },
        { item_id: 'bad', status: 'failed', output_relative_path: null, warnings: [], render_version: 1 },
      ] }),
    })
    expect(wrapper.findAll('button').some(button => button.text() === '导出 PNG')).toBe(true)
    expect(wrapper.text()).toContain('1 张通过并可导出')
  })
  it('renders an explicit empty state for a loaded job without items', async () => {
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'succeeded' }, items: [] }),
    })

    expect(wrapper.text()).toContain('当前任务没有成品')
    expect(wrapper.text()).toContain('0 / 0')
    expect(wrapper.find('button[aria-label="质检通过"]').exists()).toBe(false)
  })

  it('keeps running jobs in a non-reviewable state', async () => {
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'running' }, items: [] }),
    })

    expect(wrapper.text()).toContain('任务记录仍为生成中')
    expect(wrapper.text()).toContain('任务仍在运行，暂时没有可审核成品')
    const approve = wrapper.findAll('button').find((button) => button.text().includes('质检通过'))
    expect(approve?.attributes('disabled')).toBeDefined()
  })

  it('rejects pending_review as an unknown job status instead of treating it as completed', async () => {
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'pending_review' }, items: [] }),
    })

    expect(wrapper.text()).toContain('任务读取失败')
    expect(wrapper.text()).not.toContain('当前任务没有成品')
  })

  it('shows per-glyph personal source/version warnings returned by the S11 review payload', async () => {
    const warning = 'GLYPH_STYLE artifact=artifact-1 version=fontdiffuser-v1 chars=永:M2'
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({
        job: { status: 'succeeded' },
        items: [{
          item_id: 'item-a', job_id: 'job-a', status: 'completed', cn: '小明',
          output_relative_path: null, fallback_used: false, fallback_map: {},
          warnings: [warning], retryable: false, render_version: 1,
        }],
      }),
    })

    const provenance = wrapper.find('button[aria-expanded="false"]')
    expect(provenance.text()).toContain('字形实际来源')
    await provenance.trigger('click')
    expect(wrapper.text()).toContain(warning)
    expect(wrapper.text()).toContain('永:M2')
  })
  it('does not mislabel an M2-to-M1 fallback as system font output', async () => {
    const { wrapper } = await openReview({
      '/api/v1/render-jobs/job-a': envelope({ job: { status: 'partial' }, items: [{
        item_id: 'item-a', status: 'completed_with_warning', cn: '永', fallback_used: true, fallback_map: {},
        warnings: ['GLYPH_STYLE artifact=fixture version=v1 chars=永:M1 reason=M2_UNAVAILABLE'], retryable: false, render_version: 1,
      }] }),
    })
    expect(wrapper.text()).toContain('部分字形发生来源回退')
    expect(wrapper.text()).not.toContain('检测到系统回退字体')
    await wrapper.findAll('button').find((button) => button.text().includes('字形实际来源'))!.trigger('click')
    expect(wrapper.text()).toContain('永:M1')
  })
  it('shows only server-frozen source policy, layout and seed after reload', async () => {
    localStorage.setItem('draft:batch-render-preferences:2', JSON.stringify({ styleId: '', sourcePolicy: 'fallback_allowed', layout: 'baseline-v1', seed: 7 }))
    const { wrapper } = await openReview({ '/api/v1/render-jobs/job-a': envelope({ job: { status: 'succeeded', style_mode: 'profile', style_id: 18,
      glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 0, layout_profile_sha256: 'a'.repeat(64) }, items: [] }) })
    const summary = wrapper.get('[data-testid="frozen-render-settings"]')
    expect(summary.text()).toContain('档案 #18')
    expect(summary.text()).toContain('仅使用当前 M2')
    expect(summary.text()).toContain('natural-layout-v1 · 重复种子：0')
    expect(summary.text()).toContain('a'.repeat(64))
    expect(summary.text()).not.toContain('系统字体 M0')
    localStorage.removeItem('draft:batch-render-preferences:2')
  })
  it('labels missing historical frozen settings without inventing modern defaults', async () => {
    const { wrapper } = await openReview({ '/api/v1/render-jobs/job-a': envelope({ job: { status: 'succeeded', style_mode: null, glyph_source_policy: null, layout_profile: null, layout_seed: null }, items: [] }) })
    const summary = wrapper.get('[data-testid="frozen-render-settings"]')
    expect(summary.text()).toContain('历史任务未记录明确来源模式')
    expect(summary.text()).toContain('排版配置：未记录 · 重复种子：未记录')
    expect(summary.text()).not.toContain('baseline-v1')
  })
})
