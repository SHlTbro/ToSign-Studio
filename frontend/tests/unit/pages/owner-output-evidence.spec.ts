import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import OwnerOutputEvidence from '@/components/OwnerOutputEvidence.vue'
import { mountPage, envelope, flushPromises } from './_harness'
const base = '/api/v1/style-runs/8', hash = 'b'.repeat(64), candidate = 'a'.repeat(64)
const item = 'synthetic-item', ctx = `${base}/owner-output-evidence/${item}`
function ready(status = 'NOT_RECORDED') { return { status: 'READY', package_ok: true, target_binding: { ok: true }, candidate_id: candidate,
  acceptance: { status }, model_sha256: 'c'.repeat(64), render_version: 1, may_publish: false,
  images: ['reference', 'target'].map(kind => ({ url: `${base}/owner-output-preview/${item}/${kind}?candidate_id=${candidate}`, label: kind })) } }
const host = defineComponent({ setup: () => () => h(OwnerOutputEvidence, { runId: 8, disabled: false }) })
const cleanup: (() => void)[] = []
function setup(value = ready()) {
  localStorage.setItem('owner-output-evidence:2:8', JSON.stringify({ package_id: hash, item_id: item }))
  const plan = { [`${base}/owner-output-targets`]: envelope({ items: [{ item_id: item, label: 'SYNTHETIC', render_version: 1 }] }), [ctx]: envelope(value) }
  const result = mountPage(host, { plan }); cleanup.push(() => result.wrapper.unmount()); return { ...result, plan }
}
afterEach(() => { cleanup.splice(0).forEach(f => f()); localStorage.clear(); vi.unstubAllGlobals() })
describe('Output-only owner evidence UI; synthetic transport fixtures', () => {
  it('refresh restores a scoped receipt using GET and never repeats confirmation', async () => {
    const { wrapper, logs } = setup(ready('ACCEPTED')); await flushPromises()
    expect(wrapper.find('[role="status"]').text()).toContain('此输出'); expect(logs.every(r => r.method === 'GET')).toBe(true)
    expect(wrapper.findAll('img')).toHaveLength(2); expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
  })
  it('requires both actual images and an unchecked acknowledgement; only posts exact candidate once', async () => {
    const { wrapper, logs, plan } = setup(); await flushPromises()
    const box = wrapper.find('input[type="checkbox"]'); expect(box.attributes('disabled')).toBeDefined()
    await wrapper.findAll('img')[0].trigger('load'); expect(box.attributes('disabled')).toBeDefined()
    await wrapper.findAll('img')[1].trigger('load'); await box.setValue(true)
    plan[ctx] = envelope(ready('ACCEPTED'))
    await wrapper.findAll('button').find(b => b.text() === '记录此输出的接受')?.trigger('click'); await flushPromises()
    const posts = logs.filter(r => r.method === 'POST'); expect(posts).toHaveLength(1)
    expect(posts[0].body).toMatchObject({ candidate_id: candidate, acknowledge_viewed_images: true, decision: 'accept' })
    expect(posts[0].body).not.toHaveProperty('target_binding'); expect(wrapper.find('[role="status"]').exists()).toBe(true)
  })
  it('client ok flags cannot make a blocked context acceptable', async () => {
    const value = { ...ready(), status: 'BLOCKED', detail: 'TARGET_BINDING_INVALID', images: [] }
    const { wrapper, logs } = setup(value); await flushPromises()
    expect(wrapper.text()).toContain('TARGET_BINDING_INVALID'); expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    expect(logs.filter(r => r.method === 'POST')).toHaveLength(0)
  })
  it('cancel and refresh discard loaded acknowledgement and leave receipts server-owned', async () => {
    const { wrapper, logs } = setup(); await flushPromises()
    for (const img of wrapper.findAll('img')) await img.trigger('load')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper.findAll('button').find(b => b.text() === '刷新实际绑定')?.trigger('click'); await flushPromises()
    expect((wrapper.find('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(false)
    expect(wrapper.find('input[type="checkbox"]').attributes('disabled')).toBeDefined(); expect(logs.filter(r => r.method === 'POST')).toHaveLength(0)
  })
  it('does not retry a lost confirmation response and GET recovers its receipt', async () => {
    const { wrapper } = setup(); await flushPromises(); for (const img of wrapper.findAll('img')) await img.trigger('load')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    let posts = 0
    vi.stubGlobal('fetch', async (_input: unknown, init?: RequestInit) => {
      if (init?.method === 'POST') { posts++; throw new TypeError('Lost response') }
      return { ok: true, status: 200, json: async () => envelope(ready('ACCEPTED')).json }
    })
    await wrapper.findAll('button').find(b => b.text() === '记录此输出的接受')?.trigger('click'); await flushPromises()
    expect(posts).toBe(1); expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    await wrapper.findAll('button').find(b => b.text() === '刷新实际绑定')?.trigger('click'); await flushPromises()
    expect(posts).toBe(1); expect(wrapper.find('[role="status"]').exists()).toBe(true)
  })
  it('rejects saved state without a current owned target and never fabricates images', async () => {
    localStorage.setItem('owner-output-evidence:2:8', JSON.stringify({ package_id: hash, item_id: 'foreign-item' }))
    const { wrapper, logs } = mountPage(host, { plan: { [`${base}/owner-output-targets`]: envelope({ items: [] }) } }); cleanup.push(() => wrapper.unmount())
    await flushPromises(); expect(wrapper.findAll('img')).toHaveLength(0); expect(logs).toHaveLength(1); expect(wrapper.text()).toContain('历史示例')
  })
})
