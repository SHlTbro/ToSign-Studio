import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import OwnerDirectVisualAcceptance from '@/components/OwnerDirectVisualAcceptance.vue'
import { mountPage, envelope, flushPromises } from './_harness'

const path = '/api/v1/style-runs/8/owner-visual-acceptance'
const candidate = 'a'.repeat(64)
function ready(status = 'PENDING', evaluation = 'PASS') {
  return { status: 'READY', candidate_id: candidate, model_version: 'synthetic@fixture', evaluation_gate: evaluation,
    acceptance: { status }, images: [0, 1, 2].map(index => ({
      url: `/api/v1/style-runs/8/owner-visual-preview/${index}?candidate_id=${candidate}`,
      label: String(index), role: 'evaluation', sha256: 'b'.repeat(64),
    })) }
}
const host = defineComponent({ setup: () => () => h(OwnerDirectVisualAcceptance, { runId: 8, disabled: false }) })
const cleanup: (() => void)[] = []
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.unstubAllGlobals() })

describe('Owner direct acceptance UI; synthetic API fixtures only', () => {
  it('requires opening actual previews and explicit acknowledgement; submits scoped candidate only', async () => {
    const plan = { [path]: envelope(ready()) }
    const { wrapper, logs } = mountPage(host, { plan }); cleanup.push(() => wrapper.unmount())
    const panel = wrapper.findComponent(OwnerDirectVisualAcceptance)
    expect(panel.text()).toContain('本人直接同意')
    expect(logs).toHaveLength(0)
    await panel.find('button').trigger('click'); await flushPromises()
    expect(panel.findAll('img')).toHaveLength(3)
    expect(panel.findAll('button')[1].attributes('disabled')).toBeDefined()
    expect(panel.find('input').exists()).toBe(false)
    for (const img of panel.findAll('img')) await img.trigger('load')
    plan[path] = envelope(ready('ACCEPTED'))
    await panel.findAll('button')[1].trigger('click'); await flushPromises()
    const posts = logs.filter(row => row.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0].body).toEqual({ candidate_id: candidate, decision: 'accept', acknowledge_viewed_images: true,
      statement: '我已查看本次显示的真实输出并接受其外观，限定当前模型、快照和预览；不接受全部未来输出。' })
    expect(panel.emitted('accepted')).toHaveLength(1)
    expect(panel.find('input').exists()).toBe(false)
  })
  it('keeps objective failure visible and resets acknowledgement on refresh', async () => {
    const { wrapper, logs } = mountPage(host, { plan: { [path]: envelope(ready('PENDING', 'FAIL')) } })
    cleanup.push(() => wrapper.unmount()); const panel = wrapper.findComponent(OwnerDirectVisualAcceptance)
    await panel.find('button').trigger('click'); await flushPromises()
    expect(panel.text()).toContain('未通过')
    expect(panel.find('[data-testid="owner-objective-blocked"]').exists()).toBe(true)
    for (const img of panel.findAll('img')) await img.trigger('load')
    await panel.find('button').trigger('click'); await flushPromises()
    expect(panel.findAll('button')[1].attributes('disabled')).toBeDefined()
    for (const img of panel.findAll('img')) await img.trigger('load')
    expect(panel.findAll('button')[1].attributes('disabled')).toBeUndefined()
    expect(logs.filter(row => row.method === 'POST')).toHaveLength(0)
  })
  it('does not retry an uncertain POST; refresh can reconcile the server receipt', async () => {
    const { wrapper } = mountPage(host, { plan: { [path]: envelope(ready()) } })
    cleanup.push(() => wrapper.unmount()); const panel = wrapper.findComponent(OwnerDirectVisualAcceptance)
    await panel.find('button').trigger('click'); await flushPromises()
    for (const img of panel.findAll('img')) await img.trigger('load')
    let posts = 0
    vi.stubGlobal('fetch', async (_input: unknown, init?: RequestInit) => {
      if (init?.method === 'POST') { posts++; throw new TypeError('Response lost') }
      return { ok: true, status: 200, json: async () => envelope(ready('ACCEPTED')).json }
    })
    await panel.findAll('button')[1].trigger('click'); await flushPromises()
    expect(posts).toBe(1); expect(panel.emitted('accepted')).toBeUndefined()
    expect(panel.find('[role="alert"]').exists()).toBe(true)
    await panel.find('button').trigger('click'); await flushPromises()
    expect(posts).toBe(1); expect(panel.find('[role="status"]').exists()).toBe(true)
  })
  it('blocks acceptance when any actual preview fails to load', async () => {
    const { wrapper, logs } = mountPage(host, { plan: { [path]: envelope(ready()) } })
    cleanup.push(() => wrapper.unmount()); const panel = wrapper.findComponent(OwnerDirectVisualAcceptance)
    await panel.find('button').trigger('click'); await flushPromises()
    for (const img of panel.findAll('img')) await img.trigger('load')
    await panel.findAll('img')[1].trigger('error')
    expect(panel.text()).toContain('真实效果图片读取失败')
    expect(panel.findAll('button')[1].attributes('disabled')).toBeDefined()
    expect(logs.filter(row => row.method === 'POST')).toHaveLength(0)
  })
  it('discards a late context after changing run; blocked contexts cannot submit', async () => {
    const { wrapper } = mountPage(OwnerDirectVisualAcceptance, { plan: {} })
    cleanup.push(() => wrapper.unmount()); await wrapper.setProps({ runId: 8, disabled: false })
    let complete!: (value: unknown) => void
    vi.stubGlobal('fetch', () => new Promise(resolve => { complete = resolve }))
    await wrapper.find('button').trigger('click')
    await wrapper.setProps({ runId: 9 })
    complete({ ok: true, status: 200, json: async () => envelope(ready()).json }); await flushPromises()
    expect(wrapper.findAll('img')).toHaveLength(0)
    vi.stubGlobal('fetch', async () => ({ ok: true, status: 200, json: async () => envelope({ status: 'BLOCKED', detail: 'HASH_REQUIRED', images: [] }).json }))
    await wrapper.find('button').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('模型文件校验尚未完成'); expect(wrapper.find('input').exists()).toBe(false)
  })
})
