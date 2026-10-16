<!-- src/pages/P1_Register.vue -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError } from '@/api/http'

const router = useRouter()
const authStore = useAuthStore()

const inviteCode = ref('')
const username = ref('')
const displayName = ref('')
const password = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const successMessage = ref<string | null>(null)

// 安全红线：注册凭据仅内存驻留，不接入 useDraft、不写 localStorage、不记录日志。
const WEAK_PASSWORDS = new Set([
  '12345678',
  'password',
  'admin123',
  'to-sign888',
  '11111111',
])

// ---------- 字段级校验（与白皮书改密规则同源：>=8 位 + 字母数字混合 + 弱口令黑名单） ----------
const USERNAME_RE = /^[a-zA-Z0-9_]{4,20}$/

const inviteValid = computed(() => /^[A-Za-z0-9-]{6,20}$/.test(inviteCode.value.trim()))
const usernameValid = computed(() => USERNAME_RE.test(username.value))
const nameValid = computed(() => {
  const n = displayName.value.trim()
  return n.length >= 1 && n.length <= 20
})
const ruleLength = computed(() => password.value.length >= 8)
const ruleMixed = computed(() => /[A-Za-z]/.test(password.value) && /[0-9]/.test(password.value))
const ruleNotWeak = computed(() => password.value.length > 0 && !WEAK_PASSWORDS.has(password.value))
const passwordValid = computed(() => ruleLength.value && ruleMixed.value && ruleNotWeak.value)
const confirmMismatch = computed(
  () => confirmPassword.value.length > 0 && confirmPassword.value !== password.value,
)

const formValid = computed(
  () =>
    inviteValid.value &&
    usernameValid.value &&
    nameValid.value &&
    passwordValid.value &&
    confirmPassword.value === password.value,
)

const passwordScore = computed(
  () => [ruleLength.value, ruleMixed.value, ruleNotWeak.value].filter(Boolean).length,
)
const segmentColors = ['bg-amber', 'bg-sky', 'bg-primary']
function segmentClass(index: number): string {
  if (password.value.length === 0 || index >= passwordScore.value) return 'bg-ink/10'
  return segmentColors[Math.min(index, 2)]
}

