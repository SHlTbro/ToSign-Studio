import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, mountPage } from './_harness'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const wrappers: VueWrapper[] = []
const plan = {
  '/api/v1/assets': envelope({items:[{asset_id:'a',status:'approved',width:1080,height:1920}]}),
  '/api/v1/layout-templates': envelope({items:[{template_id:'t',base_asset_id:'a',status:'approved'}]}),
  '/api/v1/phrase-templates': envelope({items:[]}), '/api/v1/blogger-name-presets': envelope({items:[]}),
  '/api/v1/styles': envelope({items:[]}), '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs': envelope({job:{job_id:'j',status:'succeeded'},items:[]}),
}
beforeEach(() => localStorage.clear())
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); document.body.innerHTML='' })
describe('batch typography submission', () => {
  it('freezes the same chosen size, per-character variation and seed for preview and batch', async () => {
    const {wrapper,logs} = mountPage(P5Batch,{plan}); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="基础字号（倍）"]').setValue('1.8')
    await wrapper.get('[aria-label="逐字大小不规则程度（%）"]').setValue('12')
    await wrapper.get('[aria-label="逐字倾斜不规则程度（度）"]').setValue('5')
    await wrapper.get('[aria-label="排版重复种子"]').setValue('73')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
    await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    await wrapper.findAll('button').find(b => b.text().includes('预览草稿'))!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(b => b.text().includes('继续编辑'))!.trigger('click')
    await wrapper.findAll('button').find(b => b.text().includes('正式生成'))!.trigger('click'); await flushPromises()
    const bodies = logs.filter(l => l.method==='POST' && l.url==='/api/v1/render-jobs').map(l => l.body as Record<string,unknown>)
    expect(bodies).toHaveLength(2)
    for (const body of bodies) {
      expect(body.text_style).toEqual({ordinary_scale:1.8,to_ratio:2,header_gap:10})
      expect(body.handwriting).toEqual({size_variation_pct:12,tilt_variation_deg:5,seed:73})
    }
  })
  it('blocks invalid sizes and restores defaults explicitly without repeated multiplication', async () => {
    const {wrapper,logs} = mountPage(P5Batch,{plan}); wrappers.push(wrapper); await flushPromises()
    await wrapper.get('[aria-label="基础字号（倍）"]').setValue('3.5')
    await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
    await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    await wrapper.findAll('button').find(b => b.text().includes('正式生成'))!.trigger('click'); await flushPromises()
    expect(logs.some(l => l.method==='POST')).toBe(false)
    await wrapper.get('[data-testid="reset-typography"]').trigger('click')
    expect((wrapper.get('[aria-label="基础字号（倍）"]').element as HTMLInputElement).value).toBe('1.3')
    await wrapper.get('[data-testid="reset-typography"]').trigger('click')
    expect((wrapper.get('[aria-label="基础字号（倍）"]').element as HTMLInputElement).value).toBe('1.3')
  })
})
