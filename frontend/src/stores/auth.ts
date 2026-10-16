/**
 * authStore（白皮书 §1.2.1）—— T58 Phase 1：真实会话流。
 * bootstrap/login/logout/changePassword/register 走真后端 /api/v1/auth/*；
 * cookie 会话（HttpOnly; Secure; SameSite=Lax），CSRF 由 http.ts 统一注入。
 * mock fixtures 仅供 vitest（__setUserForTest）与未接线页面使用。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { User } from '@/types/domain'
import { ApiRequestError, request } from '@/api/http'
import {
  ENDPOINT_AUTH_ME,
  ENDPOINT_AUTH_LOGIN,
  ENDPOINT_AUTH_LOGOUT,
  ENDPOINT_AUTH_CHANGE_PASSWORD,
} from '@/api/endpoints'

/** 注册入参（docs/frontend-integration-checklist.md §5） */
export interface RegisterPayload {
  username: string
  password: string
  display_name: string
  invite_code: string
}

interface AuthUserPayload {
  user_id: number
  username: string
  display_name: string
  role: string
  must_change_password?: boolean
}

function toUser(d: AuthUserPayload): User {
  return {
    user_id: String(d.user_id),
    username: d.username,
    display_name: d.display_name,
    role: d.role as User['role'],
    must_change_password: d.must_change_password === true,
  }
}

export const useAuthStore = defineStore('auth', () => {
  const me = ref<User | null>(null)
  const bootstrapped = ref(false)

  const isLoggedIn = () => me.value !== null
  const isAdmin = () => me.value?.role === 'admin'
  const mustChangePwd = () => me.value?.must_change_password === true

  async function bootstrap(): Promise<void> {
    try {
      const d = await request<AuthUserPayload>(ENDPOINT_AUTH_ME.path)
      me.value = toUser(d)
    } catch (e) {
      // 未登录（401）→ 匿名态；401 的路由副作用（replace /login）由 http.ts 承担
      if (e instanceof ApiRequestError && e.status === 401) {
        me.value = null
      } else {
        throw e
      }
    } finally {
      bootstrapped.value = true
    }
  }

  async function login(username: string, password: string): Promise<User> {
    const d = await request<AuthUserPayload>(ENDPOINT_AUTH_LOGIN.path, {
      method: 'POST',
      body: { username, password },
    })
    me.value = toUser(d)
    bootstrapped.value = true
    return me.value
  }

  async function logout(): Promise<void> {
    try {
      await request(ENDPOINT_AUTH_LOGOUT.path, { method: 'POST' })
    } finally {
      me.value = null
    }
  }

  async function changePassword(oldPwd: string, newPwd: string): Promise<void> {
    await request(ENDPOINT_AUTH_CHANGE_PASSWORD.path, {
      method: 'POST',
      body: { old_password: oldPwd, new_password: newPwd },
    })
    // 重拉 /me 拿权威态（must_change_password=false）
    await bootstrap()
  }

  async function register(payload: RegisterPayload): Promise<void> {
    // 201 无自动登录（后端不种 cookie）；安全红线：password 仅内存传递，不落地不记录
    await request('/api/v1/auth/register', {
      method: 'POST',
      body: { ...payload },
    })
  }

  /** 测试钩子：模拟不同身份（未登录 / 需改密 / admin）。 */
  function __setUserForTest(u: User | null): void {
    me.value = u
    bootstrapped.value = true
  }

  return {
    me,
    bootstrapped,
    isLoggedIn,
    isAdmin,
    mustChangePwd,
    bootstrap,
    login,
    logout,
    changePassword,
    register,
    __setUserForTest,
  }
})
