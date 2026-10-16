<!-- src/pages/P1_ChangePwd.vue -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ApiRequestError } from '@/api/http'

const router = useRouter()
const authStore = useAuthStore()

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const successMessage = ref<string | null>(null)

// 安全红线：密码属凭据，不接入 useDraft 写 localStorage；表单状态仅内存驻留。
const WEAK_PASSWORDS = new Set([
  '12345678',
  'password',
  'admin123',
  'to-sign888',
  '11111111',
])

const isForcedChange = computed(() => authStore.mustChangePwd())

const ruleLength = computed(() => newPassword.value.length >= 8)
const ruleMixed = computed(
  () => /[A-Za-z]/.test(newPassword.value) && /[0-9]/.test(newPassword.value),
)
const ruleDifferent = computed(
  () => newPassword.value.length > 0 && newPassword.value !== oldPassword.value,
)
const ruleNotWeak = computed(
  () => newPassword.value.length > 0 && !WEAK_PASSWORDS.has(newPassword.value),
)

const strengthScore = computed(() => {
  const rules = [ruleLength.value, ruleMixed.value, ruleDifferent.value, ruleNotWeak.value]
  return rules.filter(Boolean).length
})

const confirmMismatch = computed(
  () => confirmPassword.value.length > 0 && confirmPassword.value !== newPassword.value,
)

const formValid = computed(
  () =>
    oldPassword.value.length > 0 &&
    newPassword.value.length > 0 &&
    confirmPassword.value === newPassword.value &&
    strengthScore.value === 4,
)

// 4 段糖果色能量：灰 -> 琥珀 -> 浅蓝 -> 墨黑
const segmentColors = ['bg-amber', 'bg-sky', 'bg-primary', 'bg-ink']
function segmentClass(index: number): string {
  if (newPassword.value.length === 0 || index >= strengthScore.value) return 'bg-ink/10'
  return segmentColors[Math.min(index, 3)]
}

const strengthLabel = computed(() => {
  if (newPassword.value.length === 0) return '等待输入'
  if (strengthScore.value <= 1) return '强度不足'
  if (strengthScore.value === 2) return '一般'
  if (strengthScore.value === 3) return '较强'
  return '安全'
})

