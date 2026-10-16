<!-- src/pages/P11_Backup.vue -->
<script setup lang="ts">
import { stateLabel } from '@/utils/userLanguage'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { request } from '@/api/http'

const router = useRouter()
const authStore = useAuthStore()

// 双保险：路由守卫已拦非 admin（规则4 → /404），页面层再校验一次
onMounted(() => {
  if (!authStore.isAdmin()) void router.replace('/404')
  void loadBackups()
  void loadWorkers()
})
const isAdmin = computed(() => authStore.isAdmin())

// Worker 遥测（B1-06）：GET /workers/status 真数据；失败/非 admin 降级 -- 占位
interface WorkersStatus {
  worker_id: string
  queue_depth: number
  running_tasks: number
  max_concurrency: number
}
const worker = ref<{ worker_id: string | null; running_tasks: number | null; queue_depth: number | null; max_concurrency: number | null }>({
  worker_id: null,
  running_tasks: null,
  queue_depth: null,
  max_concurrency: null,
})

async function loadWorkers(): Promise<void> {
  try {
    const d = await request<WorkersStatus>('/api/v1/workers/status')
    worker.value = {
      worker_id: d.worker_id,
      running_tasks: d.running_tasks,
      queue_depth: d.queue_depth,
      max_concurrency: d.max_concurrency,
    }
  } catch {
    // 遥测失败只降级为 --，不阻塞备份列表
  }
}

// 真数据源：GET/POST /backups + verify/restore（v0.2 §1.7 五端点，全 admin）
interface BackupRecord {
  backup_id: string
  status: string
  created_at: string
  size_bytes: number
  includes: string[]
  checksum: string
  path_relative: string
}
const backups = ref<BackupRecord[]>([])
const loadingBackups = ref(true)
const backupsError = ref<string | null>(null)

function formatMb(bytes: number): string {
  return bytes > 0 ? (bytes / 1024 / 1024).toFixed(1) : '0.0'
}

async function loadBackups(): Promise<void> {
  loadingBackups.value = true
  backupsError.value = null
  try {
    const d = await request<{ items: BackupRecord[] }>('/api/v1/backups')
    backups.value = d.items
  } catch (e) {
    backupsError.value = e instanceof Error && e.message ? e.message : '备份列表加载失败'
  } finally {
    loadingBackups.value = false
  }
}

function upsertBackup(record: BackupRecord): void {
  const index = backups.value.findIndex((item) => item.backup_id === record.backup_id)
  if (index < 0) {
    backups.value = [record, ...backups.value]
    return
  }
  backups.value = backups.value.map((item, i) => i === index ? record : item)
}

const creating = ref(false)
const notice = ref<string | null>(null)
let noticeTimer: number | null = null
const BACKUP_POLL_INTERVAL_MS = 1000
const BACKUP_POLL_MAX_ATTEMPTS = 30
let backupPollRunId = 0
let backupPollTimer: number | null = null
let resolveBackupPollDelay: ((active: boolean) => void) | null = null

function cancelBackupPolling(): void {
  backupPollRunId += 1
  if (backupPollTimer !== null) window.clearTimeout(backupPollTimer)
  backupPollTimer = null
  resolveBackupPollDelay?.(false)
  resolveBackupPollDelay = null
}

function waitForBackupPoll(runId: number): Promise<boolean> {
  return new Promise((resolve) => {
    resolveBackupPollDelay = resolve
    backupPollTimer = window.setTimeout(() => {
      backupPollTimer = null
      resolveBackupPollDelay = null
      resolve(runId === backupPollRunId)
    }, BACKUP_POLL_INTERVAL_MS)
  })
}

onBeforeUnmount(() => {
  cancelBackupPolling()
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
})

function flash(message: string): void {
  notice.value = message
  if (noticeTimer !== null) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.value = null
    noticeTimer = null
  }, 2600)
}

type BackupPollResult = { record: BackupRecord | null; error: string | null }

