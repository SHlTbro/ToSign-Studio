/**
 * G58B-08：P10_Audit 接新 audit 查询端点（admin only、分页、URL query 映射）；
 * G58B-09：P11_Backup 接 backups 真端点（list/create/verify/restore，admin only）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import P10_Audit from '@/pages/P10_Audit.vue'
import P11_Backup from '@/pages/P11_Backup.vue'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'
import { useAuthStore } from '@/stores/auth'

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

const LOG_ROWS = {
  data: {
    items: [
      { log_id: 5, actor: '1', timestamp: '2026-09-21T01:00:00+00:00', action: 'asset_approve', object_ref: 'ast-1', result: 'ok', error_code: null },
      { log_id: 4, actor: '2', timestamp: '2026-09-20T01:00:00+00:00', action: 'register', object_ref: '2', result: 'ok', error_code: null },
    ],
  },
  meta: { api_version: 'v1', schema_version: '0.4', total: 22, page: 1, page_size: 20 },
  warnings: [],
}

describe('P10_Audit 接 audit-logs 真端点（G58B-08）', () => {
  function auditPlan(extra: FetchPlan = {}): FetchPlan {
    return {
      '/api/v1/audit-logs': { status: 200, json: LOG_ROWS },
      ...extra,
    }
  }

  it('渲染服务端返回的审计行并展示分页总数', async () => {
    const { wrapper } = mountPage(P10_Audit, { asAdmin: true, plan: auditPlan() })
    await flushPromises()
    expect(wrapper.text()).toContain('asset_approve')
    expect(wrapper.text()).toContain('→ ast-1')
    expect(wrapper.text()).toContain('22 rows')
    expect(wrapper.text()).toContain('1 / 2')
  })

  it('翻页把 page 序列化为 URL query 重新查询（服务端分页）', async () => {
    const { wrapper, logs } = mountPage(P10_Audit, { asAdmin: true, plan: auditPlan() })
    await flushPromises()
    const next = wrapper.findAll('button').find((b) => b.text() === '下一页')!
    await next.trigger('click')
    await flushPromises()
    expect(logs.some((l) => l.url.includes('page=2'))).toBe(true)
  })

  it('查询失败显示错误态与重试', async () => {
    const { wrapper } = mountPage(P10_Audit, {
      asAdmin: true,
      plan: { '/api/v1/audit-logs': problem(403, 'FORBIDDEN', 'admin role required') },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('审计日志加载失败')
  })
})

describe('P11_Backup 接 backups 真端点（G58B-09）', () => {
  const backups = {
    items: [
      { backup_id: 'bk-1', status: 'succeeded', created_at: '2026-09-21T03:00:00Z', size_bytes: 1024 * 1024 * 128, includes: ['assets'], checksum: 'd3f1a7c2e9b44012abcd', path_relative: '_backups/bk-1' },
    ],
  }

  function backupPlan(extra: FetchPlan = {}): FetchPlan {
    return {
      '/api/v1/backups': envelope(backups),
      '/api/v1/auth/logout': envelope({ status: 'ok' }),
      ...extra,
    }
  }

  it('渲染真备份列表（大小/状态/checksum）', async () => {
    const { wrapper } = mountPage(P11_Backup, { asAdmin: true, plan: backupPlan() })
    await flushPromises()
    expect(wrapper.text()).toContain('bk-1')
    expect(wrapper.text()).toContain('已完成')
    expect(wrapper.text()).toContain('128.0 MB')
    expect(wrapper.text()).toContain('d3f1a7c2e9b44012')
  })

  it('创建备份调用 POST /backups 并刷新列表', async () => {
    const { wrapper, logs } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: backupPlan({
        '/api/v1/backups': envelope(backups),
      }),
    })
    await flushPromises()
    logs.length = 0
    const btn = wrapper.findAll('button').find((b) => b.text().includes('立即创建全量冷备份'))!
    await btn.trigger('click')
    await flushPromises()
    expect(logs.some((l) => l.method === 'POST' && l.url.endsWith('/backups'))).toBe(true)
  })

  it('POST 仅在 succeeded 时提示完成；queued 会轮询详情并同步列表状态', async () => {
    const created = { ...backups.items[0], backup_id: 'bk-2', status: 'queued' }
    const details = { status: 200, json: envelope({ ...created, status: 'running' }).json }
    const plan = backupPlan({
      '/api/v1/backups': envelope(backups),
      '/api/v1/backups/bk-2': details,
    })
    const { wrapper, logs } = mountPage(P11_Backup, { asAdmin: true, plan })
    await flushPromises()
    logs.length = 0
    plan['/api/v1/backups'] = envelope({ ...created, items: [created, ...backups.items] })

    await wrapper.findAll('button').find((b) => b.text().includes('立即创建全量冷备份'))!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('全量冷备份已完成')
    expect(wrapper.findAll('button').find((b) => b.text().includes('正在创建全量冷备份'))?.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('bk-2 · 128.0 MB · 排队中')

    await new Promise((resolve) => setTimeout(resolve, 1050))
    await flushPromises()
    expect(logs.some((l) => l.method === 'GET' && l.url.endsWith('/api/v1/backups/bk-2'))).toBe(true)
    expect(wrapper.text()).toContain('bk-2 · 128.0 MB · 进行中')

    plan['/api/v1/backups/bk-2'] = { status: 200, json: envelope({ ...created, status: 'succeeded' }).json }
    await new Promise((resolve) => setTimeout(resolve, 1050))
    await flushPromises()
    expect(wrapper.text()).toContain('bk-2 · 128.0 MB · 已完成')
    expect(wrapper.text()).toContain('全量冷备份已完成')
    const pollsAtSuccess = logs.filter((l) => l.url.endsWith('/api/v1/backups/bk-2')).length
    await new Promise((resolve) => setTimeout(resolve, 1200))
    await flushPromises()
    expect(logs.filter((l) => l.url.endsWith('/api/v1/backups/bk-2'))).toHaveLength(pollsAtSuccess)
  })

  it('创建响应 failed 时显示失败并保持列表状态', async () => {
    const failed = { ...backups.items[0], backup_id: 'bk-failed', status: 'failed' }
    const plan = backupPlan()
    const { wrapper } = mountPage(P11_Backup, {
      asAdmin: true,
      plan,
    })
    await flushPromises()
    plan['/api/v1/backups'] = envelope({ ...failed, items: [failed] })
    await wrapper.findAll('button').find((b) => b.text().includes('立即创建全量冷备份'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('备份失败')
    expect(wrapper.text()).not.toContain('全量冷备份已完成')
  })

  it('卸载页面后停止 queued 备份的后续详情轮询', async () => {
    const queued = { ...backups.items[0], backup_id: 'bk-unmount', status: 'queued' }
    const plan = backupPlan()
    const { wrapper, logs } = mountPage(P11_Backup, { asAdmin: true, plan })
    await flushPromises()
    logs.length = 0
    plan['/api/v1/backups'] = envelope({ ...queued, items: [queued] })

    await wrapper.findAll('button').find((b) => b.text().includes('立即创建全量冷备份'))!.trigger('click')
    await flushPromises()
    wrapper.unmount()
    await new Promise((resolve) => setTimeout(resolve, 1100))

    expect(logs.some((l) => l.url.endsWith('/api/v1/backups/bk-unmount'))).toBe(false)
  })

  it('只有 succeeded 快照可打开恢复确认并解锁恢复', async () => {
    const queued = { ...backups.items[0], backup_id: 'bk-queued', status: 'queued' }
    const { wrapper } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: backupPlan({ '/api/v1/backups': envelope({ items: [queued] }) }),
    })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '恢复快照')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.findAll('button').find((b) => b.text() === '恢复快照')?.attributes('disabled')).toBeDefined()
  })

  it('在线恢复确认明确说明不会回滚 SQLite 数据库', async () => {
    const { wrapper } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: backupPlan(),
    })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '恢复快照')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('不会回滚 SQLite 数据库')
  })

  it('RESTORE 门禁未解锁时确认按钮禁用；解锁后调用真 restore 并登出', async () => {
    const { wrapper, logs, pinia } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: backupPlan({ '/api/v1/backups/bk-1/restore': envelope({ backup_id: 'bk-1', restored: true }) }),
    })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '恢复快照')!.trigger('click')
    await flushPromises()
    const confirmBtn = wrapper.findAll('button').find((b) => b.text().includes('确认恢复'))!
    expect(confirmBtn.attributes('disabled')).toBeDefined()
    await wrapper.find('#restore-gate').setValue('RESTORE')
    await flushPromises()
    const confirmBtn2 = wrapper.findAll('button').find((b) => b.text().includes('确认恢复'))!
    expect(confirmBtn2.attributes('disabled')).toBeUndefined()
    await confirmBtn2.trigger('click')
    await flushPromises()
    const call = logs.find((l) => l.url.includes('/restore'))
    expect(call).toBeTruthy()
    expect((call!.body as Record<string, unknown>).confirm).toBe(true)
    // CC-17：恢复成功 → 全会话失效 → 登出回登录页
    expect(useAuthStore(pinia).isLoggedIn()).toBe(false)
  })

  it('校验按钮调用 POST /backups/{id}/verify', async () => {
    const { wrapper, logs } = mountPage(P11_Backup, {
      asAdmin: true,
      plan: backupPlan({ '/api/v1/backups/bk-1/verify': envelope({ status: 'checksum_ok', files_checked: 12 }) }),
    })
    await flushPromises()
    await wrapper.findAll('button').find((b) => b.text() === '校验')!.trigger('click')
    await flushPromises()
    expect(logs.some((l) => l.method === 'POST' && l.url.includes('/verify'))).toBe(true)
    expect(wrapper.text()).toContain('校验通过')
  })
})
