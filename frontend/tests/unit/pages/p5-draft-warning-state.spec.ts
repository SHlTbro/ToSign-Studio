import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, flushPromises, mountPage, type FetchPlan } from './_harness'
import { M2_CAPABILITIES, SYSTEM_CAPABILITIES } from './render-capability-fixtures'

// API fixtures exercise public UI behavior; no live job, model or qualification is run.
const SOURCE_RECORD = 'GLYPH_STYLE artifact=fixture version=fontdiffuser-v1 chars=T:M0,o:M0,：:M0,拉:M2,永:M2,远:M2,通:M2,畅:M2'
const FALLBACK_RECORD = 'GLYPH_STYLE_FALLBACK char=永 reason=M2_UNAVAILABLE resolved=M0:system'
const item = (change = {}) => ({ item_id: 'item-a', status: 'completed_with_warning', cn: '小美',
  output_relative_path: 'outputs/draft/item-a_v1.png', fallback_used: false, fallback_map: {},
  warnings: [SOURCE_RECORD], last_error: null, ...change })
const job = (status = 'partial', items = [item()]) => ({ job: { job_id: 'draft-warning', status }, items })
const BASE_PLAN: FetchPlan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset-a', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'blessing-a', text: '拉永远通畅', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset-a', name: '星河', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template-a', base_asset_id: 'asset-a', status: 'approved', created_at: '2026-10-08' }] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 7, name: 'Fixture personal style', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope({ ...M2_CAPABILITIES, source_policies: ['fallback_allowed', 'm2_required', 'm2_cjk_required'] }),
}
const wrappers: VueWrapper[] = []
beforeEach(() => localStorage.clear())
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.unstubAllGlobals(); localStorage.clear(); document.body.innerHTML = '' })
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text() === text)
  if (!button) throw new Error(`Missing button ${text}`)
  await button.trigger('click'); await flushPromises()
}
async function preview(result = job(), options: { poll?: ReturnType<typeof job>; rows?: number; profile?: boolean } = {}) {
  const fetchPlan = { ...BASE_PLAN, '/api/v1/render-jobs': envelope(result),
    ...(options.poll ? { '/api/v1/render-jobs/draft-warning': envelope(options.poll) } : {}) }
  const mounted = mountPage(P5Batch, { plan: fetchPlan }); wrappers.push(mounted.wrapper)
  const { wrapper } = mounted
  await flushPromises()
  if (options.profile) {
    await wrapper.get('#render-style').setValue('7'); await flushPromises()
    await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_cjk_required')
  }
  await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('小美')
  if (options.rows === 2) { await click(wrapper, '新增一行'); await wrapper.get('[aria-label="第 2 行粉丝昵称"]').setValue('小丽') }
  await click(wrapper, '预览草稿'); await flushPromises()
  return { ...mounted, fetchPlan }
}
const fallbackBadges = (wrapper: VueWrapper) => wrapper.findAll('span').filter((node) => node.text() === '回退')

