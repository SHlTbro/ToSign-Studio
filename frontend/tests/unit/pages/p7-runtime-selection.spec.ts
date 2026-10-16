import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VueWrapper } from '@vue/test-utils'
import P7 from '@/pages/P7_Training.vue'
import type { M2Runtime, M2RuntimeBinding, M2RuntimeOption, StyleRun } from '@/types/domain'
import { recipeProblem, registrationForConfig, runtimeBinding, runtimePath } from '@/api/m2Runtime'
import { USER_SCR_RECIPE, WINDOWS_SCR_RECIPE } from '@/api/userScr'
import { envelope, flushPromises, mountPage, problem, type FetchPlan } from './_harness'

const GPU = 'a'.repeat(32)
const CPU = 'b'.repeat(32)
const gpuBinding: M2RuntimeBinding = {
  schema: 'm2-runtime-binding/1', registration_id: GPU, variant_id: 'gpu-cuda128-cpu-dcn-v1', device: 'cuda',
  asset_identity_sha256: '1'.repeat(64), dependency_lock_sha256: '2'.repeat(64), execution_profile_sha256: '3'.repeat(64),
  environment_identity_sha256: '4'.repeat(64), capability_receipt_sha256: '5'.repeat(64), binding_sha256: '6'.repeat(64),
}
const cpuBinding: M2RuntimeBinding = { ...gpuBinding, registration_id: CPU, variant_id: 'cpu-v1', device: 'cpu', capability_receipt_sha256: null, binding_sha256: '7'.repeat(64) }
const options: M2RuntimeOption[] = [gpuBinding, cpuBinding].map((binding) => ({ registration_id: binding.registration_id, variant_id: binding.variant_id, device: binding.device, status: 'READY', ready: true, environment_ready: true, full_model_ready: true }))
const styles = [1, 2].map((style_id) => ({ style_id, name: `来源 ${style_id}`, source_type: 'HANDWRITING_ASSET', status: 'approved', enabled: true }))
function config(binding?: M2RuntimeBinding, count = 20, styleId = 1): Record<string, unknown> {
  return {
    engine: 'm2', style_id: styleId, snapshot_dir: 'verified/S03', snapshot_sha256: 'd'.repeat(64),
    recipe_id: 'm2-control-corrected-v1', objective_profile: 'epsilon-offset-control-v1', checkpoint_selection: 'terminal_fixed_budget',
    preprocessing_profile: 'fontdiffuser-freetype128-blackwhite-v1', train: { device: binding?.device ?? 'cpu', max_epochs: 8, batch: 2, max_steps: 8 * Math.floor(count / 2), max_seconds: 900 },
    training_recipe: { schema: 'm2-training-recipe/1', sha256: 'e'.repeat(64), VGG_asset_required: false, recipe_id: 'm2-control-corrected-v1', objective_profile: 'epsilon-offset-control-v1', preprocessing_profile: 'fontdiffuser-freetype128-blackwhite-v1', checkpoint_selection: 'terminal_fixed_budget', fixed_train: { max_epochs: 8, batch: 2 } },
    training_budget: { schema: 'm2-control-budget/1', verified_train_count: count, batch: 2, drop_last: true, max_seconds: 900, steps_per_epoch: Math.floor(count / 2), expected_complete_epochs: 8, expected_optimizer_steps: 8 * Math.floor(count / 2), terminal_validation_required: true },
    ...(binding ? { runtime_binding: binding } : {}),
  }
}
function runtime(binding?: M2RuntimeBinding, count = 20, styleId = 1): M2Runtime {
  return { status: 'READY', ready: true, config: config(binding, count, styleId), selected_runtime_binding: binding ?? null, sample_ids: [31, 32], runtime_options: options,
    blockers: [], capabilities: { create_m2_run: true, run_evaluation: true, ai_visual_review: false, ai_candidate_confirmation: false, offline_ai_review_import: true },
    acceptance_policy: { mode: 'OWNER_REQUIRED', human_blind_review: 'REQUIRED', candidate_confirmation: 'OWNER_REQUIRED' } }
}
function blocked(extra: Partial<M2Runtime> = {}): M2Runtime {
  return { ...runtime(), status: 'BLOCKED', ready: false, config: null, selected_runtime_binding: null,
    blockers: [{ code: 'E_PRODUCT_PROOF_MISSING', detail: 'Hardware passed; product full-model proof missing' }], ...extra }
}
function run(runConfig = config(gpuBinding, 23), status: StyleRun['status'] = 'created'): StyleRun {
  return { run_id: 8, status, sample_ids: [31, 32], config: runConfig, created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z' }
}
function plan(extra: FetchPlan = {}): FetchPlan {
  return {
    '/api/v1/styles': envelope({ items: styles }),
    '/api/v1/styles/1/samples': envelope({ items: [] }), '/api/v1/styles/2/samples': envelope({ items: [] }),
    '/api/v1/styles/1/m2-runtime': envelope(runtime()), '/api/v1/styles/2/m2-runtime': envelope(runtime(undefined, 20, 2)),
    [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(runtime(gpuBinding, 23)),
    [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${CPU}`]: envelope(runtime(cpuBinding)),
    '/api/v1/training-sets': envelope({ items: [], total: 0, page: 1, page_size: 24 }), '/api/v1/training-samples': envelope({ items: [], total: 0 }),
    '/api/v1/style-runs': envelope(run()), '/api/v1/style-runs/8': envelope(run()), '/api/v1/style-runs/8/start': envelope(run(config(gpuBinding, 23), 'running')),
    '/api/v1/style-runs/8/readiness': envelope({ evaluation_state: 'not_started', publication: null, allowed_actions: {}, blockers: {} }),
    '/api/v1/style-runs/8/progress': envelope({ run_id: 8, status: 'created', epoch: null, total_epochs: null, loss: null, eta_s: null, percent: null, units_done: null, units_total: null, log_tail: [] }),
    '/api/v1/style-runs/8/metrics': envelope({ state: 'pending' }), '/api/v1/style-runs/8/evaluate': envelope({ state: 'unavailable' }), '/api/v1/style-runs/8/samples': envelope({ items: [] }),
    ...extra,
  }
}
const wrappers: VueWrapper[] = []
function mount(routes = plan()) { const result = mountPage(P7, { plan: routes }); wrappers.push(result.wrapper); return result }
function select(wrapper: VueWrapper) { return wrapper.get('select[aria-label="训练运行环境"]') }
function button(wrapper: VueWrapper, label: string) { const found = wrapper.findAll('button').find((node) => node.text().includes(label)); if (!found) throw new Error(`Missing button ${label}`); return found }
function response(data: unknown): Response { return { ok: true, status: 200, json: async () => envelope(data).json } as Response }
function resume() { localStorage.setItem('yy:p7:last-style-run-id', '8') }
beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
afterEach(() => { wrappers.splice(0).forEach((wrapper) => { if (wrapper.exists()) wrapper.unmount() }); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.innerHTML = '' })

describe('P7 registered runtime selection and frozen recipe', () => {
  it('keeps signed Windows runtime discovery pending after 60 seconds without retry or CPU fallback', async () => {
    const { wrapper } = mount(); await flushPromises()
    const previous = globalThis.fetch
    const calls: AbortSignal[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).includes(`recipe_id=${WINDOWS_SCR_RECIPE}`)) return previous(input, init)
      const signal = init?.signal as AbortSignal; calls.push(signal)
      return new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => resolve(new Response(JSON.stringify(envelope(blocked()).json), { status: 200 })), 84000)
        signal.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('deadline', 'AbortError')) }, { once: true })
      })
    }))
    await wrapper.get('select[aria-label="M2 训练配方"]').setValue(WINDOWS_SCR_RECIPE); await flushPromises()
    await vi.advanceTimersByTimeAsync(60001); await flushPromises()
    expect(calls).toHaveLength(1)
    expect(calls[0]?.aborted).toBe(false)
    expect(wrapper.text()).not.toContain('请求超时')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(23999); await flushPromises()
  })
  it('labels the Windows recipe as CPU/CUDA while keeping qualification requirements visible', async () => {
    const { wrapper } = mount(); await flushPromises()
    const option = wrapper.get(`select[aria-label="M2 训练配方"] option[value="${WINDOWS_SCR_RECIPE}"]`)
    expect(option.text()).toContain('Windows CPU/CUDA')
    expect(option.text()).toContain('需要实测运行资格')
  })
  function genericConfig(seconds = 3600): Record<string, unknown> {
    return { engine: 'm2', recipe_id: USER_SCR_RECIPE, objective_profile: 'epsilon-offset-scr2n-user-v1', style_id: 1,
      snapshot_sha256: 'd'.repeat(64), preparation_id: 'SYNTHETIC_PREPARATION_ONLY', runtime_registration_id: CPU,
      runtime_binding: cpuBinding, train: { device: 'cpu', max_seconds: seconds } }
  }
  function genericRuntime(seconds = 3600): M2Runtime {
    return { ...runtime(cpuBinding), config: genericConfig(seconds) }
  }
  const genericPath = `/api/v1/styles/1/m2-runtime?recipe_id=${USER_SCR_RECIPE}&runtime_registration_id=${CPU}`
  it('uses the documented generic identifiers without client paths, samples or authorization fields; start and cancel remain explicit', async () => {
    const routes = plan({ [genericPath]: envelope(genericRuntime()), '/api/v1/style-runs/8': envelope(run(genericConfig(7200))),
      '/api/v1/style-runs/8/start': envelope(run(genericConfig(7200), 'running')), '/api/v1/style-runs/8/cancel': envelope(run(genericConfig(7200), 'cancelled')) })
    const { wrapper, logs } = mount(routes); await flushPromises()
    await wrapper.get('select[aria-label="M2 训练配方"]').setValue(USER_SCR_RECIPE); await flushPromises()
    expect(wrapper.text()).toContain('服务端返回了不同配方')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    await select(wrapper).setValue(CPU); await flushPromises()
    await wrapper.get('input[aria-label="训练时间预算（秒）"]').setValue(7200)
    await button(wrapper, '创建新运行').trigger('click'); await flushPromises()
    expect(logs.find(row => row.method === 'POST' && row.url === '/api/v1/style-runs')?.body).toEqual({ sample_ids: [], config: {
      engine: 'm2', recipe_id: USER_SCR_RECIPE, style_id: 1, snapshot_sha256: 'd'.repeat(64), preparation_id: 'SYNTHETIC_PREPARATION_ONLY', runtime_registration_id: CPU, train: { max_seconds: 7200 } } })
    expect(logs.some(row => row.url.endsWith('/start'))).toBe(false)
    routes['/api/v1/style-runs/8'] = envelope(run(genericConfig(7200), 'running'))
    await button(wrapper, '启动真实运行').trigger('click'); await flushPromises()
    routes['/api/v1/style-runs/8'] = envelope(run(genericConfig(7200), 'cancelled'))
    await button(wrapper, '取消运行').trigger('click'); await flushPromises()
    expect(logs.filter(row => row.method === 'POST' && row.url.endsWith('/start'))).toHaveLength(1)
    expect(logs.filter(row => row.method === 'POST' && row.url.endsWith('/cancel'))).toHaveLength(1)
  })
  it('saves a generic draft budget through PUT, rereads the actual record and never starts automatically', async () => {
    resume(); const routes = plan({ [genericPath]: envelope(genericRuntime()), '/api/v1/style-runs/8': envelope(run(genericConfig())) })
    const { wrapper, logs } = mount(routes); await flushPromises()
    await wrapper.get('input[aria-label="草稿时间预算（秒）"]').setValue(9000)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    routes['/api/v1/style-runs/8'] = envelope(run(genericConfig(9000)))
    await button(wrapper, '保存草稿时间预算').trigger('click'); await flushPromises()
    expect(logs.find(row => row.method === 'PUT')?.body).toEqual({ config: { engine: 'm2', recipe_id: USER_SCR_RECIPE, style_id: 1,
      snapshot_sha256: 'd'.repeat(64), preparation_id: 'SYNTHETIC_PREPARATION_ONLY', runtime_registration_id: CPU, train: { max_seconds: 9000 } } })
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('时间预算：9000')
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeUndefined()
    expect(logs.some(row => row.url.endsWith('/start'))).toBe(false)
  })
  it('rejects generic recipe identity or snapshot/registration substitution', () => {
    const original = genericConfig(); expect(recipeProblem(original)).toBeNull()
    for (const altered of [{ ...original, objective_profile: 'epsilon-offset-control-v1' }, { ...original, snapshot_sha256: 'bad' },
      { ...original, runtime_registration_id: GPU }, { ...original, preparation_id: '' }, { ...original, runtime_binding: null }, { ...original, train: { device: 'cuda', max_seconds: 3600 } }]) {
      expect(recipeProblem(altered)).not.toBeNull()
    }
  })
  it('does not claim a saved budget after PUT acknowledgement when the confirming GET fails', async () => {
    resume(); const routes = plan({ [genericPath]: envelope(genericRuntime()), '/api/v1/style-runs/8': envelope(run(genericConfig())) })
    const { wrapper } = mount(routes); await flushPromises()
    await wrapper.get('input[aria-label="草稿时间预算（秒）"]').setValue(9000)
    routes['/api/v1/style-runs/8'] = problem(503, 'UNAVAILABLE', 'Run GET unavailable')
    const previous = globalThis.fetch
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT' ? Promise.resolve(response(run(genericConfig(9000)))) : previous(input, init))
    await button(wrapper, '保存草稿时间预算').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('时间预算：3600')
    expect(wrapper.text()).toContain('操作暂未完成')
    expect(wrapper.text()).not.toContain('已重新读取服务端草稿预算')
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
  })
  it('labels M2 samples_seen as optimizer updates and does not infer death from an old update time', async () => {
    resume(); const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(config(gpuBinding), 'running')),
      '/api/v1/style-runs/8/metrics': envelope({ state: 'available', metrics: { samples_seen: 25, loss: 0.8, engine_mode: 'm2' } }),
      '/api/v1/style-runs/8/progress': envelope({ epoch: 1, total_epochs: 8, units_done: 25, units_total: 200, updated_at: '2026-01-01T00:00:00Z', failure: null }) })); await flushPromises()
    expect(wrapper.text()).toContain('优化更新步（服务端 samples_seen） 25')
    expect(wrapper.text()).not.toContain('样本数 25')
    expect(wrapper.text()).toContain('暂未更新不能据此判断训练进程死亡')
    expect(wrapper.text()).toContain('25 / 200')
  })
  it('handles generic discovery without an acceptance policy without inventing confirmation or waiver', async () => {
    localStorage.setItem('yy:p7:scr-selection:2:1', JSON.stringify({ recipe_id: USER_SCR_RECIPE, runtime_registration_id: CPU, max_seconds: 3600 }))
    const { wrapper } = mount(plan({ [genericPath]: envelope({ ...genericRuntime(), status: 'BLOCKED', ready: false, config: null,
      acceptance_policy: undefined, blockers: [{ code: 'PREPARATION_REQUIRED', detail: 'First preparation required' }] }) })); await flushPromises()
    expect(wrapper.text()).toContain('候选确认：服务端未报告')
    expect(wrapper.text()).not.toContain('用户已明确豁免')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
  })
  it('auto-discovers default CPU and displays the actual eight-epoch dynamic budget without fake progress', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('完整 epoch：8 · 优化器步数：80')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('已验证训练样本：20 · batch：2')
    expect(wrapper.text()).toContain('不是已完成进度，也不代表质量通过')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(logs.every((entry) => entry.method === 'GET')).toBe(true)
  })

  it('scopes the legacy numeric control to M1 and never presents editable M2 hyperparameters after switching or reload', async () => {
    const { wrapper } = mount(); await flushPromises()
    expect(wrapper.find('input[type="number"]').exists()).toBe(false)
    expect(wrapper.find('input[type="range"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('配方由服务端锁定')
    await wrapper.get('select[aria-label="运行引擎"]').setValue('m1'); await flushPromises()
    expect(wrapper.find('input[type="number"]').exists()).toBe(true)
    await wrapper.get('select[aria-label="运行引擎"]').setValue('m2'); await flushPromises()
    expect(wrapper.find('input[type="number"]').exists()).toBe(false)
    wrapper.unmount(); resume()
    const recovered = mount(); await flushPromises()
    expect(recovered.wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('完整 epoch：8')
    expect(recovered.wrapper.find('input[type="number"]').exists()).toBe(false)
    expect(recovered.wrapper.find('input[type="range"]').exists()).toBe(false)
  })

  it('blocks new M2 creation when READY omits the recognized complete recipe or budget', async () => {
    const incomplete = { ...config(), training_budget: undefined }
    const { wrapper } = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...runtime(), config: incomplete }) })); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('M2 配方或动态预算缺失')
  })

  it('does not start a recovered incomplete draft or replace its recipe with the current one', async () => {
    resume(); const legacy = { engine: 'm2', style_id: 1, train: { device: 'cpu', epochs: 2 } }
    const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(legacy)) })); await flushPromises()
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('未记录完整配方或预算')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('完整 epoch：8')
  })

  it('starts the complete frozen budget even when discovery now has a newer snapshot count', async () => {
    resume(); const { wrapper } = mount(plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(runtime(gpuBinding, 20)) })); await flushPromises()
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('优化器步数：88')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('优化器步数：80')
  })

  it('selects a validated GPU registration and submits every server field unchanged', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    await select(wrapper).setValue(GPU); await flushPromises()
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('完整 epoch：8 · 优化器步数：88')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('设备：cuda')
    await button(wrapper, '创建新运行').trigger('click'); await flushPromises()
    expect(logs.find((entry) => entry.method === 'POST' && entry.url === '/api/v1/style-runs')?.body).toEqual({ sample_ids: [31, 32], config: config(gpuBinding, 23) })
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain(GPU)
    expect(select(wrapper).attributes('disabled')).toBeDefined()
    expect(logs.some((entry) => entry.method === 'PATCH')).toBe(false)
  })

  it('supports an explicit CPU registration rather than silently using the legacy default', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    await select(wrapper).setValue(CPU); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
    expect(logs.at(-1)?.url).toBe(`/api/v1/styles/1/m2-runtime?runtime_registration_id=${CPU}`)
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('设备：cpu')
  })

  it('shows environment-ready/full-model-blocked registrations truthfully and prevents create', async () => {
    const blockedOptions = [{ ...options[0]!, ready: false, full_model_ready: false, code: 'E_PRODUCT_PROOF_MISSING', reason: 'Full product proof required' }, options[1]!]
    const { wrapper, logs } = mount(plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(blocked({ runtime_options: blockedOptions })) })); await flushPromises()
    await select(wrapper).setValue(GPU); await flushPromises()
    expect(wrapper.get('[data-testid="runtime-catalog"]').text()).toContain('依赖/硬件：已验证 · 完整模型：未通过')
    expect(wrapper.text()).toContain('E_PRODUCT_PROOF_MISSING')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(logs.filter((entry) => entry.method === 'POST')).toHaveLength(0)
  })

  it('fails closed when ready contradicts the full-model catalog or selected binding', async () => {
    const routes = plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope({ ...runtime(gpuBinding), runtime_options: [{ ...options[0]!, full_model_ready: false }] }) })
    const { wrapper } = mount(routes); await flushPromises(); await select(wrapper).setValue(GPU); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    routes[`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`] = envelope({ ...runtime(gpuBinding), selected_runtime_binding: cpuBinding })
    await button(wrapper, '重新检查').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('服务端配置未绑定所选注册环境')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
  })

  it('blocks a default CPU response that unexpectedly claims a selected registration', async () => {
    const { wrapper } = mount(plan({ '/api/v1/styles/1/m2-runtime': envelope({ ...runtime(), selected_runtime_binding: gpuBinding }) })); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('默认 CPU 配置与服务端运行绑定不一致')
  })

  it('rejects incomplete, malformed or extra-field runtime binding identities', () => {
    expect(runtimeBinding({ runtime_binding: gpuBinding })).toEqual(gpuBinding)
    expect(runtimeBinding({ runtime_binding: cpuBinding })).toEqual(cpuBinding)
    for (const bad of [
      { schema: 'm2-runtime-binding/1', registration_id: GPU, device: 'cuda', binding_sha256: '6'.repeat(64) },
      { ...gpuBinding, binding_sha256: '' }, { ...gpuBinding, asset_identity_sha256: 'not-a-hash' },
      { ...gpuBinding, capability_receipt_sha256: null }, { ...gpuBinding, extra: 'changed contract' },
    ]) {
      expect(runtimeBinding({ runtime_binding: bad })).toBeNull()
      expect(registrationForConfig({ runtime_binding: bad })).toBe('invalid-runtime-binding')
    }
  })

  it('ignores a late runtime response after another explicit selection', async () => {
    const { wrapper } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).includes(`runtime_registration_id=${GPU}`)
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await select(wrapper).setValue(GPU); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    await select(wrapper).setValue(CPU); await flushPromises()
    finish(response(runtime(gpuBinding, 23))); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', CPU)
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('设备：cpu')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('优化器步数：80')
  })

  it('rejects an injected/invalid selector value without requesting it or falling back', async () => {
    const { wrapper, logs } = mount(); await flushPromises()
    const count = logs.length
    const option = new Option('forged', 'bad-registration'); (select(wrapper).element as HTMLSelectElement).add(option)
    await select(wrapper).setValue('bad-registration'); await flushPromises()
    expect(logs).toHaveLength(count)
    expect(wrapper.text()).toContain('未在服务端目录登记')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(() => runtimePath(1, 'bad-registration')).toThrow('注册编号无效')
  })

  it('resets selection on style change and ignores the abandoned style response', async () => {
    const { wrapper } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).includes(`runtime_registration_id=${GPU}`)
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await select(wrapper).setValue(GPU); await flushPromises()
    await wrapper.get('select[aria-label="风格档案"]').setValue('2'); await flushPromises()
    finish(response(runtime(gpuBinding, 23))); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('优化器步数：80')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })

  it('blocks create and context switches until saved-run recovery finishes, even when images load slowly', async () => {
    resume(); const { wrapper, router } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).startsWith('/api/v1/training-sets')
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await router.replace('/training?style_id=1'); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('select[aria-label="运行引擎"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('select[aria-label="风格档案"]').attributes('disabled')).toBeDefined()
    expect(select(wrapper).attributes('disabled')).toBeDefined()
    finish(response({ items: [], total: 0, page: 1, page_size: 24 })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeUndefined()
  })

  it('ignores a late Start response after navigation to a different profile', async () => {
    resume(); const { wrapper, router } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/8/start') && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '启动真实运行').trigger('click'); await flushPromises()
    await router.replace('/training?style_id=2'); await flushPromises()
    finish(response(run(config(gpuBinding, 23), 'running'))); await flushPromises()
    expect(wrapper.get('select[aria-label="风格档案"]').element).toHaveProperty('value', '2')
    expect(wrapper.find('[data-testid="run-runtime-binding"]').exists()).toBe(false)
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(select(wrapper).attributes('disabled')).toBeUndefined()
  })

  it.each([
    ['运行评估', 'evaluate'], ['运行 AI 视觉盲评', 'ai-review'], ['请求发布', 'publish'],
  ])('ignores a late %s completion in another profile context', async (label, endpoint) => {
    resume(); const routes = plan({ '/api/v1/style-runs/8': envelope(run(config(gpuBinding, 23), 'succeeded')),
      '/api/v1/style-runs/8/readiness': envelope({ evaluation_state: 'not_started', publication: null, allowed_actions: { evaluate: true, ai_review: true, publish: true }, blockers: {} }) })
    const { wrapper, router } = mount(routes); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith(`/8/${endpoint}`) && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, label!).trigger('click'); await flushPromises()
    await router.replace('/training?style_id=2'); await flushPromises()
    finish(response(endpoint === 'publish' ? { ...styles[0], style_id: 88, status: 'pending_review' } : { run_id: 8, state: 'running' })); await flushPromises()
    expect(wrapper.find('[data-testid="run-runtime-binding"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('真实评估运行中')
    expect(wrapper.text()).not.toContain('style #88')
    expect(wrapper.get('select[aria-label="风格档案"]').element).toHaveProperty('value', '2')
  })

  it('blocks new CPU creation after saved-run read failure and retries recovery without losing the pointer', async () => {
    resume(); const routes = plan({ '/api/v1/style-runs/8': problem(503, 'RECOVERY_UNAVAILABLE', 'try again') })
    const { wrapper } = mount(routes); await flushPromises()
    expect(wrapper.get('[data-testid="run-recovery-error"]').text()).toContain('训练恢复状态暂不可用，请刷新核对。')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    expect(select(wrapper).attributes('disabled')).toBeDefined()
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('8')
    routes['/api/v1/style-runs/8'] = envelope(run())
    await button(wrapper, '重试恢复上次运行').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="run-recovery-error"]').exists()).toBe(false)
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeUndefined()
  })

  it('lets the owner explicitly leave an incomplete old draft without modifying it or silently replacing a missing GPU registration', async () => {
    resume(); const old = { engine: 'm2', style_id: 1, runtime_binding: gpuBinding, train: { device: 'cuda' } }
    const { wrapper, logs } = mount(plan({ '/api/v1/style-runs/8': envelope(run(old)), [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(blocked({ runtime_options: [options[1]!] })) })); await flushPromises()
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    await button(wrapper, '保留旧草稿').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="run-runtime-binding"]').exists()).toBe(false)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBeNull()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    await select(wrapper).setValue(''); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
    expect(logs.every((entry) => entry.method === 'GET')).toBe(true)
    await button(wrapper, '返回保留的草稿').trigger('click'); await flushPromises()
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain(GPU)
  })

  it('clears an old-draft Return error when the same Return lookup later succeeds', async () => {
    resume(); const routes = plan({ '/api/v1/style-runs/8': envelope(run(config(), 'created')) })
    const { wrapper } = mount(routes); await flushPromises()
    await button(wrapper, '保留旧草稿').trigger('click'); await flushPromises()
    routes['/api/v1/style-runs/8'] = problem(503, 'RECOVERY_UNAVAILABLE', 'temporary')
    await button(wrapper, '返回保留的草稿').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="run-recovery-error"]').exists()).toBe(true)
    routes['/api/v1/style-runs/8'] = envelope(run(config(), 'created'))
    await button(wrapper, '返回保留的草稿').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="run-recovery-error"]').exists()).toBe(false)
    await button(wrapper, '保留旧草稿').trigger('click'); await flushPromises()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })

  it('preserves a late successful create for generic recovery without reattaching it to a different explicit style', async () => {
    const { wrapper, router } = mount(); await flushPromises()
    const previous = globalThis.fetch; let finish!: (value: Response) => void
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/v1/style-runs' && init?.method === 'POST'
      ? new Promise<Response>((resolve) => { finish = resolve }) : previous(input, init))
    await button(wrapper, '创建新运行').trigger('click'); await flushPromises()
    await router.replace('/training?style_id=2'); await flushPromises()
    expect(wrapper.text()).toContain('另一个档案的创建结果尚未确认')
    finish(response(run(config(), 'created'))); await flushPromises()
    expect(wrapper.find('[data-testid="run-runtime-binding"]').exists()).toBe(false)
    expect(wrapper.get('select[aria-label="风格档案"]').element).toHaveProperty('value', '2')
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('8')
    expect(localStorage.getItem('yy:p7:pending-style-run-create')).toBeNull()
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })

  it('recovers the exact registration and enables start only for its full frozen binding', async () => {
    resume(); const { wrapper, logs } = mount(); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('优化器步数：88')
    await button(wrapper, '启动真实运行').trigger('click'); await flushPromises()
    expect(logs.filter((entry) => entry.method === 'POST').map((entry) => entry.url)).toEqual(['/api/v1/style-runs/8/start'])
    expect(logs.some((entry) => entry.method === 'PUT' || entry.method === 'PATCH')).toBe(false)
  })

  it('keeps a missing recovered registration selected and blocked, without CPU fallback', async () => {
    resume()
    const { wrapper, logs } = mount(plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(blocked({ runtime_options: [options[1]!], blockers: [{ code: 'E_RUNTIME_REGISTRATION_MISSING', detail: 'runtime disabled or not installed' }] })) })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(wrapper.get('[data-testid="missing-runtime"]').text()).toContain(GPU)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('原执行环境登记不存在，请联系维护人员恢复正确环境。')
    const runtimeReads = logs.filter((entry) => entry.url.includes('m2-runtime'))
    expect(runtimeReads.at(-1)?.url).toContain(`runtime_registration_id=${GPU}`)
    await button(wrapper, '重新检查').trigger('click'); await flushPromises()
    expect(logs.at(-1)?.url).toContain(`runtime_registration_id=${GPU}`)
  })

  it('blocks a recovered draft whose saved device contradicts its frozen registration', async () => {
    resume(); const saved = config(gpuBinding, 23)
    saved.train = { ...(saved.train as Record<string, unknown>), device: 'cpu' }
    const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(saved)) })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('设备：cpu')
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain('cuda')
  })

  it('blocks starting a recovered run when its registration identity changed', async () => {
    resume()
    const currentBinding = { ...gpuBinding, binding_sha256: 'f'.repeat(64) }
    const { wrapper } = mount(plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(runtime(currentBinding, 23)) })); await flushPromises()
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain(gpuBinding.binding_sha256)
  })

  it('shows frozen terminal recipe independently of a new explicit CPU selection and refresh', async () => {
    resume(); const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(config(gpuBinding, 23), 'succeeded')) })); await flushPromises()
    await select(wrapper).setValue(''); await flushPromises()
    await button(wrapper, '刷新/恢复').trigger('click'); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('优化器步数：88')
    expect(wrapper.get('[data-testid="m2-server-recipe"]').text()).toContain('优化器步数：80')
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain(GPU)
  })

  it('preserves an old unbound CPU record and does not invent a new recipe for it', async () => {
    resume(); const legacy = { engine: 'm2', style_id: 1, train: { device: 'cpu', epochs: 2 } }
    const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(legacy, 'succeeded')) })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain('历史默认 CPU')
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).toContain('未记录完整配方或预算')
    expect(wrapper.get('[data-testid="m2-frozen-recipe"]').text()).not.toContain('完整 epoch：8')
  })

  it('never reinterprets a historical unbound CUDA or malformed binding as default CPU', async () => {
    resume(); const invalid = { engine: 'm2', style_id: 1, train: { device: 'cuda' } }
    const { wrapper } = mount(plan({ '/api/v1/style-runs/8': envelope(run(invalid)) })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', 'invalid-runtime-binding')
    expect(button(wrapper, '启动真实运行').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="run-runtime-binding"]').text()).toContain('无效或缺失的运行绑定')
    expect(registrationForConfig({ engine: 'm2', runtime_binding: null })).toBe('invalid-runtime-binding')
  })

  it('keeps the chosen registration on API failure and retries only that registration', async () => {
    const routes = plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: problem(409, 'E_RUNTIME_CHANGED', 'Reinstall the exact registered runtime') })
    const { wrapper, logs } = mount(routes); await flushPromises(); await select(wrapper).setValue(GPU); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(wrapper.text()).toContain('执行环境已变化，请重新核对所选环境。')
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeDefined()
    routes[`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`] = envelope(runtime(gpuBinding, 23))
    await button(wrapper, '重新检查').trigger('click'); await flushPromises()
    expect(logs.at(-1)?.url).toContain(`runtime_registration_id=${GPU}`)
    expect(button(wrapper, '创建新运行').attributes('disabled')).toBeUndefined()
  })

  it('recovers an uncertain GPU create with its exact key/body despite an unavailable registration', async () => {
    const pending = { key: 'frozen-gpu-request', body: { sample_ids: [44], config: config(gpuBinding, 23) } }
    const saved = JSON.stringify(pending); localStorage.setItem('yy:p7:pending-style-run-create', saved)
    const { wrapper } = mount(plan({ [`/api/v1/styles/1/m2-runtime?runtime_registration_id=${GPU}`]: envelope(blocked()) })); await flushPromises()
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(select(wrapper).attributes('disabled')).toBeDefined()
    expect(localStorage.getItem('yy:p7:pending-style-run-create')).toBe(saved)
    const previous = globalThis.fetch; const spy = vi.fn(previous); vi.stubGlobal('fetch', spy)
    await button(wrapper, '恢复未确认的创建').trigger('click'); await flushPromises()
    const call = spy.mock.calls.find(([input, init]) => String(input) === '/api/v1/style-runs' && init?.method === 'POST')!
    expect(JSON.parse(String(call[1]?.body))).toEqual(pending.body)
    expect((call[1]?.headers as Record<string, string>)['Idempotency-Key']).toBe(pending.key)
  })

  it('keeps explicit onboarding style precedence over another profile GPU run; generic recovery still restores it', async () => {
    resume(); const { wrapper, router, logs } = mount(); await router.replace('/training?style_id=2'); await flushPromises()
    expect(wrapper.get('select[aria-label="风格档案"]').element).toHaveProperty('value', '2')
    expect(select(wrapper).element).toHaveProperty('value', '')
    expect(wrapper.find('[data-testid="run-runtime-binding"]').exists()).toBe(false)
    expect(localStorage.getItem('yy:p7:last-style-run-id')).toBe('8')
    await router.replace('/training'); await flushPromises()
    expect(wrapper.get('select[aria-label="风格档案"]').element).toHaveProperty('value', '1')
    expect(select(wrapper).element).toHaveProperty('value', GPU)
    expect(logs.every((entry) => entry.method === 'GET')).toBe(true)
  })
})
