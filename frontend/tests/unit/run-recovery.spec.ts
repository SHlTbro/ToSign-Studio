import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RunRecovery from '@/components/RunRecovery.vue'
import { ApiRequestError, request } from '@/api/http'
import { parsePendingContinuation, recoveryStorageKey, continueOwnedRun } from '@/api/runRecovery'

vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ me: { user_id: 7 } }) }))
vi.mock('@/api/http', async importOriginal => ({ ...await importOriginal<typeof import('@/api/http')>(),
  request: vi.fn(), newIdempotencyKey: () => 'fixed-ui-test-key' }))
const mocked = vi.mocked(request)
const state = (overrides = {}) => ({ run_id: 10, status: 'cancelled', execution_state: 'ENDED',
  checkpoints: [{ checkpoint_id: 5, verified: true, position: { epoch: 0, global_step: 2, batch_ptr: { epoch: 1, batch_idx: 2 } } }],
  continuation_run_id: null, retry_allowed: true, resume_allowed: false,
  resume_gate: 'FULL_TRAINER_RESUME_QUALIFICATION_REQUIRED', claims_quality_pass: false, ...overrides })
const button = (wrapper: ReturnType<typeof mount>, text: string) => wrapper.findAll('button').find(item => item.text().includes(text))!
beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); mocked.mockReset(); mocked.mockResolvedValue(state()) })

describe('owned continuation journal', () => {
  it('separates owners and parents', () => {
    expect(recoveryStorageKey(7,10)).not.toBe(recoveryStorageKey(8,10))
    expect(recoveryStorageKey(7,10)).not.toBe(recoveryStorageKey(7,11))
  })
  it('rejects caller paths, hashes, ready flags and foreign parent records', () => {
    const good = { version: 1, parent_run_id: 10, mode: 'resume', checkpoint_id: 5, key: 'fixture-ui-key' }
    expect(parsePendingContinuation(JSON.stringify(good),10)).toEqual(good)
    for (const extra of [{ directory: 'E:/caller' }, { sha256: 'a'.repeat(64) }, { ready: true }, { parent_run_id: 11 }]) {
      expect(() => parsePendingContinuation(JSON.stringify({ ...good, ...extra }),10)).toThrow()
    }
  })
  it('sends only checkpoint_id and one explicit idempotency key', async () => {
    await continueOwnedRun({ version: 1, parent_run_id: 10, mode: 'resume', checkpoint_id: 5, key: 'fixture-ui-key' })
    expect(mocked).toHaveBeenCalledWith('/api/v1/style-runs/10/resume',expect.objectContaining({ body: { checkpoint_id: 5 }, idempotencyKey: 'fixture-ui-key', retries: 0 }))
  })
})