async function pollBackupUntilTerminal(initial: BackupRecord, runId: number): Promise<BackupPollResult> {
  let latest = initial
  for (let attempt = 0; attempt < BACKUP_POLL_MAX_ATTEMPTS; attempt += 1) {
    if (runId !== backupPollRunId) return { record: null, error: null }
    const active = await waitForBackupPoll(runId)
    if (!active) return { record: null, error: null }

    try {
      latest = await request<BackupRecord>(`/api/v1/backups/${initial.backup_id}`)
      if (runId !== backupPollRunId) return { record: null, error: null }
      upsertBackup(latest)
      if (latest.status === 'succeeded' || latest.status === 'failed') {
        return { record: latest, error: null }
      }
    } catch (e) {
      if (runId !== backupPollRunId) return { record: null, error: null }
      const message = e instanceof Error && e.message ? e.message : '备份状态查询失败'
      if (attempt === BACKUP_POLL_MAX_ATTEMPTS - 1) return { record: latest, error: message }
    }
  }
  return { record: latest, error: null }
}

async function createBackup(): Promise<void> {
  if (creating.value) return
  cancelBackupPolling()
  const runId = backupPollRunId
  creating.value = true
  try {
    const created = await request<BackupRecord>('/api/v1/backups', { method: 'POST', timeoutMs: 120_000 })
    await loadBackups()
    if (runId !== backupPollRunId) return
    if (created.backup_id) upsertBackup(created)

    let finalRecord = created
    if (created.backup_id && (created.status === 'queued' || created.status === 'running')) {
      const result = await pollBackupUntilTerminal(created, runId)
      if (runId !== backupPollRunId || result.record === null) return
      finalRecord = result.record
      if (result.error) {
        flash(`备份状态查询失败：${result.error}`)
        return
      }
    }

    if (finalRecord.status === 'succeeded') {
      flash('全量冷备份已完成')
    } else if (finalRecord.status === 'failed') {
      flash('备份失败，请重试')
    } else {
      flash(`备份已提交，当前状态：${finalRecord.status || '未知'}`)
    }
  } catch (e) {
    flash(e instanceof Error && e.message ? `备份创建失败：${e.message}` : '备份创建失败，请重试')
  } finally {
    if (runId === backupPollRunId) creating.value = false
  }
}

const verifyingId = ref<string | null>(null)
async function verifyBackup(b: BackupRecord): Promise<void> {
  if (verifyingId.value) return
  verifyingId.value = b.backup_id
  try {
    const d = await request<{ ok?: boolean; verified?: boolean; files_checked?: number; status?: string }>(
      `/api/v1/backups/${b.backup_id}/verify`,
      { method: 'POST', timeoutMs: 120_000 },
    )
    const okFlag = d.ok === true || d.verified === true || d.status === 'ok' || d.status === 'verified' || d.status === 'checksum_ok'
    flash(okFlag ? `校验通过：${d.files_checked ?? '全部'} 个文件 checksum 一致` : `校验完成，状态：${d.status ?? '未知'}`)
  } catch (e) {
    flash(e instanceof Error && e.message ? `校验失败：${e.message}` : '校验失败，请重试')
  } finally {
    verifyingId.value = null
  }
}

// ---------- RESTORE 门禁（真端点 POST /backups/{id}/restore；CC-17 恢复后全会话失效） ----------
const restoreTarget = ref<BackupRecord | null>(null)
const restoreConfirm = ref('')
const restoreRunning = ref(false)

const canRestore = computed(() => restoreTarget.value?.status === 'succeeded' && restoreConfirm.value === 'RESTORE')

function openRestore(b: BackupRecord): void {
  if (b.status !== 'succeeded') return
  restoreTarget.value = b
  restoreConfirm.value = ''
}
function closeRestore(): void {
  if (restoreRunning.value) return
  restoreTarget.value = null
}

async function confirmRestore(): Promise<void> {
  if (!restoreTarget.value || restoreTarget.value.status !== 'succeeded' || !canRestore.value || restoreRunning.value) return
  restoreRunning.value = true
  try {
    await request(`/api/v1/backups/${restoreTarget.value.backup_id}/restore`, {
      method: 'POST',
      body: { confirm: true },
      timeoutMs: 300_000,
    })
    restoreRunning.value = false
    // CC-17：恢复成功 → 全部会话 revoke → 强制重新登录
    void authStore.logout().then(() => router.replace('/login'))
  } catch (e) {
    restoreRunning.value = false
    flash(e instanceof Error && e.message ? `恢复失败：${e.message}` : '恢复失败，请重试')
  }
}
</script>

