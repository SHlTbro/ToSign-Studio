import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P7Training from '@/pages/P7_Training.vue'
import type { M2Runtime, RunReadiness, StyleRun } from '@/types/domain'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const STYLE = { style_id: 1, name: '个人手写', source_type: 'HANDWRITING_ASSET', status: 'approved', enabled: true, coverage: null, model_version: null, receipt_id: null }
const RUNTIME: M2Runtime = {
  status: 'READY', ready: true, sample_ids: [],
  config: { engine: 'm2', style_id: 1, snapshot_dir: 'verified/S03', snapshot_sha256: 'a'.repeat(64),
    recipe_id: 'm2-control-corrected-v1', objective_profile: 'epsilon-offset-control-v1', preprocessing_profile: 'fontdiffuser-freetype128-blackwhite-v1', checkpoint_selection: 'terminal_fixed_budget',
    train: { device: 'cpu', max_epochs: 8, batch: 2, max_steps: 80, max_seconds: 900 },
    training_recipe: { schema: 'm2-training-recipe/1', sha256: 'd'.repeat(64), recipe_id: 'm2-control-corrected-v1', objective_profile: 'epsilon-offset-control-v1', preprocessing_profile: 'fontdiffuser-freetype128-blackwhite-v1', checkpoint_selection: 'terminal_fixed_budget', fixed_train: { max_epochs: 8, batch: 2 } },
    training_budget: { schema: 'm2-control-budget/1', verified_train_count: 20, batch: 2, drop_last: true, steps_per_epoch: 10, expected_complete_epochs: 8, expected_optimizer_steps: 80, max_seconds: 900, terminal_validation_required: true } },
  blockers: [], capabilities: { create_m2_run: true, run_evaluation: true, ai_visual_review: true, ai_candidate_confirmation: true, offline_ai_review_import: true },
  acceptance_policy: { mode: 'PERSONAL_NONCOMMERCIAL', human_blind_review: 'WAIVED_BY_USER', candidate_confirmation: 'AI_AUTHORIZED' },
}
const RUN: StyleRun = { run_id: 8, status: 'succeeded', sample_ids: [], config: { engine: 'm2', style_id: 1 }, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' }
const READINESS: RunReadiness = { run_id: 8, status: 'BLOCKED', AI_VISUAL_REVIEW: 'PENDING', AI_BLIND_REVIEW: 'PENDING', HUMAN_BLIND_REVIEW: 'WAIVED_BY_USER', OWNER_BLIND_REVIEW: 'WAIVED_BY_USER', blockers: { evaluation: 'BLOCKED', ai_visual: 'BLOCKED' }, allowed_actions: { evaluate: true, ai_review: false, publish: false }, evaluation_state: 'not_started', publication: null }
const PROGRESS = { run_id: 8, status: 'succeeded', stage: 'trained', engine_mode: 'M2_ADAPTER', epoch: 2, total_epochs: 2, loss: 0.21, eta_s: null, percent: null, samples_seen: 4, units_done: 4, units_total: 4, unit: 'steps', log_tail: [], updated_at: '2026-10-01T00:00:00Z' }
const CANDIDATE = { sample_id: 'c1', style_id: 1, text_label: '春', source_asset_id: 'a1', source_training_image_id: 7, source_family_id: 'group1', style_sample_id: 9, review_status: 'candidate', provenance_complete: true, final_alpha_sha256: 'a'.repeat(64), candidate_visual_review: { review_state: 'AI_REVIEW_COMPLETE', recommendation: 'accept' } }
function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/styles': envelope({ items: [STYLE] }),
    '/api/v1/styles/1/m2-runtime': envelope(RUNTIME),
    '/api/v1/styles/1/samples': envelope({ items: [] }),
    '/api/v1/training-sets': envelope({ items: [], total: 0, page: 1, page_size: 24 }),
    '/api/v1/training-samples': envelope({ items: [], total: 0 }),
    '/api/v1/training-samples/snapshot-preflight': envelope({ schema: 'personal-snapshot-preflight/1', target_user_id: 2, target_style_id: 1, base_intake_sha256: 'a'.repeat(64), eligible_parent_sources: [], explicit_family_split_required: false, family_components: [], blockers: [], status: 'PREFLIGHT_READY', structural_dataset_ready: true, snapshot_ready: false, snapshot_created: false, authorization_created: false, selection_requires_expected_base_intake_sha256: true }),
    '/api/v1/style-runs': envelope({ ...RUN, status: 'created' }),
    '/api/v1/style-runs/8': envelope(RUN),
    '/api/v1/style-runs/8/readiness': envelope(READINESS),
    '/api/v1/style-runs/8/progress': envelope(PROGRESS),
    '/api/v1/style-runs/8/metrics': envelope({ run_id: 8, state: 'available', reason: null, metrics: { samples_seen: 4, loss: 0.21, engine_mode: 'M2_ADAPTER' }, receipt: { sha256: 'b'.repeat(64) } }),
    '/api/v1/style-runs/8/samples': envelope({ items: [] }, { total: 0 }),
    '/api/v1/style-runs/8/evaluate': envelope({ run_id: 8, state: 'unavailable', reason: 'EVAL_NOT_PRODUCED', report: null, receipt: null }),
    ...extra,
  }
}
const wrappers: VueWrapper[] = []
function mount(plan: FetchPlan) { const result = mountPage(P7Training, { plan }); wrappers.push(result.wrapper); return result }
function button(wrapper: VueWrapper, text: string) { const value = wrapper.findAll('button').find((node) => node.text().includes(text)); if (!value) throw new Error(`Missing button: ${text}`); return value }
function jsonResponse(data: unknown) { return { ok: true, status: 200, json: async () => envelope(data).json } as Response }
function resume() { localStorage.setItem('yy:p7:last-style-run-id', '8') }
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { for (const wrapper of wrappers.splice(0)) wrapper.unmount(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('P7 verified M2 product workflow', () => {
  it('locks deployment POST and reports absent ready registration without fabricating readiness', async () => {
    resume(); const path='/api/v1/style-runs/8/inference-deployment'; const selected='d'.repeat(32)
    const ctx=mount(plan({ [path]:envelope({ run_id:8,state:'not_registered',supports_preserved_training:true,available:[{ registration_id:selected,variant_id:'GPU source fixture',device:'cuda',ready:true }] }) }))
    await flushPromises();await ctx.wrapper.get('[aria-label="推理执行资格"]').setValue(selected)
    const ordinaryFetch=globalThis.fetch;let finish!: (response:Response)=>void;let posts=0
    vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL,init?:RequestInit)=> {
      if (String(input)===path && init?.method==='POST') { posts+=1;return new Promise<Response>(resolve=>{finish=resolve}) }
      return ordinaryFetch(input,init)
    }))
    const submit=button(ctx.wrapper,'登记推理兼容部署')
    await submit.trigger('click');await submit.trigger('click')
    expect(posts).toBe(1);expect(submit.attributes('disabled')).toBeDefined();expect(ctx.wrapper.get('[aria-label="推理执行资格"]').attributes('disabled')).toBeDefined()
    finish(jsonResponse({ run_id:8,quality_approved:false,published:false }));await flushPromises()
    ctx.wrapper.unmount();localStorage.clear();resume()
    const noReady=mount(plan({ [path]:envelope({ run_id:8,state:'blocked',supports_preserved_training:true,reason:'QUALIFICATION_REQUIRED',available:[] }) }))
    await flushPromises();expect(noReady.wrapper.get('[data-testid="inference-deployment"]').text()).toContain('需要维护者完成正式资格验证')
    expect(button(noReady.wrapper,'登记推理兼容部署').attributes('disabled')).toBeDefined()
  })
  it('keeps new intake preparation warnings in input steps rather than blocking a completed run effect review', async () => {
    resume(); const ctx = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...RUNTIME, ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'NEW_INTAKE_PENDING', detail: 'New input materials need preparation' }] }) }))
    await flushPromises()
    expect(ctx.wrapper.find('[data-testid="training-blockers"]').exists() ? ctx.wrapper.find('[data-testid="training-blockers"]').text() : '').not.toContain('NEW_INTAKE_PENDING')
    await ctx.wrapper.get('[data-testid="choose-step-2"]').trigger('click')
    expect(ctx.wrapper.find('[data-testid="training-blockers"]').exists()).toBe(false)
    expect(ctx.wrapper.get('[data-testid="new-training-notices"]').text()).toContain('新训练准备提醒')
  })
  it('exposes server-supported preserved-model deployment, submits only its registration and recovers unknown outcome with GET', async () => {
    resume()
    const path = '/api/v1/style-runs/8/inference-deployment'
    const selected = 'd'.repeat(32)
    const ctx = mount(plan({ [path]: envelope({ state: 'not_registered', supports_preserved_training: true, run_id: 8,
      training_runtime_binding: { registration_id: 'original-gpu2' }, available: [{ registration_id: selected, variant_id: 'qualified-new-source', device: 'cuda', ready: true }] }) }))
    await flushPromises()
    expect(ctx.wrapper.get('[data-testid="inference-deployment"]').text()).toContain('推理兼容部署（保留原训练身份）')
    await ctx.wrapper.get('[aria-label="推理执行资格"]').setValue(selected)
    const ordinaryFetch = globalThis.fetch
    const calls: { method: string; body?: unknown }[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === path) {
        calls.push({ method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : undefined })
        if (init?.method === 'POST') return Promise.reject(new TypeError('Unknown result'))
        return Promise.resolve(jsonResponse({ state: 'registered', supports_preserved_training: true, run_id: 8,
          training_runtime_binding: { registration_id: 'original-gpu2' }, runtime_binding: { registration_id: selected }, deployment_binding: { model_sha256: 'b'.repeat(64) }, available: [] }))
      }
      return ordinaryFetch(input,init)
    }))
    await button(ctx.wrapper,'登记推理兼容部署').trigger('click'); await flushPromises()
    expect(calls.filter(call => call.method === 'POST')).toEqual([{ method: 'POST', body: { runtime_registration_id: selected } }])
    expect(calls.filter(call => call.method === 'GET')).toHaveLength(1)
    expect(ctx.wrapper.get('[data-testid="inference-deployment"]').text()).toContain('已登记（未表示质量通过或发布）')
    expect(ctx.wrapper.text()).toContain('original-gpu2')
    expect(ctx.logs.some(call => call.method === 'POST' && /prepare|\/start$|style-runs$/.test(call.url))).toBe(false)
  })
  it('does not show a preserved-model deployment action for ordinary unsupported runs', async () => {
    resume(); const ctx = mount(plan({ '/api/v1/style-runs/8/inference-deployment': envelope({ state: 'not_required', supports_preserved_training: false, reason: 'NOT_REQUIRED', available: [] }) }))
    await flushPromises(); expect(ctx.wrapper.find('[data-testid="inference-deployment"]').exists()).toBe(false)
  })
  it('guides preparation before training and keeps blocked reasons visible across steps', async () => {
    const { wrapper } = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...RUNTIME, ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'M2_RUNTIME_UNAVAILABLE', detail: 'ML runtime missing' }] }) })); await flushPromises()
    expect(wrapper.get('[aria-label="字体训练步骤"]').findAll('button')).toHaveLength(4)
    expect(wrapper.get('[data-testid="training-step-1"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="training-blockers"]').isVisible()).toBe(true)
    await wrapper.get('[data-testid="choose-step-3"]').trigger('click')
    expect(wrapper.get('[data-testid="training-step-3"]').isVisible()).toBe(true)
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="training-blockers"]').text()).toContain('训练环境暂不可用')
  })
  it('returns a recovered successful run to effect review without representing AI as the owner', async () => {
    resume(); const { wrapper } = mount(plan()); await flushPromises()
    expect(wrapper.get('[data-testid="choose-step-4"]').attributes('aria-current')).toBe('step')
    expect(wrapper.get('[data-testid="training-next-action"]').text()).toContain('查看效果')
    expect(wrapper.text()).toContain('AI 结果与本人验收分开')
  })
  it('uses server-discovered config and allows verified imported S03 without mutable sample IDs', async () => {
    const { wrapper, logs } = mount(plan()); await flushPromises()
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.text()).toContain('已就绪 · 可以创建 M2 运行')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
    await button(wrapper, '创建新运行').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST' && entry.url === '/api/v1/style-runs')?.body).toEqual({ sample_ids: [], config: RUNTIME.config })
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('8')
  })
  it('fails closed for missing runtime and shows structured blockers without manual hashes', async () => {
    const { wrapper, logs } = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...RUNTIME, ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'M2_RUNTIME_UNAVAILABLE', detail: 'ML runtime missing' }] }) })); await flushPromises()
    expect(wrapper.text()).toContain('训练环境暂不可用，请检查运行环境。')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })
  it('shows missing snapshot metadata without blocking an already verified imported S03', async () => {
    const { wrapper } = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...RUNTIME, snapshot_preparation_dependencies: { status: 'BLOCKED_DEPENDENCY', missing_metadata: [{ path: 'metadata.json', expected_sha256: 'f'.repeat(64) }], remediation: 'Restore exact backup' } }) })); await flushPromises()
    expect(wrapper.get('[data-testid="snapshot-dependency-block"]').text()).toContain('metadata.json')
    expect(wrapper.text()).toContain('历史快照仍可用于训练')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })
  it('does not treat unknown runtime capability as ready', async () => {
    const { wrapper } = mount(plan({ '/api/v1/styles/1/m2-runtime': problem(404, 'NOT_FOUND', 'capability absent') })); await flushPromises()
    expect(wrapper.text()).toContain('训练环境缺少所需能力')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
  })
  it('submits AI confirmation through a distinct endpoint with IDs only', async () => {
    const { wrapper, logs } = mount(plan({
      '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=candidate': envelope({ items: [CANDIDATE], total: 1 }),
      '/api/v1/training-samples/ai-confirm': envelope({ confirmed: 1, isolated: 0, items: [{ sample_id: 'c1', review_status: 'ai_confirmed_authorized' }] }),
    })); await flushPromises()
    await wrapper.get('input[aria-label="选择候选 c1"]').setValue(true)
    await button(wrapper, 'AI 复核并确认').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST' && entry.url.endsWith('/ai-confirm'))?.body).toEqual({ style_id: 1, sample_ids: ['c1'] })
    expect(logs.some((entry) => entry.method === 'POST' && entry.url.endsWith('/batch-review'))).toBe(false)
    expect(wrapper.text()).toContain('未记为本人确认')
    expect(wrapper.text()).not.toContain('本人复核完成')
  })
  it('blocks a second create click while first is unresolved', async () => {
    const { wrapper } = mount(plan()); await flushPromises()
    const fallback = globalThis.fetch
    let resolveCreate!: (response: Response) => void
    const fetchSpy = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/style-runs' && init?.method === 'POST') return new Promise<Response>((resolve) => { resolveCreate = resolve })
      return fallback(input, init)
    })
    vi.stubGlobal('fetch', fetchSpy)
    const create = button(wrapper, '创建新运行')
    await create.trigger('click'); await create.trigger('click')
    expect(fetchSpy.mock.calls.filter(([url, init]) => String(url) === '/api/v1/style-runs' && init?.method === 'POST')).toHaveLength(1)
    expect(localStorage.getItem('yy:p7:pending-style-run-create')).not.toBeNull()
    resolveCreate(jsonResponse({ ...RUN, status: 'created' })); await flushPromises()
    expect(localStorage.getItem('yy:p7:pending-style-run-create')).toBeNull()
  })
  it('recovers an interrupted create with exact persisted body and idempotency key', async () => {
    const pending = { key: 'stable-create-key', body: { sample_ids: [33], config: { engine: 'm2', style_id: 1, snapshot_sha256: 'c'.repeat(64) } } }
    localStorage.setItem('yy:p7:pending-style-run-create', JSON.stringify(pending))
    const { wrapper } = mount(plan()); await flushPromises()
    const fallback = globalThis.fetch; const fetchSpy = vi.fn(fallback); vi.stubGlobal('fetch', fetchSpy)
    await button(wrapper, '恢复未确认的创建').trigger('click'); await flushPromises()
    const call = fetchSpy.mock.calls.find(([url, init]) => String(url) === '/api/v1/style-runs' && init?.method === 'POST')!
    expect(JSON.parse(String(call[1]?.body))).toEqual(pending.body)
    expect((call[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(pending.key)
  })
  it('keeps real progress visible if readiness fails, with publish disabled', async () => {
    resume()
    const { wrapper } = mount(plan({ '/api/v1/style-runs/8/readiness': problem(409, 'ARTIFACT_INVALID', 'receipt mismatch') })); await flushPromises()
    expect(wrapper.text()).toContain('0.210')
    expect(wrapper.text()).toContain('模型文件校验未通过，请联系维护人员核对。')
    expect(button(wrapper, '请求发布').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('尚未生成效果评估')
    expect(wrapper.text()).not.toContain('100%')
  })
  it('runs evaluation via POST and resumes polling on terminal training while evaluation runs', async () => {
    resume(); const routes = plan(); const { wrapper } = mount(routes); await flushPromises()
    const fallback = globalThis.fetch; let postCount = 0
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/8/evaluate') && init?.method === 'POST') { postCount++; routes['/api/v1/style-runs/8/readiness'] = envelope({ ...READINESS, evaluation_state: 'running', allowed_actions: { ...READINESS.allowed_actions, evaluate: false } }); return Promise.resolve(jsonResponse({ run_id: 8, state: 'running' })) }
      return fallback(input, init)
    })
    await button(wrapper, '运行评估').trigger('click'); await flushPromises()
    expect(postCount).toBe(1)
    expect(wrapper.text()).toContain('真实评估运行中')
    const spy = vi.fn(globalThis.fetch); vi.stubGlobal('fetch', spy)
    await vi.advanceTimersByTimeAsync(2200); await flushPromises()
    expect(spy.mock.calls.some(([url]) => String(url).endsWith('/8/readiness'))).toBe(true)
  })
  it('shows AI provider failure truthfully and never submits human scores', async () => {
    resume(); const routes = plan({ '/api/v1/style-runs/8/readiness': envelope({ ...READINESS, allowed_actions: { evaluate: false, ai_review: true, publish: false } }), '/api/v1/style-runs/8/ai-review': problem(409, 'AI_PROVIDER_UNAVAILABLE', 'No actual visual provider') })
    const { wrapper, logs } = mount(routes); await flushPromises()
    await button(wrapper, '运行 AI 视觉盲评').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('自动视觉审核服务尚未就绪')
    expect(logs.some((entry) => entry.method === 'POST' && entry.url.endsWith('/blind-review'))).toBe(false)
    expect(button(wrapper, '请求发布').attributes('disabled')).toBeDefined()
  })
  it('restores persisted publication and displays waiver without claiming human PASS', async () => {
    resume()
    const { wrapper, logs } = mount(plan({ '/api/v1/style-runs/8/readiness': envelope({ ...READINESS, status: 'PUBLISH_READY_AI_AUTHORIZED', AI_VISUAL_REVIEW: 'PASS', AI_BLIND_REVIEW: 'PASS', blockers: {}, allowed_actions: { evaluate: false, ai_review: false, publish: true }, publication: { ...STYLE, style_id: 4, status: 'pending_review', quality_status: 'AI_VERIFIED' } }) })); await flushPromises()
    expect(wrapper.text()).toContain('用户明确豁免人工盲评；不表示真人评分通过')
    expect(wrapper.text()).toContain('style #4')
    expect(button(wrapper, '已发布申请').attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })
  it('clears a missing saved run and allows a new verified run', async () => {
    resume(); const { wrapper } = mount(plan({ '/api/v1/style-runs/8': problem(404, 'RUN_NOT_FOUND', 'missing run') })); await flushPromises()
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
    expect(wrapper.text()).toContain('找不到此训练记录，或当前账号无权访问。')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })
  it('surfaces snapshot mismatch from create and does not invent a run', async () => {
    const { wrapper } = mount(plan({ '/api/v1/style-runs': problem(409, 'SNAPSHOT_MISMATCH', 'S03 was changed') })); await flushPromises()
    await button(wrapper, '创建新运行').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('训练材料与已冻结记录不一致，请重新核对材料。')
    expect(wrapper.text()).toContain('不要修改历史收据或手填校验值')
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
  })
  it('disables a formerly ready publish gate after a failed readiness refresh', async () => {
    resume(); const routes = plan({ '/api/v1/style-runs/8/readiness': envelope({ ...READINESS, blockers: {}, allowed_actions: { evaluate: false, ai_review: false, publish: true } }) })
    const { wrapper } = mount(routes); await flushPromises()
    expect(button(wrapper, '请求发布').attributes('disabled')).toBeUndefined()
    routes['/api/v1/style-runs/8/readiness'] = problem(409, 'ARTIFACT_INVALID', 'file changed after earlier check')
    await button(wrapper, '刷新/恢复').trigger('click'); await flushPromises()
    expect(button(wrapper, '请求发布').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('模型文件校验未通过')
  })
  it('handles an older readiness response without allowed_actions by staying blocked', async () => {
    resume(); const { wrapper } = mount(plan({ '/api/v1/style-runs/8/readiness': envelope({ status: 'BLOCKED', blockers: { ai_visual: 'BLOCKED' }, AI_VISUAL_REVIEW: 'BLOCKED', OWNER_BLIND_REVIEW: 'PENDING' }) })); await flushPromises()
    expect(button(wrapper, '请求发布').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '运行评估').attributes('disabled')).toBeDefined()
  })
  it('polls interrupted running training after reload and stops after navigation away', async () => {
    resume(); const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope({ ...RUN, status: 'running' }) })); await flushPromises()
    const spy = vi.fn(globalThis.fetch); vi.stubGlobal('fetch', spy)
    await vi.advanceTimersByTimeAsync(2200); await flushPromises()
    expect(spy.mock.calls.some(([url]) => String(url) === '/api/v1/style-runs/8')).toBe(true)
    wrapper.unmount(); wrappers.splice(wrappers.indexOf(wrapper), 1); spy.mockClear()
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(spy).not.toHaveBeenCalled()
  })
  it('does not merge AI-confirmed characters into the human-confirmed count', async () => {
    const { wrapper } = mount(plan({ '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=ai_confirmed_authorized': envelope({ items: [{ ...CANDIDATE, review_status: 'ai_confirmed_authorized', owner_confirmed_by: null, owner_confirmed_at: null }], total: 1 }) })); await flushPromises()
    expect(wrapper.text()).toContain('本人确认 0 · AI 授权确认 1')
    expect(wrapper.text()).not.toContain('本人确认 1')
  })
  it('does not reuse a delayed runtime response after another style is selected', async () => {
    const routes = plan({ '/api/v1/styles': envelope({ items: [STYLE, { ...STYLE, style_id: 2, name: '第二风格' }] }), '/api/v1/styles/2/samples': envelope({ items: [] }), '/api/v1/styles/2/m2-runtime': envelope({ ...RUNTIME, ready: false, status: 'BLOCKED', config: null, blockers: [{ code: 'SNAPSHOT_MISMATCH', detail: 'second style blocked' }] }) })
    const { wrapper } = mount(routes); await flushPromises()
    const fallback = globalThis.fetch; let resolveRuntime!: (response: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/styles/1/m2-runtime') return new Promise<Response>((resolve) => { resolveRuntime = resolve })
      return fallback(input, init)
    })
    await button(wrapper, '重新检查').trigger('click')
    await wrapper.get('select[aria-label="风格档案"]').setValue('2'); await flushPromises()
    resolveRuntime(jsonResponse(RUNTIME)); await flushPromises()
    expect(wrapper.text()).toContain('训练材料与已冻结记录不一致')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
  })

  it('blocks starting a restored M2 run when its runtime is unavailable', async () => {
    resume(); const { wrapper, logs } = mount(plan({ '/api/v1/style-runs/8': envelope({ ...RUN, status: 'created' }), '/api/v1/styles/1/m2-runtime': envelope({ ...RUNTIME, ready: false, status: 'BLOCKED', config: null }) })); await flushPromises()
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })

  it('prepares S03 in product UI and refreshes verified runtime without sending paths or hashes', async () => {
    const routes = plan({ '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=ai_confirmed_authorized': envelope({ items: [{ ...CANDIDATE, review_status: 'ai_confirmed_authorized' }], total: 1 }), '/api/v1/training-samples/prepare-snapshot': envelope({ status: 'READY', target_user_id: 2, target_style_id: 1, base_intake_sha256: 'a'.repeat(64), snapshot_ready: true, snapshot_created: true, snapshot_reused: false, blockers: [], dataset_snapshot_sha256: 'd'.repeat(64) }) })
    const { wrapper, logs } = mount(routes); await flushPromises()
    const readsBefore = logs.filter((entry) => entry.url.endsWith('/m2-runtime')).length
    await button(wrapper, '准备训练快照').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST' && entry.url.endsWith('/prepare-snapshot'))?.body).toEqual({ style_id: 1 })
    expect(logs.filter((entry) => entry.url.endsWith('/m2-runtime')).length).toBeGreaterThan(readsBefore)
    expect(wrapper.text()).toContain('S03 已冻结并通过校验')
  })
  it('shows failed snapshot gates and never claims a frozen snapshot from blocked response', async () => {
    const { wrapper } = mount(plan({ '/api/v1/training-samples?style_id=1&page=1&page_size=100&review_status=ai_confirmed_authorized': envelope({ items: [{ ...CANDIDATE, review_status: 'ai_confirmed_authorized' }], total: 1 }), '/api/v1/training-samples/prepare-snapshot': envelope({ status: 'BLOCKED', snapshot_ready: false, snapshot_created: false, snapshot_reused: false, blockers: ['INSUFFICIENT_CONFIRMED_CHARACTERS'] }) })); await flushPromises()
    await button(wrapper, '准备训练快照').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('尚未满足快照冻结条件')
    expect(wrapper.text()).toContain('已确认的字符数量不足')
    expect(wrapper.text()).not.toContain('S03 已冻结并通过校验')
  })

})
