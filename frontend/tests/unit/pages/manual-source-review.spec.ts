import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SourceAdmission from '@/components/SourceAdmission.vue'
import HumanDatasetReview from '@/components/HumanDatasetReview.vue'
const hash = 'a'.repeat(64)
let wrappers: VueWrapper[] = []
afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers = []; vi.unstubAllGlobals() })
const reply = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200 })
const admission = () => ({ asset_id: 'source1', style_id: 3, source_sha256: hash, source_status: 'approved', history_status: 'UNKNOWN', status: 'NOT_ADMITTED', receipt: null })
const item = () => ({ sample_id: 'ss1', label: '永', source_image_url: '/source.png', glyph_image_url: '/glyph.png', source_bbox_norm: { x_norm: .1, y_norm: .1, w_norm: .2, h_norm: .2 }, binding_sha256: hash, structural_checks: { clear_errors: [] as string[], suspicious_reasons: [] as string[] }, receipt: null })
function btn(w: VueWrapper, name: string) { return w.findAll('button').find(b => b.text() === name)! }
async function sourceSetup(value: unknown = admission()) {
  const posts: RequestInit[] = []; let postResult: Response | Promise<Response> = reply({})
  vi.stubGlobal('fetch', vi.fn(async (_: unknown, init?: RequestInit) => { if (init?.method === 'POST') { posts.push(init); return postResult } return reply(value) }))
  const wrapper = mount(SourceAdmission, { props: { assetId: 'source1', styleId: 3 } }); wrappers.push(wrapper); await flushPromises()
  async function fill() { for (const input of wrapper.findAll('input[type=checkbox]')) await input.setValue(true); await wrapper.find('textarea').setValue('Unit fixture explicit source use') }
  return { wrapper, posts, fill, setResult: (value: Response | Promise<Response>) => { postResult = value } }
}
describe('explicit source use declaration', () => {
  it('shows Chinese history and retains source hash only in closed advanced details', async () => {
    const { wrapper } = await sourceSetup()
    const ordinary = wrapper.element.cloneNode(true) as HTMLElement
    ordinary.querySelectorAll('details').forEach(details => details.remove())
    expect(ordinary.textContent).toContain('使用历史未知')
    expect(ordinary.textContent).not.toMatch(/UNKNOWN|SHA256|authenticated_user/)
    expect(ordinary.textContent).not.toContain(hash)
    expect(wrapper.get('details').text()).toContain(hash)
    expect(wrapper.get('details').attributes('open')).toBeUndefined()
  })
  it('starts unchecked; cancelling permissions sends no request', async () => {
    const { wrapper, posts, fill } = await sourceSetup()
    expect(btn(wrapper, '本人提交本张来源声明').attributes('disabled')).toBeDefined()
    await fill(); await btn(wrapper, '取消来源声明').trigger('click')
    expect(wrapper.findAll('input').every(i => !(i.element as HTMLInputElement).checked)).toBe(true); expect(posts).toHaveLength(0)
  })
  it('posts retained UNKNOWN history and honest authenticated user scope', async () => {
    const { wrapper, posts, fill } = await sourceSetup(); await fill(); await btn(wrapper, '本人提交本张来源声明').trigger('click'); await flushPromises()
    expect(posts).toHaveLength(1); expect(JSON.parse(String(posts[0].body))).toEqual({ style_id: 3, source_sha256: hash, origin: 'user_supplied_attachment', history_status: 'UNKNOWN', allow_unknown_history: true, actor_kind: 'authenticated_user', rights_confirmed: true, personal_training_confirmed: true, reason: 'Unit fixture explicit source use' })
  })
  it('pending source cannot use a user declaration to approve itself', async () => {
    const { wrapper, posts } = await sourceSetup({ ...admission(), source_status: 'pending' })
    expect(wrapper.find('fieldset').attributes('disabled')).toBeDefined(); expect(btn(wrapper, '本人提交本张来源声明').attributes('disabled')).toBeDefined(); expect(posts).toHaveLength(0)
  })
  it('single-flight submit prevents duplicate clicks', async () => {
    const { wrapper, posts, fill, setResult } = await sourceSetup(); let resolve!: (r: Response) => void
    setResult(new Promise<Response>(r => { resolve = r })); await fill(); await btn(wrapper, '本人提交本张来源声明').trigger('click')
    await btn(wrapper, '保存来源声明中…').trigger('click'); expect(posts).toHaveLength(1); resolve(reply({})); await flushPromises()
  })
  it('ambiguous failure requires refresh before any resubmit', async () => {
    const { wrapper, posts, fill, setResult } = await sourceSetup(); setResult(new Response(JSON.stringify({ code: 'SOURCE_CHANGED', detail: 'changed' }), { status: 409 }))
    await fill(); await btn(wrapper, '本人提交本张来源声明').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('请刷新核对记录'); expect(btn(wrapper, '本人提交本张来源声明').attributes('disabled')).toBeDefined(); expect(posts).toHaveLength(1)
  })
  it('restored source receipt shows actual actor without new consent writes', async () => {
    const { wrapper, posts } = await sourceSetup({ ...admission(), status: 'ADMITTED', receipt: { actor_kind: 'authenticated_user', actor: 'user:7', receipt_sha256: hash } })
    expect(wrapper.text()).toContain('user:7'); expect(wrapper.find('textarea').exists()).toBe(false); expect(posts).toHaveLength(0)
  })
  it('late source response cannot restore consent under a different profile', async () => {
    let resolve!: (r: Response) => void; let count = 0
    vi.stubGlobal('fetch', vi.fn(async () => ++count === 1 ? new Promise<Response>(r => { resolve = r }) : reply({ ...admission(), style_id: 4 })))
    const wrapper = mount(SourceAdmission, { props: { assetId: 'source1', styleId: 3 } }); wrappers.push(wrapper)
    await wrapper.setProps({ styleId: 4 }); await flushPromises(); resolve(reply({ ...admission(), status: 'ADMITTED', receipt: { actor: 'OLD_SCOPE', actor_kind: 'authenticated_user', receipt_sha256: hash } })); await flushPromises()
    expect(wrapper.text()).not.toContain('OLD_SCOPE'); expect(btn(wrapper, '本人提交本张来源声明').attributes('disabled')).toBeDefined()
  })
})
async function humanSetup(value = item()) {
  const posts: RequestInit[] = []; let postResult: Response | Promise<Response> = reply({})
  vi.stubGlobal('fetch', vi.fn(async (_: unknown, init?: RequestInit) => { if (init?.method === 'POST') { posts.push(init); return postResult } return reply({ style_id: 3, items: [value] }) }))
  const wrapper = mount(HumanDatasetReview, { props: { styleId: 3 } }); wrappers.push(wrapper); await flushPromises(); await btn(wrapper, '查看并审核字 永').trigger('click')
  async function fill(images = true) { if (images) for (const image of wrapper.findAll('img')) await image.trigger('load'); for (const input of wrapper.findAll('input[type=checkbox]')) await input.setValue(true); await wrapper.find('textarea').setValue('Unit fixture separate input pixel opinion') }
  return { wrapper, posts, fill, setResult: (value: Response | Promise<Response>) => { postResult = value } }
}
describe('separate human input pixel review', () => {
  it('shows real source plus alpha and starts without implied acceptance', async () => {
    const { wrapper, posts } = await humanSetup(); expect(wrapper.findAll('img').map(i => i.attributes('src'))).toEqual(['/source.png', '/glyph.png'])
    expect(btn(wrapper, '本人接受当前输入').attributes('disabled')).toBeDefined(); expect(posts).toHaveLength(0)
  })
  it('cannot accept before both images load; cancellation makes no mutation', async () => {
    const { wrapper, posts, fill } = await humanSetup(); await fill(false)
    expect(btn(wrapper, '本人接受当前输入').attributes('disabled')).toBeDefined(); await btn(wrapper, '取消输入审核').trigger('click'); expect(posts).toHaveLength(0)
  })
  it('posts exact pinned pixel binding and user checklist without owner/AI claims', async () => {
    const { wrapper, posts, fill } = await humanSetup(); await fill(); await btn(wrapper, '本人接受当前输入').trigger('click'); await flushPromises()
    const payload = JSON.parse(String(posts[0].body)); expect(payload.expected_binding_sha256).toBe(hash); expect(payload.decision).toBe('accept'); expect(Object.values(payload.checks)).toEqual([true, true, true, true]); expect(payload.owner_confirmed).toBeUndefined(); expect(payload.actor_kind).toBeUndefined(); expect(wrapper.emitted('reviewed')).toHaveLength(1)
  })
  it('structural failure or image read failure cannot be waived by checked boxes', async () => {
    const { wrapper, fill } = await humanSetup({ ...item(), structural_checks: { clear_errors: ['BLANK_OR_TOO_LITTLE_INK'], suspicious_reasons: [] } }); await fill()
    expect(btn(wrapper, '本人接受当前输入').attributes('disabled')).toBeDefined(); await wrapper.find('img').trigger('error'); expect(wrapper.text()).toContain('图片读取失败')
  })
  it('failure disables blind retry and review submit is single-flight', async () => {
    const { wrapper, posts, fill, setResult } = await humanSetup(); let resolve!: (r: Response) => void
    setResult(new Promise<Response>(r => { resolve = r })); await fill(); await btn(wrapper, '本人接受当前输入').trigger('click'); await btn(wrapper, '本人接受当前输入').trigger('click'); expect(posts).toHaveLength(1)
    resolve(new Response('{}', { status: 409 })); await flushPromises(); expect(wrapper.text()).toContain('先取消并刷新'); expect(btn(wrapper, '本人接受当前输入').attributes('disabled')).toBeDefined()
  })
  it('changing profile discards checklist while old submission completes', async () => {
    const { wrapper, posts, fill, setResult } = await humanSetup(); let resolve!: (r: Response) => void
    setResult(new Promise<Response>(r => { resolve = r })); await fill(); await btn(wrapper, '本人接受当前输入').trigger('click'); await wrapper.setProps({ styleId: 4 })
    expect(wrapper.find('textarea').exists()).toBe(false); resolve(reply({})); await flushPromises(); expect(posts).toHaveLength(1); expect(wrapper.emitted('reviewed')).toBeUndefined()
  })
})
