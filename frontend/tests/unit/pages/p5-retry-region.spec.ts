import { afterEach, expect, it, vi } from 'vitest'
import P5Review from '@/pages/P5_Review.vue'
import { envelope, flushPromises, mountPage } from './_harness'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = '' })
const region = { x_norm: 35 / 1170, y_norm: 1450 / 2034, w_norm: 1100 / 1170, h_norm: 545 / 2034 }
async function open() {
  const mounted = mountPage(P5Review, { plan: {
    '/api/v1/render-jobs/job-a': envelope({ job: { status: 'failed' }, items: [{
      item_id: 'bad', status: 'failed', retryable: true, warnings: [], render_version: 1,
      retry_regions: { blessing: region }, image_size: [1170, 2034],
    }] }),
    '/api/v1/render-jobs/job-a/items/bad/retry': envelope({ job: { job_id: 'child' }, retried_item_ids: ['bad'] }),
  } })
  await mounted.router.push('/batch/job-a/review'); await flushPromises(); await flushPromises()
  return mounted
}
it('can dismiss a terminal failure summary and reach the retry controls', async () => {
  const { wrapper } = await open()
  expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(true)
  const close = wrapper.findAll('button').find(b => b.text() === '返回本批次继续处理')
  expect(close).toBeDefined()
  await close!.trigger('click'); await flushPromises()
  expect(wrapper.find('[aria-label="批次质检完成"]').exists()).toBe(false)
  await wrapper.get('[aria-label="调整这张文字区域"]').setValue(true)
  expect(wrapper.find('[aria-label="文字区域宽度（像素）"]').exists()).toBe(true)
  wrapper.unmount()
})
it('submits only the explicitly changed region width with the single-item request', async () => {
  const { wrapper } = await open()
  const ordinary = globalThis.fetch
  let body: unknown
  vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => {
    if (String(args[0]).endsWith('/items/bad/retry')) body = JSON.parse(String(args[1]?.body))
    return ordinary(...args)
  }))
  await wrapper.get('[aria-label="调整这张文字区域"]').setValue(true)
  await wrapper.get('[aria-label="文字区域宽度（像素）"]').setValue(1121)
  await wrapper.findAll('button').find(b => b.text() === '重试此项')!.trigger('click'); await flushPromises()
  expect(body).toEqual({ region_override: { blessing: { ...region, w_norm: 1121 / 1170 } } })
  wrapper.unmount()
})
it('restores the exact persisted changed region after an uncertain request', async () => {
  const first = await open(); const ordinary = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => String(args[0]).endsWith('/items/bad/retry')
    ? Promise.reject(new TypeError('unknown result')) : ordinary(...args)))
  await first.wrapper.get('[aria-label="调整这张文字区域"]').setValue(true)
  await first.wrapper.get('[aria-label="文字区域宽度（像素）"]').setValue(1121)
  await first.wrapper.findAll('button').find(b => b.text() === '重试此项')!.trigger('click'); await flushPromises()
  first.wrapper.unmount()
  const restored = await open(); const nextFetch = globalThis.fetch; let body: unknown
  vi.stubGlobal('fetch', vi.fn((...args: Parameters<typeof fetch>) => {
    if (String(args[0]).endsWith('/items/bad/retry')) body = JSON.parse(String(args[1]?.body))
    return nextFetch(...args)
  }))
  expect(restored.logs.filter(c => c.method === 'POST')).toHaveLength(0)
  await restored.wrapper.findAll('button').find(b => b.text().includes('确认并恢复原重试'))!.trigger('click'); await flushPromises()
  expect(body).toEqual({ region_override: { blessing: { ...region, w_norm: 1121 / 1170 } } })
  restored.wrapper.unmount()
})
