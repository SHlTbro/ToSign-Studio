import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import { envelope, mountPage } from './_harness'
import { M2_CAPABILITIES, SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const wrappers: VueWrapper[] = []
const v3 = {
  ...M2_CAPABILITIES, source_policies: ['fallback_allowed', 'm2_required', 'm2_cjk_required'],
  accepted_v3: { available: true, fonts: { latin: 'preset-caveat-600', other: 'preset-noto-cjk-sc-face2' },
    binding: { schema: 'accepted-v3-layout-binding/1', profile_sha256: 'a'.repeat(64), implementation_sha256: 'b'.repeat(64),
      adapter_sha256: '017881442b7f8b909f7d8d8c4c5e7ad1893d0caa3c3267ae3c793a9a96175f81' }, seed: 2026100401, rgb: [140, 48, 68] },
  ink_colors: { default_mode: 'auto-contrast-v1', custom_solid: { available: true }, source_solid: { available: false, reason: 'fixture', palettes: [] },
    supported_layouts: ['natural-layout-v1'], supported_source_policies: ['m2_required'], automatic_recoloring: false },
  non_cjk_fonts: { schema: 'non-cjk-font-catalog/1', missing_requested_fonts: [], fonts: [
    { id: 'preset-caveat-600', name: 'Caveat 600 (V3)', roles: ['latin'], available: true },
    { id: 'preset-noto-cjk-sc-face2', name: 'Noto face 2 (V3)', roles: ['other'], available: true },
  ] },
}
beforeEach(() => localStorage.clear())
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); document.body.innerHTML = '' })
it('uses the server verified V3 defaults in the actual preview and formal render requests', async () => {
  const plan = {
    '/api/v1/assets': envelope({ items: [{ asset_id: 'a', status: 'approved', width: 1170, height: 2014 }] }),
    '/api/v1/layout-templates': envelope({ items: [{ template_id: 't', base_asset_id: 'a', status: 'approved' }] }),
    '/api/v1/phrase-templates': envelope({ items: [] }), '/api/v1/blogger-name-presets': envelope({ items: [] }),
    '/api/v1/styles': envelope({ items: [{ style_id: 7, name: 'SYNTHETIC 0178', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
    '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES), '/api/v1/render-jobs/capabilities?style_id=7': envelope(v3),
    '/api/v1/render-jobs': envelope({ job: { job_id: 'j', status: 'succeeded' }, items: [] }),
  }
  const { wrapper, logs } = mountPage(P5Batch, { plan }); wrappers.push(wrapper); await flushPromises()
  await wrapper.get('#render-style').setValue('7'); await flushPromises()
  expect(wrapper.get('[aria-label="英文手写字体"]').element).toHaveProperty('value', 'preset-caveat-600')
  expect(wrapper.get('[aria-label="标点及其他字体"]').element).toHaveProperty('value', 'preset-noto-cjk-sc-face2')
  expect(wrapper.get('[aria-label="排版重复种子"]').element).toHaveProperty('value', '2026100401')
  await wrapper.get('[aria-label="第 1 行粉丝昵称"]').setValue('shitT')
  await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
  await wrapper.findAll('button').find(b => b.text().includes('预览草稿'))!.trigger('click'); await flushPromises()
  await wrapper.findAll('button').find(b => b.text().includes('继续编辑'))!.trigger('click')
  await wrapper.findAll('button').find(b => b.text().includes('正式生成'))!.trigger('click'); await flushPromises()
  const bodies = logs.filter(l => l.method === 'POST' && l.url === '/api/v1/render-jobs').map(l => l.body as Record<string, unknown>)
  expect(bodies).toHaveLength(2)
  for (const body of bodies) expect(body).toMatchObject({ glyph_source_policy: 'm2_cjk_required',
    non_cjk_fonts: { latin: 'preset-caveat-600', other: 'preset-noto-cjk-sc-face2' }, ink_color_mode: 'custom-solid-srgb-v1',
    ink_color_rgb: [140, 48, 68], text_style: { ordinary_scale: 1.3, to_ratio: 2, header_gap: 10 },
    handwriting: { size_variation_pct: 0, tilt_variation_deg: 0, seed: 2026100401 } })
})
