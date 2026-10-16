import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P5Batch from '@/pages/P5_Batch.vue'
import P5Review from '@/pages/P5_Review.vue'
import type { RenderSelection } from '@/types/renderControls'
import { envelope, flushPromises, mountPage, type FetchLog, type FetchPlan } from './_harness'
import { M2_CAPABILITIES, SYSTEM_CAPABILITIES } from './render-capability-fixtures'

const CN = '  \u3000小雨  星河\u3000\n\n  春风\u3000 \n '
const ROWS_KEY = 'draft:batch-generation-rows'
const PREFERENCES_KEY = 'draft:batch-render-preferences:2'
const PENDING_KEY = 'yy:p5:pending-render:2'
const NATURAL: RenderSelection = {
  style_mode: 'profile', style_id: 7, glyph_source_policy: 'm2_required', layout_profile: 'natural-layout-v1', layout_seed: 314159,
}
const PLAN: FetchPlan = {
  '/api/v1/assets': envelope({ items: [{ asset_id: 'asset1', status: 'approved', width: 1080, height: 1920 }] }),
  '/api/v1/phrase-templates': envelope({ items: [{ id: 'bless1', text: '天天开心', enabled: true }] }),
  '/api/v1/blogger-name-presets': envelope({ items: [{ preset_id: 'preset1', name: '署名', is_default: true }] }),
  '/api/v1/layout-templates': envelope({ items: [{ template_id: 'template1', base_asset_id: 'asset1', status: 'approved', created_at: '2026-10-02' }] }),
  '/api/v1/styles': envelope({ items: [{ style_id: 7, name: 'Synthetic adapter', source_type: 'M2_ADAPTER', status: 'approved', enabled: true }] }),
  '/api/v1/render-jobs/capabilities': envelope(SYSTEM_CAPABILITIES),
  '/api/v1/render-jobs/capabilities?style_id=7': envelope(M2_CAPABILITIES),
  '/api/v1/render-jobs': envelope({ job: { job_id: 'fixture-render', status: 'succeeded' }, items: [] }),
}
interface Payload extends RenderSelection { draft?: boolean; items: { cn: string; sign_blogger: boolean }[] }
const wrappers: VueWrapper[] = []
const row = (cn: string, sign_blogger = false) => ({ cn, sign_blogger, blessing_id: '' })
const posts = (logs: FetchLog[]) => logs.filter(({ method, url }) => method === 'POST' && url === '/api/v1/render-jobs').map(({ body }) => body as Payload)
const editor = (wrapper: VueWrapper, index = 1) => wrapper.get(`[aria-label="第 ${index} 行粉丝昵称"]`)
async function click(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((node) => node.text() === text)
  if (!button) throw new Error(`Missing button: ${text}`)
  await button.trigger('click')
  await flushPromises()
}
async function openBatch(plan = PLAN) {
  const mounted = mountPage(P5Batch, { plan }); wrappers.push(mounted.wrapper)
  await flushPromises()
  return mounted
}
async function selectNatural(wrapper: VueWrapper) {
  await wrapper.get('#render-style').setValue('7'); await flushPromises()
  await wrapper.get('[aria-label="字形来源要求"]').setValue('m2_required')
  await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
  await wrapper.get('[aria-label="自然排版重复种子"]').setValue(String(NATURAL.layout_seed))
}
function restoreNatural(cn: string) {
  localStorage.setItem(ROWS_KEY, JSON.stringify([row(cn)]))
  localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ styleId: '7', sourcePolicy: 'm2_required', layout: 'natural-layout-v1', seed: NATURAL.layout_seed }))
}
beforeEach(() => { localStorage.clear() })
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = ''
})

