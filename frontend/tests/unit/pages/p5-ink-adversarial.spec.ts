import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import FrozenInkSummary from '@/components/FrozenInkSummary.vue'
import { INK_BINDING, INK_RENDER_CAPABILITIES, FROZEN_INK } from './ink-capability-fixtures'
import { SYSTEM_CAPABILITIES } from './render-capability-fixtures'
import { envelope, mountPage } from './_harness'
import { inkBindingIdentity } from '@/api/inkColor'

const wrappers: VueWrapper[] = []
const PENDING = 'yy:p5:pending-render:2'
const PLAN = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset1', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset1', name: 'Test', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template1', base_asset_id: 'asset1', status: 'approved', created_at: '2026-10-02' }] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 7, name: 'Adapter', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope(INK_RENDER_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { ...FROZEN_INK, job_id: 'second-job', status: 'succeeded' }, items: [] }),
}
async function click(w: VueWrapper, text: string) { await w.findAll('button').find(b => b.text() === text)!.trigger('click'); await flushPromises() }
async function ink(w: VueWrapper, palette: string) {
 await w.get('#render-style').setValue('7'); await flushPromises()
 await w.get('[aria-label="字形来源要求"]').setValue('m2_required')
 await w.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
 await w.get('[aria-label="墨色方式"]').setValue('source-solid-srgb-v1')
 await w.get('[aria-label="来源墨色"]').setValue(palette)
 await w.get('[aria-label="第 1 行粉丝昵称"]').setValue('  小雨\n 星河  ')
}
beforeEach(() => localStorage.clear())
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })

describe('independent expected-safety regression probes (failures expose gaps)', () => {
 it('preserves the first unknown submission when a previously open second instance submits', async () => {
  // Shared localStorage and separate mounted app state model two already-open tabs.
  const first = mountPage(P5Batch, { plan: PLAN }); wrappers.push(first.wrapper); await flushPromises(); await ink(first.wrapper, 'train-magenta-v1')
  const second = mountPage(P5Batch, { plan: PLAN }); wrappers.push(second.wrapper); await flushPromises(); await ink(second.wrapper, 'train-violet-v1')
  const normal = globalThis.fetch; let posts = 0
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
   if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') { posts++; if (posts === 1) throw new TypeError('lost response') }
   return normal(url, init)
  }))
  await click(first.wrapper, '正式生成')
  const saved = localStorage.getItem(PENDING)!; expect(JSON.parse(saved).body.ink_palette_id).toBe('train-magenta-v1')
  window.dispatchEvent(new StorageEvent('storage', { key: PENDING, oldValue: null, newValue: saved, storageArea: localStorage }))
  await flushPromises()
  await click(second.wrapper, '正式生成')
  expect(localStorage.getItem(PENDING), 'first uncertain request must remain durably recoverable').toBe(saved)
 })
 it.each([undefined, null, 'bad'])('warns when modern source proof is missing/has malformed full-binding digest %s', (digest) => {
  const w = mount(FrozenInkSummary, { props: { settings: { ...FROZEN_INK, ink_color_binding_sha256: digest } } }); wrappers.push(w)
  expect(w.find('[role="alert"]').exists(), 'modern complete proof requires a digest').toBe(true)
 })
 it('does not label partial modern color authority as a historical task', () => {
  const w = mount(FrozenInkSummary, { props: { settings: { ink_palette_id: 'train-magenta-v1', ink_color_binding: INK_BINDING } } }); wrappers.push(w)
  expect(w.find('[role="alert"]').exists(), 'partial modern metadata must not downgrade to historical').toBe(true)
 })
 it('does not label contradictory auto-plus-source authority as ordinary default', () => {
  const w = mount(FrozenInkSummary, { props: { settings: { ...FROZEN_INK, ink_color_mode: 'auto-contrast-v1' } } }); wrappers.push(w)
  expect(w.find('[role="alert"]').exists(), 'auto mode cannot carry source binding').toBe(true)
 })
 it('detects changed current-owned-source authority when core identity hashes stay the same', () => {
  const a = { ...INK_BINDING, current_owned_source_bindings: [{ source_admission_sha256: 'a'.repeat(64) }] }
  const b = { ...INK_BINDING, current_owned_source_bindings: [{ source_admission_sha256: 'b'.repeat(64) }] }
  expect(inkBindingIdentity(a), 'server binding equality includes live ownership/admission evidence').not.toBe(inkBindingIdentity(b))
 })
})
