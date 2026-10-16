/**
 * 路由表 + 五条守卫规则（白皮书 §1.3，2026-09-15 修订）。
 *
 * 规则：
 *  1. 未登录访问受保护页 -> /login?redirect=<fullPath>
 *  2. must_change_password=true 且不在改密页/登录页 -> 强制 /change-password
 *  3. 已登录用户可自由访问改密页（自愿改密，P9 个人中心入口依赖此规则）
 *  4. admin 页角色不符 -> 404 视图（不暴露页面存在，与防枚举语义一致）
 *  5. 已登录访问 /login 或 /register -> 首页（mustChangePwd 时交给规则 2）
 */
import { createRouter, createWebHistory, type Router, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

import P0_Home from '@/pages/P0_Home.vue'
import P1_Login from '@/pages/P1_Login.vue'
import P1_ChangePwd from '@/pages/P1_ChangePwd.vue'
import P1_Register from '@/pages/P1_Register.vue'
import P2_Gallery from '@/pages/P2_Gallery.vue'
import P3_Annotate from '@/pages/P3_Annotate.vue'
import P4_Extract from '@/pages/P4_Extract.vue'
import P5_Batch from '@/pages/P5_Batch.vue'
import P5_Review from '@/pages/P5_Review.vue'
import P6_Styles from '@/pages/P6_Styles.vue'
import P6_NewStyle from '@/pages/P6_NewStyle.vue'
import P7_Training from '@/pages/P7_Training.vue'
import P8_Blessings from '@/pages/P8_Blessings.vue'
import P9_UserCenter from '@/pages/P9_UserCenter.vue'
import P10_Audit from '@/pages/P10_Audit.vue'
import P11_Backup from '@/pages/P11_Backup.vue'
import NotFound from '@/pages/NotFound.vue'

export const routes: RouteRecordRaw[] = [
  { path: '/login', component: P1_Login, meta: { public: true } },
  { path: '/register', component: P1_Register, meta: { public: true } },
  { path: '/change-password', component: P1_ChangePwd, meta: { public: true, changePwdOnly: true } },
  { path: '/', component: P0_Home, meta: { requiresAuth: true } },
  { path: '/assets', component: P2_Gallery, meta: { requiresAuth: true } },
  { path: '/assets/:id/annotate', component: P3_Annotate, meta: { requiresAuth: true } },
  { path: '/extract', component: P4_Extract, meta: { requiresAuth: true } },
  { path: '/extract/:assetId', component: P4_Extract, meta: { requiresAuth: true } },
  { path: '/batch', component: P5_Batch, meta: { requiresAuth: true } },
  { path: '/batch/:jobId/review', component: P5_Review, meta: { requiresAuth: true } },
  { path: '/styles', component: P6_Styles, meta: { requiresAuth: true } },
  { path: '/styles/new', component: P6_NewStyle, meta: { requiresAuth: true } },
  { path: '/training', component: P7_Training, meta: { requiresAuth: true } },
  { path: '/blessings', component: P8_Blessings, meta: { requiresAuth: true } },
  { path: '/me', component: P9_UserCenter, meta: { requiresAuth: true } },
  { path: '/audit', component: P10_Audit, meta: { requiresAuth: true, requiresAdmin: true } },
  { path: '/admin/backup', component: P11_Backup, meta: { requiresAuth: true, requiresAdmin: true } },
  { path: '/404', component: NotFound, meta: { public: true } },
  { path: '/:pathMatch(.*)*', component: NotFound },
]

/** 注册五条守卫到任意 router 实例（生产与单测共用） */
export function registerGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    if (!auth.bootstrapped) await auth.bootstrap()

    // 规则 1：未登录访问受保护页 -> /login?redirect=
    if (to.meta.requiresAuth && !auth.isLoggedIn()) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    // 规则 2：must_change_password 只放行改密页（与 /login）
    if (auth.mustChangePwd() && !to.meta.changePwdOnly && to.path !== '/login') {
      return { path: '/change-password' }
    }

    // 规则 3：已登录用户可自由访问 /change-password（自愿改密）；不拦截。

    // 规则 4：admin 页角色不符 -> 404 视图
    if (to.meta.requiresAdmin && !auth.isAdmin()) {
      return { path: '/404' }
    }

    // 规则 5：已登录（且无需改密）访问 /login 或 /register -> 首页
    if ((to.path === '/login' || to.path === '/register') && auth.isLoggedIn() && !auth.mustChangePwd()) {
      return { path: '/' }
    }

    return true
  })
}

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

registerGuards(router)