async function handleSubmit(): Promise<void> {
  if (submitting.value || !formValid.value) return
  submitting.value = true
  errorMessage.value = null
  try {
    // 模拟注册受理 1.2s；真实实现直接 await authStore.register（内部 POST /auth/register）
    await new Promise((r) => window.setTimeout(r, 1200))
    await authStore.register({
      username: username.value.trim(),
      password: password.value,
      display_name: displayName.value.trim(),
      invite_code: inviteCode.value.trim().toUpperCase(),
    })
    successMessage.value = '注册申请已受理，正在前往登录…'
    password.value = ''
    confirmPassword.value = ''
    window.setTimeout(() => {
      void router.replace('/login')
    }, 1500)
  } catch (e) {
    errorMessage.value =
      e instanceof ApiRequestError ? e.message : '注册失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="bg-blueprint relative flex h-[100dvh] items-center justify-center overflow-hidden p-5 text-ink">
    <h1 class="sr-only">注册创作账号</h1>

    <!-- 贴纸 -->
    <div aria-hidden="true" class="pointer-events-none absolute inset-0">
      <span class="motion-float absolute right-8 top-16 hidden rounded-lg border-2 border-ink bg-sky px-2 py-1 font-mono text-[10px] font-black shadow-hard sm:inline" style="--float-tilt:8deg">&lt;join&gt;</span>
      <span class="motion-float-slow absolute bottom-16 left-10 hidden h-9 w-9 items-center justify-center rounded-xl border-2 border-ink bg-mint shadow-hard sm:flex" style="--float-tilt:-10deg">
        <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>
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
          <span class="rounded-md border border-ink/25 bg-canvas px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;join&gt;</span>
        </div>

        <div class="mt-6">
          <h2 class="text-4xl font-black leading-[0.92] tracking-tighter">
            Sign<br><span class="text-primary">Up</span>
          </h2>
          <p class="mt-3 text-xs font-bold text-ink-muted">工作室邀请制 · 注册即开通创作工作台</p>
        </div>

        <form class="mt-7 flex flex-col gap-4" novalidate @submit.prevent="handleSubmit">
          <!-- 邀请码 -->
          <div class="flex flex-col gap-1.5">
            <label for="reg-invite" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              邀请码 / Invite Code
            </label>
            <input
              id="reg-invite"
              v-model="inviteCode"
              type="text"
              inputmode="text"
              autocomplete="off"
              placeholder="XXXX-2025"
              class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 font-mono text-base font-bold uppercase tracking-widest text-ink placeholder:normal-case placeholder:tracking-normal placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
            >
            <p class="font-mono text-[10px] font-bold text-ink-muted">6-20 位字母数字 · 无邀请码请联系工作室管理员</p>
          </div>

          <!-- 账号 -->
          <div class="flex flex-col gap-1.5">
            <label for="reg-username" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              登录账号 / Username
            </label>
            <input
              id="reg-username"
              v-model="username"
              type="text"
              inputmode="text"
              autocomplete="username"
              placeholder="blogger_x"
              :aria-invalid="username.length > 0 && !usernameValid"
              class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 font-mono text-base font-bold text-ink placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
            >
            <p
              class="font-mono text-[10px] font-bold"
              :class="username.length > 0 && !usernameValid ? 'text-danger' : 'text-ink-muted'"
            >
              4-20 位字母 / 数字 / 下划线
            </p>
          </div>

          <!-- 昵称 -->
          <div class="flex flex-col gap-1.5">
            <label for="reg-name" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              展示昵称 / Display Name
            </label>
            <input
              id="reg-name"
              v-model="displayName"
              type="text"
              autocomplete="nickname"
              placeholder="你的博主昵称"
              class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 text-base font-bold text-ink placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
            >
          </div>

          <!-- 密码 -->
          <div class="flex flex-col gap-1.5">
            <label for="reg-password" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              访问密码 / Password
            </label>
            <div class="relative">
              <input
                id="reg-password"
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="new-password"
                placeholder="至少 8 位，含字母与数字"
                class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 pr-12 text-base font-bold text-ink placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
              >
              <button
                type="button"
                :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                :aria-pressed="showPassword"
                class="absolute right-1.5 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-lg text-ink-muted transition-colors hover:bg-sky/60 hover:text-ink"
                @click="showPassword = !showPassword"
              >
                <svg v-if="showPassword" aria-hidden="true" class="h-4.5 w-4.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"></path>
                  <path d="M6.61 6.61A13.5 13.5 0 0 0 2 12s3 8 10 8a9.74 9.74 0 0 0 5.39-1.61"></path>
                  <line x1="2" y1="2" x2="22" y2="22"></line>
                </svg>
                <svg v-else aria-hidden="true" class="h-4.5 w-4.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2 12s3-8 10-8 10 8 10 8-3 8-10 8-10-8-10-8Z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
              </button>
            </div>
            <!-- 3 段能量栅 -->
            <div class="mt-1 grid grid-cols-3 gap-1.5" aria-hidden="true">
              <span
                v-for="i in 3"
                :key="i"
                class="h-1.5 rounded-full transition-colors duration-200 motion-reduce:transition-none"
                :class="segmentClass(i - 1)"
              ></span>
            </div>
          </div>

          <!-- 确认密码 -->
          <div class="flex flex-col gap-1.5">
            <label for="reg-confirm" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
              确认密码 / Confirm
            </label>
            <input
              id="reg-confirm"
              v-model="confirmPassword"
              type="password"
              autocomplete="new-password"
              enterkeyhint="done"
              placeholder="再输入一次密码"
              :aria-invalid="confirmMismatch"
              :aria-describedby="confirmMismatch ? 'reg-confirm-error' : undefined"
              class="w-full rounded-xl border-2 bg-canvas px-3.5 py-3 text-base font-bold transition-all focus:outline-none focus:ring-2"
              :class="confirmMismatch
                ? 'border-danger text-danger focus:ring-danger'
                : 'border-ink text-ink focus:ring-primary'"
            >
            <p
              v-if="confirmMismatch"
              id="reg-confirm-error"
              role="alert"
              class="font-mono text-[11px] font-black text-danger"
            >
              两次输入的密码不一致
            </p>
          </div>

          <!-- 反馈 -->
          <div
            v-if="errorMessage"
            role="alert"
            aria-live="assertive"
            class="motion-fade flex items-center gap-2 rounded-xl border-2 border-danger bg-blush px-3 py-2 text-xs font-black text-danger"
          >
            <span aria-hidden="true" class="flex h-4 w-4 items-center justify-center rounded-full border border-danger">!</span>
            <span>{{ errorMessage }}</span>
          </div>
          <div
            v-if="successMessage"
            role="status"
            class="motion-fade flex items-center gap-2 rounded-xl border-2 border-ink bg-mint px-3 py-2 text-xs font-black"
          >
            <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"></path></svg>
            <span>{{ successMessage }}</span>
          </div>

          <!-- Split-Block CTA -->
          <button
            type="submit"
            :disabled="!formValid || submitting"
            :aria-busy="submitting"
            class="group flex h-[52px] w-full items-stretch overflow-hidden rounded-2xl border-2 border-ink shadow-[3px_3px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-[3px_3px_0_var(--color-ink)]"
          >
            <span class="flex flex-1 items-center justify-center gap-2 bg-ink px-4 text-sm font-black uppercase tracking-[0.2em] text-white transition-colors duration-150 group-hover:bg-ink-soft">
              <svg v-if="submitting" aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
              </svg>
              {{ submitting ? '受理中' : '创建账号' }}
            </span>
            <span aria-hidden="true" class="flex w-14 items-center justify-center border-l-2 border-ink bg-primary text-lg font-black text-white transition-transform duration-150 group-hover:translate-x-0.5">↗</span>
          </button>

          <p class="text-center text-xs font-bold text-ink-muted">
            已有账号？
            <RouterLink to="/login" class="font-black text-primary underline underline-offset-2">返回登录</RouterLink>
          </p>
        </form>
      </section>
    </div>
  </main>
</template>

<!-- 验收: 邀请码必填(开放策略待编排者确认); 字段级校验实时反馈; 密码3段能量栅; 409重名模拟; 凭据仅内存驻留; 成功跳/login; Split CTA与登录页同源; 动效可降级 -->
