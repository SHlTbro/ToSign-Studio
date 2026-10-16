import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, mountPage } from './_harness'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { decodePending } from '@/api/pendingRender'
const wrappers: VueWrapper[] = []
const plan = {
 '/api/v1/assets':envelope({items:[{asset_id:'a',status:'approved',width:1080,height:1920}]}),
 '/api/v1/layout-templates':envelope({items:[{template_id:'t',base_asset_id:'a',status:'approved'}]}),
 '/api/v1/phrase-templates':envelope({items:[]}),'/api/v1/blogger-name-presets':envelope({items:[]}),
 '/api/v1/styles':envelope({items:[]}),'/api/v1/render-jobs/capabilities':envelope({...SYSTEM_CAPABILITIES,
  ink_colors:{...SYSTEM_CAPABILITIES.ink_colors,custom_solid:{available:true}}}),
 '/api/v1/render-jobs':envelope({job:{job_id:'j',status:'succeeded'},items:[]}),
}
beforeEach(()=>localStorage.clear())
afterEach(()=>{wrappers.splice(0).forEach(w=>w.unmount());vi.unstubAllGlobals();document.body.innerHTML=''})
describe('batch custom ink',()=>{
 it('replays frozen custom RGB without replacing it with the current editor choice',()=>{
  const body={template_id:'t',items:[{cn:'Alice'}],style_mode:'system',glyph_source_policy:'fallback_allowed',layout_profile:'baseline-v1',ink_color_mode:'custom-solid-srgb-v1',ink_color_rgb:[91,40,197]}
  const raw=JSON.stringify({key:'frozen-color',body})
  expect(decodePending(raw).body).toEqual(body)
  expect(()=>decodePending(JSON.stringify({key:'invalid',body:{...body,ink_color_rgb:[1,2,300]}}))).toThrow()
 })
 it('shares explicit color between preview/formal and restores the user selection on refresh',async()=>{
  const {wrapper,logs}=mountPage(P5Batch,{plan});wrappers.push(wrapper);await flushPromises()
  await wrapper.get('[aria-label="墨色方式"]').setValue('custom-solid-srgb-v1')
  await wrapper.get('[aria-label="字体颜色 HEX"]').setValue('#5b28c5')
  await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
  await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
  await wrapper.findAll('button').find(b=>b.text()==='预览草稿')!.trigger('click');await flushPromises()
  await wrapper.findAll('button').find(b=>b.text()==='继续编辑')!.trigger('click')
  await wrapper.findAll('button').find(b=>b.text()==='正式生成')!.trigger('click');await flushPromises()
  const bodies=logs.filter(l=>l.method==='POST'&&l.url==='/api/v1/render-jobs').map(l=>l.body as Record<string,unknown>)
  expect(bodies).toHaveLength(2)
  for(const body of bodies){expect(body.ink_color_mode).toBe('custom-solid-srgb-v1');expect(body.ink_color_rgb).toEqual([91,40,197]);expect(body.ink_palette_id).toBeUndefined()}
  wrapper.unmount()
  const restored=mountPage(P5Batch,{plan}).wrapper;wrappers.push(restored);await flushPromises()
  expect((restored.get('[aria-label="墨色方式"]').element as HTMLSelectElement).value).toBe('custom-solid-srgb-v1')
  expect((restored.get('[aria-label="字体颜色 HEX"]').element as HTMLInputElement).value.toUpperCase()).toBe('#5B28C5')
 })
 it('blocks illegal HEX, synchronizes picker/presets, and leaves defaults automatic',async()=>{
  const {wrapper,logs}=mountPage(P5Batch,{plan});wrappers.push(wrapper);await flushPromises()
  expect((wrapper.get('[aria-label="墨色方式"]').element as HTMLSelectElement).value).toBe('auto-contrast-v1')
  await wrapper.get('[aria-label="墨色方式"]').setValue('custom-solid-srgb-v1')
  await wrapper.get('[aria-label="字体颜色 HEX"]').setValue('#12')
  await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('Alice')
  await wrapper.findAll('button').find(b=>b.text()==='正式生成')!.trigger('click');await flushPromises()
  expect(logs.some(l=>l.method==='POST')).toBe(false)
  expect(wrapper.text()).toContain('6 位')
  expect(wrapper.get('[aria-label="字体颜色：黑色"]').attributes('aria-pressed')).toBe('false')
  await wrapper.get('[aria-label="自定义字体取色"]').setValue('#2468ac')
  expect((wrapper.get('[aria-label="字体颜色 HEX"]').element as HTMLInputElement).value).toBe('#2468ac')
  await wrapper.get('[aria-label="字体颜色：红色"]').trigger('click')
  expect((wrapper.get('[aria-label="字体颜色 HEX"]').element as HTMLInputElement).value).toBe('#D32F2F')
 })
})
