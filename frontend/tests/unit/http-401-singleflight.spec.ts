/**
 * 硬验收① / R1：并发 3 请求同时 401 → 恰好 1 次 router.replace、0 次 push；
 * 时窗（1.5s）去重；每个请求仍照常抛 ApiRequestError（调用方静默降级）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Router } from 'vue-router'
import {
  ApiRequestError,
  __resetHttpGatesForTest,
  request,
  setHttpRouter,
} from '@/api/http'

function makeRouter(fullPath = '/assets?p=2') {
  return {
    replace: vi.fn(),
    push: vi.fn(),
    currentRoute: { value: { fullPath } },
  } as unknown as Router & { replace: ReturnType<typeof vi.fn>; push: ReturnType<typeof vi.fn> }
}

function jsonResponse(status: number, body: Record<string, unknown> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('R1 401 单飞 + replace（禁 push）', () => {
  beforeEach(() => {
    __resetHttpGatesForTest()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __resetHttpGatesForTest()
  })

  it('并发 3 请求同时 401 → 恰好 1 次 replace、0 次 push', async () => {
    const router = makeRouter()
    setHttpRouter(router)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(401, { code: 'UNAUTHORIZED' })),
    )

    const results = await Promise.allSettled([
      request('/api/v1/a'),
      request('/api/v1/b'),
      request('/api/v1/c'),
    ])

    expect(fetch).toHaveBeenCalledTimes(3)
    // 单飞：replace 恰好一次
    expect(router.replace).toHaveBeenCalledTimes(1)
    // 严禁 push（R1 替代旧实现的核心）
    expect(router.push).not.toHaveBeenCalled()
    expect(router.replace).toHaveBeenCalledWith({
      path: '/login',
      query: { redirect: '/assets?p=2' },
    })
    // 401 后请求照常抛错，交由调用方静默
    for (const r of results) {
      expect(r.status).toBe('rejected')
      if (r.status === 'rejected') {
        expect(r.reason).toBeInstanceOf(ApiRequestError)
        expect((r.reason as ApiRequestError).status).toBe(401)
      }
    }
  })

  it('同一失效时窗内后续 401 全部静默（不再 replace）', async () => {
    const router = makeRouter()
    setHttpRouter(router)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401)))

    await request('/a').catch(() => {})
    await request('/b').catch(() => {})
    expect(router.replace).toHaveBeenCalledTimes(1)
  })

  it('1.5s 时窗过后复位，下一波 401 可再触发一次 replace', async () => {
    const router = makeRouter()
    setHttpRouter(router)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401)))

    await request('/a').catch(() => {})
    expect(router.replace).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1500)
    await request('/b').catch(() => {})
    expect(router.replace).toHaveBeenCalledTimes(2)
    expect(router.push).not.toHaveBeenCalled()
  })
})