describe('P5 draft completed outputs and honest warnings', () => {
  it('accepts a complete partial response, enlarges its real image and submits the same authored selection formally', async () => {
    const { wrapper, logs, router, fetchPlan } = await preview(job(), { profile: true })
    expect(wrapper.text()).toContain('草稿渲染完成')
    expect(wrapper.text()).toContain('含渲染提示')
    expect(wrapper.text()).not.toContain('草稿渲染失败')
    expect(fallbackBadges(wrapper)).toHaveLength(0)
    expect(wrapper.text()).toContain('字形来源')
    expect(wrapper.text()).toContain(SOURCE_RECORD)
    expect(wrapper.get('img[alt="小美 草稿渲染图"]').attributes('src')).toBe('/api/v1/render-jobs/draft-warning/items/item-a/image')
    await wrapper.get('[aria-label="放大预览第 1 行 To 签效果"]').trigger('click')
    expect(wrapper.get('[aria-label="To 签成品放大预览"] img').attributes('src')).toBe('/api/v1/render-jobs/draft-warning/items/item-a/image')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await flushPromises()
    fetchPlan['/api/v1/render-jobs'] = envelope({ job: { job_id: 'formal-warning', status: 'queued' }, items: [] })
    await click(wrapper, '确认正式生成'); await flushPromises()
    const posts = logs.filter((entry) => entry.method === 'POST' && entry.url === '/api/v1/render-jobs')
    expect(posts).toHaveLength(2)
    const draftBody = posts[0]!.body as Record<string, unknown>
    expect(draftBody).toMatchObject({ draft: true, style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_cjk_required' })
    const { draft: _draft, ...formalBody } = draftBody
    expect(posts[1]!.body).toEqual(formalBody)
    expect(router.currentRoute.value.path).toBe('/batch/formal-warning/review')
  })
  it('accepts complete partial items received by polling and stops without retrying the draft POST', async () => {
    const { wrapper, logs } = await preview(job('queued', [item({ status: 'pending', output_relative_path: null, warnings: [] })]), { poll: job() })
    expect(logs.some((entry) => entry.url === '/api/v1/render-jobs/draft-warning')).toBe(true)
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    expect(wrapper.text()).toContain('草稿渲染完成')
    expect(wrapper.find('img[alt="小美 草稿渲染图"]').exists()).toBe(true)
    expect(fallbackBadges(wrapper)).toHaveLength(0)
  })
  it('keeps an ordinary successful draft without warnings ready', async () => {
    const { wrapper } = await preview(job('succeeded', [item({ status: 'completed', warnings: [] })]))
    expect(wrapper.text()).toContain('草稿渲染完成')
    expect(wrapper.text()).not.toContain('含渲染提示')
    expect(fallbackBadges(wrapper)).toHaveLength(0)
  })
  it.each([
    { name: 'failed item', change: { status: 'failed', output_relative_path: null, last_error: 'GLYPH_MISSING' }, expected: 'GLYPH_MISSING' },
    { name: 'missing output', change: { output_relative_path: null }, expected: '未出图' },
    { name: 'empty output', change: { output_relative_path: '' }, expected: '未出图' },
    { name: 'unfinished item', change: { status: 'running', output_relative_path: null }, expected: '未出图' },
    { name: 'error despite completed status', change: { last_error: 'RENDER_EXECUTION_FAILED' }, expected: 'RENDER_EXECUTION_FAILED' },
  ])('rejects partial with $name and keeps its real error/absence visible', async ({ change, expected }) => {
    const { wrapper } = await preview(job('partial', [item(change)]))
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).not.toContain('草稿渲染完成')
    expect(wrapper.text()).toContain(expected)
    expect(wrapper.find('img[alt="小美 草稿渲染图"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('重试草稿渲染')
  })
  it('keeps actual mixed-success partial results failed while retaining the completed row image', async () => {
    const { wrapper } = await preview(job('partial', [item(), item({ item_id: 'item-b', cn: '小丽', status: 'failed', output_relative_path: null, last_error: 'M2_CJK_REQUIRED_FAILED' })]), { rows: 2 })
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).not.toContain('草稿渲染完成')
    expect(wrapper.text()).toContain('M2_CJK_REQUIRED_FAILED')
    expect(wrapper.find('img[alt="小美 草稿渲染图"]').exists()).toBe(true)
    expect(wrapper.find('img[alt="小丽 草稿渲染图"]').exists()).toBe(false)
  })
  it('rejects succeeded metadata without an output image', async () => {
    const { wrapper } = await preview(job('succeeded', [item({ status: 'completed', output_relative_path: null, warnings: [] })]))
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).not.toContain('草稿渲染完成')
    expect(wrapper.find('img[alt="小美 草稿渲染图"]').exists()).toBe(false)
  })
  it.each(['partial', 'succeeded'])('rejects %s when the response omits an authored row', async (status) => {
    const { wrapper } = await preview(job(status), { rows: 2 })
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).not.toContain('草稿渲染完成')
  })
  it.each(['failed', 'cancelled'])('preserves %s even if stale completed output metadata exists', async (status) => {
    const { wrapper } = await preview(job(status))
    expect(wrapper.text()).toContain('草稿渲染失败')
    expect(wrapper.text()).not.toContain('草稿渲染完成')
  })
  it.each([
    { name: 'backend fallback_used', change: { fallback_used: true, warnings: [SOURCE_RECORD] } },
    { name: 'fallback_used without a warning', change: { fallback_used: true, warnings: [] } },
    { name: 'explicit glyph fallback record', change: { warnings: [SOURCE_RECORD, FALLBACK_RECORD] } },
    { name: 'legacy missing-glyph fallback', change: { fallback_used: true, fallback_map: { 永: 'KaiTi' }, warnings: ['GLYPH_MISSING fallback to KaiTi: 永'] } },
  ])('retains honest fallback from $name', async ({ change }) => {
    const { wrapper } = await preview(job('partial', [item(change)]))
    expect(wrapper.text()).toContain('草稿渲染完成')
    expect(fallbackBadges(wrapper)).toHaveLength(1)
    for (const warning of change.warnings) expect(wrapper.text()).toContain(warning)
  })
  it('keeps other warnings as visible prompts without inventing a glyph fallback', async () => {
    const warning = 'LAYOUT_CLIPPED slot=nickname'
    const { wrapper } = await preview(job('partial', [item({ warnings: [warning] })]))
    expect(wrapper.text()).toContain('草稿渲染完成')
    expect(wrapper.text()).toContain('提示')
    expect(wrapper.text()).toContain(warning)
    expect(fallbackBadges(wrapper)).toHaveLength(0)
  })
})