async function handleSubmit(): Promise<void> {
  if (submitting.value || !formValid.value) return
  submitting.value = true
  errorMessage.value = null
  try {
    await authStore.changePassword(oldPassword.value, newPassword.value)
    successMessage.value = '密码修改成功，马上进入工作台…'
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    window.setTimeout(() => {
      void router.replace('/')
    }, 1500)
  } catch (e) {
    errorMessage.value =
      e instanceof ApiRequestError ? e.message : '密码修改失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between border-b border-ink/15 px-4">
      <button
        v-if="!isForcedChange"
        type="button"
        aria-label="返回"
        class="flex min-h-[44px] items-center gap-1 rounded-full border-2 border-ink bg-surface px-3.5 text-xs font-black shadow-[2px_2px_0_var(--color-ink)] transition-all duration-150 hover:-translate-y-0.5 hover:bg-sky active:translate-y-0 active:shadow-none"
        @click="router.push('/me')"
      >
        <svg aria-hidden="true" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"></path>
          <path d="m12 19-7-7 7-7"></path>
        </svg>
        返回
      </button>
      <span v-else></span>
      <h1 class="font-mono text-[11px] font-black uppercase tracking-[0.3em] text-ink-muted">Security / Password</h1>
      <span class="w-[56px]" aria-hidden="true"></span>
    </header>

    <section class="flex-1 overflow-y-auto overflow-x-hidden px-5 py-7">
      <div class="motion-rise relative mx-auto max-w-md">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1 translate-y-1 rounded-2xl bg-ink"></div>
        <div class="relative rounded-2xl border-2 border-ink bg-surface p-6">
          <div class="flex items-start justify-between">
            <h2 class="text-3xl font-black leading-[0.92] tracking-tighter">
              New<br>Password<span class="text-primary">.</span>
            </h2>
            <span class="rounded-md border border-ink/25 bg-canvas px-1.5 py-0.5 font-mono text-[10px] font-black text-ink-muted">&lt;form&gt;</span>
          </div>

          <div
            v-if="isForcedChange"
            role="note"
            class="mt-5 rounded-xl border-2 border-amber bg-amber/10 px-4 py-3 text-xs font-bold"
          >
            <span class="font-mono text-[10px] font-black uppercase tracking-widest text-amber">Mandatory / </span>
            首次登录或管理员已重置密码，修改完成前其他功能锁定。
          </div>

          <div
            v-if="errorMessage"
            role="alert"
            class="motion-fade mt-4 rounded-xl border-2 border-danger bg-blush px-4 py-2.5 font-mono text-[11px] font-black text-danger"
          >
            '          <svg aria-hidden="true" class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            {{ errorMessage }}'
          </div>
          <div
            v-if="successMessage"
            role="status"
            class="motion-fade mt-4 rounded-xl border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white"
          >
            '            <svg aria-hidden="true" class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"></path></svg>
            {{ successMessage }}'
          </div>

          <form class="mt-7 flex flex-col gap-6" novalidate @submit.prevent="handleSubmit">
            <div class="flex flex-col gap-2">
              <label for="old-password" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
                原密码 / Current
              </label>
              <input
                id="old-password"
                v-model="oldPassword"
                type="password"
                autocomplete="current-password"
                placeholder="请输入当前密码"
                class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 text-base font-bold text-ink placeholder:font-bold placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
              >
            </div>

            <div class="flex flex-col gap-2.5">
              <label for="new-password" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
                新密码 / New
              </label>
              <input
                id="new-password"
                v-model="newPassword"
                type="password"
                autocomplete="new-password"
                placeholder="至少 8 位，含字母与数字"
                aria-describedby="password-strength"
                class="w-full rounded-xl border-2 border-ink bg-canvas px-3.5 py-3 text-base font-bold text-ink placeholder:font-bold placeholder:text-ink-muted transition-all focus:bg-surface focus:outline-none focus:ring-2 focus:ring-primary"
              >
              <!-- 4 段圆润能量栅（相邻圆角胶囊） -->
              <div
                class="grid h-3 grid-cols-4 gap-1.5"
                role="progressbar"
                aria-label="密码强度"
                :aria-valuenow="strengthScore"
                aria-valuemin="0"
                aria-valuemax="4"
              >
                <span
                  v-for="i in 4"
                  :key="i"
                  :class="['h-3 rounded-full transition-colors duration-200', segmentClass(i - 1)]"
                ></span>
              </div>
              <p id="password-strength" class="font-mono text-[10px] uppercase tracking-widest text-ink-muted" aria-live="polite">
                当前强度：<span class="font-black text-ink">{{ strengthLabel }}</span>
              </p>
            </div>

            <div class="flex flex-col gap-2">
              <label for="confirm-password" class="font-mono text-[10px] font-black uppercase tracking-[0.25em] text-ink-muted">
                确认新密码 / Confirm
              </label>
              <input
                id="confirm-password"
                v-model="confirmPassword"
                type="password"
                autocomplete="new-password"
                placeholder="再输入一次新密码"
                :aria-invalid="confirmMismatch"
                :aria-describedby="confirmMismatch ? 'confirm-error' : undefined"
                class="w-full rounded-xl border-2 bg-canvas px-3.5 py-3 text-base font-bold transition-all focus:outline-none focus:ring-2"
                :class="confirmMismatch
                  ? 'border-danger text-danger focus:ring-danger'
                  : 'border-ink text-ink focus:ring-primary'"
              >
              <p
                v-if="confirmMismatch"
                id="confirm-error"
                role="alert"
                class="font-mono text-[11px] font-black text-danger"
              >
                <svg aria-hidden="true" class="inline h-3 w-3 align-[-1px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                两次输入的新密码不一致
              </p>
            </div>

            <!-- 圆角对勾清单 -->
            <ul class="space-y-2 rounded-2xl border-2 border-ink/10 bg-canvas p-4" aria-label="密码规则校验">
              <li
                v-for="rule in [
                  { ok: ruleLength, text: '字符长度不少于 8 位' },
                  { ok: ruleMixed, text: '包含英文字母与数字组合' },
                  { ok: ruleDifferent, text: '新密码不和原密码相同' },
                  { ok: ruleNotWeak, text: '不在常见弱口令名单里' },
                ]"
                :key="rule.text"
                class="flex items-center gap-3 transition-opacity duration-150"
                :class="rule.ok ? 'opacity-100' : 'opacity-70'"
              >
                <span
                  class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 transition-all duration-150"
                  :class="rule.ok ? 'border-ink bg-ink text-white scale-100' : 'border-ink/25 bg-surface text-transparent'"
                >
                  <svg v-if="rule.ok" aria-hidden="true" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M20 6 9 17l-5-5"></path>
                  </svg>
                </span>
                <span class="text-xs font-bold" :class="rule.ok ? 'text-ink' : 'text-ink-muted'">{{ rule.text }}</span>
              </li>
            </ul>

            <!-- Split CTA -->
            <button
              type="submit"
              :disabled="!formValid || submitting"
              :aria-busy="submitting"
              class="group flex h-[52px] w-full items-stretch overflow-hidden rounded-2xl border-2 border-ink shadow-[3px_3px_0_var(--color-ink)] transition-all duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] hover:-translate-y-0.5 hover:shadow-[5px_5px_0_var(--color-ink)] active:translate-y-0 active:shadow-none disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-[3px_3px_0_var(--color-ink)]"
            >
              <span class="flex flex-1 items-center justify-center gap-2 bg-ink px-4 text-sm font-black uppercase tracking-[0.2em] text-white transition-colors group-hover:bg-ink-soft">
                <svg v-if="submitting" aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                </svg>
                {{ submitting ? '提交中' : '确认修改' }}
              </span>
              <span aria-hidden="true" class="flex w-14 items-center justify-center border-l-2 border-ink bg-primary text-lg font-black text-white transition-transform duration-150 group-hover:translate-x-0.5">↗</span>
            </button>
          </form>
        </div>
      </div>
    </section>
  </main>
</template>

<!-- 验收: 4段圆润糖果能量栅实时反馈; 弱口令黑名单; 两次不一致防呆; 提交防双击; 强改密隐藏返回键; 蓝图网格+圆润双层+浅蓝淡黑; 动效可降级 -->
