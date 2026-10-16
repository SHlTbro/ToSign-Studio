import { createPinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import { SYSTEM_CAPABILITIES } from './pages/render-capability-fixtures'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P5Batch from '@/pages/P5_Batch.vue'

const { requestMock, pushMock } = vi.hoisted(() => ({ requestMock: vi.fn(), pushMock: vi.fn() }))
vi.mock('@/api/http', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/http')>(),
  request: requestMock,
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: pushMock }) }))

const MESSAGE = '请先选择或填写博主署名，或关闭有内容行的「图上署名」开关'
const DRAFT_KEY = 'draft:batch-generation-rows'
let wrapper: VueWrapper | undefined
let presets: { preset_id: string; name: string; is_default: boolean }[]
let presetsFail: boolean

async function openPage() {
  const pinia = createPinia(); useAuthStore(pinia).__setUserForTest({ user_id: '2', username: 'fixture', display_name: 'Fixture', role: 'user', must_change_password: false })
  wrapper = mount(P5Batch, { global: { plugins: [pinia], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
  await flushPromises()
  return wrapper
}

function button(text: string) {
  const match = wrapper!.findAll('button').find((node) => node.text() === text)
  if (!match) throw new Error(`Missing button: ${text}`)
  return match
}

function renderPosts() {
  return requestMock.mock.calls.filter(([url, opts]) => url === '/api/v1/render-jobs' && opts?.method === 'POST')
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  presets = []
  presetsFail = false
  requestMock.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/v1/assets?')) return { items: [{ asset_id: 'asset-1', status: 'approved', width: 1000, height: 1000 }] }
    if (url.startsWith('/api/v1/phrase-templates')) return { items: [] }
    if (url === '/api/v1/blogger-name-presets') {
      if (presetsFail) throw new Error('Unavailable')
      return { items: presets }
    }
    if (url.startsWith('/api/v1/layout-templates')) return { items: [{ template_id: 'template-1', base_asset_id: 'asset-1', status: 'approved', created_at: '2026-10-01' }] }
    if (url.startsWith('/api/v1/styles')) return { items: [] }
    if (url === '/api/v1/render-jobs/capabilities') return SYSTEM_CAPABILITIES
    if (url === '/api/v1/render-jobs') return { job: { job_id: 'job-1', status: 'completed' }, items: [] }
    throw new Error(`Unexpected request: ${url}`)
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  localStorage.clear()
})

describe('P5 missing signature name is validated before creating a render job', () => {
  it.each(['预览草稿', '正式生成'])('fresh account: %s prompts for a name without posting or changing the signed row', async (action) => {
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    expect(page.get('#signature-name-required').text()).toBe(MESSAGE)
    await button(action).trigger('click')
    await flushPromises()

    expect(renderPosts()).toHaveLength(0)
    expect(pushMock).not.toHaveBeenCalled()
    expect(page.get('[aria-label="第 1 行图上署名开关"]').attributes('aria-checked')).toBe('true')
    expect(page.get('[aria-label="自定义博主名"]').attributes('aria-invalid')).toBe('true')
    expect(page.find('[role="dialog"]').exists()).toBe(false)
  })

  it('rejects whitespace-only names and repeated submit/preview attempts', async () => {
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await button('自定义').trigger('click')
    await page.get('[aria-label="自定义博主名"]').setValue(' \t ')
    for (const action of ['正式生成', '正式生成', '预览草稿']) await button(action).trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(0)
    expect(page.get('#signature-name-required').text()).toBe(MESSAGE)
  })

  it.each(['预览草稿', '正式生成'])('an explicit custom name repairs %s and preserves the signed payload', async (action) => {
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await button(action).trigger('click')
    await page.get('[aria-label="自定义博主名"]').setValue('  真署名  ')
    await button(action).trigger('click')
    await flushPromises()

    expect(page.find('#signature-name-required').exists()).toBe(false)
    expect(renderPosts()).toHaveLength(1)
    expect(renderPosts()[0][1].body).toMatchObject({
      template_id: 'template-1',
      blogger_name: '真署名',
      sign_blogger: false,
      items: [{ cn: '小雨', sign_blogger: true, asset_id: 'asset-1', template_id: 'template-1' }],
    })
    expect(renderPosts()[0][1].body.draft).toBe(action === '预览草稿' ? true : undefined)
  })

  it('allows unsigned rendering only after the user explicitly turns the signature off', async () => {
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await page.get('[aria-label="第 1 行图上署名开关"]').trigger('click')
    await button('正式生成').trigger('click')
    await flushPromises()

    expect(page.find('#signature-name-required').exists()).toBe(false)
    expect(renderPosts()).toHaveLength(1)
    expect(renderPosts()[0][1].body.blogger_name).toBeUndefined()
    expect(renderPosts()[0][1].body.items[0].sign_blogger).toBe(false)
  })

  it('uses an existing default preset without changing normal generation', async () => {
    presets = [{ preset_id: 'preset-1', name: '原署名', is_default: true }]
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await button('正式生成').trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(1)
    expect(renderPosts()[0][1].body.blogger_name).toBe('原署名')
    expect(renderPosts()[0][1].body.items[0].sign_blogger).toBe(true)
  })

  it('keeps a restored mixed draft blocked until its signed row has a name', async () => {
    localStorage.setItem(DRAFT_KEY, JSON.stringify([
      { cn: '无署名', blessing_id: '', sign_blogger: false },
      { cn: '有署名', blessing_id: '', sign_blogger: true },
    ]))
    const page = await openPage()
    await button('正式生成').trigger('click')
    expect(renderPosts()).toHaveLength(0)
    await page.get('[aria-label="自定义博主名"]').setValue('恢复署名')
    await button('正式生成').trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(1)
    expect(renderPosts()[0][1].body.items.map((row: { sign_blogger: boolean }) => row.sign_blogger)).toEqual([false, true])
  })

  it('ignores unfilled signed rows when every filled row is explicitly unsigned', async () => {
    localStorage.setItem(DRAFT_KEY, JSON.stringify([
      { cn: '无署名', blessing_id: '', sign_blogger: false },
      { cn: '', blessing_id: '', sign_blogger: true },
    ]))
    const page = await openPage()
    await button('正式生成').trigger('click')
    await flushPromises()
    expect(page.find('#signature-name-required').exists()).toBe(false)
    expect(renderPosts()).toHaveLength(1)
    expect(renderPosts()[0][1].body.items).toHaveLength(1)
    expect(renderPosts()[0][1].body.items[0].sign_blogger).toBe(false)
  })

  it('a preset-load failure never silently removes the signature', async () => {
    presetsFail = true
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await button('正式生成').trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(0)
    expect(page.get('[aria-label="第 1 行图上署名开关"]').attributes('aria-checked')).toBe('true')
    expect(page.get('#signature-name-required').text()).toBe(MESSAGE)
  })

  it('closing preview and clearing a name blocks both later render lanes', async () => {
    const page = await openPage()
    await page.get('[aria-label="第 1 行粉丝昵称"]').setValue('小雨')
    await button('自定义').trigger('click')
    await page.get('[aria-label="自定义博主名"]').setValue('署名')
    await button('预览草稿').trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(1)
    await page.get('[aria-label="关闭预览"]').trigger('click')
    expect(page.find('[role="dialog"]').exists()).toBe(false)
    await page.get('[aria-label="自定义博主名"]').setValue('')
    await button('预览草稿').trigger('click')
    await button('正式生成').trigger('click')
    await flushPromises()
    expect(renderPosts()).toHaveLength(1)
    expect(page.find('[role="dialog"]').exists()).toBe(false)
    expect(pushMock).not.toHaveBeenCalled()
  })
})
