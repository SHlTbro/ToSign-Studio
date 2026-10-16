import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import P3 from '@/pages/P3_Annotate.vue'
import { useAuthStore } from '@/stores/auth'
import type { Slot } from '@/types/domain'
import { __resetHttpGatesForTest } from '@/api/http'

let saved: Slot[] | null
const wrappers: VueWrapper[] = []
beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  saved = null
  __resetHttpGatesForTest()
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    let data: unknown = { asset_id: 'a1', width: 1200, height: 1600 }
    if (url.includes('layout-templates')) {
      if (init?.method === 'POST') {
        saved = JSON.parse(String(init.body)).slots as Slot[]
        data = { template_id: 't-new' }
      } else if (url.includes('/layout-templates/t-new')) data = { template_id: 't-new', base_asset_id: 'a1', slots: saved, status: 'draft', version: 1 }
      else data = { items: saved ? [{ base_asset_id: 'a1', slots: saved }] : [] }
    }
    return new Response(JSON.stringify({ data, meta: {}, warnings: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
})
afterEach(() => {
  for (const w of wrappers.splice(0)) w.unmount()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})
async function open() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().__setUserForTest({ user_id: '2', username: 'font-test', display_name: 'Font Test', role: 'user', must_change_password: false })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/assets/:id/annotate', component: P3 }] })
  await router.push('/assets/a1/annotate')
  await router.isReady()
  const w = mount(P3, { global: { plugins: [pinia, router] } })
  wrappers.push(w)
  await flushPromises()
  return w
}
describe('P3 frozen text-size settings', () => {
  it('shows defaults, accepts dynamic text and changes live text preview', async () => {
    const w = await open()
    expect((w.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.3')
    expect((w.find('input[aria-label="To 相对字号"]').element as HTMLInputElement).value).toBe('2')
    const before = w.find('[data-testid="slot-text-preview"]').attributes('style')
    await w.find('input[aria-label="普通文字比例滑块"]').setValue('1.8')
    await w.find('input[aria-label="To 相对字号"]').setValue('2.5')
    await w.find('input[aria-label="预览昵称"]').setValue('独立昵称')
    expect(w.find('[data-testid="slot-text-preview"]').attributes('style')).not.toBe(before)
    expect(w.find('[data-testid="slot-text-preview"]').text()).toContain('独立昵称')
  })
  it('rejects empty and out-of-range input without changing saved sizes', async () => {
    const w = await open()
    await w.find('input[aria-label="普通文字比例"]').setValue('')
    expect(w.find('[role="alert"]').text()).toContain('0.5')
    await w.find('input[aria-label="To 相对字号"]').setValue('9')
    expect(w.find('[role="alert"]').exists()).toBe(true)
    await w.find('input[aria-label="普通文字比例"]').setValue('1.6')
    expect(w.find('[role="alert"]').exists()).toBe(false)
    expect((w.find('input[aria-label="To 相对字号"]').element as HTMLInputElement).value).toBe('2')
  })
  it('saves actual slot parameters and restores them from API after restart', async () => {
    const w = await open()
    await w.find('input[aria-label="普通文字比例"]').setValue('1.7')
    await w.find('input[aria-label="To 相对字号"]').setValue('2.4')
    await w.findAll('button').find(b => b.text().trim() === '保存模板')!.trigger('click')
    await flushPromises()
    const confirm = w.find('[role="dialog"]').findAll('button').find(b => b.text().includes('确认'))!
    await confirm.trigger('click')
    await flushPromises()
    expect(saved?.[0]?.text_style).toEqual({ ordinary_scale: 1.7, to_ratio: 2.4, header_gap: 10 })
    expect(saved?.[0]?.text_template).toContain('{cn}')
    w.unmount()
    wrappers.splice(wrappers.indexOf(w), 1)
    expect(localStorage.getItem('draft:annotate-slots-draft:a1')).toBeNull()
    const restarted = await open()
    expect((restarted.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.7')
    expect((restarted.find('input[aria-label="To 相对字号"]').element as HTMLInputElement).value).toBe('2.4')
  })
  it('restores a scoped draft, undo and cancel without a save', async () => {
    const w = await open()
    await w.find('input[aria-label="普通文字比例"]').setValue('1.9')
    await w.findAll('button').find(b => b.text().trim() === '撤销')!.trigger('click')
    expect((w.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.3')
    await w.find('input[aria-label="普通文字比例"]').setValue('1.5')
    window.dispatchEvent(new Event('pagehide'))
    w.unmount()
    wrappers.splice(wrappers.indexOf(w), 1)
    const restarted = await open()
    expect((restarted.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.5')
    await restarted.findAll('button').find(b => b.text().trim() === '保存模板')!.trigger('click')
    await flushPromises()
    await restarted.find('[role="dialog"]').findAll('button').find(b => b.attributes('aria-label')?.includes('关闭'))!.trigger('click')
    expect(saved).toBeNull()
  })
  it('keeps each asset draft separate when the same page instance changes route', async () => {
    const w = await open()
    await w.find('input[aria-label="普通文字比例"]').setValue('1.9')
    await w.vm.$router.push('/assets/a2/annotate')
    await flushPromises()
    expect((w.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.3')
    await w.find('input[aria-label="普通文字比例"]').setValue('1.6')
    await w.vm.$router.push('/assets/a1/annotate')
    await flushPromises()
    expect((w.find('input[aria-label="普通文字比例"]').element as HTMLInputElement).value).toBe('1.9')
    expect(JSON.parse(localStorage.getItem('draft:annotate-slots-draft:a2')!)[0].text_style.ordinary_scale).toBe(1.6)
  })
  it('resumes draft persistence for explicit edits and undo after a successful save', async () => {
    const w = await open()
    const size = () => w.find('input[aria-label="\u666e\u901a\u6587\u5b57\u6bd4\u4f8b"]')
    await size().setValue('1.7')
    await w.findAll('button').find(b => b.text().trim() === '\u4fdd\u5b58\u6a21\u677f')!.trigger('click')
    await flushPromises()
    await w.find('[role="dialog"]').findAll('button').find(b => b.text().includes('\u786e\u8ba4'))!.trigger('click')
    await flushPromises()
    expect(localStorage.getItem('draft:annotate-slots-draft:a1')).toBeNull()
    await size().setValue('1.9')
    await w.findAll('button').find(b => b.text().trim() === '\u64a4\u9500')!.trigger('click')
    window.dispatchEvent(new Event('pagehide'))
    expect(JSON.parse(localStorage.getItem('draft:annotate-slots-draft:a1')!)[0].text_style.ordinary_scale).toBe(1.7)
  })
  it('keeps the fullwidth colon in the scaled To header and saved template text', async () => {
    saved = [{ key: 'to_cn', text_template: 'To\uff1a{cn}', region: { x_norm: .1, y_norm: .1, w_norm: .8, h_norm: .2 }, text_style: { ordinary_scale: 1.3, to_ratio: 2, header_gap: 10 } }]
    const w = await open()
    expect(w.find('[data-testid="slot-text-preview"] span').text()).toBe('To\uff1a')
    expect(w.find('[data-testid="slot-text-preview"] span').attributes('style')).toContain('2em')
  })

})
