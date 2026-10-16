<!-- src/pages/P1_Login.vue -->
<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError } from '@/api/http'
import type { User } from '@/types/domain'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const submitting = ref(false)
const errorMessage = ref<string | null>(null)

const passwordInput = ref<HTMLInputElement | null>(null)

function focusPassword(): void {
  passwordInput.value?.focus()
}

function resolveRedirectTarget(user: User): string {
  if (user.must_change_password) return '/change-password'
  const redirect = route.query.redirect
  if (typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//') && !redirect.startsWith('/login')) {
    return redirect
  }
  return '/'
}

async function handleSubmit(): Promise<void> {
  if (submitting.value) return
  errorMessage.value = null
  if (!username.value.trim() || !password.value) {
    errorMessage.value = '请输入账号与访问密码'
    return
  }
  submitting.value = true
  try {
    const user = await authStore.login(username.value.trim(), password.value)
    await router.replace(resolveRedirectTarget(user))
  } catch (e) {
    // 失败保留 username，仅清空 password 并回焦（PRD §5.1）
    if (e instanceof ApiRequestError) {
      errorMessage.value =
        e.status === 401 ? '账号或密码不正确，请重试' : e.message || '登录失败，请稍后重试'
    } else {
      errorMessage.value = '登录失败，请稍后重试'
    }
    password.value = ''
    passwordInput.value?.focus()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="bg-blueprint relative flex h-[100dvh] items-center justify-center overflow-hidden p-5 text-ink">
    <!-- 贴纸 -->
    <div aria-hidden="true" class="pointer-events-none absolute inset-0">
      <span class="motion-float absolute left-8 top-16 hidden rounded-lg border-2 border-ink bg-mint px-2 py-1 font-mono text-[10px] font-black shadow-hard sm:inline" style="--float-tilt:-8deg">{ }</span>
      <span class="motion-float-slow absolute bottom-20 right-10 hidden h-9 w-9 items-center justify-center rounded-xl border-2 border-ink bg-amber shadow-hard sm:flex" style="--float-tilt:10deg">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="currentColor"><path d="m12 2 2.9 6.3L22 9.2l-5 4.9 1.2 7L12 17.8 5.8 21.1 7 14.1l-5-4.9 7.1-.9z"></path></svg>
      </span>
    </div>

    <div class="motion-rise relative w-full max-w-sm">
      <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-ink"></div>
      <section class="relative rounded-2xl border-2 border-ink bg-surface p-7">
        <div class="flex items-center justify-between">
          <span class="inline-flex items-center gap-1.5 rounded-md border-2 border-ink bg-sky px-2.5 py-1 font-mono text-[10px] font-black tracking-widest">
            <span aria-hidden="true" class="h-1.5 w-1.5 rounded-full bg-primary"></span>
            TO-SIGN STUDIO
          </span>
          <span class="rounded-md border border-ink/25 bg-canvas px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;v0.7&gt;</span>
        </div>

        <div class="mt-6">
          <h1 class="text-4xl font-black leading-[0.92] tracking-tighter">
            Sign<br><span class="text-primary">In</span>
          </h1>
          <p class="mt-3 text-xs font-bold text-ink-muted">释放纯净高清 Cos 创作生产力</p>
        </div>

        <form class="mt-8 flex flex-col gap-5" novalidate @submit.prevent="handleSubmit">
          <div class="flex flex-col gap-2">
            <label for="login-username" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              账号 / Username
            </label>
            <input
              id="login-username"
              v-model="username"
              type="text"
              autocomplete="username"
              placeholder="blogger_x"
              :aria-invalid="errorMessage !== null"
              class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 font-mono text-base font-bold text-ink placeholder:font-bold placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
              @keyup.enter="focusPassword"
            >
          </div>

          <div class="flex flex-col gap-2">
            <label for="login-password" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              访问密码 / Password
            </label>
            <div class="relative">
              <input
                id="login-password"
                ref="passwordInput"
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="current-password"
                enterkeyhint="go"
                placeholder="请输入访问密码"
                :aria-invalid="errorMessage !== null"
                aria-describedby="login-error"
                class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 pr-12 font-mono text-base font-bold text-ink placeholder:font-bold placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
                @keyup.enter="handleSubmit"
              >
              <button
                type="button"
                :aria-pressed="showPassword"
                :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                class="absolute right-1.5 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-lg text-ink transition-colors hover:bg-sky"
                @click="showPassword = !showPassword"
              >
                <svg v-if="!showPassword" aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
                <svg v-else aria-hidden="true" class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"></path>
                  <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c6.5 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"></path>
                  <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3.5 7 10 7a9.74 9.74 0 0 0 5.39-1.61"></path>
                  <line x1="2" y1="2" x2="22" y2="22"></line>
                </svg>
              </button>
            </div>
          </div>

          <div class="min-h-[24px]">
            <div
              v-if="errorMessage"
              id="login-error"
              role="alert"
              aria-live="assertive"
              class="motion-fade flex items-center gap-2 rounded-xl border-2 border-danger bg-blush px-3 py-2 text-xs font-black text-danger"
            >
              <span aria-hidden="true" class="flex h-4 w-4 items-center justify-center rounded-full border border-danger">!</span>
              <span>{{ errorMessage }}</span>
            </div>
          </div>

          <!-- Split-Block CTA：深色块 + 亮蓝箭头，圆润轮廓 -->
          <button
            type="submit"
            :disabled="submitting"
            :aria-busy="submitting"
            class="group flex h-[52px] w-full items-stretch overflow-hidden rounded-2xl border-2 border-ink shadow-[3px_3px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-[3px_3px_0_var(--color-ink)]"
          >
            <span class="flex flex-1 items-center justify-center gap-2 bg-ink px-4 text-sm font-black uppercase tracking-[0.2em] text-white transition-colors duration-150 group-hover:bg-ink-soft">
              <svg v-if="submitting" aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
              </svg>
              {{ submitting ? '验证中' : '登录系统' }}
            </span>
            <span aria-hidden="true" class="flex w-14 items-center justify-center border-l-2 border-ink bg-primary text-lg font-black text-white transition-transform duration-150 group-hover:translate-x-0.5">↗</span>
          </button>

          <p class="text-center text-xs font-bold text-ink-muted">
            还没有创作账号？
            <RouterLink to="/register" class="font-black text-primary underline underline-offset-2">邀请码注册</RouterLink>
          </p>
        </form>
      </section>
    </div>
  </main>
</template>

<!-- 验收: 输入框热区≥44px; 登录失败保留用户名; 提交防双击; 软键盘无不可逆变形; 蓝图网格+圆润双层卡+深色CTA亮蓝箭头+浅蓝淡黑; 动效可降级 -->
