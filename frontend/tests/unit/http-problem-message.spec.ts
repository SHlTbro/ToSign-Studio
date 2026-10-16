import { afterEach, describe, expect, it, vi } from 'vitest'
import { request } from '@/api/http'

afterEach(() => vi.unstubAllGlobals())
describe('Real RFC9457 empty-detail responses remain visible', () => {
  it.each(['', '   ', undefined])('uses title when detail is %j', async (detail) => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({
      type: 'about:blank', title: 'Style not found', detail,
      status: 404, code: 'STYLE_NOT_FOUND', retryable: false,
    }), { status: 404 }))
    await expect(request('/api/v1/styles/999')).rejects.toMatchObject({ code: 'STYLE_NOT_FOUND', message: 'Style not found' })
  })
  it('retains a useful fallback even when detail/title are both empty', async () => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ title: '', detail: '', code: 'INVALID_REQUEST' }), { status: 404 }))
    await expect(request('/api/v1/layout-templates/missing')).rejects.toMatchObject({ message: '请求失败（HTTP 404）' })
  })
})