<template>
  <main v-if="isAdmin" class="bg-blueprint flex h-[100dvh] flex-col overflow-hidden text-ink select-none">
    <h1 class="sr-only">备份与 Worker 遥测</h1>

    <header class="safe-t z-20 flex h-14 shrink-0 items-center justify-between gap-2 border-b border-ink/15 px-3">
      <button type="button" aria-label="返回用户中心" class="btn-hard btn-hard--compact !rounded-full !px-3" @click="router.push('/me')">
        <svg aria-hidden="true" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5"></path><path d="m12 19-7-7 7-7"></path></svg>
      </button>
      <div class="flex min-w-0 flex-col items-center">
        <p class="truncate text-sm font-black">备份与 Worker 遥测</p>
        <span class="tag-code mt-0.5">{{ worker.worker_id ?? '--' }} · queue {{ worker.queue_depth ?? '--' }}</span>
      </div>
      <span class="rounded-md border-2 border-ink bg-danger px-2 py-0.5 text-[10px] font-black text-white">ADMIN</span>
    </header>

    <section class="flex-1 space-y-5 overflow-y-auto overflow-x-hidden px-4 pb-10 pt-4">
      <!-- Worker 三卡（B1-06：GET /workers/status 真数据；失败降级 --） -->
      <div class="grid grid-cols-3 gap-2.5" data-test="worker-cards" title="遥测来自 GET /workers/status；加载失败时显示 --">
        <div class="rounded-xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)]" data-test="card-running">
          <p class="flex items-center justify-center gap-1.5 text-[10px] font-black text-ink-muted">
            <span aria-hidden="true" class="h-2 w-2 rounded-full bg-mint"></span>运行中
          </p>
          <p class="mt-1.5 font-mono text-2xl font-black">{{ worker.running_tasks ?? '--' }}</p>
        </div>
        <div class="rounded-xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)]" data-test="card-queue">
          <p class="flex items-center justify-center gap-1.5 text-[10px] font-black text-ink-muted">
            <span aria-hidden="true" class="h-2 w-2 rounded-full bg-amber"></span>排队深度
          </p>
          <p class="mt-1.5 font-mono text-2xl font-black">{{ worker.queue_depth ?? '--' }}</p>
        </div>
        <div class="rounded-xl border-2 border-ink bg-surface p-3 text-center shadow-[2px_2px_0_var(--color-ink)]" data-test="card-concurrency">
          <p class="text-[10px] font-black text-ink-muted">并发上限</p>
          <p class="mt-1.5 font-mono text-2xl font-black">{{ worker.max_concurrency ?? '--' }}</p>
        </div>
      </div>

      <!-- 创建备份 -->
      <button type="button" class="btn-hard btn-hard--primary w-full !min-h-[52px]" :disabled="creating" @click="createBackup">
        <svg v-if="creating" aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
        </svg>
        {{ creating ? '正在创建全量冷备份…' : '立即创建全量冷备份' }}
      </button>

      <!-- 错误态 -->
      <div
        v-if="backupsError"
        class="flex items-center justify-between gap-3 rounded-2xl border-2 border-coral bg-blush px-4 py-3"
        role="alert"
      >
        <p class="text-xs font-black text-danger">备份列表加载失败：{{ backupsError }}</p>
        <button type="button" class="btn-hard btn-hard--compact shrink-0" @click="loadBackups">重试</button>
      </div>

      <!-- 历史备份 -->
      <div class="space-y-3">
        <p class="font-mono text-[10px] font-black uppercase tracking-widest text-ink-muted">history · {{ backups.length }} snapshots</p>
        <p v-if="!loadingBackups && !backupsError && backups.length === 0" class="rounded-2xl border-2 border-dashed border-ink/30 bg-surface/70 px-6 py-10 text-center text-xs font-bold text-ink-muted" role="status">
          暂无备份快照 · 点上方按钮创建第一个全量冷备份
        </p>
        <article v-for="b in backups" :key="b.backup_id" class="flex items-center justify-between gap-3 rounded-2xl border-2 border-ink bg-surface p-3.5 shadow-[2px_2px_0_var(--color-ink)]">
          <div class="min-w-0">
            <p class="font-mono text-xs font-black">{{ b.backup_id }} · {{ formatMb(b.size_bytes) }} MB · {{ stateLabel(b.status) }}</p>
            <p class="mt-0.5 font-mono text-[10px] font-bold text-ink-muted">{{ b.created_at }}</p>
            <p class="mt-0.5 truncate font-mono text-[10px] font-bold text-ink-muted">checksum: {{ b.checksum.slice(0, 16) }}…</p>
          </div>
          <div class="flex shrink-0 flex-col gap-1.5">
            <button type="button" class="btn-hard btn-hard--compact" :disabled="verifyingId === b.backup_id" @click="verifyBackup(b)">
              {{ verifyingId === b.backup_id ? '校验中…' : '校验' }}
            </button>
            <button type="button" class="btn-hard btn-hard--danger btn-hard--compact" :disabled="b.status !== 'succeeded'" @click="openRestore(b)">恢复快照</button>
          </div>
        </article>
      </div>
    </section>

    <!-- 灾难恢复高危确认 Modal -->
    <div v-if="restoreTarget" class="motion-fade fixed inset-0 z-50 flex items-center justify-center bg-ink/70 p-4" @click.self="closeRestore">
      <div class="relative w-full max-w-md" role="dialog" aria-modal="true" aria-label="灾难恢复确认">
        <div aria-hidden="true" class="absolute inset-0 translate-x-1.5 translate-y-1.5 rounded-2xl bg-danger"></div>
        <div class="relative space-y-4 rounded-2xl border-4 border-danger bg-surface p-5">
          <div class="flex items-start gap-2.5 rounded-xl border-2 border-coral bg-blush p-3" role="alert">
            <svg aria-hidden="true" class="h-5 w-5 shrink-0 text-coral" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path>
              <line x1="12" y1="9" x2="12" y2="13"></line>
              <line x1="12" y1="17" x2="12.01" y2="17"></line>
            </svg>
            <p class="text-xs font-black text-danger">高风险操作：在线恢复会替换备份包含的文件池，但不会回滚 SQLite 数据库；恢复后需要重新登录。</p>
          </div>

          <div>
            <p class="text-xs font-black">目标快照：<span class="font-mono">{{ restoreTarget.backup_id }}</span>（{{ restoreTarget.created_at }}）</p>
            <label for="restore-gate" class="mt-2 block text-[11px] font-black text-ink-muted">请在下方准确输入 RESTORE 以解除确认按钮锁定：</label>
            <input
              id="restore-gate"
              v-model="restoreConfirm"
              class="input-hard input-hard--danger mt-2 text-center font-mono text-lg font-black tracking-widest"
              placeholder="RESTORE"
              autocomplete="off"
              aria-describedby="restore-hint"
            >
            <p id="restore-hint" class="mt-1.5 font-mono text-[10px] font-bold" :class="canRestore ? 'text-emerald-700' : 'text-ink-muted'" aria-live="polite">
              {{ canRestore ? '门禁已解锁，可执行恢复' : '需与 RESTORE 完全一致（区分大小写）' }}
            </p>
          </div>

          <div v-if="restoreRunning" class="flex items-center justify-center gap-2 rounded-xl border-2 border-ink bg-canvas p-3" role="status">
            <svg aria-hidden="true" class="h-4 w-4 motion-safe:animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
            </svg>
            <p class="font-mono text-[10px] font-black text-danger">系统镜像重建中 · checksum 先验 · 失败不覆盖</p>
          </div>

          <div class="flex items-center justify-between gap-3">
            <button type="button" class="btn-hard w-[45%]" :disabled="restoreRunning" @click="closeRestore">取消</button>
            <button
              type="button"
              class="btn-hard w-[45%]"
              :class="canRestore ? 'btn-hard--danger !bg-danger !text-white' : '!cursor-not-allowed !border-gray-400 !bg-gray-300 !text-gray-500 !opacity-60 !shadow-none'"
              :disabled="!canRestore || restoreRunning"
              @click="confirmRestore"
            >
              确认恢复
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 系统提示 Toast -->
    <div v-if="notice" role="status" class="pointer-events-none fixed inset-x-0 z-[60] flex justify-center px-4" style="bottom: calc(1.5rem + env(safe-area-inset-bottom));">
      <span class="motion-fade max-w-full truncate rounded-full border-2 border-ink bg-ink px-4 py-2.5 font-mono text-[11px] font-black text-white shadow-hard">
        {{ notice }}
      </span>
    </div>
  </main>
</template>

<!-- 验收: 真端点list/create/verify/restore(admin only); RESTORE文本门禁; 校验/错误Toast; 恢复成功强制回登录(CC-17); Worker三卡接GET /workers/status真数据、失败降级--; 非admin守卫404 -->
