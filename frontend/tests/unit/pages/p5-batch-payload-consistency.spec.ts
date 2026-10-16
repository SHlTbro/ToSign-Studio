import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P5_Batch from '@/pages/P5_Batch.vue'
import { envelope, flushPromises, mountPage } from './_harness'

const commonPlan = {
  '/api/v1/assets': envelope({ items: [
    { asset_id: 'asset-1', status: 'approved', width: 1080, height: 1920 },
    { asset_id: 'asset-2', status: 'approved', width: 1200, height: 900 },
  ] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'bless-1', text: '天天开心', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset-1', name: '星河', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [
    { template_id: 'template-1', base_asset_id: 'asset-1', status: 'approved', created_at: '2026-09-27' },
    { template_id: 'template-2', base_asset_id: 'asset-2', status: 'approved', created_at: '2026-09-28' },
  ] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 1, name: 'QA system baseline', source_type: 'SYSTEM_FONT' }] }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { job_id: 'job-a', status: 'pending_review' }, items: [] }),
  '/api/v1/render-jobs/job-a': envelope({ job: { job_id: 'job-a', status: 'completed' }, items: [] }),
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('P5_Batch request parity', () => {
  it('submits each render item with its approved asset, template, blessing text, and row signature', async () => {
    const { wrapper, logs } = mountPage(P5_Batch, { plan: commonPlan })
    await flushPromises()
    await wrapper.find('input[aria-label="第 1 行粉丝昵称"]').setValue('小美')
    await wrapper.find('#blessing-0').setValue('bless-1')
    await wrapper.findAll('button').find((button) => button.text().includes('正式生成'))!.trigger('click')
    await flushPromises()

    const post = logs.find((entry) => entry.method === 'POST' && entry.url.endsWith('/render-jobs'))!
    expect(post.body).toMatchObject({
      template_id: 'template-1',
      items: [{
        cn: '小美',
        blessing: '天天开心',
        asset_id: 'asset-1',
        template_id: 'template-1',
        sign_blogger: true,
      }],
    })
    expect((post.body as Record<string, unknown>).style_id).toBeUndefined()
    expect((post.body as Record<string, unknown>).style_mode).toBe('system')
  })

  it('preserves different approved asset/template and signature choices for each row', async () => {
    const { wrapper, logs } = mountPage(P5_Batch, { plan: commonPlan })
    await flushPromises()
    await wrapper.find('input[aria-label="第 1 行粉丝昵称"]').setValue('小美')
    await wrapper.find('#blessing-0').setValue('bless-1')
    await wrapper.find('select[aria-label="第 1 行底图"]').setValue('asset-2')
    await wrapper.find('select[aria-label="第 1 行布局模板"]').setValue('template-2')

    await wrapper.findAll('button').find((button) => button.text().includes('新增一行'))!.trigger('click')
    await wrapper.find('input[aria-label="第 2 行粉丝昵称"]').setValue('小兰')
    await wrapper.find('#blessing-1').setValue('bless-1')
    await wrapper.find('button[aria-label="第 2 行图上署名开关"]').trigger('click')
    await wrapper.findAll('button').find((button) => button.text().includes('正式生成'))!.trigger('click')
    await flushPromises()

    const post = logs.find((entry) => entry.method === 'POST' && entry.url.endsWith('/render-jobs'))!
    expect(post.body).toMatchObject({
      template_id: 'template-2',
      style_mode: 'system',
      sign_blogger: false,
      items: [
        { cn: '小美', asset_id: 'asset-2', template_id: 'template-2', sign_blogger: true },
        { cn: '小兰', asset_id: 'asset-1', template_id: 'template-1', sign_blogger: false },
      ],
    })
  })

  it('uses the same render inputs for preview and formal submission', async () => {
    const { wrapper, logs } = mountPage(P5_Batch, { plan: commonPlan })
    await flushPromises()
    await wrapper.find('input[aria-label="第 1 行粉丝昵称"]').setValue('小美')

    await wrapper.findAll('button').find((button) => button.text().includes('预览草稿'))!.trigger('click')
    await flushPromises()
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('继续编辑'))!.trigger('click')
    await wrapper.findAll('button').find((button) => button.text().includes('正式生成'))!.trigger('click')
    await flushPromises()

    const posts = logs.filter((entry) => entry.method === 'POST' && entry.url.endsWith('/render-jobs'))
    expect(posts).toHaveLength(2)
    const withoutDraft = (body: unknown) => Object.fromEntries(
      Object.entries(body as Record<string, unknown>).filter(([key]) => key !== 'draft'),
    )
    expect(withoutDraft(posts[0].body)).toEqual(posts[1].body)
    expect((posts[0].body as Record<string, unknown>).draft).toBe(true)
  })
})