describe('P5 natural-layout authored whitespace', () => {
  it('exposes an accessible multiline editor and sends identical exact text/settings in preview and formal lanes', async () => {
    const { wrapper, logs } = await openBatch()
    expect(editor(wrapper).element.tagName).toBe('INPUT')
    await selectNatural(wrapper)
    expect(editor(wrapper).element.tagName).toBe('TEXTAREA')
    expect(editor(wrapper).attributes('rows')).toBe('3')
    expect(editor(wrapper).attributes('aria-describedby')).toBe('natural-text-help-0')
    expect(wrapper.get('#natural-text-help-0').text()).toContain('回车在同一单内换行')
    await editor(wrapper).setValue(CN)
    await wrapper.get('#blessing-0').setValue('bless1')
    await click(wrapper, '预览草稿')
    expect(wrapper.get('[data-testid="draft-cn"]').element.textContent).toBe(CN)
    expect(wrapper.get('[data-testid="draft-cn"]').classes()).toContain('whitespace-break-spaces')
    await click(wrapper, '确认正式生成')
    const sent = posts(logs)
    expect(sent).toHaveLength(2)
    expect(sent[0]).toEqual({ ...sent[1], draft: true })
    for (const body of sent) {
      expect(body).toMatchObject({ ...NATURAL, items: [{ cn: CN, blessing: '天天开心', sign_blogger: true }] })
      expect(body.items[0]!.cn).toBe(CN)
    }
  })

  it.each(['baseline-v1', 'natural-layout-v1'])('rejects repeated whitespace-only preview/formal attempts in %s', async (layout) => {
    const { wrapper, logs } = await openBatch()
    if (layout === 'natural-layout-v1') await selectNatural(wrapper)
    await editor(wrapper).setValue('  \t\u3000\n\n ')
    for (const action of ['预览草稿', '正式生成', '预览草稿', '正式生成']) await click(wrapper, action)
    expect(posts(logs)).toHaveLength(0)
    expect(wrapper.find('[aria-label="批量生成草稿预览"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('请先录入至少 1 个粉丝 CN 昵称')
  })

  it('excludes whitespace-only signed rows without trimming the valid natural row', async () => {
    restoreNatural(CN)
    localStorage.setItem(ROWS_KEY, JSON.stringify([row(' \u3000\n ', true), row(CN)]))
    const { wrapper, logs } = await openBatch({ ...PLAN, '/api/v1/blogger-name-presets': envelope({ items: [] }) })
    await click(wrapper, '正式生成')
    expect(posts(logs)).toHaveLength(1)
    expect(posts(logs)[0]!.items).toEqual([{ cn: CN, sign_blogger: false, asset_id: 'asset1', template_id: 'template1' }])
    expect(wrapper.find('#signature-name-required').exists()).toBe(false)
  })

  it('retains legacy trim behavior and internal spacing/newlines for restored baseline text', async () => {
    localStorage.setItem(ROWS_KEY, JSON.stringify([row(CN)]))
    const { wrapper, logs } = await openBatch()
    expect(editor(wrapper).element.tagName).toBe('INPUT')
    await click(wrapper, '预览草稿'); await click(wrapper, '继续编辑'); await click(wrapper, '正式生成')
    expect(posts(logs)).toHaveLength(2)
    for (const body of posts(logs)) {
      expect(body.items[0]!.cn).toBe(CN.trim())
      expect(body).toMatchObject({ style_mode: 'system', glyph_source_policy: 'fallback_allowed', layout_profile: 'baseline-v1' })
      expect(body).not.toHaveProperty('layout_seed')
    }
  })

  it('preserves restored CRLF bytes in stored text and requests without a user edit', async () => {
    const restored = '\r\n\u3000 小雨\r\n\r\n星河 \u3000\r\n'
    restoreNatural(restored)
    const one = await openBatch()
    // HTML textarea values use LF; merely displaying the value must not rewrite stored/request bytes.
    expect((editor(one.wrapper).element as HTMLTextAreaElement).value).toBe(restored.replace(/\r\n/g, '\n'))
    one.wrapper.unmount()
    expect(JSON.parse(localStorage.getItem(ROWS_KEY)!)[0].cn).toBe(restored)
    const two = await openBatch()
    await click(two.wrapper, '预览草稿'); await click(two.wrapper, '继续编辑'); await click(two.wrapper, '正式生成')
    for (const body of posts(two.logs)) expect(body.items[0]!.cn).toBe(restored)
  })

  it('keeps multiline editing while an invalid seed blocks submission, then preserves edits after repair', async () => {
    const { wrapper, logs } = await openBatch(); await selectNatural(wrapper)
    await wrapper.get('[aria-label="自然排版重复种子"]').setValue('')
    expect(editor(wrapper).element.tagName).toBe('TEXTAREA')
    await editor(wrapper).setValue(CN)
    await click(wrapper, '正式生成')
    expect(posts(logs)).toHaveLength(0)
    await wrapper.get('[aria-label="自然排版重复种子"]').setValue('0')
    await click(wrapper, '正式生成')
    expect(posts(logs)[0]).toMatchObject({ ...NATURAL, layout_seed: 0, items: [{ cn: CN }] })
  })

  it('keeps authored multiline text through explicit baseline/natural switches without an edit', async () => {
    const { wrapper, logs } = await openBatch(); await selectNatural(wrapper); await editor(wrapper).setValue(CN)
    await wrapper.get('[aria-label="排版方式"]').setValue('baseline-v1')
    expect(editor(wrapper).element.tagName).toBe('INPUT')
    await wrapper.get('[aria-label="排版方式"]').setValue('natural-layout-v1')
    expect((editor(wrapper).element as HTMLTextAreaElement).value).toBe(CN)
    await click(wrapper, '正式生成')
    expect(posts(logs)[0]!.items[0]!.cn).toBe(CN)
  })

  it('does not lose exact natural text when a capability recheck temporarily blocks readiness', async () => {
    const { wrapper, logs } = await openBatch(); await selectNatural(wrapper); await editor(wrapper).setValue(CN)
    const original = globalThis.fetch
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => String(url).includes('/capabilities?style_id=7')
      ? new Promise<Response>((resolve) => { finish = resolve }) : original(url, init)))
    await click(wrapper, '重新核验所选风格')
    expect(editor(wrapper).element.tagName).toBe('TEXTAREA')
    await click(wrapper, '正式生成'); expect(posts(logs)).toHaveLength(0)
    finish({ ok: true, status: 200, json: async () => envelope(M2_CAPABILITIES).json } as Response)
    await flushPromises(); await click(wrapper, '正式生成')
    expect(posts(logs)[0]).toMatchObject({ ...NATURAL, items: [{ cn: CN }] })
  })

  it('freezes exact natural text/settings/key through response loss, later edits, reload and explicit retry', async () => {
    const one = await openBatch(); await selectNatural(one.wrapper); await editor(one.wrapper).setValue(CN)
    const original = globalThis.fetch
    let originalBody = ''; let originalKey: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') {
        originalBody = String(init.body); originalKey = new Headers(init.headers).get('Idempotency-Key')
        throw new TypeError('response lost')
      }
      return original(url, init)
    }))
    await click(one.wrapper, '正式生成')
    const saved = JSON.parse(localStorage.getItem(PENDING_KEY)!)
    expect(saved.body).toMatchObject({ ...NATURAL, items: [{ cn: CN }] })
    expect(saved.key).toBe(originalKey)
    await editor(one.wrapper).setValue(' 后改\n内容 '); one.wrapper.unmount()
    const two = await openBatch()
    expect(posts(two.logs)).toHaveLength(0)
    expect((editor(two.wrapper).element as HTMLTextAreaElement).value).toBe(' 后改\n内容 ')
    const secondFetch = globalThis.fetch
    let replay = ''; let replayKey: string | null = null
    vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/api/v1/render-jobs' && init?.method === 'POST') {
        replay = String(init.body); replayKey = new Headers(init.headers).get('Idempotency-Key')
      }
      return secondFetch(url, init)
    }))
    await click(two.wrapper, '重试原提交')
    expect(replay).toBe(originalBody); expect(replayKey).toBe(originalKey)
    expect(localStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it.each(['baseline-v1', 'natural-layout-v1'])('keeps bulk-paste row splitting/trimming unchanged for %s', async (layout) => {
    const { wrapper, logs } = await openBatch()
    if (layout === 'natural-layout-v1') await selectNatural(wrapper)
    await wrapper.findAll('button').find((node) => node.text().includes('从剪贴板粘贴粉丝名单'))!.trigger('click')
    if (layout === 'natural-layout-v1') expect(wrapper.text()).toContain('名单粘贴仍按换行分成多单，并去除每行首尾空白')
    await wrapper.get('textarea[placeholder^="格式："]').setValue(' \u3000小雨  \n\n 星河\u3000 \n')
    await click(wrapper, '一键智能解析分行'); await click(wrapper, '正式生成')
    expect(posts(logs)[0]!.items.map(({ cn }) => cn)).toEqual(['小雨', '星河'])
  })

  it('retains the signature-name guard for multiline natural text until an explicit unsigned choice', async () => {
    const { wrapper, logs } = await openBatch({ ...PLAN, '/api/v1/blogger-name-presets': envelope({ items: [] }) })
    await selectNatural(wrapper); await editor(wrapper).setValue(CN)
    await click(wrapper, '预览草稿'); await click(wrapper, '正式生成')
    expect(posts(logs)).toHaveLength(0)
    expect(wrapper.get('#signature-name-required').text()).toContain('请先选择或填写博主署名')
    await wrapper.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    await click(wrapper, '正式生成')
    expect(posts(logs)[0]!.items[0]).toMatchObject({ cn: CN, sign_blogger: false })
  })
})

describe('P5 review exact server-frozen text', () => {
  it.each(['baseline-v1', 'natural-layout-v1'])('uses returned cn and frozen %s for placeholder whitespace display', async (layout_profile) => {
    restoreNatural('local text must not replace server text')
    const mounted = mountPage(P5Review, { plan: {
      '/api/v1/render-jobs/job-a': envelope({
        job: { status: 'succeeded', ...NATURAL, layout_profile },
        items: [{ item_id: 'item-a', status: 'completed', cn: CN, output_relative_path: null, fallback_used: false, fallback_map: {}, warnings: [], render_version: 1 }],
      }),
    } }); wrappers.push(mounted.wrapper)
    await mounted.router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
    const placeholder = mounted.wrapper.get('[data-testid="review-cn"]')
    expect(placeholder.element.textContent).toBe(`To ${CN}`)
    expect(placeholder.classes().includes('whitespace-break-spaces')).toBe(layout_profile === 'natural-layout-v1')
    expect(mounted.wrapper.find('textarea[aria-label="第 1 行粉丝昵称"]').exists()).toBe(false)
  })
})
