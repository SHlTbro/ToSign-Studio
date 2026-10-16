import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, flushPromises, mountPage } from './_harness'
import { M2_CAPABILITIES } from './render-capability-fixtures'

const wrappers: VueWrapper[] = []
const preferences = { styleId: '7', sourcePolicy: 'm2_required', layout: 'natural-layout-v1', seed: 1,
  mixedFontsEnabled: false, inkMode: 'auto-contrast-v1', inkHex: '#8C3044', inkPaletteId: '', inkPaletteIdentity: '' }
const plan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset-a', status: 'approved', width: 1170, height: 2034 }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template-a', base_asset_id: 'asset-a', status: 'approved' }] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'blessing-a', text: '美貌永驻6百6十6', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 7, name: '经典手写 V3', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
  '/api/v1/render-jobs/capabilities': envelope({ ...M2_CAPABILITIES, layouts: [
    { id: 'baseline-v1', available: true, reason: null, profile_sha256: null },
    { id: 'natural-layout-v1', available: false, reason: '自然排版尚未通过本机核验，请选择原有排版', profile_sha256: null },
  ] }),
}
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('draft:batch-render-preferences:2', JSON.stringify(preferences))
  localStorage.setItem('draft:batch-generation-rows', JSON.stringify([{ cn: 'yy', blessing_id: 'blessing-a', sign_blogger: false }]))
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })
async function open() {
  const mounted = mountPage(P5Batch, { plan }); wrappers.push(mounted.wrapper); await flushPromises(); return mounted
}
const formal = (w: VueWrapper) => w.findAll('button').find(b => b.text() === '正式生成')!

describe('batch footer explains the actual generation blocker', () => {
  it('keeps unsupported saved choices and explains the precise reason beside disabled submission', async () => {
    const { wrapper, logs } = await open()
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').text()).toContain('自然排版尚未通过本机核验')
    expect(wrapper.get('[aria-label="排版方式"]').element).toHaveProperty('value', 'natural-layout-v1')
    expect(formal(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').find('a').attributes('href')).toBe('#batch-render-settings')
    expect(wrapper.get('footer').classes()).toContain('!relative')
    expect(wrapper.get('footer').classes()).toContain('shrink-0')
    expect(logs.filter(call => call.method === 'POST')).toHaveLength(0)
  })
  it('updates the reason when both invalid controls remain null, then clears it after explicit repair', async () => {
    localStorage.setItem('draft:batch-typography:2', JSON.stringify({ ordinaryScale: 4, sizeVariation: 0, tiltVariation: 0, seed: 1 }))
    const { wrapper, logs } = await open()
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').text()).toContain('自然排版')
    await wrapper.get('[aria-label="排版方式"]').setValue('baseline-v1'); await flushPromises()
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').text()).toContain('基础字号请输入 0.5–3 倍')
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').find('a').attributes('href')).toBe('#batch-typography-settings')
    await wrapper.get('[aria-label="基础字号（倍）"]').setValue('1.3')
    await wrapper.get('[aria-label="排版重复种子"]').setValue('-1'); await flushPromises()
    expect(wrapper.get('[data-testid="batch-generation-blocker"]').text()).toContain('重复种子请输入')
    expect(formal(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="reset-typography"]').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="batch-generation-blocker"]').exists()).toBe(false)
    expect(formal(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[aria-label="第 1 行粉丝昵称"]').element).toHaveProperty('value', 'yy')
    expect(wrapper.get('#blessing-0').element).toHaveProperty('value', 'blessing-a')
    expect(logs.filter(call => call.method === 'POST')).toHaveLength(0)
  })
})
