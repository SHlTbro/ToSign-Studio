<script setup lang="ts">
import { userError } from '@/utils/userLanguage'
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiRequestError, request } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

interface Profile {
  style_id: number
  name: string
  source_type: string
  status: string
  receipt_id: string | null
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const name = ref('')
const profile = ref<Profile | null>(null)
const confirmed = ref(false)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const creationUncertain = ref(false)
const reconciled = ref(false)
const pendingMessage = '创建结果尚未确认，请先返回风格资产库检查是否已保存，避免重复创建'
let generation = 0
let disposed = false

function failureMessage(error: unknown, fallback: string): string {
  return error instanceof ApiRequestError ? userError(error)
    : error instanceof Error && error.message ? error.message : fallback
}

function pendingKey(): string {
  if (!auth.me?.user_id) throw new Error('请先登录后再创建档案')
  return `yy:p6:pending-profile:${auth.me.user_id}`
}
function readPending(): void {
  creationUncertain.value = false
  reconciled.value = false
  try {
    const raw = sessionStorage.getItem(pendingKey())
    if (raw) { creationUncertain.value = true; error.value = pendingMessage }
  } catch { error.value = '无法读取本次创建的恢复状态，请检查浏览器会话存储；暂时不能新建档案'; creationUncertain.value = true }
}
function clearPending(): void {
  try { sessionStorage.removeItem(pendingKey()); creationUncertain.value = false; error.value = ''; reconciled.value = false }
  catch { error.value = '无法保存恢复状态，请检查浏览器会话存储后重试' }
}
function resolvePending(): void {
  if (!creationUncertain.value || (!profile.value && !reconciled.value) || busy.value) return
  clearPending()
}

async function recoverProfile(value: unknown, force = false): Promise<void> {
  const current = ++generation
  confirmed.value = false
  error.value = ''
  readPending()
  if (value == null) { profile.value = null; loading.value = false; return }
  const id = Number(value)
  if (!Number.isSafeInteger(id) || id < 1) { profile.value = null; loading.value = false; error.value = '档案编号无效，请返回风格资产库'; return }
  if (!force && profile.value?.style_id === id) return
  profile.value = null
  loading.value = true
  try {
    const saved = await request<Profile>(`/api/v1/styles/${id}`)
    if (disposed || current !== generation) return
    if (saved.source_type !== 'HANDWRITING_ASSET' || saved.receipt_id) throw new Error('此入口仅用于手写来源档案，不能审核训练发布结果')
    profile.value = saved
  } catch (e) {
    if (!disposed && current === generation) error.value = failureMessage(e, '档案读取失败')
  } finally {
    if (!disposed && current === generation) loading.value = false
  }
}
watch(() => [route.query.style_id, auth.me?.user_id], () => { profile.value = null; name.value = ''; void recoverProfile(route.query.style_id) }, { immediate: true })
onBeforeUnmount(() => { disposed = true; generation += 1 })

async function createProfile(): Promise<void> {
  if (busy.value || loading.value || profile.value || route.query.style_id != null || creationUncertain.value || !name.value.trim()) return
  busy.value = true
  error.value = ''
  const current = generation
  let key: string
  try {
    key = pendingKey()
    sessionStorage.setItem(key, JSON.stringify({ version: 1, name: name.value.trim(), started_at: new Date().toISOString() }))
  } catch {
    error.value = '无法保存本次创建的恢复状态，请检查浏览器会话存储后重试；尚未发送创建请求'
    busy.value = false
    return
  }
  try {
    const created = await request<Profile>('/api/v1/styles', {
      method: 'POST', body: { name: name.value.trim(), source_type: 'HANDWRITING_ASSET' }, retries: 0,
    })
    // A 201 is a confirmed outcome even if navigation happened meanwhile.
    sessionStorage.removeItem(key)
    if (disposed || current !== generation) return
    profile.value = created
    // Preserve the returned server ID for refresh/recovery. Never auto-approve.
    await router.replace({ path: '/styles/new', query: { style_id: String(created.style_id) } })
  } catch (e) {
    if (disposed || current !== generation) return
    creationUncertain.value = !(e instanceof ApiRequestError) || e.status === 0 || e.status >= 500
    if (!creationUncertain.value) {
      try { sessionStorage.removeItem(key) } catch { creationUncertain.value = true }
    }
    error.value = creationUncertain.value
      ? pendingMessage
      : failureMessage(e, '创建失败')
  } finally { if (!disposed) busy.value = false }
}

async function approveProfile(): Promise<void> {
  const saved = profile.value
  if (busy.value || !confirmed.value || !saved || saved.status !== 'draft') return
  busy.value = true
  error.value = ''
  const current = generation
  try {
    const approved = await request<Profile>(`/api/v1/styles/${saved.style_id}/approve`, { method: 'POST', body: {}, retries: 0 })
    if (!disposed && current === generation) profile.value = approved
  } catch (e) {
    if (!disposed && current === generation) error.value = failureMessage(e, '档案准入失败，可重试或返回风格库继续')
  } finally { if (!disposed) busy.value = false }
}
</script>

<template>
  <section class="space-y-3 rounded-2xl border-2 border-ink bg-sky p-4 shadow-hard" aria-labelledby="handwriting-onboarding-title">
    <h2 id="handwriting-onboarding-title" class="text-sm font-black">创建个人手写档案</h2>
    <p class="text-xs font-bold text-ink-muted">先建立来源档案，再上传历史 To 签图并提取字符。档案准入不代表字符已确认、模型已训练或质量通过。</p>
    <p v-if="loading" role="status" class="text-sm">正在读取已保存的档案…</p>
    <form v-else-if="!profile && route.query.style_id == null" class="space-y-3" @submit.prevent="createProfile">
      <label for="handwriting-profile-name" class="block text-xs font-black">档案名称</label>
      <input id="handwriting-profile-name" v-model="name" class="w-full rounded-lg border-2 border-ink bg-surface px-3 py-2 select-text" maxlength="80" required :disabled="busy || creationUncertain" placeholder="例如：我的 To 签手写" />
      <p class="text-xs text-ink-muted">来源：个人手写</p>
      <button type="submit" class="btn-hard btn-hard--primary" :disabled="busy || creationUncertain || !name.trim()">{{ busy ? '正在保存…' : '创建手写档案' }}</button>
    </form>
    <template v-else-if="profile">
      <p role="status" class="text-sm font-black">{{ profile.name }} · {{ profile.status === 'approved' ? '来源档案已准入' : profile.status === 'draft' ? '草稿已保存' : '尚未准入' }}</p>
      <template v-if="profile.status === 'draft'">
        <label class="flex items-start gap-2 text-xs font-bold">
          <input v-model="confirmed" type="checkbox" :disabled="busy" aria-label="确认档案准入" />
          将这个手写来源档案设为已准入，用于后续字符采集。此操作仅更新档案状态，不代表字符已确认、模型已训练或质量通过；字符确认、训练快照、评估与发布继续遵守原有流程。
        </label>
        <button type="button" class="btn-hard btn-hard--primary" :disabled="busy || !confirmed" @click="approveProfile">{{ busy ? '正在确认…' : '确认准入档案' }}</button>
      </template>
      <div v-else-if="profile.status === 'approved'" class="flex flex-wrap gap-2">
        <RouterLink :to="{ path: '/extract', query: { style_id: String(profile.style_id) } }" class="btn-hard btn-hard--primary">上传历史图并提取字符</RouterLink>
        <RouterLink :to="{ path: '/training', query: { style_id: String(profile.style_id) } }" class="btn-hard">查看此档案训练准备情况</RouterLink>
      </div>
    </template>
    <p v-if="error" role="alert" class="text-xs font-bold text-danger">{{ error }}</p>
    <div v-if="creationUncertain" class="space-y-2 rounded-lg border border-amber p-3 text-xs">
      <p>本次未确认创建只记录在当前账号、当前浏览器标签页；刷新后仍会阻止重复提交，不会自动重发。</p>
      <button v-if="profile" type="button" class="btn-hard btn-hard--compact" :disabled="busy" @click="resolvePending">使用当前已保存档案结束未确认状态</button>
      <template v-else>
        <label class="flex gap-2"><input v-model="reconciled" type="checkbox" aria-label="已检查风格库并决定重新创建" />我已检查风格库，决定允许重新创建；若原请求已经保存，可能产生重复档案。</label>
        <button type="button" class="btn-hard btn-hard--compact" :disabled="busy || !reconciled" @click="resolvePending">清除未确认状态，允许新建</button>
      </template>
    </div>
    <button v-if="error && route.query.style_id != null" type="button" class="btn-hard btn-hard--compact" :disabled="busy || loading" @click="recoverProfile(route.query.style_id, true)">重新读取档案</button>
    <RouterLink to="/styles" class="block text-xs font-bold underline">返回风格资产库，继续已保存的档案</RouterLink>
  </section>
</template>
