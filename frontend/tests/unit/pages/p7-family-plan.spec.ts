import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import SnapshotFamilyPlan from '@/components/SnapshotFamilyPlan.vue'
import { useAuthStore } from '@/stores/auth'
import { assertSnapshotPreflight } from '@/api/snapshotPlan'
import { __resetHttpGatesForTest } from '@/api/http'
import type { SnapshotPreflight } from '@/types/snapshot'
import { envelope, problem } from './_harness'

const sha = (letter: string) => letter.repeat(64)
const BASE: SnapshotPreflight = {
  schema: 'personal-snapshot-preflight/1', target_user_id: 2, target_style_id: 1, base_intake_sha256: sha('a'),
  eligible_parent_sources: ['b', 'c', 'd'].map((id) => ({ source_asset_id: `asset-${id}`, source_asset_sha256: sha(id),
    source_admission: { user_id: 2, style_id: 1, asset_id: `asset-${id}`, source_sha256: sha(id), receipt_sha256: sha('e'), history_status: 'UNKNOWN' } })),
  explicit_family_split_required: true,
  family_components: [{ component_id: 'related', parent_hashes: [sha('b'), sha('c')], source_asset_ids: ['asset-b', 'asset-c'], family_ids: ['family-1'] },
    { component_id: 'independent', parent_hashes: [sha('d')], source_asset_ids: ['asset-d'], family_ids: [] }],
  blockers: ['Explicit authenticated train/val family selection is required for this intake'], status: 'BLOCKED', structural_dataset_ready: false,
  snapshot_ready: false, snapshot_created: false, authorization_created: false, selection_requires_expected_base_intake_sha256: true,
}
const READY = { status: 'READY', target_user_id: 2, target_style_id: 1, base_intake_sha256: sha('a'), snapshot_ready: true, snapshot_created: true, snapshot_reused: false, blockers: [], dataset_snapshot_sha256: sha('f') }
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
function response(value: unknown): Response { return { ok: true, status: 200, json: async () => envelope(value).json } as Response }
function rejected(code: string): Response { return { ok: false, status: 409, json: async () => problem(409, code, 'server blocked current input').json } as Response }
const wrappers: VueWrapper[] = []
function setup(read: () => Promise<SnapshotPreflight> = async () => copy(BASE), post: () => Promise<Response> = async () => response(READY)) {
  const logs: { method: string; body?: unknown }[] = []
  const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? 'GET'; logs.push({ method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    return method === 'POST' ? await post() : response(await read())
  })
  vi.stubGlobal('fetch', fetcher)
  const pinia = createPinia(); const auth = useAuthStore(pinia)
  auth.__setUserForTest({ user_id: '2', username: 'fixture', display_name: 'Fixture', role: 'user', must_change_password: false })
  const wrapper = mount(SnapshotFamilyPlan, { props: { styleId: 1, disabled: false, refreshToken: 'initial' }, global: { plugins: [pinia] } })
  wrappers.push(wrapper)
  return { wrapper, logs, auth, fetcher }
}
const prepareButton = (wrapper: VueWrapper) => wrapper.findAll('button').find((node) => node.text() === '准备训练快照')!
const refreshButton = (wrapper: VueWrapper) => wrapper.findAll('button').find((node) => node.text() === '刷新来源分组')!
async function choose(wrapper: VueWrapper) { await wrapper.get('select[aria-label="来源组 1 用途"]').setValue('train'); await wrapper.get('select[aria-label="来源组 2 用途"]').setValue('val') }
beforeEach(() => { localStorage.clear(); __resetHttpGatesForTest() })
afterEach(() => { for (const wrapper of wrappers.splice(0)) wrapper.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('P7 explicit current-intake family plan', () => {
  it('discovers read-only, never assigns a default, and groups related parent sources', async () => {
    const { wrapper, logs } = setup(); await flushPromises()
    expect(logs.map((entry) => entry.method)).toEqual(['GET'])
    expect(wrapper.findAll('select')).toHaveLength(2)
    expect(wrapper.text()).toContain('2 张原图')
    expect(wrapper.text()).toContain('状态未知')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.findAll('select')[0]!.setValue('train')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.findAll('select')[1]!.setValue('train')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
  })
  it('submits exact complete parent assignment and latest server intake identity after preflight', async () => {
    const { wrapper, logs } = setup(); await flushPromises(); await choose(wrapper)
    await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(logs.map((entry) => entry.method)).toEqual(['GET', 'GET', 'POST'])
    expect(logs[2]!.body).toEqual({ style_id: 1, parent_assignments: { [sha('b')]: 'train', [sha('c')]: 'train', [sha('d')]: 'val' }, expected_base_intake_sha256: sha('a') })
    expect(wrapper.text()).toContain('S03 已冻结并通过校验')
    expect(wrapper.emitted('prepared')?.[0]).toEqual([READY])
  })
  it('restores only a same-owner/style/intake draft, without claiming server authorization', async () => {
    const one = setup(); await flushPromises(); await choose(one.wrapper); one.wrapper.unmount()
    const two = setup(); await flushPromises()
    expect(two.wrapper.text()).toContain('已恢复当前来源的分组草稿')
    expect(two.wrapper.text()).not.toContain('S03 已冻结并通过校验')
    expect((two.wrapper.findAll('select')[0]!.element as HTMLSelectElement).value).toBe('train')
    expect(two.logs.every((entry) => entry.method === 'GET')).toBe(true)
  })
  it('ignores a saved plan after intake changes even when all parent hashes stay the same', async () => {
    const one = setup(); await flushPromises(); await choose(one.wrapper); one.wrapper.unmount()
    const two = setup(async () => ({ ...copy(BASE), base_intake_sha256: sha('f') })); await flushPromises()
    expect(two.wrapper.text()).toContain('旧分组草稿与当前样本不匹配')
    expect(prepareButton(two.wrapper).attributes('disabled')).toBeDefined()
  })
  it('does not submit when the pre-submit read discovers a changed occurrence or sample', async () => {
    let count = 0
    const { wrapper, logs } = setup(async () => ({ ...copy(BASE), base_intake_sha256: ++count === 1 ? sha('a') : sha('f') }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
    expect(wrapper.text()).toContain('当前来源已变化，本次未提交')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
  })
  it('shows atomic server stale rejection and requires refresh before any explicit retry', async () => {
    const { wrapper, logs } = setup(undefined, async () => rejected('INTAKE_PREFLIGHT_STALE'))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('服务端检测到来源已变化')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    expect(wrapper.emitted('prepared')).toBeUndefined()
  })
  it('does not automatically retry an uncertain snapshot write after a lost response', async () => {
    const { wrapper, logs } = setup(undefined, async () => { throw new TypeError('response lost') })
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('快照结果尚未确认')
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    await refreshButton(wrapper).trigger('click'); await flushPromises()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    expect(prepareButton(wrapper).attributes('disabled')).toBeUndefined()
  })
  it('blocks duplicate clicks during preflight and writing', async () => {
    let finish!: (value: Response) => void
    const { wrapper, logs } = setup(undefined, () => new Promise((resolve) => { finish = resolve }))
    await flushPromises(); await choose(wrapper); const button = prepareButton(wrapper)
    await button.trigger('click'); await button.trigger('click'); await flushPromises()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(1)
    finish(response(READY)); await flushPromises()
    expect(wrapper.emitted('prepared')).toHaveLength(1)
  })
  it('rejects foreign ownership in discovery', async () => {
    const { wrapper, logs } = setup(async () => ({ ...copy(BASE), target_user_id: 9 }))
    await flushPromises(); expect(wrapper.text()).toContain('当前账号或档案不匹配')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })
  it('rejects missing group coverage, duplicate parents, and foreign source receipts', () => {
    for (const mutate of [(value: SnapshotPreflight) => { value.family_components.pop() },
      (value: SnapshotPreflight) => { value.family_components[1]!.parent_hashes.push(sha('b')) },
      (value: SnapshotPreflight) => { value.eligible_parent_sources[0]!.source_admission.user_id = 9 }]) {
      const bad = copy(BASE); mutate(bad); expect(() => assertSnapshotPreflight(bad, '2', 1)).toThrow()
    }
  })
  it('does not expose an old snapshot success after style changes during the write', async () => {
    let finish!: (value: Response) => void
    let active = copy(BASE)
    const { wrapper } = setup(async () => active, () => new Promise((resolve) => { finish = resolve }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    active = { ...copy(BASE), target_style_id: 2, eligible_parent_sources: BASE.eligible_parent_sources.map((source) => ({ ...source, source_admission: { ...source.source_admission, style_id: 2 } })) }
    await wrapper.setProps({ styleId: 2 }); await flushPromises(); finish(response(READY)); await flushPromises()
    expect(wrapper.emitted('prepared')).toBeUndefined()
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
  })
  it('clears plan on account change and ignores the old account response', async () => {
    let finish!: (value: SnapshotPreflight) => void
    const { wrapper, auth } = setup(() => new Promise((resolve) => { finish = resolve }))
    auth.__setUserForTest(null); finish(copy(BASE)); await flushPromises()
    expect(wrapper.findAll('select')).toHaveLength(0)
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
  })
  it('keeps blocked discovery and failed snapshot results visibly blocked', async () => {
    const { wrapper } = setup(undefined, async () => response({ status: 'BLOCKED', snapshot_ready: false, snapshot_created: false, snapshot_reused: false, blockers: ['NEED_VALIDATION_CHARACTERS'] }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('尚未满足快照冻结条件')
    expect(wrapper.text()).toContain('验证组还需补充合格字符')
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
  })
  it('invalidates a pending result when confirmed-sample state changes without a style change', async () => {
    let finish!: (value: Response) => void
    const { wrapper } = setup(undefined, () => new Promise((resolve) => { finish = resolve }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    await wrapper.setProps({ refreshToken: 'new-confirmed-samples' }); finish(response(READY)); await flushPromises()
    expect(wrapper.emitted('prepared')).toBeUndefined()
    expect(wrapper.text()).toContain('返回结果未作为当前来源快照显示')
    expect(wrapper.emitted('busy')?.at(-1)).toEqual([false])
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
  })
  it('does not send after a sample-state change while its fresh preflight is in flight', async () => {
    let finish!: (value: SnapshotPreflight) => void; let reads = 0
    const { wrapper, logs } = setup(() => ++reads === 1 ? Promise.resolve(copy(BASE)) : new Promise((resolve) => { finish = resolve }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click')
    await wrapper.setProps({ refreshToken: 'changed' }); finish(copy(BASE)); await flushPromises()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
    expect(wrapper.text()).toContain('本次未提交，请刷新来源')
  })
  it('blocks submission after discovery failure without discarding visible historical draft choices', async () => {
    let fail = false
    const { wrapper, logs } = setup(async () => { if (fail) throw new Error('network unavailable'); return copy(BASE) })
    await flushPromises(); await choose(wrapper); fail = true
    await refreshButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('network unavailable')
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })
  it('does not enable preparation when current intake identity is unavailable', async () => {
    const { wrapper } = setup(async () => ({ ...copy(BASE), base_intake_sha256: null, blockers: ['Missing current occurrence selection'] }))
    await flushPromises(); await choose(wrapper)
    expect(prepareButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('尚未选择当前采用的字样版本')
  })
  it('does not claim success from a READY response with no verified snapshot identity', async () => {
    const { wrapper } = setup(undefined, async () => response({ ...READY, dataset_snapshot_sha256: null }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('服务端快照结果不完整')
    expect(wrapper.emitted('prepared')).toBeUndefined()
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
  })
  it('clears old success immediately when new samples trigger a failed preflight', async () => {
    let fail = false
    const { wrapper } = setup(async () => { if (fail) throw new Error('offline'); return copy(BASE) })
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('S03 已冻结并通过校验')
    fail = true; await wrapper.setProps({ refreshToken: 'new-samples' }); await flushPromises()
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
    expect(wrapper.text()).toContain('offline')
  })
  it.each([{ target_user_id: 9 }, { target_style_id: 88 }, { base_intake_sha256: sha('f') }, { snapshot_created: false }, { snapshot_reused: true }])('rejects contradictory READY authority %j', async (change) => {
    const { wrapper } = setup(undefined, async () => response({ ...READY, ...change }))
    await flushPromises(); await choose(wrapper); await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(wrapper.emitted('prepared')).toBeUndefined()
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
  })
  it('keeps legacy ready preparation supported without inventing a client split', async () => {
    const { wrapper, logs } = setup(async () => ({ ...copy(BASE), explicit_family_split_required: false, structural_dataset_ready: true, status: 'PREFLIGHT_READY', blockers: [] }))
    await flushPromises(); expect(wrapper.findAll('select')).toHaveLength(0)
    await prepareButton(wrapper).trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST')?.body).toEqual({ style_id: 1 })
  })
})
