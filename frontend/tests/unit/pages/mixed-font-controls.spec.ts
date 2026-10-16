import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import Controls from '@/components/RenderStyleControls.vue'
import { SYSTEM_CAPABILITIES, M2_CAPABILITIES } from './render-capability-fixtures'
import { envelope } from './_harness'
import { __resetHttpGatesForTest } from '@/api/http'
import type { RenderCapabilities, RenderSelection } from '@/types/renderControls'

const wrappers: VueWrapper[] = []
const catalog: NonNullable<RenderCapabilities['non_cjk_fonts']> = {
  schema: 'non-cjk-font-catalog/1', missing_requested_fonts: ['Caveat'], fonts: [
    { id: 'system-segoe-print', name: 'Segoe Print', roles: ['latin'], available: true },
    { id: 'system-simkai', name: 'Windows KaiTi', roles: ['other'], available: true },
    { id: 'missing-Caveat', name: 'Caveat', roles: ['latin'], available: false },
  ],
}
const ready = (wrapper: VueWrapper) => wrapper.emitted('ready')?.at(-1)?.[0] as RenderSelection | null
function controls(fonts = catalog) {
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    const value = String(url).startsWith('/api/v1/styles')
      ? { items: [{ style_id: 7, name: 'SYNTHETIC M2 BOUNDARY', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }
      : String(url).includes('style_id=7') ? { ...M2_CAPABILITIES, source_policies: ['fallback_allowed', 'm2_required', 'm2_cjk_required'], non_cjk_fonts: fonts }
        : { ...SYSTEM_CAPABILITIES, non_cjk_fonts: fonts }
    return { ok: true, status: 200, json: async () => envelope(value).json } as Response
  }))
  const wrapper = mount(Controls, { props: { disabled: false } }); wrappers.push(wrapper); return wrapper
}
async function chooseMixed(wrapper: VueWrapper) {
  await flushPromises(); await wrapper.get('#render-style').setValue('7'); await flushPromises()
  await wrapper.findAll('select')[1]!.setValue('m2_cjk_required')
  await wrapper.get('[aria-label="分别指定英文和标点字体"]').setValue(true)
}
beforeEach(() => { localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks() })
describe('optional explicit mixed font selection: synthetic model capabilities only', () => {
  it('requires both explicit fonts and emits honest opt-in selection', async () => {
    const wrapper = controls(); await chooseMixed(wrapper); expect(ready(wrapper)).toBeNull()
    await wrapper.get('[aria-label="英文手写字体"]').setValue('system-segoe-print'); expect(ready(wrapper)).toBeNull()
    await wrapper.get('[aria-label="标点及其他字体"]').setValue('system-simkai')
    expect(ready(wrapper)).toMatchObject({ glyph_source_policy: 'm2_cjk_required', non_cjk_fonts: { latin: 'system-segoe-print', other: 'system-simkai' } })
    expect(wrapper.text()).toContain('英文不要求同一作者')
    expect(wrapper.text()).toContain('全角冒号保留原字符')
    expect(wrapper.get('option[value="missing-Caveat"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="分别指定英文和标点字体"]').setValue(false)
    expect(ready(wrapper)).not.toHaveProperty('non_cjk_fonts')
  })
  it('restores explicit selections after reload but blocks a now-unavailable font', async () => {
    const first = controls(); await chooseMixed(first)
    await first.get('[aria-label="英文手写字体"]').setValue('system-segoe-print')
    await first.get('[aria-label="标点及其他字体"]').setValue('system-simkai'); first.unmount()
    const second = controls(); await flushPromises()
    expect(ready(second)?.non_cjk_fonts).toEqual({ latin: 'system-segoe-print', other: 'system-simkai' }); second.unmount()
    const unavailable = structuredClone(catalog); unavailable.fonts[0]!.available = false
    const third = controls(unavailable); await flushPromises(); expect(ready(third)).toBeNull()
    expect((third.get('[aria-label="英文手写字体"]').element as HTMLSelectElement).value).toBe('system-segoe-print')
    expect(third.text()).toContain('请明确选择可用的英文手写字体')
  })
  it('strict-all and M0 choices do not consume retained optional font preferences', async () => {
    const wrapper = controls(); await chooseMixed(wrapper)
    await wrapper.get('[aria-label="英文手写字体"]').setValue('system-segoe-print')
    await wrapper.get('[aria-label="标点及其他字体"]').setValue('system-simkai')
    await wrapper.findAll('select')[1]!.setValue('m2_required')
    expect(ready(wrapper)?.glyph_source_policy).toBe('m2_required'); expect(ready(wrapper)).not.toHaveProperty('non_cjk_fonts')
    expect(wrapper.find('[data-testid="mixed-font-selection"]').exists()).toBe(false)
  })
  it('retains visible disabled selections if the current font catalog becomes empty', async () => {
    const first = controls(); await chooseMixed(first)
    await first.get('[aria-label="英文手写字体"]').setValue('system-segoe-print')
    await first.get('[aria-label="标点及其他字体"]').setValue('system-simkai'); first.unmount()
    const second = controls({ ...catalog, fonts: [] }); await flushPromises()
    expect(ready(second)).toBeNull()
    expect((second.get('[aria-label="英文手写字体"]').element as HTMLSelectElement).value).toBe('system-segoe-print')
    expect(second.get('option[value="system-segoe-print"]').attributes('disabled')).toBeDefined()
    expect(second.text()).toContain('当前不可用')
  })
})
