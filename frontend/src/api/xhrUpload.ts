/**
 * XHR 上传通道（白皮书 §1.4.3；v1.1 R7；PRD N18）。
 *
 * fetch 拿不到 upload.onprogress，上传必须走 XHR。
 * - 真进度：xhr.upload.onprogress → onProgress(0..100)
 * - 60s 超时（N18 工程值）、失败重试 2 次、退避 500×2^n 封顶 4s
 * - R7 visibilitychange 门卫：切后台 abort 在途 XHR → reject ApiRequestError(PAUSED)，
 *   **不消耗重试次数、不调度新 timer**；回前台由调用方调 resume()，按原 attempt 续跑。
 */
import { ApiRequestError } from './http'

export type UploadState = 'idle' | 'running' | 'paused' | 'succeeded' | 'failed'

export interface XhrUploadOpts {
  timeoutMs?: number
  retries?: number
  backoffCapMs?: number
  headers?: Record<string, string>
}

const UPLOAD_TIMEOUT_MS = 60_000
const UPLOAD_RETRIES = 2
const UPLOAD_BACKOFF_CAP_MS = 4_000
const UPLOAD_BACKOFF_BASE_MS = 500

// ---------- XHR 工厂（测试可注入 mock XHR） ----------

/** 本模块实际使用到的 XHR 最小结构（真实 XMLHttpRequest 天然满足，测试可注入替身） */
export interface MinimalXhr {
  readonly upload: {
    onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null
  }
  responseType: string
  status: number
  response: unknown
  onabort: (() => void) | null
  onload: (() => void) | null
  onerror: (() => void) | null
  open(method: string, url: string): void
  setRequestHeader(name: string, value: string): void
  send(body: FormData): void
  abort(): void
}

type XhrFactory = () => MinimalXhr
let xhrFactory: XhrFactory = () => new XMLHttpRequest() as unknown as MinimalXhr
export function __setXhrFactoryForTest(factory: XhrFactory): void {
  xhrFactory = factory
}
export function __resetXhrFactoryForTest(): void {
  xhrFactory = () => new XMLHttpRequest() as unknown as MinimalXhr
}

function uploadBackoffMs(attempt: number): number {
  return Math.min(UPLOAD_BACKOFF_BASE_MS * 2 ** attempt, UPLOAD_BACKOFF_CAP_MS)
}

// ---------- 上传会话（pause/resume 跨 Promise 保持 attempt） ----------

export class UploadSession<T> {
  state: UploadState = 'idle'
  /** 已真实消耗的重试次数（PAUSED 不计） */
  retriesConsumed = 0
  private attempt = 0
  private xhr: MinimalXhr | null = null
  private timeoutTimer: number | null = null
  private retryTimer: number | null = null

  constructor(
    private readonly path: string,
    private readonly form: FormData,
    private readonly onProgress: (percent: number) => void,
    private readonly opts: XhrUploadOpts = {},
  ) {}

  private get retries(): number {
    return this.opts.retries ?? UPLOAD_RETRIES
  }

  private get timeoutMs(): number {
    return this.opts.timeoutMs ?? UPLOAD_TIMEOUT_MS
  }

  private clearTimers(): void {
    if (this.timeoutTimer !== null) {
      window.clearTimeout(this.timeoutTimer)
      this.timeoutTimer = null
    }
  }

  private clearRetryTimer(): void {
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }

  /** 发起/续传一次上传；PAUSED 后由 resume() 再次调用，attempt 不变。 */
  start(): Promise<T> {
    this.state = 'running'
    return new Promise<T>((resolve, reject) => {
      // R7 门卫：页面处于后台时不发起请求，直接 PAUSED，等 resume
      if (typeof document !== 'undefined' && document.hidden) {
        this.state = 'paused'
        reject(new ApiRequestError(0, 'PAUSED', false, '已切后台，上传暂停'))
        return
      }
      this.attemptSend(resolve, reject)
    })
  }

  /** R7：切后台调用——abort 在途请求并冻结退避；reject PAUSED 由 onabort 完成。 */
  pause(): void {
    if (this.state !== 'running') return
    this.state = 'paused'
    this.clearTimers()
    this.clearRetryTimer() // 退避计时一并冻结，回前台按原 attempt 续跑
    this.xhr?.abort()
  }

  /** 回前台续跑（保持 attempt / retriesConsumed） */
  resume(): Promise<T> {
    if (this.state !== 'paused') {
      // 非暂停态 resume 等价于重新 start（幂等保护）
      return this.start()
    }
    return this.start()
  }

  private rejectPaused(reject: (e: unknown) => void): void {
    this.state = 'paused'
    reject(new ApiRequestError(0, 'PAUSED', false, '已切后台，上传暂停'))
  }

