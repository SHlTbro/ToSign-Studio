/**
 * 网络层 —— 原生 fetch 薄封装（白皮书 §1.4.1/§1.4.2；v1.1 R1/R9；PRD §6 全局拦截器；N18）。
 *
 * 实现要点（T35 主验收口径）：
 * - 15s AbortController 超时（N18，可经 opts.timeoutMs 覆盖）。
 * - 指数退避：500ms × 2^n + 抖动[0,250)，封顶 10s；仅作用于幂等面
 *   （GET 或显式声明 idempotent 的写请求），默认重试 2 次。
 * - 仅 5xx 与 status=0（超时/网络）可重试；4xx 一律上抛由人工/页面处理。
 * - R1：401 模块级单飞 + router.replace('/login',{redirect})，时窗 1.5s 去重，严禁 push。
 * - 403 PASSWORD_CHANGE_REQUIRED 单飞 replace('/change-password')。
 * - 所有写请求（非 GET）带 X-Requested-With（CSRF，契约 §11）。
 * - Idempotency-Key 粒度 = 一次逻辑提交动作（R9）：同一 action 的自动重试复用同一 key。
 * - 成功响应在本层拆信封（{data,meta,warnings} → data）；store 只接触领域类型。
 */
import type { ApiError, ApiErrorCode, ProblemDetail } from '@/types/domain'
import type { Router } from 'vue-router'

// ---------- 路由注入（避免循环依赖，main.ts 调 setHttpRouter） ----------

let routerRef: Router | null = null
export function setHttpRouter(router: Router): void {
  routerRef = router
}

function currentFullPath(): string {
  return routerRef?.currentRoute.value.fullPath ?? '/'
}

// ---------- R1：401 单飞 + replace（禁 push） ----------

const SINGLE_FLIGHT_WINDOW_MS = 1500
let handling401 = false
let reset401Timer: number | null = null

function handle401(): void {
  if (handling401) return // 同一失效窗口内后续 401 全部静默
  handling401 = true
  // replace 不污染历史栈（R1 核心）
  routerRef?.replace({ path: '/login', query: { redirect: currentFullPath() } })
  if (reset401Timer !== null) window.clearTimeout(reset401Timer)
  reset401Timer = window.setTimeout(() => {
    handling401 = false
    reset401Timer = null
  }, SINGLE_FLIGHT_WINDOW_MS)
}

// ---------- 403 PASSWORD_CHANGE_REQUIRED 单飞 ----------

let handlingPwdChange = false
let reset403Timer: number | null = null

function handlePasswordChangeRequired(): void {
  if (handlingPwdChange) return
  handlingPwdChange = true
  routerRef?.replace('/change-password')
  if (reset403Timer !== null) window.clearTimeout(reset403Timer)
  reset403Timer = window.setTimeout(() => {
    handlingPwdChange = false
    reset403Timer = null
  }, SINGLE_FLIGHT_WINDOW_MS)
}

/** Share the same auth redirect gates with binary responses; callers retain error parsing/throwing. */
export function handleResponseAuth(status: number, code?: string): void {
  if (status === 401) handle401()
  else if (status === 403 && code === 'PASSWORD_CHANGE_REQUIRED') handlePasswordChangeRequired()
}

// ---------- 统一错误类（implements domain.ApiError） ----------

export class ApiRequestError extends Error implements ApiError {
  constructor(
    public status: number,
    public code: ApiErrorCode | string,
    public retryable: boolean,
    message: string,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

// ---------- 退避（纯函数，便于单测断言序列 500/1000/2000 ± 抖动上界） ----------

const BACKOFF_BASE_MS = 500
const BACKOFF_JITTER_MS = 250
const BACKOFF_CAP_MS = 10_000

let backoffRng: () => number = Math.random

/**
 * 第 attempt 次失败后的等待毫秒（attempt 从 0 起）：
 * min(500×2^attempt + rand×250, 10000)。注入 rng 仅为测试确定性。
 */
export function computeBackoffMs(attempt: number, rng: () => number = backoffRng): number {
  const base = BACKOFF_BASE_MS * 2 ** attempt
  return Math.min(base + rng() * BACKOFF_JITTER_MS, BACKOFF_CAP_MS)
}

function backoffSleep(attempt: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, computeBackoffMs(attempt))
  })
}

// ---------- Idempotency-Key（R9：粒度=逻辑提交动作） ----------

/**
 * 生成 Idempotency-Key（UUIDv4）。非安全上下文无 crypto.randomUUID 时
 * 用 getRandomValues 手拼，保证两个提交动作绝不撞 key。
 * 同一动作内的自动重试复用调用方持有的那一个 key（见 request 循环）。
 */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto
  if (c?.randomUUID) return c.randomUUID()
  const b = new Uint8Array(16)
  c?.getRandomValues(b)
  const hex = [...b].map((x) => x.toString(16).padStart(2, '0'))
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex
    .slice(6, 8)
    .join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
}

// ---------- 请求选项 ----------

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface ReqOpts {
  method?: HttpMethod
  body?: unknown
  /** 逻辑提交动作的幂等键；同一次动作的所有自动重试复用它（R9） */
  idempotencyKey?: string
  timeoutMs?: number
  /** 覆盖默认重试次数（幂等面默认 2，非幂等默认 0） */
  retries?: number
  /** 显式声明该写请求幂等（可安全重试），如 retry-cancelled */
  idempotent?: boolean
  /** 返回原始成功信封，供需要读取 meta 的列表消费者使用。 */
  preserveEnvelope?: boolean
}

export interface ApiEnvelope<T> {
  data: T
  meta?: Record<string, unknown>
  warnings?: unknown[]
}

