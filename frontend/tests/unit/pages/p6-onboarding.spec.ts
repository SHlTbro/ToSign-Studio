import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P6_NewStyle from '@/pages/P6_NewStyle.vue'
import P6_Styles from '@/pages/P6_Styles.vue'
import P4_Extract from '@/pages/P4_Extract.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const draft = { style_id: 42, name: '我的手写', source_type: 'HANDWRITING_ASSET', status: 'draft', receipt_id: null }
const emptyCatalog = envelope({ items: [], catalog: { status: 'not_installed', message: '此安装未包含可选预设字体资源' }, fallback: { available: true } })
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('Packaged Base handwriting onboarding', () => {
  it('shows the real foreign-profile 404 envelope with an empty detail and keeps recovery visible', async () => {
    const missing = { status: 404, json: { type: 'about:blank', title: 'Style not found', status: 404, detail: '', instance: '/api/v1/styles/42', code: 'STYLE_NOT_FOUND', retryable: false, trace_id: 'qa' } }
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog, '/api/v1/styles/42': missing } })
    await router.replace('/styles/new?style_id=42'); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('找不到此字体')
    expect(wrapper.text()).toContain('重新读取档案')
    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.find('input[aria-label="确认档案准入"]').exists()).toBe(false)
    expect(logs.every((call) => call.method === 'GET')).toBe(true)
    wrapper.unmount()
  })
  it('missing optional presets stays usable and does not advertise counterfeit fonts', async () => {
    const { wrapper, logs } = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog } })
    await flushPromises()
    expect(wrapper.text()).toContain('此安装未包含可选预设字体资源')
    expect(wrapper.text()).toContain('本机楷体（simkai）')
    expect(wrapper.text()).not.toContain('加载失败')
    expect(wrapper.findAll('[data-preset-id]')).toHaveLength(0)
    expect(wrapper.find('#handwriting-profile-name').exists()).toBe(true)
    expect(logs.filter((call) => call.url === '/api/v1/style-presets')).toHaveLength(1)
    wrapper.unmount()
  })

  it('creates one draft, preserves its recovery URL and requires a separate explicit source approval', async () => {
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan: {
      '/api/v1/style-presets': emptyCatalog,
      '/api/v1/styles': envelope(draft),
      '/api/v1/styles/42/approve': envelope({ ...draft, status: 'approved' }),
    } })
    await flushPromises()
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.find('#handwriting-profile-name').setValue('我的手写')
    const form = wrapper.find('form')
    void form.trigger('submit')
    void form.trigger('submit')
    await flushPromises()
    expect(logs.filter((call) => call.url === '/api/v1/styles' && call.method === 'POST')).toHaveLength(1)
    expect(logs.find((call) => call.url === '/api/v1/styles' && call.method === 'POST')?.body).toEqual({ name: '我的手写', source_type: 'HANDWRITING_ASSET' })
    expect(router.currentRoute.value.fullPath).toBe('/styles/new?style_id=42')
    expect(wrapper.text()).toContain('草稿已保存')
    const approve = wrapper.findAll('button').find((button) => button.text() === '确认准入档案')!
    expect(approve.attributes('disabled')).toBeDefined()
    expect(logs.some((call) => call.url.endsWith('/approve'))).toBe(false)
    await wrapper.find('input[type="checkbox"]').setValue(true)
    void approve.trigger('click')
    void approve.trigger('click')
    await flushPromises()
    expect(logs.filter((call) => call.url.endsWith('/approve') && call.method === 'POST')).toHaveLength(1)
    expect(wrapper.text()).toContain('来源档案已准入')
    expect(wrapper.find('a[href="/extract?style_id=42"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/training?style_id=42"]').exists()).toBe(true)
    expect(logs.some((call) => /style-runs|prepare-snapshot|batch-review|ai-confirm/.test(call.url))).toBe(false)
    wrapper.unmount()
  })

  it('recovers an interrupted draft without creating or approving it again', async () => {
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan: {
      '/api/v1/style-presets': emptyCatalog, '/api/v1/styles/42': envelope(draft),
    } })
    await router.replace('/styles/new?style_id=42')
    await flushPromises()
    expect(wrapper.text()).toContain('草稿已保存')
    expect(wrapper.find('input[type="checkbox"]').element).toHaveProperty('checked', false)
    expect(logs.every((call) => call.method === 'GET')).toBe(true)
    await router.replace('/styles/new?style_id=bad')
    await flushPromises()
    expect(wrapper.text()).toContain('档案编号无效')
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('foreign or trained profiles cannot be approved through the onboarding form', async () => {
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan: {
      '/api/v1/style-presets': emptyCatalog,
      '/api/v1/styles/42': problem(404, 'STYLE_NOT_FOUND', 'Style not found'),
      '/api/v1/styles/43': envelope({ ...draft, style_id: 43, source_type: 'M2_ADAPTER', receipt_id: 'real-receipt' }),
    } })
    await router.replace('/styles/new?style_id=42'); await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('找不到此字体')
    expect(wrapper.find('form').exists()).toBe(false)
    await router.replace('/styles/new?style_id=43'); await flushPromises()
    expect(wrapper.text()).toContain('不能审核训练发布结果')
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    expect(logs.every((call) => call.method === 'GET')).toBe(true)
    wrapper.unmount()
  })

  it('failed approval preserves the draft and retries only the same profile action', async () => {
    const plan: FetchPlan = {
      '/api/v1/style-presets': emptyCatalog,
      '/api/v1/styles/42': envelope(draft),
      '/api/v1/styles/42/approve': problem(503, 'UNAVAILABLE', 'Temporary failure'),
    }
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan })
    await router.replace('/styles/new?style_id=42'); await flushPromises()
    await wrapper.find('input[type="checkbox"]').setValue(true)
    const approve = wrapper.findAll('button').find((button) => button.text() === '确认准入档案')!
    await approve.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('操作暂未完成')
    expect(wrapper.text()).toContain('草稿已保存')
    plan['/api/v1/styles/42/approve'] = envelope({ ...draft, status: 'approved' })
    await approve.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('来源档案已准入')
    expect(logs.filter((call) => call.url.endsWith('/approve'))).toHaveLength(2)
    expect(logs.some((call) => call.url === '/api/v1/styles' && call.method === 'POST')).toBe(false)
    wrapper.unmount()
  })

  it('uncertain create does not retry or allow a duplicate and points to saved records', async () => {
    const { wrapper, logs } = mountPage(P6_NewStyle, { plan: {
      '/api/v1/style-presets': emptyCatalog, '/api/v1/styles': problem(503, 'UNAVAILABLE', 'interrupted'),
    } })
    await flushPromises()
    await wrapper.find('#handwriting-profile-name').setValue('我的手写')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('避免重复创建')
    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(logs.filter((call) => call.url === '/api/v1/styles' && call.method === 'POST')).toHaveLength(1)
    wrapper.unmount()
    const reload = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog } })
    await flushPromises()
    expect(reload.wrapper.text()).toContain('避免重复创建')
    expect(reload.wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    expect(reload.logs.every((call) => call.method === 'GET')).toBe(true)
    await reload.wrapper.find('input[aria-label="已检查风格库并决定重新创建"]').setValue(true)
    await reload.wrapper.findAll('button').find((button) => button.text() === '清除未确认状态，允许新建')!.trigger('click')
    expect(sessionStorage.getItem('yy:p6:pending-profile:2')).toBeNull()
    reload.wrapper.unmount()
  })

  it('pending guard is account-scoped and can reconcile only against a fetched owned profile', async () => {
    sessionStorage.setItem('yy:p6:pending-profile:2', JSON.stringify({ version: 1, name: 'other account pending' }))
    const admin = mountPage(P6_NewStyle, { asAdmin: true, plan: { '/api/v1/style-presets': emptyCatalog } })
    await flushPromises()
    expect(admin.wrapper.text()).not.toContain('创建结果尚未确认')
    expect(admin.wrapper.text()).not.toContain('other account pending')
    admin.wrapper.unmount()
    const owner = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog, '/api/v1/styles/42': envelope(draft) } })
    await owner.router.replace('/styles/new?style_id=42'); await flushPromises()
    expect(owner.logs.some((call) => call.url === '/api/v1/styles/42')).toBe(true)
    await owner.wrapper.findAll('button').find((button) => button.text() === '使用当前已保存档案结束未确认状态')!.trigger('click')
    expect(sessionStorage.getItem('yy:p6:pending-profile:2')).toBeNull()
    expect(owner.logs.every((call) => call.method === 'GET')).toBe(true)
    owner.wrapper.unmount()
  })

  it('unavailable session storage stops before sending a create request', async () => {
    const { wrapper, logs } = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog } })
    await flushPromises()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage blocked') })
    await wrapper.find('#handwriting-profile-name').setValue('我的手写')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('尚未发送创建请求')
    expect(logs.some((call) => call.method === 'POST')).toBe(false)
    wrapper.unmount()
  })

  it('explicit reread after ambiguous approval fetches authoritative status', async () => {
    const plan: FetchPlan = {
      '/api/v1/style-presets': emptyCatalog, '/api/v1/styles/42': envelope(draft),
      '/api/v1/styles/42/approve': problem(503, 'UNAVAILABLE', 'Temporary failure'),
    }
    const { wrapper, logs, router } = mountPage(P6_NewStyle, { plan })
    await router.replace('/styles/new?style_id=42'); await flushPromises()
    await wrapper.find('input[aria-label="确认档案准入"]').setValue(true)
    await wrapper.findAll('button').find((button) => button.text() === '确认准入档案')!.trigger('click'); await flushPromises()
    plan['/api/v1/styles/42'] = envelope({ ...draft, status: 'approved' })
    const before = logs.filter((call) => call.url === '/api/v1/styles/42').length
    await wrapper.findAll('button').find((button) => button.text() === '重新读取档案')!.trigger('click'); await flushPromises()
    expect(logs.filter((call) => call.url === '/api/v1/styles/42')).toHaveLength(before + 1)
    expect(wrapper.text()).toContain('来源档案已准入')
    wrapper.unmount()
  })

  it('leaving during creation does not navigate back when the request completes', async () => {
    const { wrapper, router } = mountPage(P6_NewStyle, { plan: { '/api/v1/style-presets': emptyCatalog } })
    await flushPromises()
    const before = globalThis.fetch
    let resolve!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/styles'
      ? new Promise<Response>((done) => { resolve = done }) : before(input, init))
    await wrapper.find('#handwriting-profile-name').setValue('我的手写')
    void wrapper.find('form').trigger('submit'); await flushPromises()
    wrapper.unmount()
    await router.replace('/styles')
    resolve(new Response(JSON.stringify(envelope(draft).json), { status: 201 }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/styles')
  })

  it('saved drafts expose the same explicit recovery entry from the style library', async () => {
    const { wrapper } = mountPage(P6_Styles, { plan: { '/api/v1/styles': envelope({ items: [{ ...draft, coverage: null, enabled: true, font_url: null }] }) } })
    await flushPromises()
    expect(wrapper.find('a[href="/styles/new?style_id=42"]').text()).toBe('继续确认手写来源')
    wrapper.unmount()
  })

  it('new P4 upload entry is guarded and does not fetch a fabricated asset ID', async () => {
    const { wrapper, logs, router } = mountPage(P4_Extract, { plan: { '/api/v1/styles': envelope({ items: [] }) } })
    await router.replace('/extract?style_id=42'); await flushPromises()
    expect(router.currentRoute.value.matched[0]?.meta.requiresAuth).toBe(true)
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(logs.some((call) => /assets\/(new|undefined)|detect-text/.test(call.url))).toBe(false)
    wrapper.unmount()
  })
})
