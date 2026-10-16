import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import Panel from '@/components/UserScrPreparation.vue'
import { USER_SCR_RECIPE, assertPreparation, validBudget } from '@/api/userScr'
import { runtimePath } from '@/api/m2Runtime'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
const registration = 'a'.repeat(32), snapshot = 'b'.repeat(64)
const endpoint = '/api/v1/styles/1/scr-preparations'
const key = `yy:p7:scr-preparation:2:1:${registration}:${snapshot}`
const prepared = { status: 'PREPARED', preparation_id: 'server-reference', cache_prepared: true, style_id: 1, snapshot_sha256: snapshot }
const wrappers: VueWrapper[] = []
function mount(plan: FetchPlan, extra = {}) {
  const host = defineComponent({ render: () => h(Panel, { styleId: 1, registrationId: registration, snapshotSha: snapshot,
    blockers: [{ code: 'PREPARATION_REQUIRED', detail: 'No record yet; prepare_user_scr is false' }], runtimeError: null, disabled: false, ...extra }) })
  const result = mountPage(host, { plan }); wrappers.push(result.wrapper); return result
}
function button(wrapper: VueWrapper, label: string) { return wrapper.findAll('button').find(node => node.text().includes(label))! }
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
describe('SCR preparation contract fixtures; no actual cache/training/qualification', () => {
  it('recovers an uncertain request by reading an explicit owned job without repeating POST', async () => {
    localStorage.setItem(key, JSON.stringify({ job_id: null, uncertain: true }))
    const id = 'c'.repeat(64)
    const { wrapper, logs } = mount({ [`${endpoint}/${id}`]: envelope(prepared) }); await flushPromises()
    await wrapper.get('[aria-label="服务端准备作业标识"]').setValue(id)
    await button(wrapper, '读取已有准备作业').trigger('click'); await flushPromises()
    expect(logs).toEqual([{ method: 'GET', url: `${endpoint}/${id}`, body: undefined }])
    expect(wrapper.text()).toContain('缓存已准备')
  })
  it('does not abandon a preparation POST after 60 seconds or repeat it', async () => {
    const { wrapper } = mount({}); await flushPromises()
    let signal: AbortSignal | undefined; let posts = 0
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal as AbortSignal; posts++
      return new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => resolve(new Response(JSON.stringify(envelope(prepared).json), { status: 200 })), 84000)
        signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('deadline', 'AbortError')) }, { once: true })
      })
    }))
    await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(60001); await flushPromises()
    expect(signal?.aborted).toBe(false); expect(posts).toBe(1)
    await vi.advanceTimersByTimeAsync(23999); await flushPromises()
    expect(wrapper.text()).toContain('缓存已准备')
  })
  it('allows the first preparation despite the discovery preparation capability being false; accepts immediate PREPARED without a job', async () => {
    const { wrapper, logs } = mount({ [endpoint]: envelope(prepared) }); await flushPromises()
    expect(button(wrapper, '准备个人快照 SCR').attributes('disabled')).toBeUndefined()
    await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(logs).toEqual([{ method: 'POST', url: endpoint, body: { snapshot_sha256: snapshot, runtime_registration_id: registration } }])
    expect(wrapper.text()).toContain('缓存已准备'); expect(wrapper.text()).toContain('启动与发布仍受服务端门控')
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ preparation_id: 'server-reference', uncertain: false })
    expect(button(wrapper, '准备个人快照 SCR').attributes('disabled')).toBeDefined()
  })
  it.each(['RUNTIME_UNQUALIFIED', 'FONT_COVERAGE', 'SNAPSHOT_UNAVAILABLE'])('blocks a real prerequisite: %s', async (code) => {
    const { wrapper, logs } = mount({}, { blockers: [{ code, detail: 'Missing' }] }); await flushPromises()
    await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(logs).toHaveLength(0)
  })
  it('polls the known job after refresh, keeps repeat POST disabled and can pause/resume only polling', async () => {
    const plan = { [endpoint]: envelope({ status: 'PREPARING', job_id: 'job one' }), [`${endpoint}/job%20one`]: envelope(prepared) }
    const first = mount(plan); await flushPromises(); await button(first.wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(button(first.wrapper, '准备个人快照 SCR').attributes('disabled')).toBeDefined()
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ job_id: 'job one', uncertain: false })
    await button(first.wrapper, '暂停进度读取').trigger('click'); await vi.advanceTimersByTimeAsync(5000); await flushPromises()
    expect(first.logs).toHaveLength(1)
    first.wrapper.unmount(); wrappers.splice(wrappers.indexOf(first.wrapper), 1)
    const next = mount(plan); await flushPromises()
    expect(next.logs).toEqual([{ method: 'GET', url: `${endpoint}/job%20one`, body: undefined }])
    expect(next.wrapper.text()).toContain('缓存已准备')
  })
  it('persists an uncertain POST before transmission and never silently repeats it on reload', async () => {
    const first = mount({ [endpoint]: problem(503, 'UNAVAILABLE', 'Disconnected after request') }); await flushPromises()
    await button(first.wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ job_id: null, uncertain: true })
    first.wrapper.unmount(); wrappers.splice(wrappers.indexOf(first.wrapper), 1)
    const next = mount({}); await flushPromises()
    expect(next.logs).toHaveLength(0); expect(next.wrapper.text()).toContain('不会自动重发')
    expect(button(next.wrapper, '准备个人快照 SCR').attributes('disabled')).toBeDefined()
  })
  it('keeps an immediate preparation pointer across re-entry without treating browser storage as a verified cache', async () => {
    localStorage.setItem(key, JSON.stringify({ preparation_id: 'previous-reference', uncertain: false }))
    const { wrapper, logs } = mount({}); await flushPromises()
    expect(wrapper.text()).toContain('当前缓存状态需重新检查服务端环境')
    expect(wrapper.text()).not.toContain('缓存已准备：')
    expect(button(wrapper, '准备个人快照 SCR').attributes('disabled')).toBeDefined()
    expect(logs).toHaveLength(0)
  })
  it('shows an exact prerequisite error and permits explicit retry after a definite 409', async () => {
    const plan: FetchPlan = { [endpoint]: problem(409, 'STALE_CACHE', 'Snapshot changed') }
    const { wrapper, logs } = mount(plan); await flushPromises(); await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('训练缓存已过期，请重新准备缓存。'); expect(localStorage.getItem(key)).toBeNull()
    plan[endpoint] = envelope(prepared)
    await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises(); expect(logs).toHaveLength(2)
  })
  it('retains job recovery after a failed GET and sends no second POST', async () => {
    localStorage.setItem(key, JSON.stringify({ job_id: 'known', uncertain: false }))
    const plan: FetchPlan = { [`${endpoint}/known`]: problem(404, 'STYLE_NOT_FOUND', 'Unavailable') }
    const { wrapper, logs } = mount(plan); await flushPromises()
    expect(wrapper.text()).toContain('找不到此字体'); expect(localStorage.getItem(key)).not.toBeNull()
    plan[`${endpoint}/known`] = envelope(prepared)
    await button(wrapper, '刷新准备进度').trigger('click'); await flushPromises()
    expect(logs.every(row => row.method === 'GET')).toBe(true)
  })
  it('rejects a cross-style or altered snapshot prepared result and does not promote it', async () => {
    const { wrapper } = mount({ [endpoint]: envelope({ ...prepared, style_id: 2 }) }); await flushPromises()
    await button(wrapper, '准备个人快照 SCR').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('不匹配'); expect(wrapper.text()).not.toContain('缓存已准备')
    expect(() => assertPreparation({ ...prepared, snapshot_sha256: 'c'.repeat(64) }, 1, snapshot)).toThrow()
  })
  it('limits the budget and preserves an explicit recipe in discovery', () => {
    for (const value of [0, -1, Infinity, NaN, 86401, '3600']) expect(validBudget(value)).toBe(false)
    expect(validBudget(3600)).toBe(true); expect(validBudget(86400)).toBe(true)
    expect(runtimePath(1, registration, USER_SCR_RECIPE)).toBe(`/api/v1/styles/1/m2-runtime?recipe_id=${USER_SCR_RECIPE}&runtime_registration_id=${registration}`)
  })
})