const DEFAULT_TIMEOUT_MS = 15_000
const DEFAULT_RETRIES = 2

function isProblemLike(x: unknown): x is Partial<ProblemDetail> {
  return typeof x === 'object' && x !== null
}

/** 拆成功信封：契约成功壳 {data,...} → data；裸对象原样返回。 */
function unwrap<T>(json: unknown): T {
  if (isProblemLike(json) && 'data' in json) return json.data as T
  return json as T
}

async function readProblem(res: Response): Promise<Partial<ProblemDetail>> {
  return (await res.json().catch(() => null)) as Partial<ProblemDetail> | null ?? {}
}

function problemMessage(problem: Partial<ProblemDetail>, fallback: string): string {
  const detail = typeof problem.detail === 'string' ? problem.detail.trim() : ''
  const title = typeof problem.title === 'string' ? problem.title.trim() : ''
  return detail || title || fallback
}

/**
 * 发起一次 HTTP 请求；返回已拆信封的 data（204 返回 undefined）。
 * 失败抛 ApiRequestError。
 */
export async function request<T>(path: string, opts: ReqOpts = {}): Promise<T> {
  const method: HttpMethod = opts.method ?? 'GET'
  const { body, idempotencyKey } = opts
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const idempotentSurface = method === 'GET' || opts.idempotent === true
  const retries = opts.retries ?? (idempotentSurface ? DEFAULT_RETRIES : 0)

  let lastErr: unknown
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController()
    const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      const headers: Record<string, string> = { Accept: 'application/json' }
      if (body !== undefined) headers['Content-Type'] = 'application/json'
      // 写请求一律带 X-Requested-With（CSRF）
      if (method !== 'GET') headers['X-Requested-With'] = 'XMLHttpRequest'
      if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey

      const res = await fetch(path, {
        method,
        headers,
        credentials: 'same-origin',
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      })
      window.clearTimeout(timer)

      // R1：401 单飞
      if (res.status === 401) {
        handleResponseAuth(res.status)
        // 请求照常抛错，由调用方静默降级，不得叠加 toast
        throw new ApiRequestError(401, 'UNAUTHORIZED', false, '登录已过期')
      }

      // 403：PASSWORD_CHANGE_REQUIRED 单飞跳改密；其余直接抛
      if (res.status === 403) {
        const problem = await readProblem(res)
        if (problem.code === 'PASSWORD_CHANGE_REQUIRED') {
          handleResponseAuth(res.status, problem.code)
          throw new ApiRequestError(403, 'PASSWORD_CHANGE_REQUIRED', false, '请先修改密码')
        }
        throw new ApiRequestError(
          403,
          problem.code ?? 'FORBIDDEN',
          problem.retryable ?? false,
          problemMessage(problem, '权限不足'),
        )
      }

      if (!res.ok) {
        const problem = await readProblem(res)
        // 仅 5xx 可重试，且只在幂等面 + 仍有剩余次数时退避续跑
        if (res.status >= 500 && idempotentSurface && attempt < retries) {
          lastErr = new ApiRequestError(
            res.status,
            problem.code ?? 'WORKER_UNAVAILABLE',
            problem.retryable ?? true,
            problemMessage(problem, `服务异常 ${res.status}`),
          )
          await backoffSleep(attempt)
          continue
        }
        // 404 USER_NOT_FOUND/ASSET_NOT_FOUND、409 VERSION_CONFLICT 等原样上抛
        throw new ApiRequestError(
          res.status,
          problem.code ?? 'UNKNOWN',
          problem.retryable ?? false,
          problemMessage(problem, res.statusText || `请求失败（HTTP ${res.status}）`),
        )
      }

      if (res.status === 204) return undefined as T
      const json: unknown = await res.json()
      return (opts.preserveEnvelope ? json : unwrap<T>(json)) as T
    } catch (e) {
      window.clearTimeout(timer)
      // 自身抛出的 ApiRequestError 直接传播（401/403/4xx/耗尽的 5xx）
      if (e instanceof ApiRequestError) throw e
      const isTimeout = e instanceof DOMException && e.name === 'AbortError'
      const isNetwork = e instanceof TypeError
      // status=0（超时/网络）：幂等面内退避重试
      if ((isTimeout || isNetwork) && idempotentSurface && attempt < retries) {
        lastErr = e
        await backoffSleep(attempt)
        continue
      }
      if (isTimeout) throw new ApiRequestError(0, 'TIMEOUT', true, '请求超时')
      if (isNetwork) throw new ApiRequestError(0, 'NETWORK', true, '网络不可用')
      throw e
    }
  }
  if (lastErr instanceof ApiRequestError) throw lastErr
  throw new ApiRequestError(0, 'UNKNOWN', false, '未知请求失败')
}

// ---------- 语义化快捷方法 ----------

export const http = {
  get: <T>(path: string, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'GET' }),
  getEnvelope: <T>(path: string, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<ApiEnvelope<T>>(path, { ...opts, method: 'GET', preserveEnvelope: true }),
  post: <T>(path: string, body?: unknown, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),
  delete: <T>(path: string, opts?: Omit<ReqOpts, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'DELETE' }),
}

// ---------- 测试钩子（生产代码勿用） ----------

/** 复位 401/403 单飞标志，保证用例间隔离 */
export function __resetHttpGatesForTest(): void {
  handling401 = false
  handlingPwdChange = false
  if (reset401Timer !== null) {
    window.clearTimeout(reset401Timer)
    reset401Timer = null
  }
  if (reset403Timer !== null) {
    window.clearTimeout(reset403Timer)
    reset403Timer = null
  }
}

/** 注入确定性随机源（退避抖动断言用） */
export function __setBackoffRngForTest(rng: () => number): void {
  backoffRng = rng
}
