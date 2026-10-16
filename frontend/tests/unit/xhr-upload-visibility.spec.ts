/**
 * 硬验收③ / R7：上传真进度、60s 超时、重试 2 次；
 * visibilitychange 门卫——切后台 abort→reject PAUSED 且不消耗重试次数，
 * 回前台 resume 按原 attempt 续跑。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '@/api/http'
import {
  __resetXhrFactoryForTest,
  __setXhrFactoryForTest,
  createUploadVisibilityGate,
  xhrUpload,
} from '@/api/xhrUpload'

function makeMockXhrClass() {
  const instances: MockXHR[] = []
  class MockXHR {
    static instances = instances
    upload = {
      onprogress: null as
        | ((e: { lengthComputable: boolean; loaded: number; total: number }) => void)
        | null,
    }
    status = 0
    response: unknown = null
    responseType = ''
    onabort: (() => void) | null = null
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    open = vi.fn()
    setRequestHeader = vi.fn()
    send = vi.fn()
    abort = vi.fn(() => {
      // 与真实 XHR 一致：abort 触发 onabort（同步派发即可）
      this.onabort?.()
    })
    constructor() {
      instances.push(this)
    }
    emitProgress(loaded: number, total: number) {
      this.upload.onprogress?.({ lengthComputable: true, loaded, total })
    }
    respondOk(body: unknown) {
      this.status = 200
      this.response = body
      this.onload?.()
    }
    respond(status: number, body: unknown = {}) {
      this.status = status
      this.response = body
      this.onload?.()
    }
    emitError() {
      this.onerror?.()
    }
  }
  return { MockXHR, instances }
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => hidden,
  })
}

describe('xhrUpload 真进度 / 超时 / 重试', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setHidden(false)
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetXhrFactoryForTest()
    setHidden(false)
  })

  it('onprogress 真进度回调（0..100），2xx 以 xhr.response 解决', async () => {
    const { MockXHR, instances } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    const onProgress = vi.fn()
    const session = xhrUpload<{ ok: boolean }>(
      '/api/v1/assets',
      new FormData(),
      onProgress,
    )
    const p = session.start()
    instances[0].emitProgress(30, 100)
    instances[0].emitProgress(100, 100)
    instances[0].respondOk({ ok: true })
    await expect(p).resolves.toEqual({ ok: true })
    expect(onProgress).toHaveBeenNthCalledWith(1, 30)
    expect(onProgress).toHaveBeenNthCalledWith(2, 100)
    // 写请求带 X-Requested-With
    expect(instances[0].setRequestHeader).toHaveBeenCalledWith('X-Requested-With', 'XMLHttpRequest')
  })

  it('5xx 退避后重试成功，retriesConsumed=1', async () => {
    const { MockXHR, instances } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    const session = xhrUpload('/api/v1/assets', new FormData(), () => {})
    const p = session.start()
    instances[0].respond(503, { code: 'WORKER_UNAVAILABLE' })
    await vi.advanceTimersByTimeAsync(500) // 退避 500ms（§1.4.3 首档）
    expect(instances).toHaveLength(2)
    instances[1].respondOk({ asset_id: 'a1' })
    await p
    expect(session.retriesConsumed).toBe(1)
    expect(session.state).toBe('succeeded')
  })

  it('60s 超时 abort；retries=0 时最终抛 TIMEOUT', async () => {
    const { MockXHR } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    const session = xhrUpload(
      '/api/v1/assets',
      new FormData(),
      () => {},
      { retries: 0, timeoutMs: 60_000 },
    )
    const p = session.start()
    const rejection = expect(p).rejects.toMatchObject({ code: 'TIMEOUT', retryable: true })
    await vi.advanceTimersByTimeAsync(60_000)
    await rejection
  })
})

describe('R7 visibilitychange 门卫：PAUSED 不计数、resume 续跑', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setHidden(false)
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetXhrFactoryForTest()
    setHidden(false)
  })

  it('在途上传切后台：abort→reject PAUSED，重试次数不消耗；resume 续跑成功', async () => {
    const { MockXHR, instances } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    const session = xhrUpload<{ asset_id: string }>(
      '/api/v1/assets',
      new FormData(),
      () => {},
    )

    const p1 = session.start()
    expect(instances).toHaveLength(1)
    expect(session.state).toBe('running')

    // 切后台：abort 在途请求
    session.pause()
    await expect(p1).rejects.toMatchObject({ code: 'PAUSED' })
    expect(session.state).toBe('paused')
    expect(session.retriesConsumed).toBe(0) // 关键：PAUSED 不消耗重试

    // 回前台 resume：按原 attempt 续跑（新 XHR，计数仍为 0）
    const p2 = session.resume()
    expect(instances).toHaveLength(2)
    expect(session.retriesConsumed).toBe(0)
    instances[1].respondOk({ asset_id: 'asset-x' })
    await expect(p2).resolves.toEqual({ asset_id: 'asset-x' })
    expect(session.state).toBe('succeeded')
  })

  it('后台状态下 start 直接 PAUSED，不创建 XHR、不计数', () => {
    const { MockXHR, instances } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    setHidden(true)
    const session = xhrUpload('/api/v1/assets', new FormData(), () => {})
    return session.start().then(
      () => {
        throw new Error('应当 reject PAUSED')
      },
      (e) => {
        expect(e).toBeInstanceOf(ApiRequestError)
        expect((e as ApiRequestError).code).toBe('PAUSED')
        expect(instances).toHaveLength(0)
        expect(session.retriesConsumed).toBe(0)
      },
    )
  })

  it('退避等待期间切后台：不启动新尝试；回前台 resume 不补扣重试', async () => {
    const { MockXHR, instances } = makeMockXhrClass()
    __setXhrFactoryForTest(() => new MockXHR())
    const session = xhrUpload('/api/v1/assets', new FormData(), () => {})
    const p1 = session.start()
    instances[0].respond(500) // 进入 500ms 退避
    await vi.advanceTimersByTimeAsync(200) // 未到重试点
    session.pause() // 冻结退避计时
    await expect(p1).rejects.toMatchObject({ code: 'PAUSED' })
    await vi.advanceTimersByTimeAsync(5000) // 后台再久也不发新请求
    expect(instances).toHaveLength(1)
    expect(session.retriesConsumed).toBe(0)

    setHidden(false)
    const p2 = session.resume()
    expect(instances).toHaveLength(2) // 原 attempt 重发
    instances[1].respondOk({})
    await p2
    expect(session.retriesConsumed).toBe(0)
  })

  it('createUploadVisibilityGate：hidden→onHide，visible→onShow，disconnect 后不再触发', () => {
    const onHide = vi.fn()
    const onShow = vi.fn()
    const gate = createUploadVisibilityGate({ onHide, onShow })

    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onHide).toHaveBeenCalledTimes(1)

    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onShow).toHaveBeenCalledTimes(1)

    gate.disconnect()
    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onHide).toHaveBeenCalledTimes(1)
  })
})
