import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P5Review from '@/pages/P5_Review.vue'
import { envelope, flushPromises, mountPage, type FetchPlan } from './_harness'

const PATH = '/api/v1/render-jobs/export-recovery'
const artifacts = [{ artifact_id: 'png-one', relative_path: '1/outputs/export/group/one.png' },
  { artifact_id: 'zip-one', relative_path: '1/outputs/export/group/group.zip' }]
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:download'), revokeObjectURL: vi.fn() }))
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); document.body.innerHTML = '' })
async function open(plan: FetchPlan) {
  const page = mountPage(P5Review, { plan })
  await page.router.push('/batch/export-recovery/review'); await flushPromises(); await flushPromises()
  return page
}
function plan(): FetchPlan {
  return { [PATH]: envelope({ job: { job_id: 'export-recovery', status: 'succeeded' },
    items: [{ item_id: 'one', cn: '星', status: 'approved', render_version: 1, warnings: [], output_relative_path: 'one.png' }] }),
    [`${PATH}/outputs`]: envelope({ job_id: 'export-recovery', export_artifacts: [] }),
    [`${PATH}/export`]: envelope({ artifacts, failures: [] }) }
}
async function primary(page: Awaited<ReturnType<typeof open>>) {
  await page.wrapper.findAll('button').find(b => /导出 PNG|再次下载已通过成品/.test(b.text()))!.trigger('click')
  await flushPromises(); await flushPromises()
}