describe('RunRecovery finite UI fixtures', () => {
  it('reports incomplete recovery responses and never offers a continuation', async () => {
    mocked.mockResolvedValue({ run_id:10, status:'cancelled' })
    const wrapper=mount(RunRecovery,{props:{runId:10,status:'cancelled'}}); await flushPromises()
    expect(wrapper.text()).toContain('恢复状态响应不完整')
    expect(wrapper.text()).not.toContain('从 checkpoint 继续')
    expect(mocked.mock.calls.every(([,options]) => !options?.method)).toBe(true)
    wrapper.unmount()
  })
  it('keeps resume disabled without independent measured authority', async () => {
    const wrapper=mount(RunRecovery,{ props: { runId:10,status:'cancelled' } });await flushPromises()
    expect(wrapper.text()).toContain('尚无真实全状态恢复资格')
    expect(button(wrapper,'从 checkpoint').attributes('disabled')).toBeDefined()
    expect(button(wrapper,'重新训练').attributes('disabled')).toBeUndefined()
    expect(mocked.mock.calls.every(([, options]) => !options?.method)).toBe(true)
    wrapper.unmount()
  })
  it('records the key before POST, suppresses duplicate click, and never starts the child', async () => {
    let resolve!: (value: unknown) => void
    mocked.mockImplementation(async (_path,options) => options?.method === 'POST' ? new Promise(done => { resolve=done }) : state({ resume_allowed:true,resume_gate:'READY' }))
    const wrapper=mount(RunRecovery,{ props: { runId:10,status:'cancelled' } });await flushPromises()
    await button(wrapper,'从 checkpoint').trigger('click')
    expect(JSON.parse(localStorage.getItem(recoveryStorageKey(7,10))!).key).toBe('fixed-ui-test-key')
    await button(wrapper,'使用原请求').trigger('click')
    expect(mocked.mock.calls.filter(([,options]) => options?.method==='POST')).toHaveLength(1)
    resolve({ run_id:42,status:'created' });await flushPromises()
    expect(wrapper.emitted('continued')).toEqual([[42]])
    expect(localStorage.getItem(recoveryStorageKey(7,10))).toBeNull()
    expect(mocked.mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
    wrapper.unmount()
  })
  it('refresh reads state and explicit replay retains the original checkpoint and key', async () => {
    const pending={ version:1,parent_run_id:10,mode:'resume',checkpoint_id:5,key:'persisted-ui-key' }
    localStorage.setItem(recoveryStorageKey(7,10),JSON.stringify(pending))
    mocked.mockImplementation(async (_path,options) => options?.method==='POST' ? { run_id:42,status:'created' } : state({ resume_allowed:true,resume_gate:'READY' }))
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } });await flushPromises()
    expect(mocked.mock.calls.filter(([,options]) => options?.method==='POST')).toHaveLength(0)
    await button(wrapper,'使用原请求').trigger('click');await flushPromises()
    expect(mocked).toHaveBeenCalledWith('/api/v1/style-runs/10/resume',expect.objectContaining({ body:{ checkpoint_id:5 },idempotencyKey:'persisted-ui-key' }))
    wrapper.unmount()
  })
  it('retains uncertain operations across reload and preserves transport error details', async () => {
    mocked.mockImplementation(async (_path,options) => { if(options?.method==='POST') throw new ApiRequestError(0,'NETWORK_ERROR',true,'injected transport failure');return state() })
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } });await flushPromises()
    await button(wrapper,'重新训练').trigger('click');await flushPromises()
    expect(localStorage.getItem(recoveryStorageKey(7,10))).not.toBeNull()
    expect(wrapper.text()).toContain('网络通信失败')
    expect(wrapper.text()).toContain('上次请求结果尚待确认')
    wrapper.unmount()
  })
  it('shows the server existing continuation instead of creating another attempt', async () => {
    mocked.mockResolvedValue(state({ continuation_run_id:42,retry_allowed:false }))
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } });await flushPromises()
    await button(wrapper,'查看已创建').trigger('click')
    expect(wrapper.emitted('continued')).toEqual([[42]])
    expect(mocked.mock.calls.every(([,options]) => !options?.method)).toBe(true)
    wrapper.unmount()
  })
  it('does not POST when persistence fails', async () => {
    vi.spyOn(Storage.prototype,'setItem').mockImplementation(() => { throw new Error('injected storage denial') })
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } });await flushPromises()
    await button(wrapper,'重新训练').trigger('click');await flushPromises()
    expect(mocked.mock.calls.every(([,options]) => !options?.method)).toBe(true)
    expect(wrapper.text()).toContain('尚未发送提交')
    wrapper.unmount()
  })
  it('clears a definitively rejected operation and keeps the actual gate message', async () => {
    mocked.mockImplementation(async (_path,options) => { if(options?.method==='POST') throw new ApiRequestError(409,'E_RESUME_SCOPE',false,'injected exact scope mismatch');return state({ resume_allowed:true,resume_gate:'READY' }) })
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } });await flushPromises()
    await button(wrapper,'从 checkpoint').trigger('click');await flushPromises()
    expect(localStorage.getItem(recoveryStorageKey(7,10))).toBeNull()
    expect(wrapper.text()).toContain('操作范围与当前任务不一致')
    wrapper.unmount()
  })
  it('ignores stale recovery responses after changing run context', async () => {
    let resolve!: (value: unknown) => void
    mocked.mockImplementation(async path => path.includes('/10/') ? new Promise(done => { resolve=done }) : state({ run_id:11,execution_state:'OWNERSHIP_UNKNOWN',checkpoints:[],retry_allowed:false }))
    const wrapper=mount(RunRecovery,{ props:{ runId:10,status:'cancelled' } })
    await wrapper.setProps({ runId:11 });await flushPromises()
    resolve(state());await flushPromises()
    expect(wrapper.text()).toContain('任务归属尚未确认，请刷新核对。')
    expect(wrapper.text()).not.toContain('下一个 batch')
    expect(button(wrapper,'重新训练').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