  private scheduleRetryOrFail(
    err: ApiRequestError,
    resolve: (v: T) => void,
    reject: (e: unknown) => void,
  ): void {
    // 门卫优先：已暂停 / 已隐藏 → PAUSED，不计数、不调度
    if (this.state === 'paused' || (typeof document !== 'undefined' && document.hidden)) {
      this.state = 'paused'
      this.clearRetryTimer()
      this.rejectPaused(reject)
      return
    }
    if (err.retryable && this.attempt < this.retries) {
      // R7：此处只“预约”下一次尝试；计数在定时器真正触发时才增加——
      // 若退避期间切后台（pause 清 timer / hidden），本次重试不计消耗。
      const nextAttempt = this.attempt + 1
      this.retryTimer = window.setTimeout(() => {
        this.retryTimer = null
        // 触发瞬间若已切后台：PAUSED，attempt/retriesConsumed 保持不变
        if (this.state === 'paused' || (typeof document !== 'undefined' && document.hidden)) {
          this.rejectPaused(reject)
          return
        }
        this.attempt = nextAttempt
        this.retriesConsumed = nextAttempt
        this.state = 'running'
        this.attemptSend(resolve, reject)
      }, uploadBackoffMs(this.attempt))
      return
    }
    this.state = 'failed'
    reject(err)
  }

  private attemptSend(resolve: (v: T) => void, reject: (e: unknown) => void): void {
    const xhr = xhrFactory()
    this.xhr = xhr
    xhr.open('POST', this.path)
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest')
    for (const [k, v] of Object.entries(this.opts.headers ?? {})) xhr.setRequestHeader(k, v)
    xhr.responseType = 'json'

    xhr.upload.onprogress = (e: { lengthComputable: boolean; loaded: number; total: number }) => {
      if (e.lengthComputable) this.onProgress(Math.round((e.loaded / e.total) * 100))
    }

    this.timeoutTimer = window.setTimeout(() => {
      xhr.abort()
      // abort 后由 onabort 统一分流（超时 vs 暂停）
    }, this.timeoutMs)

    xhr.onabort = () => {
      this.clearTimers()
      if (this.state === 'paused') {
        // pause() 主动 abort：PAUSED，不计数
        this.rejectPaused(reject)
        return
      }
      // 非暂停的 abort = 超时
      this.scheduleRetryOrFail(
        new ApiRequestError(0, 'TIMEOUT', true, '上传超时'),
        resolve,
        reject,
      )
    }

    xhr.onload = () => {
      this.clearTimers()
      if (xhr.status >= 200 && xhr.status < 300) {
        this.state = 'succeeded'
        resolve(xhr.response as T)
        return
      }
      const body = (xhr.response ?? {}) as { code?: string; detail?: string; retryable?: boolean }
      if (xhr.status >= 500) {
        this.scheduleRetryOrFail(
          new ApiRequestError(xhr.status, body.code ?? 'WORKER_UNAVAILABLE', true, body.detail ?? '服务异常'),
          resolve,
          reject,
        )
        return
      }
      this.state = 'failed'
      reject(
        new ApiRequestError(
          xhr.status,
          body.code ?? 'UNKNOWN',
          body.retryable ?? false,
          body.detail ?? '上传失败',
        ),
      )
    }

    xhr.onerror = () => {
      this.clearTimers()
      this.scheduleRetryOrFail(
        new ApiRequestError(0, 'NETWORK', true, '网络错误'),
        resolve,
        reject,
      )
    }

    xhr.send(this.form)
  }
}

/** 工厂函数：返回 UploadSession，调用方自行 start()/pause()/resume()。 */
export function xhrUpload<T>(
  path: string,
  form: FormData,
  onProgress: (percent: number) => void,
  opts?: XhrUploadOpts,
): UploadSession<T> {
  return new UploadSession<T>(path, form, onProgress, opts)
}

// ---------- R7 visibilitychange 门卫接线 ----------

export interface UploadVisibilityHandlers {
  /** 切后台：abort 全部在途上传（attempt 不变） */
  onHide(): void
  /** 回前台：按原 attempt 续跑 status=paused 的任务 */
  onShow(): void
}

/**
 * 绑定 document visibilitychange；返回 disconnect 注销。
 * 队列层（assetStore，T36）传入 pauseAll/resumeAll。
 */
export function createUploadVisibilityGate(
  handlers: UploadVisibilityHandlers,
): { disconnect: () => void } {
  const onChange = (): void => {
    if (typeof document === 'undefined') return
    if (document.hidden) handlers.onHide()
    else handlers.onShow()
  }
  document.addEventListener('visibilitychange', onChange)
  return {
    disconnect() {
      document.removeEventListener('visibilitychange', onChange)
    },
  }
}
