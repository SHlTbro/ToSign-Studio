/**
 * 硬验收②：指数退避序列断言（500/1000/2000ms ± 抖动上界，封顶 10s）；
 * 同时覆盖：仅幂等面（GET/声明幂等）重试、5xx 才重试、4xx 立即上抛、信封拆包、
 * 403 PASSWORD_CHANGE_REQUIRED 单飞 replace('/change-password')。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Router } from 'vue-router'
import {
  ApiRequestError,
  __resetHttpGatesForTest,
  __setBackoffRngForTest,
  http,
  computeBackoffMs,
  request,
  setHttpRouter,
} from '@/api/http'

function jsonResponse(status: number, body: unknown = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function makeRouter() {
  return {
    replace: vi.fn(),
    push: vi.fn(),
    currentRoute: { value: { fullPath: '/' } },
  } as unknown as Router & { replace: ReturnType<typeof vi.fn>; push: ReturnType<typeof vi.fn> }
}

describe('退避序列 computeBackoffMs（500×2^n + 抖动[0,250)，封顶 10s）', () => {
  it('rng=0 取下界：500 / 1000 / 2000', () => {
    expect(computeBackoffMs(0, () => 0)).toBe(500)
    expect(computeBackoffMs(1, () => 0)).toBe(1000)
    expect(computeBackoffMs(2, () => 0)).toBe(2000)
  })

  it('rng=1 取上界（不含）：750 / 1250 / 2250', () => {
    expect(computeBackoffMs(0, () => 1)).toBe(750)
    expect(computeBackoffMs(1, () => 1)).toBe(1250)
    expect(computeBackoffMs(2, () => 1)).toBe(2250)
  })

  it('任意 rng 落在 [下界, 上界) 区间', () => {
    for (let n = 0; n < 3; n++) {
      for (let k = 0; k < 20; k++) {
        const v = computeBackoffMs(n, () => Math.random())
        const base = 500 * 2 ** n
        expect(v).toBeGreaterThanOrEqual(base)
        expect(v).toBeLessThan(base + 250)
      }
    }
  })

  it('高次退避封顶 10000ms', () => {
    expect(computeBackoffMs(10, () => 1)).toBe(10_000)
  })
})

describe('request 重试面（仅幂等面、仅 5xx/0）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __setBackoffRngForTest(() => 0) // 确定性退避
    __resetHttpGatesForTest()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __resetHttpGatesForTest()
    __setBackoffRngForTest(Math.random)
  })

  it('GET 连续两次 500 后成功：共 3 次请求，退避等待 500ms→1000ms', async () => {
    const delays: number[] = []
    const fakeSetTimeout = window.setTimeout
    vi.spyOn(window, 'setTimeout').mockImplementation(((handler: TimerHandler, ms?: number) => {
      delays.push(ms ?? 0)
      return fakeSetTimeout(handler, ms)
    }) as typeof window.setTimeout)

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(503, { code: 'WORKER_UNAVAILABLE' }))
      .mockResolvedValueOnce(jsonResponse(500, { code: 'EXPORT_FAILED' }))
      .mockResolvedValueOnce(
        jsonResponse(200, { data: { ok: 1 }, meta: {}, warnings: [] }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const p = request<{ ok: number }>('/api/v1/x')
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(1000)
    const data = await p

    expect(fetchMock).toHaveBeenCalledTimes(3)
    // 退避序列（另有每次 15000ms 超时定时器，过滤之）
    const backoffs = delays.filter((d) => d < 15_000)
    expect(backoffs).toEqual([500, 1000])
    expect(data).toEqual({ ok: 1 }) // 信封拆包
  })

  it('getEnvelope 保留 data/meta 给需要列表总数的页面消费者', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, {
      data: { items: [], total: 7 }, meta: { total: 9 }, warnings: [],
    })))

    await expect(http.getEnvelope<{ items: unknown[]; total: number }>('/api/v1/training-sets'))
      .resolves.toEqual({ data: { items: [], total: 7 }, meta: { total: 9 }, warnings: [] })
  })

  it('非幂等 POST 遇 500 不重试，立即抛错（只请求 1 次）', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(503, { code: 'QUEUE_FULL' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      request('/api/v1/x', { method: 'POST', body: { a: 1 } }),
    ).rejects.toBeInstanceOf(ApiRequestError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('显式声明 idempotent 的写请求允许退避重试 2 次', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(500))
      .mockResolvedValueOnce(jsonResponse(500))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    const p = request('/api/v1/render-jobs/x/retry-cancelled', {
      method: 'POST',
      idempotent: true,
    })
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(1000)
    const result = await p
    expect(result).toBeUndefined() // 204
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('4xx 不重试：409 VERSION_CONFLICT 原样上抛（页面呈现对比刷新）', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(409, { code: 'VERSION_CONFLICT', detail: '版本冲突', retryable: true }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/api/v1/x')).rejects.toMatchObject({
      status: 409,
      code: 'VERSION_CONFLICT',
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('403 PASSWORD_CHANGE_REQUIRED 单飞 replace("/change-password")，禁 push', async () => {
    const router = makeRouter()
    setHttpRouter(router)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(403, { code: 'PASSWORD_CHANGE_REQUIRED', detail: '请先改密' }),
      ),
    )

    const results = await Promise.allSettled([request('/a'), request('/b')])
    expect(router.replace).toHaveBeenCalledTimes(1)
    expect(router.replace).toHaveBeenCalledWith('/change-password')
    expect(router.push).not.toHaveBeenCalled()
    results.forEach((r) => expect(r.status).toBe('rejected'))
  })

  it('普通 403 直接抛 FORBIDDEN，不跳改密页', async () => {
    const router = makeRouter()
    setHttpRouter(router)
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(403, { code: 'FORBIDDEN' })))
    await expect(request('/a')).rejects.toMatchObject({ status: 403, code: 'FORBIDDEN' })
    expect(router.replace).not.toHaveBeenCalled()
  })
})
