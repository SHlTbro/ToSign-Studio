/**
 * 硬验收⑦ / R9：Idempotency-Key 粒度=逻辑提交动作——
 * 同一动作内自动重试复用同一 key；新动作生成新 key；写请求带 X-Requested-With。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetHttpGatesForTest,
  __setBackoffRngForTest,
  newIdempotencyKey,
  request,
} from '@/api/http'

function jsonResponse(status: number, body: unknown = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function headerOf(call: unknown[], name: string): string | undefined {
  const headers = call[1] as RequestInit | undefined
  return (headers?.headers as Record<string, string> | undefined)?.[name]
}

describe('R9 Idempotency-Key 绑定提交动作', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __setBackoffRngForTest(() => 0)
    __resetHttpGatesForTest()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __setBackoffRngForTest(Math.random)
    __resetHttpGatesForTest()
  })

  it('newIdempotencyKey：UUIDv4 形态且两次不同（新动作新 key）', () => {
    const k1 = newIdempotencyKey()
    const k2 = newIdempotencyKey()
    expect(k1).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    expect(k1).not.toBe(k2)
  })

  it('同一提交动作内的两次自动重试复用同一 Idempotency-Key', async () => {
    const key = newIdempotencyKey()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(503, { code: 'WORKER_UNAVAILABLE' }))
      .mockResolvedValueOnce(jsonResponse(500))
      .mockResolvedValueOnce(jsonResponse(200, { data: { job_id: 'j1' } }))
    vi.stubGlobal('fetch', fetchMock)

    // 声明幂等的 POST（render-jobs 创建）；key 在动作入口生成一次
    const p = request('/api/v1/render-jobs', {
      method: 'POST',
      idempotent: true,
      idempotencyKey: key,
      body: { draft: true },
    })
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(1000)
    const result = await p

    expect(fetchMock).toHaveBeenCalledTimes(3)
    const keys = fetchMock.mock.calls.map((c) => headerOf(c, 'Idempotency-Key'))
    expect(keys).toEqual([key, key, key]) // 重试复用
    // 写请求同时带 X-Requested-With
    for (const c of fetchMock.mock.calls) {
      expect(headerOf(c, 'X-Requested-With')).toBe('XMLHttpRequest')
    }
    expect(result).toEqual({ job_id: 'j1' })
  })

  it('两个独立提交动作（两次点击）使用不同 key', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { data: {} }))
    vi.stubGlobal('fetch', fetchMock)

    const keyA = newIdempotencyKey()
    const keyB = newIdempotencyKey()
    expect(keyA).not.toBe(keyB)

    await request('/api/v1/render-jobs', { method: 'POST', idempotencyKey: keyA, body: {} })
    await request('/api/v1/render-jobs', { method: 'POST', idempotencyKey: keyB, body: {} })

    expect(headerOf(fetchMock.mock.calls[0], 'Idempotency-Key')).toBe(keyA)
    expect(headerOf(fetchMock.mock.calls[1], 'Idempotency-Key')).toBe(keyB)
  })

  it('GET 不带 Idempotency-Key（仅动作提交端点携带）', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { data: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await request('/api/v1/assets')
    expect(headerOf(fetchMock.mock.calls[0], 'Idempotency-Key')).toBeUndefined()
    expect(headerOf(fetchMock.mock.calls[0], 'X-Requested-With')).toBeUndefined()
  })
})
