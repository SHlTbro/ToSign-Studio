/**
 * 路由守卫五规则单测（GWT 验收：路由五规则可用）。
 *
 * 规则：
 *  1. 未登录访问受保护页 -> /login?redirect=
 *  2. must_change_password -> 强制 /change-password
 *  3. 已登录用户可自由访问改密页（自愿改密）
 *  4. admin 页角色不符 -> /404
 *  5. 已登录访问 /login -> 首页
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createRouter, createWebHistory, type Router } from 'vue-router'
import { registerGuards, routes } from '@/router'
import { useAuthStore } from '@/stores/auth'
import { mockMe, mockMeAdmin, mockMeMustChangePwd } from '@/api/mock'

function makeRouter(): Router {
  const r = createRouter({
    history: createWebHistory(),
    routes,
  })
  registerGuards(r)
  return r
}

async function navigate(router: Router, path: string): Promise<void> {
  await router.push(path)
  await router.isReady().catch(() => {})
}

describe('路由守卫五规则', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('规则1：未登录访问受保护页 -> /login 带 redirect', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(null)
    const router = makeRouter()
    await navigate(router, '/assets')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query.redirect).toBe('/assets')
  })

  it('规则2：must_change_password -> 强制 /change-password', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMeMustChangePwd)
    const router = makeRouter()
    await navigate(router, '/assets')
    expect(router.currentRoute.value.path).toBe('/change-password')
  })

  it('规则2：must_change_password 允许停留在改密页', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMeMustChangePwd)
    const router = makeRouter()
    await navigate(router, '/change-password')
    expect(router.currentRoute.value.path).toBe('/change-password')
  })

  it('规则3：已登录用户可自愿访问改密页（停留 /change-password）', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMe)
    const router = makeRouter()
    await navigate(router, '/change-password')
    expect(router.currentRoute.value.path).toBe('/change-password')
  })

  it('规则5扩展：未登录可访问 /register', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(null)
    const router = makeRouter()
    await navigate(router, '/register')
    expect(router.currentRoute.value.path).toBe('/register')
  })

  it('规则5扩展：已登录访问 /register -> 首页', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMe)
    const router = makeRouter()
    await navigate(router, '/register')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('规则4：user 访问 admin 页 -> /404', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMe)
    const router = makeRouter()
    await navigate(router, '/admin/backup')
    expect(router.currentRoute.value.path).toBe('/404')
  })

  it('规则4：admin 可访问 admin 页', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMeAdmin)
    const router = makeRouter()
    await navigate(router, '/admin/backup')
    expect(router.currentRoute.value.path).toBe('/admin/backup')
  })

  it('规则5：已登录访问 /login -> 首页', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMe)
    const router = makeRouter()
    await navigate(router, '/login')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('规则5例外：已登录但 must_change_password 时访问 /login 不被弹走', async () => {
    const auth = useAuthStore()
    auth.__setUserForTest(mockMeMustChangePwd)
    const router = makeRouter()
    await navigate(router, '/login')
    expect(router.currentRoute.value.path).toBe('/login')
  })
})
