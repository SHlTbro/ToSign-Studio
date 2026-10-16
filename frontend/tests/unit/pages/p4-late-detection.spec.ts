import { afterEach, expect, it, vi } from 'vitest'
import P4 from '@/pages/P4_Extract.vue'
import { envelope, flushPromises, mountPage } from './_harness'

let cleanup = () => {}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.body.innerHTML = '' })
it('preserves user manual boxes when a delayed detection response arrives', async () => {
  let resolve!: (value: Response) => void
  const deferred = new Promise<Response>(r => { resolve = r })
  const ctx = mountPage(P4, { plan: {
    '/api/v1/assets/source': envelope({ width: 400, height: 300, type: 'sample_source', status: 'approved' }),
    '/api/v1/styles': envelope({ items: [] }),
  } })
  cleanup = () => ctx.wrapper.unmount()
  const fetch = globalThis.fetch
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/detect-text') ? deferred : fetch(input, init))
  await ctx.router.push('/extract/source'); await flushPromises()
  await ctx.wrapper.get('[data-testid=p4-manual-toggle]').trigger('click')
  const stage = ctx.wrapper.get('[data-testid=p4-stage]')
  stage.element.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 300, right: 400, bottom: 300, x: 0, y: 0, toJSON: () => ({}) })
  await stage.trigger('pointerdown', { clientX: 80, clientY: 60 })
  await stage.trigger('pointermove', { clientX: 280, clientY: 210 })
  await stage.trigger('pointerup')
  expect(ctx.wrapper.get('[data-testid=p4-selected-count]').text()).toBe('1')
  resolve(new Response(JSON.stringify(envelope({ items: [] }).json))); await flushPromises()
  expect(ctx.wrapper.get('[data-testid=p4-selected-count]').text()).toBe('1')
  expect(ctx.wrapper.get('[data-testid=p4-extract]').attributes('disabled')).toBeUndefined()
})
