import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive } from 'vue'
import type { VueWrapper } from '@vue/test-utils'
import NativeOccurrenceSelection from '@/components/NativeOccurrenceSelection.vue'
import { refKey } from '@/api/nativeIntake'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
import { JOB, OTHER_JOB, DRAFT, INTAKE, applied, version, plan } from './native-intake-fixtures'
const wrappers: VueWrapper[] = []
const inventory = () => ({ native_intake_sha256: INTAKE, versions: [version(), version(OTHER_JOB)], selection_required: true })
const receipt = () => ({ schema: 'native-occurrence-selection/1', user_id: 2, style_id: 9, native_intake_sha256: INTAKE, plan_sha256: 'a'.repeat(64), selected_sample_ids: [`sample-${OTHER_JOB}`], excluded_sample_ids: [`sample-${JOB}`], preserves_all_review_rows: true, authority: { actor_kind: 'delegated_ai', human_review: false, changes_review_decisions: false } })
function mount(extra: FetchPlan = {}) {
  const state = reactive({ styleId: 9, knownJobIds: [JOB, OTHER_JOB], refreshToken: 0 })
  const routes = plan({ '/api/v1/styles/9/occurrence-selection': envelope(inventory()), [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(applied()), [`/api/v1/candidate-review-jobs/${OTHER_JOB}`]: envelope(applied(OTHER_JOB)), ...extra })
  const ctx = mountPage(defineComponent({ setup: () => () => h(NativeOccurrenceSelection, state) }), { plan: routes }); wrappers.push(ctx.wrapper); return { ...ctx, state, routes }
}
function button(wrapper: VueWrapper, label: string) { const node = wrapper.findAll('button').find((item) => item.text().includes(label)); if (!node) throw new Error(`Missing ${label}`); return node }
function response(value: unknown): Response { return { ok: true, status: 200, json: async () => envelope(value).json } as Response }
async function choose(wrapper: VueWrapper) {
  const origin = refKey(version().ref)
  for (const node of wrapper.findAll('select')) await node.setValue(origin)
  await wrapper.get(`input[aria-label="唯一采用 ${DRAFT.candidate_id} ${OTHER_JOB}"]`).setValue(true)
  await flushPromises()
}
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.innerHTML = '' })
describe('P4 explicit native occurrence grouping', () => {
  it('distinguishes source boxes and matches same-character radio versions to their preview cards', async () => {
    const secondBox = { x_norm: .12, y_norm: .22, w_norm: .28, h_norm: .38 }
    const secondJob = applied(OTHER_JOB)
    secondJob.package.items[0]!.source_bbox_norm = secondBox
    const secondVersion = { ...version(OTHER_JOB), source_bbox_norm: secondBox }
    const { wrapper } = mount({ '/api/v1/styles/9/occurrence-selection': envelope({ ...inventory(), versions: [version(), secondVersion] }), [`/api/v1/candidate-review-jobs/${OTHER_JOB}`]: envelope(secondJob) })
    await flushPromises()
    const options = wrapper.findAll('select')[0]!.findAll('option').slice(1)
    expect(options[0]!.text()).toContain('横坐标 0.1 · 纵坐标 0.2 · 宽 0.3 · 高 0.4')
    expect(options[1]!.text()).toContain('横坐标 0.12 · 纵坐标 0.22 · 宽 0.28 · 高 0.38')
    expect(options.map(option => option.element.value)).toEqual([refKey(version().ref), refKey(secondVersion.ref)])
    expect(wrapper.text()).toContain('版本 1 · 春')
    expect(wrapper.text()).toContain('版本 2 · 春')
    await choose(wrapper)
    const radios = wrapper.findAll('input[type="radio"]')
    expect(radios.map(radio => radio.element.parentElement?.textContent)).toEqual(['唯一采用版本 1 · 春', '唯一采用版本 2 · 春'])
    expect(radios.map(radio => (radio.element as HTMLInputElement).value)).toEqual([refKey(version().ref), refKey(secondVersion.ref)])
  })
  it('requires every accepted revision assigned and exactly one chosen per original occurrence', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    expect(wrapper.findAll('select')).toHaveLength(2)
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
    await wrapper.findAll('select')[0]!.setValue(refKey(version().ref)); await flushPromises()
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
    await choose(wrapper)
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method === 'POST'
      ? (logs.push({ url: String(input), method: 'POST', body: JSON.parse(String(init.body)) }), Promise.resolve(response(receipt()))) : previous(input, init))
    await button(wrapper, '提交唯一版本').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST')?.body).toEqual({ native_intake_sha256: INTAKE, groups: [{ origin: version().ref, revisions: [version().ref, version(OTHER_JOB).ref], selected: version(OTHER_JOB).ref }] })
    expect(wrapper.get('[data-testid="native-occurrence-receipt"]').text()).toContain('保留 1')
    expect(wrapper.get('[data-testid="native-occurrence-receipt"]').text()).toContain('排除 1')
    expect(wrapper.text()).toContain('不是人工复核')
  })
  it('accepts a rejected original as an anchor but never as a selectable training revision', async () => {
    const original = applied(); original.result!.accepted = 0; original.result!.isolated = 1; original.result!.items[0] = { ...original.result!.items[0]!, canonical_sample_id: null, review_status: 'candidate', recommendation: 'AI_REJECT' }
    const { wrapper } = mount({ '/api/v1/styles/9/occurrence-selection': envelope({ ...inventory(), versions: [version(OTHER_JOB)] }), [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(original) }); await flushPromises()
    expect(wrapper.findAll('select')).toHaveLength(1)
    await wrapper.find('select').setValue(refKey(version().ref)); await flushPromises()
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(1)
    expect(wrapper.find('input[type="radio"]').attributes('aria-label')).toContain(OTHER_JOB)
  })
  it('detects stale intake before POST and requires fresh grouping', async () => {
    const { wrapper, routes, logs } = mount(); await flushPromises(); await choose(wrapper)
    routes['/api/v1/styles/9/occurrence-selection'] = envelope({ ...inventory(), native_intake_sha256: 'f'.repeat(64) })
    await button(wrapper, '提交唯一版本').trigger('click'); await flushPromises()
    expect(logs.some((entry) => entry.method === 'POST')).toBe(false)
    expect(wrapper.text()).toContain('旧选择未提交')
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
  })
  it('retains ambiguity errors from the server without claiming duplicate occurrences were resolved', async () => {
    const { wrapper } = mount(); await flushPromises(); await choose(wrapper)
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method === 'POST'
      ? Promise.resolve({ ok: false, status: 409, json: async () => problem(409, 'OCCURRENCE_SELECTION_BLOCKED', 'E_OCCURRENCE_OVERLAP').json } as Response) : previous(input, init))
    await button(wrapper, '提交唯一版本').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('字样位置存在重叠，请检查框选位置。')
    expect(wrapper.find('[data-testid="native-occurrence-receipt"]').exists()).toBe(false)
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
  })
  it('restores only a hash-bound local draft after reload, never a server success claim', async () => {
    const first = mount(); await flushPromises(); await choose(first.wrapper); first.wrapper.unmount(); wrappers.splice(wrappers.indexOf(first.wrapper), 1)
    const reload = mount(); await flushPromises()
    expect(reload.wrapper.text()).toContain('已恢复本机选择草稿')
    expect(reload.wrapper.find('[data-testid="native-occurrence-receipt"]').exists()).toBe(false)
    expect(button(reload.wrapper, '提交唯一版本').attributes('disabled')).toBeUndefined()
  })
  it('does not restore stale draft choices to a changed native intake', async () => {
    localStorage.setItem('yy:p4:occurrence-draft:2:9', JSON.stringify({ native_intake_sha256: '0'.repeat(64), assignment: { [refKey(version().ref)]: refKey(version().ref) }, selected: {} }))
    const { wrapper } = mount(); await flushPromises()
    expect(wrapper.findAll('select')[0]!.element).toHaveProperty('value', '')
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
  })
  it('fails closed when an accepted version resolves to another owner or mismatched AI result', async () => {
    const wrong = applied(); wrong.package.user_id = 99
    const { wrapper } = mount({ [`/api/v1/candidate-review-jobs/${JOB}`]: envelope(wrong) }); await flushPromises()
    expect(wrapper.text()).toContain('身份、来源或版本不匹配')
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
  })
  it('does not expose other-source anchors just because they have the same label', async () => {
    const other = applied(OTHER_JOB); other.package.items[0]!.source_asset_id = 'another-source'; other.package.items[0]!.source_image_sha256 = '8'.repeat(64)
    const otherVersion = { ...version(OTHER_JOB), source_asset_id: 'another-source', parent_source_hash: '8'.repeat(64) }
    const { wrapper } = mount({ '/api/v1/styles/9/occurrence-selection': envelope({ ...inventory(), versions: [version(), otherVersion] }), [`/api/v1/candidate-review-jobs/${OTHER_JOB}`]: envelope(other) }); await flushPromises()
    const choices = wrapper.findAll('select')
    expect(choices[0]!.findAll('option')).toHaveLength(2)
    expect(choices[1]!.findAll('option')).toHaveLength(2)
  })
  it('keeps submission locked while an existing POST is pending across known-job refresh', async () => {
    const { wrapper, state, routes } = mount(); await flushPromises(); await choose(wrapper)
    const previous = globalThis.fetch; let posts = 0
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method === 'POST'
      ? (posts++, new Promise<Response>(() => {})) : previous(input, init))
    await button(wrapper, '提交唯一版本').trigger('click'); await flushPromises()
    const extra = '3'.repeat(32); routes[`/api/v1/candidate-review-jobs/${extra}`] = envelope(applied(extra))
    state.knownJobIds.push(extra); await flushPromises()
    expect(posts).toBe(1)
    expect(button(wrapper, '提交唯一版本').attributes('disabled')).toBeDefined()
  })

  it('ignores a selection receipt that arrives after the profile changes', async () => {
    const { wrapper, state, routes } = mount(); await flushPromises(); await choose(wrapper)
    let finish!: (value: Response) => void; const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method === 'POST' ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '提交唯一版本').trigger('click'); await flushPromises()
    routes['/api/v1/styles/10/occurrence-selection'] = envelope({ native_intake_sha256: null, versions: [], selection_required: false })
    state.styleId = 10; state.knownJobIds = []; await flushPromises()
    finish(response(receipt())); await flushPromises()
    expect(wrapper.find('[data-testid="native-occurrence-receipt"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前没有需要去重选择')
  })
})