describe('owned existing export download and uncertain-submit recovery', () => {
  it('never submits after an outputs GET returns to a different review task', async () => {
    const routes = plan(); routes['/api/v1/render-jobs/second'] = envelope({ job: { status: 'succeeded' }, items: [] })
    const page = await open(routes); const ordinary = globalThis.fetch
    let release!: (response: Response) => void; const posts: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === `${PATH}/outputs`) return new Promise<Response>(resolve => { release = resolve })
      if (init?.method === 'POST') posts.push(String(input))
      return ordinary(input, init)
    }))
    await primary(page)
    await page.router.push('/batch/second/review'); await flushPromises()
    release(new Response(JSON.stringify(envelope({ job_id: 'export-recovery', export_artifacts: [] }).json), { status: 200 }))
    await flushPromises()
    expect(posts).toEqual([])
    page.wrapper.unmount()
  })

  it('isolates two original targets and clears historical links when the target changes', async () => {
    const routes = plan()
    routes['/api/v1/render-jobs/original-a/outputs'] = envelope({ job_id: 'original-a', export_artifacts: [] })
    routes['/api/v1/render-jobs/original-b/outputs'] = envelope({ job_id: 'original-b', export_artifacts: [] })
    const page = await open(routes); const ordinary = globalThis.fetch; const posts: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/export-linked') && init?.method === 'POST') { posts.push(String(input)); return Promise.reject(new TypeError('lost reply')) }
      return ordinary(input, init)
    }))
    await page.router.push({ path: '/batch/export-recovery/review', query: { retry_of: 'original-a' } }); await flushPromises()
    await page.wrapper.findAll('button').find(b => b.text().includes('导出原批次关联成品 ZIP'))!.trigger('click'); await flushPromises()
    await page.router.push({ path: '/batch/export-recovery/review', query: { retry_of: 'original-b' } }); await flushPromises()
    await page.wrapper.findAll('button').find(b => b.text().includes('导出原批次关联成品 ZIP'))!.trigger('click'); await flushPromises()
    expect(posts).toEqual(['/api/v1/render-jobs/original-a/export-linked', '/api/v1/render-jobs/original-b/export-linked'])
    page.wrapper.unmount()
  })

  it('does not label a direct export as another download after downloading linked history', async () => {
    const routes = plan()
    const linkedZip = { artifact_id: 'linked-one', relative_path: '1/outputs/export/linked-group/bundle.zip' }
    routes[`${PATH}/outputs`] = envelope({ job_id: 'export-recovery', export_artifacts: [linkedZip] })
    // A failed item exposes the linked export control, independently of direct approvals.
    routes[PATH] = envelope({ job: { status: 'partial' }, items: [
      { item_id: 'one', status: 'approved', warnings: [], render_version: 1 }, { item_id: 'bad', status: 'failed', warnings: [], render_version: 1 } ] })
    const page = await open(routes); const ordinary = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/file')
      ? Promise.resolve(new Response(new Blob(['zip']), { status: 200 })) : ordinary(input, init)))
    await page.wrapper.findAll('button').find(b => b.text().includes('导出原批次关联成品 ZIP'))!.trigger('click'); await flushPromises()
    await page.wrapper.get('[aria-label="下载已有 ZIP 1"]').trigger('click'); await flushPromises()
    expect(page.wrapper.text()).not.toContain('再次下载已通过成品')
    expect(page.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    await page.router.push({ path: '/batch/export-recovery/review', query: { retry_of: 'different-original' } }); await flushPromises()
    expect(page.wrapper.find('[aria-label="下载已有 ZIP 1"]').exists()).toBe(false)
    page.wrapper.unmount()
  })

  it('lists pre-existing history without a new export POST', async () => {
    const routes = plan(); routes[`${PATH}/outputs`] = envelope({ job_id: 'export-recovery', export_artifacts: artifacts })
    const page = await open(routes)
    await primary(page)
    expect(page.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    expect(page.wrapper.text()).toContain('保留当时的图片版本')
    expect(page.wrapper.find('[aria-label="下载已有 PNG 1"]').exists()).toBe(true)
    expect(page.wrapper.find('[aria-label="下载已有 ZIP 1"]').exists()).toBe(true)
    page.wrapper.unmount()
  })

  it('rejects outputs belonging to a different job before a POST', async () => {
    const routes = plan(); routes[`${PATH}/outputs`] = envelope({ job_id: 'wrong-job', export_artifacts: [] })
    const page = await open(routes); await primary(page)
    expect(page.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    expect(page.wrapper.text()).toContain('未提交新导出')
    page.wrapper.unmount()
  })

  it('does not expose or download a late export reply on another task', async () => {
    const routes = plan(); routes['/api/v1/render-jobs/second'] = envelope({ job: { status: 'succeeded' }, items: [] })
    const page = await open(routes); const ordinary = globalThis.fetch
    let release!: (response: Response) => void
    const downloads: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === `${PATH}/export` && init?.method === 'POST') return new Promise<Response>(resolve => { release = resolve })
      if (String(input).endsWith('/file')) downloads.push(String(input))
      return ordinary(input, init)
    }))
    await primary(page)
    await page.router.push('/batch/second/review'); await flushPromises()
    release(new Response(JSON.stringify(envelope({ artifacts, failures: [] }).json), { status: 200 }))
    await flushPromises()
    expect(downloads).toEqual([])
    expect(page.wrapper.find('[aria-label="下载已有 PNG 1"]').exists()).toBe(false)
    page.wrapper.unmount()
  })

  it('downloads known PNG again without a second export POST, and offers the already created ZIP', async () => {
    const routes = plan(); const page = await open(routes); const ordinary = globalThis.fetch
    let posts = 0; const downloads: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === `${PATH}/export` && init?.method === 'POST') {
        posts++; routes[`${PATH}/outputs`] = envelope({ job_id: 'export-recovery', export_artifacts: artifacts })
      }
      if (path.endsWith('/file')) { downloads.push(path); return Promise.resolve(new Response(new Blob(['owned image']), { status: 200 })) }
      return ordinary(input, init)
    }))
    await primary(page); await primary(page)
    expect(posts).toBe(1)
    expect(downloads).toEqual([`${PATH}/exports/png-one/file`, `${PATH}/exports/png-one/file`])
    await page.wrapper.get('[aria-label="下载已有 ZIP 1"]').trigger('click'); await flushPromises()
    expect(downloads.at(-1)).toBe(`${PATH}/exports/zip-one/file`)
    expect(posts).toBe(1); page.wrapper.unmount()
  })

  it('remembers an unknown POST across remount and only reads its original outputs', async () => {
    const routes = plan(); const first = await open(routes); const ordinary = globalThis.fetch
    let posts = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === `${PATH}/export` && init?.method === 'POST') { posts++; return Promise.reject(new TypeError('lost reply')) }
      return ordinary(input, init)
    }))
    await primary(first); expect(posts).toBe(1); first.wrapper.unmount()
    const restored = await open(routes)
    await primary(restored)
    expect(restored.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    expect(restored.wrapper.text()).toContain('导出结果尚未确认')
    routes[`${PATH}/outputs`] = envelope({ job_id: 'export-recovery', export_artifacts: artifacts })
    await restored.wrapper.get('[aria-label="读取已有导出文件"]').trigger('click'); await flushPromises()
    expect(restored.wrapper.find('[aria-label="下载已有 PNG 1"]').exists()).toBe(true)
    expect(restored.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    restored.wrapper.unmount()
  })

  it('never starts an export when the submit marker cannot be saved', async () => {
    const page = await open(plan()); const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith('yy:render-export:')) throw new DOMException('full', 'QuotaExceededError')
      return setItem.call(this, key, value)
    })
    await primary(page)
    expect(page.logs.filter(c => c.method === 'POST')).toHaveLength(0)
    expect(page.wrapper.text()).toContain('无法保存导出请求标记')
    page.wrapper.unmount()
  })
})
