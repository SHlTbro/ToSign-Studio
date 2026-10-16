/**
 * T58B 页面接线测试公用挂载件：
 * - pinia + memory router + auth store 身份注入；
 * - fetch stub 按 URL 前缀路由到用例自定义应答；
 * - 信封应答构造（{data, meta, warnings}）与 problem+json 错误体。
 */
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { vi } from 'vitest'
import { createRouter, createMemoryHistory, type Router } from 'vue-router'
import { routes, registerGuards } from '@/router'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types/domain'
import { __resetHttpGatesForTest } from '@/api/http'

export interface FetchLog {
  method: string
  url: string
  body?: unknown
}

export interface FetchPlan {
  /** 精确/前缀匹配的应答表；value 为信封 data 或 problem 体（配合 status） */
  [prefix: string]: { status: number; json: unknown }
}

function matchPlan(plan: FetchPlan, url: string): { status: number; json: unknown } | null {
  const path = url.split('?')[0]
  let best: { key: string; hit: { status: number; json: unknown } } | null = null
  for (const [prefix, hit] of Object.entries(plan)) {
    const querySpecificMatch = prefix.includes('?') && url === prefix
    const pathMatch = !prefix.includes('?') && (path === prefix || path.startsWith(prefix))
    if ((querySpecificMatch || pathMatch) && (best === null || prefix.length > best.key.length)) {
      best = { key: prefix, hit }
    }
  }
  return best?.hit ?? null
}

export function envelope(data: unknown, meta: Record<string, unknown> = {}) {
  return { status: 200, json: { data, meta: { api_version: 'v1', schema_version: '0.4', ...meta }, warnings: [] } }
}

export function problem(status: number, code: string, detail: string) {
  return {
    status,
    json: {
      type: 'about:blank', title: code, status, detail, instance: '/',
      code, retryable: false, trace_id: 't58b-test',
    },
  }
}

const TEST_USER: User = {
  user_id: '2', username: 'creator58b', display_name: '创作者', role: 'user', must_change_password: false,
}
const TEST_ADMIN: User = {
  user_id: '1', username: 'admin58b', display_name: '管理员', role: 'admin', must_change_password: false,
}

export interface MountOpts {
  asAdmin?: boolean
  plan: FetchPlan
}

export function mountPage(component: unknown, opts: MountOpts): { wrapper: VueWrapper; logs: FetchLog[]; router: Router; pinia: Pinia } {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.__setUserForTest(opts.asAdmin ? TEST_ADMIN : TEST_USER)

  const router = createRouter({ history: createMemoryHistory(), routes })
  registerGuards(router)

  const logs: FetchLog[] = []
  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    let body: unknown
    if (typeof init?.body === 'string') {
      try { body = JSON.parse(init.body) } catch { body = init.body }
    }
    logs.push({ method, url, body })
    // Route transitions bootstrap the same authenticated fixture identity.
    const hit = matchPlan(opts.plan, url) ?? (url === '/api/v1/auth/me' ? envelope(opts.asAdmin ? TEST_ADMIN : TEST_USER) : null)
    if (hit === null) {
      return { ok: false, status: 404, json: async () => problem(404, 'NOT_FOUND', `no plan for ${url}`) } as Response
    }
    return {
      ok: hit.status < 400,
      status: hit.status,
      json: async () => hit.json,
    } as Response
  }
  vi.stubGlobal('fetch', fetchImpl)
  __resetHttpGatesForTest()

  const wrapper = mount(component as never, {
    global: { plugins: [pinia, router] },
    attachTo: document.body,
  })
  return { wrapper, logs, router, pinia }
}

export { flushPromises, TEST_USER, TEST_ADMIN }
