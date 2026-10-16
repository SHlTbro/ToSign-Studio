import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import S03Training from '@/components/S03Training.vue'

const hash = (letter: string) => letter.repeat(64)
const preflight = () => ({ target_style_id: 1, status: 'PREFLIGHT_READY', base_intake_sha256: hash('c'), blockers: [] as string[],
  eligible_confirmed_sample_count: 20, eligible_confirmed_characters: ['永'], m2_missing_unique_char_count: 0,
  eligible_parent_sources: [{ source_asset_id: 'a', source_asset_sha256: hash('a') }, { source_asset_id: 'b', source_asset_sha256: hash('b') }],
  family_components: [{ component_id: 'a', parent_hashes: [hash('a')], source_asset_ids: ['a'], family_ids: [] as string[] }, { component_id: 'b', parent_hashes: [hash('b')], source_asset_ids: ['b'], family_ids: [] as string[] }], explicit_family_split_required: true })
const blocked = () => ({ status: 'BLOCKED', ready: false, config: null, sample_ids: [], blockers: [{ code: 'RUNTIME_UNAVAILABLE', detail: 'Missing real model' }], capabilities: { create_m2_run: false } })
const ready = () => ({ status: 'READY', ready: true, config: { engine: 'm2', style_id: 1, snapshot_sha256: hash('d') }, sample_ids: [7, 8], snapshot: { snapshot_sha256: hash('d'), snapshot_id: 's', sample_count: 20, distinct_labels: 20 }, blockers: [], capabilities: { create_m2_run: true } })
let wrappers: VueWrapper[] = []
afterEach(() => { wrappers.forEach(wrapper => wrapper.unmount()); wrappers = []; vi.unstubAllGlobals() })
async function setup(p = preflight(), r: unknown = blocked()) {
  const posts: { path: string; body: unknown }[] = []
  const handlers = { preflight: p, runtime: r, freeze: { status: 'BLOCKED', snapshot_ready: false, snapshot_created: false, blockers: ['Real AI audit unavailable'] } as unknown }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (init?.method === 'POST') { posts.push({ path, body: JSON.parse(String(init.body)) }); return new Response(JSON.stringify({ data: await handlers.freeze })) }
    return new Response(JSON.stringify({ data: path.includes('snapshot-preflight') ? handlers.preflight : handlers.runtime }))
  }))
  const wrapper = mount(S03Training, { props: { styleId: 1 } }); wrappers.push(wrapper); await flushPromises()
  const button = (name: string) => wrapper.findAll('button').find(element => element.text() === name)!
  async function preview() {
    await wrapper.find('[aria-label="划分来源组 a"]').setValue('train'); await wrapper.find('[aria-label="划分来源组 b"]').setValue('val')
    await button('预览并冻结 S03').trigger('click')
  }
  return { wrapper, posts, handlers, button, preview }
}
describe('S03 canonical freeze / M2 capability UI (synthetic unit responses only)', () => {
  it('does not freeze or create when real data or runtime is blocked', async () => {
    const p = preflight(); p.m2_missing_unique_char_count = 19; p.blockers = ['Need 19 actual reviewed characters']
    const { wrapper, posts, button } = await setup(p)
    expect(button('预览并冻结 S03').attributes('disabled')).toBeDefined()
    expect(button('创建真实 M2 草稿运行').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('Missing real model'); expect(posts).toHaveLength(0)
  })
  it('requires both train and validation and cancellation never writes', async () => {
    const { wrapper, posts, button, preview } = await setup()
    expect(button('预览并冻结 S03').attributes('disabled')).toBeDefined()
    await preview()
    expect(button('提交冻结请求').attributes('disabled')).toBeDefined()
    expect(wrapper.find('fieldset').attributes('disabled')).toBeDefined()
    await button('取消冻结预览').trigger('click'); expect(posts).toHaveLength(0)
    expect(wrapper.find('#s03-confirm-heading').exists()).toBe(false)
  })
  it('posts exact server intake hash and whole-family SHA256 assignments after explicit acknowledgement', async () => {
    const { wrapper, posts, button, preview } = await setup(); await preview()
    await wrapper.find('[aria-label="确认当前来源分组与冻结操作"]').setValue(true)
    await button('提交冻结请求').trigger('click'); await flushPromises()
    expect(posts).toEqual([{ path: '/api/v1/training-samples/prepare-snapshot', body: { style_id: 1, expected_base_intake_sha256: hash('c'), parent_assignments: { [hash('a')]: 'train', [hash('b')]: 'val' } } }])
    expect(wrapper.text()).toContain('冻结被阻断'); expect(wrapper.text()).toContain('Real AI audit unavailable')
    expect(button('创建真实 M2 草稿运行').attributes('disabled')).toBeDefined()
  })
  it('does not trust a READY status lacking the full snapshot hash binding', async () => {
    const invalid = ready(); invalid.snapshot.snapshot_sha256 = hash('e')
    const { button, wrapper } = await setup(preflight(), invalid)
    expect(button('创建真实 M2 草稿运行').attributes('disabled')).toBeDefined(); expect(wrapper.emitted('createM2')).toBeUndefined()
  })
  it('rechecks runtime before emitting M2 config and never starts execution', async () => {
    const { wrapper, handlers, posts, button } = await setup(preflight(), ready())
    handlers.runtime = blocked(); await button('创建真实 M2 草稿运行').trigger('click'); await flushPromises()
    expect(wrapper.emitted('createM2')).toBeUndefined(); expect(posts).toHaveLength(0)
    expect(wrapper.text()).toContain('能力或快照已变化')
  })
  it('emits only server-derived M2 configuration and locked sample IDs', async () => {
    const { wrapper, posts, button } = await setup(preflight(), ready())
    await button('创建真实 M2 草稿运行').trigger('click'); await flushPromises()
    expect(wrapper.emitted('createM2')?.[0]).toEqual([{ config: ready().config, sample_ids: [7, 8] }]); expect(posts).toHaveLength(0)
  })
  it('an ambiguous freeze response blocks blind retry', async () => {
    const { wrapper, handlers, posts, button, preview } = await setup(); await preview()
    handlers.freeze = { status: 'READY', snapshot_ready: true, snapshot_created: true }
    await wrapper.find('[aria-label="确认当前来源分组与冻结操作"]').setValue(true)
    await button('提交冻结请求').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('冻结响应不完整'); expect(button('提交冻结请求').attributes('disabled')).toBeDefined(); expect(posts).toHaveLength(1)
  })
  it('cannot split the two parents of one family into train and validation', async () => {
    const p = preflight(); p.family_components = [{ component_id: 'same-family', parent_hashes: [hash('a'), hash('b')], source_asset_ids: ['a', 'b'], family_ids: ['f'] }]
    const { wrapper, button } = await setup(p)
    await wrapper.find('[aria-label="划分来源组 same-family"]').setValue('train')
    expect(button('预览并冻结 S03').attributes('disabled')).toBeDefined()
  })
  it('a double submit keeps one freeze request while the server is pending', async () => {
    const { wrapper, handlers, posts, button, preview } = await setup(); await preview()
    let finish!: (value: unknown) => void
    handlers.freeze = new Promise(resolve => { finish = resolve })
    await wrapper.find('[aria-label="确认当前来源分组与冻结操作"]').setValue(true)
    const submit = button('提交冻结请求')
    await Promise.all([submit.trigger('click'), submit.trigger('click')])
    expect(posts).toHaveLength(1); expect(wrapper.findAll('button').find(b => b.text() === '服务端核验中…')?.attributes('disabled')).toBeDefined()
    finish({ status: 'BLOCKED', snapshot_ready: false, snapshot_created: false, blockers: ['Synthetic unit response'] }); await flushPromises()
  })
})
